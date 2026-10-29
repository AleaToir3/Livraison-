import itertools

import httpx
import numpy as np
import pytest

from config import Settings
from conftest import fake_osrm_transport
from hybrid_matrix import HybridMatrix, HybridMatrixBuilder, OSRMClient, fuse_matrices, walk_time_matrix
from routing_engine import (
    PlanConfig,
    RoutingEngine,
    SolverPool,
    StopMeta,
    build_plan,
    plan_from_groups,
    refine_pockets,
    solve_tour,
)

CFG = PlanConfig(service_time_s=30.0, max_cluster_size=8)


def make_hm(d: np.ndarray, m_van: np.ndarray) -> HybridMatrix:
    t_walk = walk_time_matrix(d, 1.65, 10.0)
    hybrid, mask = fuse_matrices(m_van, d, t_walk, 160.0)
    return HybridMatrix(
        m_van=m_van, d_haversine=d, t_walk=t_walk, m_hybrid=hybrid, walk_mask=mask,
        van_int=np.rint(m_van).astype(np.int64), hybrid_int=np.rint(hybrid).astype(np.int64),
        unreachable_pairs=0, numpy_ms=0.0, osrm_ms=0.0, total_ms=0.0,
    )


def line_distances(xs: list[float]) -> np.ndarray:
    x = np.array(xs)
    return np.abs(x[:, None] - x[None, :])


def metas(n: int) -> list[StopMeta]:
    return [StopMeta(f"s{i}", f"Stop {i}", 53.0 + i * 1e-4, -1.0) for i in range(1, n + 1)]


def recompute_total(plan, hm: HybridMatrix, cfg: PlanConfig) -> float:
    """Chiffrage indépendant : conduite entre points de stationnement + boucles + service."""
    pos, total = 0, 0.0
    for c in plan.clusters:
        total += hm.m_van[pos, c.park_node]
        walk = 0.0
        nodes = [d.node for d in c.deliveries]
        for a, b in zip(nodes, nodes[1:]):
            walk += hm.t_walk[a, b]
        if len(nodes) > 1:
            walk += hm.t_walk[nodes[-1], nodes[0]]
        assert c.walk_loop_s == pytest.approx(walk)
        total += walk + len(nodes) * cfg.service_time_s
        pos = c.park_node
    return total + hm.m_van[pos, 0]


def test_solver_matches_brute_force_on_small_instance():
    rng = np.random.default_rng(7)
    pts = rng.random((8, 2)) * 3000
    cost = np.rint(np.sqrt(((pts[:, None] - pts[None]) ** 2).sum(-1))).astype(np.int64)
    best = min(
        sum(cost[a, b] for a, b in zip((0, *p), (*p, 0))) for p in itertools.permutations(range(1, 8))
    )
    res = solve_tour(cost, 150)
    assert sorted(res.order) == list(range(1, 8))
    assert res.objective == best


def test_trivial_sizes():
    assert solve_tour(np.zeros((1, 1), dtype=np.int64), 10).order == []
    c = np.array([[0, 5, 1], [5, 0, 1], [1, 1, 0]], dtype=np.int64)
    assert solve_tour(c, 10).order in ([1, 2], [2, 1])


def test_walk_chain_becomes_one_parking_cluster():
    # 0 dépôt ; 1,2,3 alignés à 30 m (le van met 400 s : bollards) ; 4,5 voisins que le van relie en 5 s
    d = np.full((6, 6), 5000.0)
    d[1:4, 1:4] = line_distances([0, 30, 60])
    d[4:6, 4:6] = line_distances([0, 40])
    m_van = np.full((6, 6), 1000.0)
    m_van[1:4, 1:4] = 400.0
    m_van[4, 5] = m_van[5, 4] = 5.0
    m_van[0, 1:4] = [100, 110, 120]
    m_van[1:4, 0] = [100, 110, 120]
    np.fill_diagonal(m_van, 0.0)
    hm = make_hm(d, m_van)
    plan = build_plan([1, 2, 3, 4, 5], hm, metas(5), CFG, allow_walk=True, strategy="HYBRID")
    kinds = [c.kind for c in plan.clusters]
    assert kinds == ["WALK_LOOP", "SINGLE", "SINGLE"]
    loop = plan.clusters[0]
    assert sorted(d.node for d in loop.deliveries) == [1, 2, 3]
    assert loop.deliveries[0].node == loop.park_node and loop.deliveries[0].walk_from_previous_s == 0
    assert plan.totals.total_s == pytest.approx(recompute_total(plan, hm, CFG))
    assert plan.totals.van_stops == 3 and plan.totals.parcels == 5
    # le van strict est strictement plus long (400 s de contournement ×2)
    strict = build_plan([1, 2, 3, 4, 5], hm, metas(5), CFG, allow_walk=False, strategy="VAN_STRICT")
    assert strict.totals.total_s > plan.totals.total_s + 300


def test_drive_through_wins_when_walking_back_to_the_van_costs_more():
    # 3 portes alignées tous les 150 m : chaque arc de marche (100,9 s) bat le van (110 s),
    # mais la boucle complète avec retour au van (~393 s) coûte plus que conduire (220 s).
    d = np.full((4, 4), 3000.0)
    d[1:4, 1:4] = line_distances([0, 150, 300])
    m_van = np.full((4, 4), 800.0)
    m_van[1, 2] = m_van[2, 3] = m_van[2, 1] = m_van[3, 2] = 110.0
    np.fill_diagonal(m_van, 0.0)
    hm = make_hm(d, m_van)
    assert hm.walk_mask[1, 2] and hm.walk_mask[2, 3]
    plan = build_plan([1, 2, 3], hm, metas(3), CFG, allow_walk=True, strategy="HYBRID")
    assert [c.kind for c in plan.clusters] == ["SINGLE"] * 3
    assert plan.totals.walk_s == 0
    assert plan.totals.total_s == pytest.approx(recompute_total(plan, hm, CFG))


def test_long_chains_are_split_to_max_cluster_size():
    n = 7
    d = np.full((n + 1, n + 1), 5000.0)
    d[1:, 1:] = line_distances([20.0 * i for i in range(n)])
    m_van = np.full((n + 1, n + 1), 500.0)
    np.fill_diagonal(m_van, 0.0)
    hm = make_hm(d, m_van)
    plan = build_plan(list(range(1, n + 1)), hm, metas(n), PlanConfig(30.0, 3), allow_walk=True, strategy="HYBRID")
    assert all(len(c.deliveries) <= 3 for c in plan.clusters)
    assert sorted(dl.node for c in plan.clusters for dl in c.deliveries) == list(range(1, n + 1))
    assert plan.totals.total_s == pytest.approx(recompute_total(plan, hm, PlanConfig(30.0, 3)))


def test_large_loop_uses_two_opt_and_visits_every_door():
    n = 9  # 9 portes > EXACT_MAX_OTHERS : chemin heuristique
    xs = [20.0 * i for i in range(n)]
    d = np.full((n + 1, n + 1), 5000.0)
    d[1:, 1:] = line_distances(xs)
    m_van = np.full((n + 1, n + 1), 900.0)
    np.fill_diagonal(m_van, 0.0)
    hm = make_hm(d, m_van)
    plan = build_plan(list(range(1, n + 1)), hm, metas(n), PlanConfig(30.0, 9), allow_walk=True, strategy="HYBRID")
    assert len(plan.clusters) == 1 and plan.clusters[0].kind == "WALK_LOOP"
    assert sorted(dl.node for dl in plan.clusters[0].deliveries) == list(range(1, n + 1))


async def make_engine(workers: int = 0):
    return RoutingEngine(SolverPool(workers), CFG, 150)


async def test_engine_invariants_on_dense_random_instance():
    rng = np.random.default_rng(3)
    n = 25
    lat = 53.80 + rng.random(n + 1) * 0.003
    lon = -1.55 + rng.random(n + 1) * 0.004
    lat[0], lon[0] = 53.78, -1.56  # dépôt éloigné
    client = httpx.AsyncClient(transport=fake_osrm_transport(circuity=3.0))
    builder = HybridMatrixBuilder(OSRMClient(client, "http://osrm", timeout_s=1, max_table_size=100), Settings())
    async with client:
        hm = await builder.build(lat, lon)
    engine = await make_engine()
    out = await engine.plan(hm, metas(n), time_limit_ms=100)
    assert [c.strategy for c in out.candidates] == ["VAN_STRICT", "HYBRID_POCKETS", "HYBRID_SPEC"]
    for plan in out.candidates:
        delivered = sorted(dl.node for c in plan.clusters for dl in c.deliveries)
        assert delivered == list(range(1, n + 1))
        assert plan.totals.total_s == pytest.approx(recompute_total(plan, hm, CFG))
    assert out.plan.totals.total_s == min(c.totals.total_s for c in out.candidates)
    assert out.plan.totals.total_s <= out.van_plan.totals.total_s + 1e-6
    assert out.saved_s == pytest.approx(out.van_plan.totals.total_s - out.plan.totals.total_s) and out.saved_s >= 0
    body = out.to_dict()
    assert body["comparison"]["saved_s"] == round(out.saved_s, 1)
    assert set(body["timings_ms"]) >= {"solver_wall", "solver_hybrid", "solver_van", "pockets", "clustering"}


def remote_pocket_matrix() -> HybridMatrix:
    """Dépôt 0 ; hôte 1 sur une route ouverte ; poche {2, 3} à 40 m de l'hôte mais à 6 min de van
    (filtre LTN) ; colis 4 ordinaire. Les colis 2 et 3 sont à 10 s l'un de l'autre en van."""
    d = np.full((5, 5), 4000.0)
    d[1, 2] = d[2, 1] = 40.0
    d[1, 3] = d[3, 1] = 70.0
    d[2, 3] = d[3, 2] = 30.0
    m_van = np.full((5, 5), 400.0)
    m_van[2, 3] = m_van[3, 2] = 10.0
    m_van[1, 2] = m_van[2, 1] = m_van[1, 3] = m_van[3, 1] = 360.0  # détour de 6 min à cause du filtre
    m_van[0, 1] = m_van[1, 0] = 120.0
    m_van[0, 4] = m_van[4, 0] = 130.0
    m_van[1, 4] = m_van[4, 1] = 60.0
    m_van[0, 2] = m_van[2, 0] = m_van[0, 3] = m_van[3, 0] = 700.0
    m_van[2, 4] = m_van[4, 2] = m_van[3, 4] = m_van[4, 3] = 700.0
    np.fill_diagonal(m_van, 0.0)
    return make_hm(d, m_van)


def pocket_args(hm: HybridMatrix, order, **kw):
    return dict(order=order, mv=hm.m_van.tolist(), tw=hm.t_walk.tolist(), d=hm.d_haversine.tolist(),
                max_size=8, max_walk_m=160.0, van_close_s=30.0) | kw


def test_remote_pocket_is_attached_to_the_host_and_saves_real_time():
    hm = remote_pocket_matrix()
    order = [1, 4, 2, 3]  # le van strict doit faire l'aller-retour de 6 min jusqu'à la poche
    strict = build_plan(order, hm, metas(4), CFG, allow_walk=False, strategy="VAN_STRICT")
    groups = refine_pockets(**pocket_args(hm, order))
    plan = plan_from_groups(groups, hm, metas(4), CFG, "HYBRID_POCKETS")
    loops = [c for c in plan.clusters if c.kind == "WALK_LOOP"]
    assert len(loops) == 1 and loops[0].park_node == 1
    assert sorted(d.node for d in loops[0].deliveries) == [1, 2, 3]
    assert plan.totals.total_s == pytest.approx(recompute_total(plan, hm, CFG))
    assert strict.totals.total_s - plan.totals.total_s > 400  # ~2 allers-retours de 6 min évités, moins ~3 min de marche
    assert plan.totals.van_stops == 2


def test_dense_street_is_never_attached():
    # 6 portes alignées tous les 30 m, le van passe de l'une à l'autre en 5 s : marcher ne rapporte rien
    n = 6
    d = np.full((n + 1, n + 1), 3000.0)
    d[1:, 1:] = line_distances([30.0 * i for i in range(n)])
    m_van = np.full((n + 1, n + 1), 300.0)
    m_van[1:, 1:] = 5.0
    np.fill_diagonal(m_van, 0.0)
    hm = make_hm(d, m_van)
    groups = refine_pockets(**pocket_args(hm, list(range(1, n + 1))))
    assert all(len(g.doors) == 1 for g in groups)


def test_pocket_refinement_never_lengthens_the_tour_and_respects_limits():
    hm = remote_pocket_matrix()
    order = [1, 4, 2, 3]
    base = sum(hm.m_van[a, b] for a, b in zip([0, *order], [*order, 0]))
    groups = refine_pockets(**pocket_args(hm, order))
    pos, cost = 0, 0.0
    for g in groups:
        cost += hm.m_van[pos, g.park] + g.loop_s
        pos = g.park
    assert cost + hm.m_van[pos, 0] <= base
    # un hôte trop loin à pied (> 160 m) ou une taille maximale trop petite interdisent le rattachement
    far = pocket_args(hm, order, max_walk_m=20.0)
    assert all(len(g.doors) == 1 for g in refine_pockets(**far))
    small = pocket_args(hm, order, max_size=2)
    assert all(len(g.doors) == 1 for g in refine_pockets(**small))


async def test_engine_picks_pockets_plan_when_it_is_best():
    hm = remote_pocket_matrix()
    out = await (await make_engine()).plan(hm, metas(4), time_limit_ms=60)
    assert out.plan.strategy in ("HYBRID_POCKETS", "HYBRID_SPEC")
    assert out.saved_s > 400 and out.saved_pct > 10
    assert out.plan.totals.total_s == pytest.approx(recompute_total(out.plan, hm, CFG))


async def test_process_pool_runs_two_solves_in_parallel():
    rng = np.random.default_rng(1)
    pts = rng.random((40, 2)) * 4000
    cost = np.rint(np.sqrt(((pts[:, None] - pts[None]) ** 2).sum(-1))).astype(np.int64)
    pool = SolverPool(2)
    try:
        await pool.warmup()
        import time
        t0 = time.perf_counter()
        a, b = await pool.solve_many([cost, cost.T.copy()], 150)
        wall = (time.perf_counter() - t0) * 1000
    finally:
        pool.shutdown()
    assert len(a.order) == len(b.order) == 39
    assert wall < 290  # deux résolutions de 150 ms en parallèle, pas 300 ms en série
