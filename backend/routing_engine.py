"""Solveur OR-Tools, regroupement en ParkingClusters et calcul du temps économisé.

Trois plans candidats sont chiffrés de façon réaliste (retour à pied jusqu'au van inclus) :

  VAN_STRICT      OR-Tools sur `M_van` : le "GPS classique", référence de comparaison.
  HYBRID_SPEC     OR-Tools sur `M_hybrid`, puis les arrêts consécutifs reliés par un arc WALK_SHORTCUT
                  forment des chaînes ; point de stationnement et boucle de marche optimaux par
                  programmation dynamique (Viterbi). C'est le cahier des charges, à la lettre.
  HYBRID_POCKETS  tournée VAN_STRICT où l'on rattache une "poche" de 1 à n colis isolés derrière une
                  barrière (filtre LTN, impasse dos à dos) au stationnement d'un colis voisin.

Pourquoi deux stratégies hybrides : sur `M_hybrid`, un arc WALK_SHORTCUT i -> j "téléporte" le van de i
à j (le solveur repart du bout de la rue où le van n'est pas). Le solveur exploite cette téléportation
pour traverser un filtre LTN à pied puis repartir de l'autre côté, un gain qui n'existe pas. Le
chiffrage réaliste le démasque ; HYBRID_POCKETS, lui, garde toujours le van à un seul endroit par
cluster. Le plan retenu est le moins long des trois : l'hybride ne peut donc jamais faire pire que
le van strict.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from hybrid_matrix import HybridMatrix

log = logging.getLogger("routing_engine")

_STATUS_NAMES = {
    0: "NOT_SOLVED",
    1: "SUCCESS",
    2: "PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
    3: "FAIL",
    4: "FAIL_TIMEOUT",
    5: "INVALID",
    6: "INFEASIBLE",
    7: "OPTIMAL",
}
EXACT_MAX_OTHERS = 5  # boucle de marche exacte (permutations) jusqu'à 6 portes, sinon 2-opt


# =============================================================================================
# Solveur OR-Tools
# =============================================================================================
@dataclass(frozen=True, slots=True)
class SolveResult:
    order: list[int]  # nœuds visités hors dépôt, dans l'ordre
    objective: int
    solve_ms: float
    status: str


def solve_tour(cost: np.ndarray, time_limit_ms: int, depot: int = 0) -> SolveResult:
    """Tournée fermée à 1 véhicule sur la matrice de coûts entière `cost` (dépôt = `depot`)."""
    t0 = time.perf_counter()
    n = int(cost.shape[0])
    stops = [i for i in range(n) if i != depot]
    if len(stops) <= 3:  # trivial : on énumère
        best, best_cost = stops, None
        for perm in itertools.permutations(stops):
            path = [depot, *perm, depot]
            c = sum(int(cost[a, b]) for a, b in zip(path, path[1:]))
            if best_cost is None or c < best_cost:
                best, best_cost = list(perm), c
        return SolveResult(best, best_cost or 0, (time.perf_counter() - t0) * 1000.0, "OPTIMAL")

    from ortools.constraint_solver import pywrapcp, routing_enums_pb2

    manager = pywrapcp.RoutingIndexManager(n, 1, depot)
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitMatrix(np.ascontiguousarray(cost, dtype=np.int64).tolist())
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(time_limit_ms))

    solution = routing.SolveWithParameters(params)
    if solution is None:
        raise RuntimeError(f"OR-Tools sans solution (statut {_STATUS_NAMES.get(routing.status(), routing.status())})")

    order: list[int] = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        if node != depot:
            order.append(node)
        index = solution.Value(routing.NextVar(index))
    return SolveResult(
        order,
        int(solution.ObjectiveValue()),
        (time.perf_counter() - t0) * 1000.0,
        _STATUS_NAMES.get(routing.status(), str(routing.status())),
    )


# =============================================================================================
# Boucle de marche et poches
# =============================================================================================
def _best_loop(park: int, others: list[int], w: list[list[float]]) -> tuple[float, list[int]]:
    """Boucle de marche minimale park -> toutes les `others` -> park (exacte si petite, sinon 2-opt)."""
    if not others:
        return 0.0, []
    if len(others) <= EXACT_MAX_OTHERS:
        best_cost, best_perm = float("inf"), others
        for perm in itertools.permutations(others):
            cost = w[park][perm[0]] + w[perm[-1]][park]
            for a, b in zip(perm, perm[1:]):
                cost += w[a][b]
            if cost < best_cost:
                best_cost, best_perm = cost, list(perm)
        return best_cost, best_perm

    tour = [park]
    remaining = set(others)
    while remaining:  # plus proche voisin
        nxt = min(remaining, key=lambda x: w[tour[-1]][x])
        tour.append(nxt)
        remaining.discard(nxt)
    improved = True
    while improved:  # 2-opt, le parking reste en position 0
        improved = False
        for i in range(1, len(tour) - 1):
            for j in range(i + 1, len(tour)):
                a, b, c, d = tour[i - 1], tour[i], tour[j], tour[(j + 1) % len(tour)]
                if w[a][c] + w[b][d] < w[a][b] + w[c][d] - 1e-9:
                    tour[i : j + 1] = reversed(tour[i : j + 1])
                    improved = True
    cost = sum(w[tour[k]][tour[(k + 1) % len(tour)]] for k in range(len(tour)))
    return cost, tour[1:]


@dataclass(frozen=True, slots=True)
class PocketGroup:
    """Un stationnement du van : `doors[0]` est le colis devant lequel le van se gare."""

    park: int
    doors: list[int]
    loop_s: float  # boucle de marche park -> portes -> park (0 si une seule porte)


def refine_pockets(
    order: Sequence[int],
    mv: list[list[float]],
    tw: list[list[float]],
    d: list[list[float]],
    *,
    max_size: int,
    max_walk_m: float,
    van_close_s: float,
    depot: int = 0,
) -> list[PocketGroup]:
    """Rattache des poches de colis isolés au stationnement d'un colis voisin, tant que le temps baisse.

    Une *poche* est un groupe de colis que le van relie entre eux en moins de `van_close_s` secondes dans
    les deux sens (même tronçon de rue, côté d'un filtre LTN, cul-de-sac...). La rattacher à un hôte
    veut dire : on supprime ses arrêts de la tournée et le livreur s'y rend à pied depuis le van garé
    chez l'hôte, puis revient. Le coût est recalculé en entier à chaque essai, donc l'effet de la
    suppression sur la tournée et le retour au van sont comptés ; une poche n'est rattachée que si le
    total baisse, et la tournée de départ n'est jamais allongée.
    """
    parent = {s: s for s in order}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    stops = list(order)
    for i, a in enumerate(stops):
        for b in stops[i + 1 :]:
            if max(mv[a][b], mv[b][a]) <= van_close_s:
                parent[find(a)] = find(b)
    components: dict[int, list[int]] = {}
    for s in stops:
        components.setdefault(find(s), []).append(s)
    pockets = [c for c in components.values() if len(c) < max_size]

    def total(groups: list[PocketGroup]) -> float:
        pos, cost = depot, 0.0
        for g in groups:
            cost += mv[pos][g.park] + g.loop_s
            pos = g.park
        return cost + mv[pos][depot]

    loop_cache: dict[tuple[int, frozenset[int]], tuple[float, list[int]]] = {}

    def loop(park: int, others: list[int]) -> tuple[float, list[int]]:
        key = (park, frozenset(others))
        if key not in loop_cache:
            loop_cache[key] = _best_loop(park, others, tw)
        return loop_cache[key]

    groups = [PocketGroup(s, [s], 0.0) for s in stops]
    current = total(groups)
    for _ in range(len(stops)):
        owner = {door: gi for gi, g in enumerate(groups) for door in g.doors}
        best: tuple[float, list[PocketGroup]] | None = None
        for pocket in pockets:
            if any(len(groups[owner[r]].doors) > 1 for r in pocket):
                continue  # déjà rattaché ailleurs
            members = set(pocket)
            for hi, host in enumerate(groups):
                if host.park in members or len(host.doors) + len(pocket) > max_size:
                    continue
                if any(d[host.park][r] > max_walk_m for r in pocket):
                    continue
                loop_s, walk_order = loop(host.park, host.doors[1:] + pocket)
                merged = PocketGroup(host.park, [host.park, *walk_order], loop_s)
                candidate = [
                    merged if gi == hi else g for gi, g in enumerate(groups) if gi == hi or g.park not in members
                ]
                cost = total(candidate)
                if cost < current - 1e-6 and (best is None or cost < best[0]):
                    best = (cost, candidate)
        if best is None:
            break
        current, groups = best
    return groups


def job_hybrid(cost: np.ndarray, time_limit_ms: int) -> SolveResult:
    """Travail d'un worker : OR-Tools sur `M_hybrid` (cahier des charges)."""
    return solve_tour(cost, time_limit_ms)


def job_strict(
    cost: np.ndarray,
    m_van: np.ndarray,
    t_walk: np.ndarray,
    d_hav: np.ndarray,
    time_limit_ms: int,
    max_size: int,
    max_walk_m: float,
    van_close_s: float,
) -> tuple[SolveResult, list[PocketGroup], float]:
    """Travail d'un worker : OR-Tools sur `M_van`, puis rattachement des poches (durée en ms)."""
    result = solve_tour(cost, time_limit_ms)
    t0 = time.perf_counter()
    groups = refine_pockets(
        result.order,
        m_van.tolist(),
        t_walk.tolist(),
        d_hav.tolist(),
        max_size=max_size,
        max_walk_m=max_walk_m,
        van_close_s=van_close_s,
    )
    return result, groups, (time.perf_counter() - t0) * 1000.0


def _init_worker() -> None:
    """Charge OR-Tools et chauffe un modèle minuscule (évite ~100 ms au premier appel)."""
    rng = np.random.default_rng(0)
    pts = rng.random((8, 2)) * 1000
    cost = np.rint(np.sqrt(((pts[:, None] - pts[None]) ** 2).sum(-1))).astype(np.int64)
    solve_tour(cost, 5)


class SolverPool:
    """Exécute les résolutions hors de la boucle d'événements.

    OR-Tools garde le GIL pendant `SolveWithParameters` : des threads bloqueraient l'event loop
    (donc les WebSockets de scan). Un pool de processus "chauds" l'évite et permet de résoudre
    `M_hybrid` et `M_van` réellement en parallèle.
    """

    def __init__(self, workers: int) -> None:
        self._workers = workers
        self._pool: ProcessPoolExecutor | None = None

    def _ensure_pool(self) -> ProcessPoolExecutor | None:
        if self._workers <= 0:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
            )
        return self._pool

    async def warmup(self) -> None:
        pool = self._ensure_pool()
        if pool is None:
            return
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(pool, _init_worker) for _ in range(self._workers)))

    async def run_all(self, calls: Sequence[tuple[Callable[..., Any], tuple]]) -> list[Any]:
        """Lance `fn(*args)` pour chaque couple, en parallèle, et renvoie les résultats dans l'ordre."""
        loop = asyncio.get_running_loop()
        for attempt in (0, 1):
            pool = self._ensure_pool()
            try:
                return list(await asyncio.gather(*(loop.run_in_executor(pool, fn, *args) for fn, args in calls)))
            except BrokenProcessPool:
                log.error("pool de solveurs cassé, recréation")
                self.shutdown()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    async def solve_many(self, costs: Sequence[np.ndarray], time_limit_ms: int) -> list[SolveResult]:
        return await self.run_all([(solve_tour, (c, time_limit_ms)) for c in costs])

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


# =============================================================================================
# Modèles de plan
# =============================================================================================
@dataclass(frozen=True, slots=True)
class StopMeta:
    id: str
    label: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class PlanConfig:
    service_time_s: float = 30.0
    max_cluster_size: int = 8
    max_walk_distance_m: float = 160.0
    pocket_van_close_s: float = 30.0


@dataclass(slots=True)
class Delivery:
    stop_id: str
    label: str
    lat: float
    lon: float
    node: int
    walk_from_previous_s: float  # 0 pour la porte devant laquelle le van est garé


@dataclass(slots=True)
class ParkingCluster:
    index: int
    kind: str  # SINGLE | WALK_LOOP
    park_node: int
    park_stop_id: str
    park_label: str
    park_lat: float
    park_lon: float
    drive_s: float  # trajet depuis le stationnement précédent (ou le dépôt)
    walk_loop_s: float  # marche totale, retour au van compris
    return_walk_s: float
    service_s: float
    deliveries: list[Delivery]

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "kind": self.kind,
            "park": {
                "stop_id": self.park_stop_id,
                "label": self.park_label,
                "lat": round(self.park_lat, 6),
                "lon": round(self.park_lon, 6),
            },
            "drive_s": round(self.drive_s, 1),
            "walk_loop_s": round(self.walk_loop_s, 1),
            "return_walk_s": round(self.return_walk_s, 1),
            "service_s": round(self.service_s, 1),
            "deliveries": [
                {
                    "stop_id": d.stop_id,
                    "label": d.label,
                    "lat": round(d.lat, 6),
                    "lon": round(d.lon, 6),
                    "walk_s": round(d.walk_from_previous_s, 1),
                }
                for d in self.deliveries
            ],
        }


@dataclass(slots=True)
class PlanTotals:
    drive_s: float = 0.0
    walk_s: float = 0.0
    service_s: float = 0.0
    total_s: float = 0.0
    van_stops: int = 0
    parcels: int = 0

    def to_dict(self) -> dict:
        return {
            "drive_s": round(self.drive_s, 1),
            "walk_s": round(self.walk_s, 1),
            "service_s": round(self.service_s, 1),
            "total_s": round(self.total_s, 1),
            "van_stops": self.van_stops,
            "parcels": self.parcels,
        }


@dataclass(slots=True)
class RoutePlan:
    strategy: str  # VAN_STRICT | HYBRID_SPEC | HYBRID_POCKETS
    clusters: list[ParkingCluster]
    drive_back_s: float
    totals: PlanTotals

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "totals": self.totals.to_dict(),
            "drive_back_s": round(self.drive_back_s, 1),
            "clusters": [c.to_dict() for c in self.clusters],
        }


@dataclass(slots=True)
class PlanOutcome:
    plan: RoutePlan  # le plus court des candidats
    van_plan: RoutePlan  # référence "GPS classique"
    candidates: list[RoutePlan]  # VAN_STRICT, HYBRID_POCKETS, HYBRID_SPEC
    saved_s: float
    saved_pct: float
    objective_hybrid: int
    objective_van: int
    solver_status: str
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def reference_total_s(self) -> float:
        return self.van_plan.totals.total_s

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.to_dict(),
            "comparison": {
                "van_only_total_s": round(self.van_plan.totals.total_s, 1),
                "chosen_total_s": round(self.plan.totals.total_s, 1),
                "chosen_strategy": self.plan.strategy,
                "candidates_s": {c.strategy: round(c.totals.total_s, 1) for c in self.candidates},
                "saved_s": round(self.saved_s, 1),
                "saved_pct": round(self.saved_pct, 2),
                "solver_objective_hybrid": self.objective_hybrid,
                "solver_objective_van": self.objective_van,
            },
            "timings_ms": {k: round(v, 2) for k, v in self.timings_ms.items()},
        }


# =============================================================================================
# Construction des plans
# =============================================================================================
@dataclass(slots=True)
class _Option:
    kind: str  # SINGLE | WALK_LOOP | DRIVE_THROUGH
    entry: int  # nœud où le van arrive
    exit: int  # nœud où le van repart
    internal_s: float
    chain: list[int]
    walk_order: list[int] = field(default_factory=list)  # WALK_LOOP : portes dans l'ordre, parking en tête


def _chains(seq: Sequence[int], walk_mask: np.ndarray) -> list[list[int]]:
    """Découpe la séquence en chaînes maximales d'arrêts consécutifs reliés par WALK_SHORTCUT."""
    chains: list[list[int]] = [[seq[0]]]
    for a, b in zip(seq, seq[1:]):
        if walk_mask[a, b]:
            chains[-1].append(b)
        else:
            chains.append([b])
    return chains


def _split_chain(chain: list[int], t_walk: list[list[float]], max_size: int) -> list[list[int]]:
    """Coupe récursivement une chaîne trop longue à son arc de marche le plus coûteux."""
    if len(chain) <= max_size:
        return [chain]
    cut = max(range(len(chain) - 1), key=lambda i: t_walk[chain[i]][chain[i + 1]]) + 1
    return _split_chain(chain[:cut], t_walk, max_size) + _split_chain(chain[cut:], t_walk, max_size)


def _options(chain: list[int], mv: list[list[float]], tw: list[list[float]]) -> list[_Option]:
    if len(chain) == 1:
        return [_Option("SINGLE", chain[0], chain[0], 0.0, chain, [chain[0]])]
    options: list[_Option] = []
    for park in chain:
        loop_cost, order = _best_loop(park, [x for x in chain if x != park], tw)
        options.append(_Option("WALK_LOOP", park, park, loop_cost, chain, [park, *order]))
    drive = sum(mv[a][b] for a, b in zip(chain, chain[1:]))
    options.append(_Option("DRIVE_THROUGH", chain[0], chain[-1], drive, chain))
    return options


def _viterbi(layers: list[list[_Option]], mv: list[list[float]], depot: int) -> list[_Option]:
    """Choisit une option par couche : minimise conduite + marche sur toute la tournée."""
    costs = [mv[depot][o.entry] + o.internal_s for o in layers[0]]
    back: list[list[int]] = []
    for prev, layer in zip(layers, layers[1:]):
        new_costs: list[float] = []
        ptrs: list[int] = []
        for o in layer:
            best_p = min(range(len(prev)), key=lambda p: costs[p] + mv[prev[p].exit][o.entry])
            new_costs.append(costs[best_p] + mv[prev[best_p].exit][o.entry] + o.internal_s)
            ptrs.append(best_p)
        back.append(ptrs)
        costs = new_costs
    last = min(range(len(layers[-1])), key=lambda k: costs[k] + mv[layers[-1][k].exit][depot])
    chosen = [layers[-1][last]]
    for layer_idx in range(len(layers) - 1, 0, -1):
        last = back[layer_idx - 1][last]
        chosen.append(layers[layer_idx - 1][last])
    chosen.reverse()
    return chosen


class _PlanBuilder:
    """Accumule des ParkingClusters et chiffre le plan (même code pour toutes les stratégies)."""

    def __init__(self, hm: HybridMatrix, stops: Sequence[StopMeta], cfg: PlanConfig, depot: int = 0) -> None:
        self.mv = hm.m_van.tolist()
        self.tw = hm.t_walk.tolist()
        self._stops = stops
        self._cfg = cfg
        self._depot = depot
        self.clusters: list[ParkingCluster] = []
        self.pos = depot  # nœud où se trouve le van

    def add(self, doors: list[int], loop_s: float | None = None) -> None:
        """`doors[0]` est la porte du stationnement ; `doors[1:]` sont livrées à pied, dans l'ordre."""
        park = doors[0]
        pm = self._stops[park - 1]
        deliveries: list[Delivery] = []
        for k, node in enumerate(doors):
            m = self._stops[node - 1]
            walk = 0.0 if k == 0 else self.tw[doors[k - 1]][node]
            deliveries.append(Delivery(m.id, m.label, m.lat, m.lon, node, walk))
        ret = self.tw[doors[-1]][park] if len(doors) > 1 else 0.0
        loop = sum(d.walk_from_previous_s for d in deliveries) + ret
        if loop_s is not None:
            assert abs(loop - loop_s) < 1e-6 * max(1.0, loop), (loop, loop_s)
        self.clusters.append(
            ParkingCluster(
                index=len(self.clusters),
                kind="WALK_LOOP" if len(doors) > 1 else "SINGLE",
                park_node=park,
                park_stop_id=pm.id,
                park_label=pm.label,
                park_lat=pm.lat,
                park_lon=pm.lon,
                drive_s=self.mv[self.pos][park],
                walk_loop_s=loop,
                return_walk_s=ret,
                service_s=len(doors) * self._cfg.service_time_s,
                deliveries=deliveries,
            )
        )
        self.pos = park

    def finish(self, strategy: str) -> RoutePlan:
        drive_back = self.mv[self.pos][self._depot]
        totals = PlanTotals(
            drive_s=sum(c.drive_s for c in self.clusters) + drive_back,
            walk_s=sum(c.walk_loop_s for c in self.clusters),
            service_s=sum(c.service_s for c in self.clusters),
            van_stops=len(self.clusters),
            parcels=sum(len(c.deliveries) for c in self.clusters),
        )
        totals.total_s = totals.drive_s + totals.walk_s + totals.service_s
        return RoutePlan(strategy, self.clusters, drive_back, totals)


def build_plan(
    order: Sequence[int],
    hm: HybridMatrix,
    stops: Sequence[StopMeta],
    cfg: PlanConfig,
    *,
    allow_walk: bool,
    strategy: str,
    depot: int = 0,
) -> RoutePlan:
    """Plan à partir d'une séquence d'arrêts (nœuds 1..n) : `allow_walk=False` = van strict.

    Avec `allow_walk=True` (HYBRID_SPEC), les arrêts consécutifs reliés par un arc WALK_SHORTCUT forment
    des chaînes ; une programmation dynamique choisit pour chacune le point de stationnement et la
    boucle de marche, ou renonce à marcher (DRIVE_THROUGH) quand conduire coûte moins.
    """
    builder = _PlanBuilder(hm, stops, cfg, depot)
    if not order:
        return builder.finish(strategy)
    if not allow_walk:
        for node in order:
            builder.add([node])
        return builder.finish(strategy)

    mv, tw = builder.mv, builder.tw
    chains = [piece for chain in _chains(order, hm.walk_mask) for piece in _split_chain(chain, tw, cfg.max_cluster_size)]
    for opt in _viterbi([_options(c, mv, tw) for c in chains], mv, depot):
        if opt.kind == "DRIVE_THROUGH":  # conduire est moins cher que marcher : un stationnement par arrêt
            for node in opt.chain:
                builder.add([node])
        else:
            builder.add(opt.walk_order if opt.kind == "WALK_LOOP" else [opt.entry])
    return builder.finish(strategy)


def plan_from_groups(
    groups: Sequence[PocketGroup], hm: HybridMatrix, stops: Sequence[StopMeta], cfg: PlanConfig, strategy: str
) -> RoutePlan:
    builder = _PlanBuilder(hm, stops, cfg)
    for g in groups:
        builder.add(list(g.doors), g.loop_s)
    return builder.finish(strategy)


# =============================================================================================
# Orchestration
# =============================================================================================
class RoutingEngine:
    def __init__(self, pool: SolverPool, cfg: PlanConfig, time_limit_ms: int = 150) -> None:
        self._pool = pool
        self._cfg = cfg
        self._time_limit_ms = time_limit_ms

    async def plan(
        self, hm: HybridMatrix, stops: Sequence[StopMeta], *, time_limit_ms: int | None = None
    ) -> PlanOutcome:
        limit = time_limit_ms or self._time_limit_ms
        cfg = self._cfg
        t0 = time.perf_counter()
        hybrid, (strict, groups, pockets_ms) = await self._pool.run_all(
            [
                (job_hybrid, (hm.hybrid_int, limit)),
                (
                    job_strict,
                    (hm.van_int, hm.m_van, hm.t_walk, hm.d_haversine, limit, cfg.max_cluster_size,
                     cfg.max_walk_distance_m, cfg.pocket_van_close_s),
                ),
            ]
        )
        solver_wall_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        van_plan = build_plan(strict.order, hm, stops, cfg, allow_walk=False, strategy="VAN_STRICT")
        pockets_plan = plan_from_groups(groups, hm, stops, cfg, "HYBRID_POCKETS")
        spec_plan = build_plan(hybrid.order, hm, stops, cfg, allow_walk=True, strategy="HYBRID_SPEC")
        clustering_ms = (time.perf_counter() - t1) * 1000.0

        candidates = [van_plan, pockets_plan, spec_plan]  # à égalité, le van strict l'emporte
        chosen = min(candidates, key=lambda p: p.totals.total_s)
        saved = van_plan.totals.total_s - chosen.totals.total_s
        pct = 100.0 * saved / van_plan.totals.total_s if van_plan.totals.total_s > 0 else 0.0
        return PlanOutcome(
            plan=chosen,
            van_plan=van_plan,
            candidates=candidates,
            saved_s=saved,
            saved_pct=pct,
            objective_hybrid=hybrid.objective,
            objective_van=strict.objective,
            solver_status=hybrid.status,
            timings_ms={
                "solver_wall": solver_wall_ms,
                "solver_hybrid": hybrid.solve_ms,
                "solver_van": strict.solve_ms,
                "pockets": pockets_ms,
                "clustering": clustering_ms,
            },
        )
