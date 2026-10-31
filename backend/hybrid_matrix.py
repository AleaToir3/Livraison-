"""Matrice de coûts hybride du "livreur opportuniste UK".

    M_van    : temps de conduite (s) via la Table API d'OSRM local (CH, en RAM)
    D_hav    : distances à vol d'oiseau (m), Haversine 100 % vectorisée NumPy
    T_walk   : D_hav / 1.65 + 10   (course légère + 10 s d'arrêt / traversée / bollard)
    M_hybrid : T_walk si (D_hav <= 160 m ET T_walk < M_van), sinon M_van

Aucune boucle Python sur les couples (i, j) : tout est calculé par broadcasting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
import numpy as np
import orjson

from config import EARTH_RADIUS_M, Settings

log = logging.getLogger("hybrid_matrix")

ARC_DRIVE = "DRIVE"
ARC_WALK = "WALK_SHORTCUT"


# =============================================================================================
# NumPy : Haversine, temps piéton, fusion
# =============================================================================================
def haversine_matrix(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    """Matrice N x N des distances (mètres) à vol d'oiseau, sans boucle Python."""
    lat = np.radians(np.asarray(lat_deg, dtype=np.float64))
    lon = np.radians(np.asarray(lon_deg, dtype=np.float64))
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    cos_lat = np.cos(lat)
    a = np.sin(dlat * 0.5) ** 2 + (cos_lat[:, None] * cos_lat[None, :]) * np.sin(dlon * 0.5) ** 2
    d = 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    np.fill_diagonal(d, 0.0)
    return d


def walk_time_matrix(d_haversine: np.ndarray, speed_mps: float, fixed_s: float) -> np.ndarray:
    """T_walk = D / vitesse + durée fixe. Diagonale à 0 (rester sur place ne coûte rien)."""
    t = d_haversine / speed_mps + fixed_s
    np.fill_diagonal(t, 0.0)
    return t


def fuse_matrices(
    m_van: np.ndarray,
    d_haversine: np.ndarray,
    t_walk: np.ndarray,
    walk_max_distance_m: float,
    no_walk_nodes: tuple[int, ...] = (0,),
) -> tuple[np.ndarray, np.ndarray]:
    """Règle d'écrasement : retourne (M_hybrid float64, masque booléen WALK_SHORTCUT).

    `no_walk_nodes` (dépôt par défaut) : on ne marche pas depuis / vers le dépôt, le van y est garé.
    """
    mask = (d_haversine <= walk_max_distance_m) & (t_walk < m_van)
    np.fill_diagonal(mask, False)
    for node in no_walk_nodes:
        mask[node, :] = False
        mask[:, node] = False
    m_hybrid = np.where(mask, t_walk, m_van)
    return m_hybrid, mask


@dataclass(slots=True)
class HybridMatrix:
    m_van: np.ndarray  # float64, secondes
    d_haversine: np.ndarray  # float64, mètres
    t_walk: np.ndarray  # float64, secondes
    m_hybrid: np.ndarray  # float64, secondes
    walk_mask: np.ndarray  # bool : True = WALK_SHORTCUT
    van_int: np.ndarray  # int64 : coût du solveur "van strict"
    hybrid_int: np.ndarray  # int64 : coût du solveur "hybride"
    unreachable_pairs: int
    numpy_ms: float
    osrm_ms: float
    total_ms: float

    @property
    def size(self) -> int:
        return int(self.m_van.shape[0])

    @property
    def walk_arcs(self) -> int:
        return int(self.walk_mask.sum())

    def arc_type_matrix(self) -> np.ndarray:
        """Arc_Type[i, j] ∈ {"DRIVE", "WALK_SHORTCUT"} (tableau de chaînes N x N)."""
        return np.where(self.walk_mask, ARC_WALK, ARC_DRIVE)

    def arc_type(self, i: int, j: int) -> str:
        return ARC_WALK if self.walk_mask[i, j] else ARC_DRIVE


# =============================================================================================
# OSRM
# =============================================================================================
class OSRMError(RuntimeError):
    """OSRM indisponible, réponse invalide ou requête refusée."""


class OSRMClient:
    """Client asynchrone de la Table API d'OSRM (pool de connexions persistantes via httpx)."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, *, timeout_s: float, max_table_size: int) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self.max_table_size = max_table_size

    async def table(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Durations N x N en secondes (float64, NaN si inatteignable)."""
        n = len(lat)
        if n > self.max_table_size:
            raise OSRMError(f"{n} points > osrm_max_table_size ({self.max_table_size})")
        coords = ";".join(f"{lo:.6f},{la:.6f}" for la, lo in zip(np.asarray(lat).tolist(), np.asarray(lon).tolist()))
        url = f"{self._base}/table/v1/driving/{coords}"
        resp: httpx.Response | None = None
        for attempt in (0, 1):  # un retry : une connexion keep-alive peut avoir été fermée côté serveur
            try:
                resp = await self._client.get(url, params={"annotations": "duration"}, timeout=self._timeout)
                break
            except httpx.TransportError as exc:
                if attempt == 1 or isinstance(exc, httpx.TimeoutException):
                    raise OSRMError(f"OSRM injoignable: {exc.__class__.__name__}: {exc}") from exc
        assert resp is not None
        try:
            data = orjson.loads(resp.content)
        except ValueError as exc:
            raise OSRMError(f"réponse OSRM illisible (HTTP {resp.status_code})") from exc
        if resp.status_code != 200 or data.get("code") != "Ok":
            raise OSRMError(f"OSRM {resp.status_code} {data.get('code')}: {data.get('message', '')}")
        durations = np.array(data["durations"], dtype=np.float64)  # None -> NaN
        if durations.shape != (n, n):
            raise OSRMError(f"matrice OSRM de forme {durations.shape}, attendue {(n, n)}")
        return durations

    async def ping(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self._base}/nearest/v1/driving/-0.1278,51.5074", timeout=min(self._timeout, 1.0)
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


class HybridMatrixBuilder:
    def __init__(self, osrm: OSRMClient, settings: Settings) -> None:
        self._osrm = osrm
        self._s = settings

    async def build(
        self,
        lat: np.ndarray,
        lon: np.ndarray,
        no_walk_nodes: tuple[int, ...] = (0,),
        return_to_depot: bool = True,
    ) -> HybridMatrix:
        """`return_to_depot=False` : tournée ouverte, revenir au nœud 0 ne coûte rien, donc le solveur finit où il veut."""
        t_start = time.perf_counter()

        async def timed_table() -> tuple[np.ndarray, float]:
            t = time.perf_counter()
            durations = await self._osrm.table(lat, lon)
            return durations, (time.perf_counter() - t) * 1000.0

        osrm_task = asyncio.ensure_future(timed_table())
        await asyncio.sleep(0)  # laisse partir la requête HTTP avant de calculer la partie piétonne

        t = time.perf_counter()
        d_hav = haversine_matrix(lat, lon)
        t_walk = walk_time_matrix(d_hav, self._s.walk_speed_mps, self._s.walk_fixed_s)
        numpy_ms = (time.perf_counter() - t) * 1000.0

        m_van_raw, osrm_ms = await osrm_task

        t = time.perf_counter()
        unreachable = ~np.isfinite(m_van_raw)
        m_van = np.where(unreachable, self._s.osrm_unreachable_cost_s, m_van_raw)
        if not return_to_depot:
            m_van[:, 0] = 0.0
        m_hybrid, mask = fuse_matrices(m_van, d_hav, t_walk, self._s.walk_max_distance_m, no_walk_nodes)
        van_int = np.rint(m_van).astype(np.int64)
        hybrid_int = np.rint(m_hybrid).astype(np.int64)
        numpy_ms += (time.perf_counter() - t) * 1000.0

        return HybridMatrix(
            m_van=m_van,
            d_haversine=d_hav,
            t_walk=t_walk,
            m_hybrid=m_hybrid,
            walk_mask=mask,
            van_int=van_int,
            hybrid_int=hybrid_int,
            unreachable_pairs=int(unreachable.sum()),
            numpy_ms=numpy_ms,
            osrm_ms=osrm_ms,
            total_ms=(time.perf_counter() - t_start) * 1000.0,
        )
