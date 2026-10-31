import httpx
import numpy as np
import orjson
import pytest

from config import Settings
from conftest import fake_osrm_transport
from hybrid_matrix import (
    ARC_DRIVE,
    ARC_WALK,
    HybridMatrixBuilder,
    OSRMClient,
    OSRMError,
    fuse_matrices,
    haversine_matrix,
    walk_time_matrix,
)


def test_haversine_known_distances():
    # 1° de latitude ≈ 111,195 km ; Londres -> Paris ≈ 343,5 km
    d = haversine_matrix(np.array([0.0, 1.0]), np.array([0.0, 0.0]))
    assert d[0, 1] == pytest.approx(111_195, rel=1e-3)
    london_paris = haversine_matrix(np.array([51.5074, 48.8566]), np.array([-0.1278, 2.3522]))
    assert london_paris[0, 1] == pytest.approx(343_500, rel=5e-3)
    assert np.allclose(d, d.T) and np.all(np.diag(d) == 0)


def test_haversine_is_vectorised_and_fast_for_100_points():
    rng = np.random.default_rng(0)
    lat, lon = 53.8 + rng.random(101) * 0.01, -1.55 + rng.random(101) * 0.01
    d = haversine_matrix(lat, lon)
    assert d.shape == (101, 101)
    i, j = 3, 40  # comparaison avec une formule scalaire indépendante
    import math
    p1, p2 = math.radians(lat[i]), math.radians(lat[j])
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon[j] - lon[i]) / 2) ** 2
    assert d[i, j] == pytest.approx(2 * 6_371_008.8 * math.asin(math.sqrt(a)), rel=1e-9)


def test_walk_time_formula_matches_spec():
    d = np.array([[0.0, 165.0], [165.0, 0.0]])
    t = walk_time_matrix(d, 1.65, 10.0)
    assert t[0, 1] == pytest.approx(110.0)  # 165/1.65 + 10
    assert t[0, 0] == 0.0


def test_fusion_rule_threshold_speed_and_depot_exclusion():
    # nœuds : 0 dépôt, 1, 2 (50 m), 3 (200 m de 1), 4 (80 m de 1, mais le van y va en 5 s)
    d = np.zeros((5, 5))
    d[1, 2] = d[2, 1] = 50
    d[1, 3] = d[3, 1] = 200
    d[1, 4] = d[4, 1] = 80
    d[0, 1] = d[1, 0] = 40  # proche du dépôt : jamais de marche
    t_walk = walk_time_matrix(d, 1.65, 10.0)
    m_van = np.full((5, 5), 600.0)
    np.fill_diagonal(m_van, 0.0)
    m_van[1, 4] = 5.0  # le van est plus rapide que T_walk (80/1.65+10 = 58,5 s)
    m_van[0, 1] = m_van[1, 0] = 600.0
    hybrid, mask = fuse_matrices(m_van, d, t_walk, 160.0)
    assert mask[1, 2] and mask[2, 1]
    assert hybrid[1, 2] == pytest.approx(50 / 1.65 + 10)
    assert not mask[1, 3]  # > 160 m
    assert hybrid[1, 3] == 600.0
    assert not mask[1, 4] and hybrid[1, 4] == 5.0  # le van gagne
    assert mask[4, 1] and hybrid[4, 1] == pytest.approx(80 / 1.65 + 10)  # asymétrie : sens interdit au van
    assert not mask[0, 1] and not mask[1, 0]  # dépôt exclu
    assert not mask.diagonal().any()


def make_osrm(transport):
    client = httpx.AsyncClient(transport=transport)
    return client, OSRMClient(client, "http://osrm", timeout_s=1.0, max_table_size=100)


async def test_osrm_client_parses_nulls_as_nan():
    def handler(request):
        return httpx.Response(200, content=orjson.dumps({"code": "Ok", "durations": [[0, None], [12.5, 0]]}))

    client, osrm = make_osrm(httpx.MockTransport(handler))
    async with client:
        out = await osrm.table(np.array([53.0, 53.001]), np.array([-1.0, -1.001]))
    assert out[1, 0] == 12.5 and np.isnan(out[0, 1])


async def test_osrm_client_errors():
    client, osrm = make_osrm(httpx.MockTransport(lambda r: httpx.Response(400, json={"code": "NoSegment", "message": "x"})))
    async with client:
        with pytest.raises(OSRMError, match="NoSegment"):
            await osrm.table(np.array([53.0, 53.1]), np.array([-1.0, -1.1]))
    client, osrm = make_osrm(fake_osrm_transport())
    async with client:
        with pytest.raises(OSRMError, match="osrm_max_table_size"):
            await osrm.table(np.zeros(101) + 53, np.zeros(101) - 1)


async def test_osrm_client_retries_once_on_stale_connection():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("closed by peer")
        return httpx.Response(200, json={"code": "Ok", "durations": [[0, 1], [1, 0]]})

    client, osrm = make_osrm(httpx.MockTransport(handler))
    async with client:
        out = await osrm.table(np.array([53.0, 53.1]), np.array([-1.0, -1.1]))
    assert len(attempts) == 2 and out[0, 1] == 1


async def test_builder_end_to_end_with_unreachable_pairs():
    base = fake_osrm_transport()

    def handler(request):
        data = orjson.loads(base.handler(request).content)
        data["durations"][1][2] = None  # inatteignable en van
        return httpx.Response(200, content=orjson.dumps(data))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    osrm = OSRMClient(client, "http://osrm", timeout_s=1.0, max_table_size=100)
    builder = HybridMatrixBuilder(osrm, Settings())
    # dépôt à 2 km, deux portes à 30 m l'une de l'autre
    lat = np.array([53.80, 53.8200, 53.82027])
    lon = np.array([-1.55, -1.5500, -1.5500])
    async with client:
        hm = await builder.build(lat, lon)
    assert hm.size == 3 and hm.unreachable_pairs == 1
    assert hm.arc_type(1, 2) == ARC_WALK  # inatteignable en van : la marche gagne
    assert hm.arc_type(2, 1) == ARC_DRIVE  # 30 m en van = 4,7 s < T_walk : le van gagne
    assert hm.arc_type(0, 1) == ARC_DRIVE
    assert hm.hybrid_int[1, 2] == round(hm.t_walk[1, 2])
    assert hm.arc_type_matrix().dtype.kind == "U" and hm.arc_type_matrix()[1, 2] == "WALK_SHORTCUT"
    assert hm.van_int.dtype == np.int64 and hm.hybrid_int.dtype == np.int64
    assert hm.numpy_ms >= 0 and hm.osrm_ms >= 0 and hm.total_ms >= hm.osrm_ms


async def test_builder_open_tour_makes_returning_to_the_depot_free():
    client = httpx.AsyncClient(transport=fake_osrm_transport())
    osrm = OSRMClient(client, "http://osrm", timeout_s=1.0, max_table_size=100)
    builder = HybridMatrixBuilder(osrm, Settings())
    lat = np.array([53.80, 53.82, 53.84, 53.86])
    lon = np.array([-1.55, -1.55, -1.55, -1.55])
    async with client:
        closed = await builder.build(lat, lon)
        opened = await builder.build(lat, lon, return_to_depot=False)
    assert (closed.m_van[1:, 0] > 0).all()
    assert (opened.m_van[:, 0] == 0).all() and (opened.van_int[:, 0] == 0).all() and (opened.hybrid_int[:, 0] == 0).all()
    assert (opened.m_van[:, 1:] == closed.m_van[:, 1:]).all()  # le reste de la matrice ne change pas

