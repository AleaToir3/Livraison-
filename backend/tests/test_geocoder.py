import asyncio
import math

import httpx
import orjson
import pytest

from config import Settings
from geocoder_uk import (
    GeocoderUK,
    GeoPoint,
    GoogleRooftopProvider,
    IdealPostcodesProvider,
    MemoryCache,
    OSPlacesProvider,
    RedisCache,
    StaticProvider,
    SyntheticProvider,
    create_cache,
    decode_point,
    encode_point,
    synthetic_coordinates,
)
from hybrid_matrix import haversine_matrix
from uk_address_parser import parse_structured


def addr(pc, b, **kw):
    return parse_structured(pc, b, **kw)


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------- cache + chaîne
async def test_miss_then_hit_with_memory_cache():
    provider = StaticProvider({"LS61AB:14": (53.8, -1.55)})
    geo = GeocoderUK(MemoryCache(), [provider])
    first = await geo.geocode(addr("LS6 1AB", "14"))
    second = await geo.geocode(addr("LS6 1AB", "14"))
    assert first and not first.cached and first.point.lat == 53.8
    assert second and second.cached and second.point.lon == -1.55
    assert provider.calls == 1


async def test_batch_one_mget_and_unresolved():
    provider = StaticProvider({"LS61AB:14": (53.8, -1.55), "LS61AB:16": (53.8001, -1.5501)})
    geo = GeocoderUK(MemoryCache(), [provider])
    batch = await geo.geocode_many(
        [addr("LS6 1AB", "14"), addr("LS6 1AB", "16"), addr("LS6 1AB", "99"), addr("nope", "1"), addr("LS6 1AB", "14")]
    )
    assert [r is not None for r in batch.results] == [True, True, False, False, True]
    assert batch.unresolved == 2 and batch.resolved == 3
    warm = await geo.geocode_many([addr("LS6 1AB", "14"), addr("LS6 1AB", "16")])
    assert warm.cache_hits == 2 and all(r.cached for r in warm.results)


async def test_single_flight_one_call_for_concurrent_scans():
    class Slow(StaticProvider):
        async def lookup(self, address):
            await asyncio.sleep(0.05)
            return await super().lookup(address)

    slow = Slow({"LS61AB:14": (53.8, -1.55)})
    geo = GeocoderUK(MemoryCache(), [slow])
    results = await asyncio.gather(*(geo.geocode(addr("LS6 1AB", "14")) for _ in range(20)))
    assert all(r is not None for r in results)
    assert slow.calls == 1


async def test_synthetic_is_not_cached_and_chain_falls_through():
    cache = MemoryCache()
    geo = GeocoderUK(cache, [StaticProvider(), SyntheticProvider()])
    r1 = await geo.geocode(addr("M1 1AE", "7"))
    r2 = await geo.geocode(addr("M1 1AE", "7"))
    assert r1.point.source == "synthetic" and not r2.cached
    assert (r1.point.lat, r1.point.lon) == (r2.point.lat, r2.point.lon)


async def test_failing_provider_does_not_break_chain():
    class Boom(StaticProvider):
        async def lookup(self, address):
            raise RuntimeError("boom")

    geo = GeocoderUK(MemoryCache(), [Boom(), StaticProvider({"LS61AB:14": (1.0, 2.0)})])
    assert (await geo.geocode(addr("LS6 1AB", "14"))).point.lat == 1.0


def test_point_codec_roundtrip():
    p = GeoPoint(53.123456, -1.654321, "ideal_postcodes", "uprn", "100012345")
    assert decode_point(encode_point(p)) == p


# --------------------------------------------------------------------- fournisseurs HTTP
IDEAL_BODY = {
    "result": [
        {"building_number": "14", "building_name": "", "sub_building_name": "", "thoroughfare": "Albion Avenue",
         "latitude": 53.81, "longitude": -1.55, "uprn": "1001"},
        {"building_number": "14", "building_name": "", "sub_building_name": "Flat 2", "thoroughfare": "Albion Avenue",
         "latitude": 53.8101, "longitude": -1.5501, "uprn": "1002"},
        {"building_number": "", "building_name": "Rose Cottage", "sub_building_name": "", "thoroughfare": "Albion Avenue",
         "latitude": 53.8102, "longitude": -1.5502, "uprn": "1003"},
        {"building_number": "", "building_name": "14A", "sub_building_name": "", "thoroughfare": "Albion Avenue",
         "latitude": 53.8103, "longitude": -1.5503, "uprn": "1004"},
    ],
    "code": 2000,
}


async def test_ideal_postcodes_prefetches_whole_postcode():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, content=orjson.dumps(IDEAL_BODY))

    async with mock_client(handler) as client:
        geo = GeocoderUK(MemoryCache(), [IdealPostcodesProvider(client, "k", 1.0)])
        first = await geo.geocode(addr("LS6 1AB", "14"))
        # un autre bâtiment du même code postal est déjà dans le cache : aucun nouvel appel HTTP
        name = await geo.geocode(addr("LS6 1AB", "Rose Cottage"))
        letter = await geo.geocode(addr("LS6 1AB", "14A"))
    assert len(calls) == 1
    assert "LS61AB" in str(calls[0])
    assert first.point.uprn == "1001"  # le bâtiment prime sur l'appartement
    assert name.cached and name.point.uprn == "1003"
    assert letter.cached and letter.point.uprn == "1004"


async def test_ideal_postcodes_http_error_returns_nothing():
    async with mock_client(lambda r: httpx.Response(500)) as client:
        geo = GeocoderUK(MemoryCache(), [IdealPostcodesProvider(client, "k", 1.0)])
        assert await geo.geocode(addr("LS6 1AB", "14")) is None


async def test_os_places_paginates_and_reads_wgs84():
    seen_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        seen_offsets.append(offset)
        assert request.url.params["output_srs"] == "WGS84"
        rows = [{"DPA": {"UPRN": f"{offset + i}", "BUILDING_NUMBER": str(offset + i + 1), "LAT": 53.0 + i * 1e-5, "LNG": -1.0}}
                for i in range(100 if offset == 0 else 30)]
        return httpx.Response(200, json={"header": {"totalresults": 130}, "results": rows})

    async with mock_client(handler) as client:
        geo = GeocoderUK(MemoryCache(), [OSPlacesProvider(client, "k", 1.0)])
        res = await geo.geocode(addr("LS6 1AB", "120"))
    assert seen_offsets == [0, 100]
    assert res is not None and res.point.source == "os_places"


@pytest.mark.parametrize("location_type,partial,found", [("ROOFTOP", False, True), ("RANGE_INTERPOLATED", False, False), ("ROOFTOP", True, False)])
async def test_google_accepts_only_full_rooftop(location_type, partial, found):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["components"].startswith("country:GB")
        result = {"geometry": {"location": {"lat": 53.8, "lng": -1.5}, "location_type": location_type}}
        if partial:
            result["partial_match"] = True
        return httpx.Response(200, json={"status": "OK", "results": [result]})

    async with mock_client(handler) as client:
        geo = GeocoderUK(MemoryCache(), [GoogleRooftopProvider(client, "k", 1.0)])
        res = await geo.geocode(addr("LS6 1AB", "14", street="Albion Avenue"))
    assert (res is not None) is found


# --------------------------------------------------------------------- synthétique
def test_synthetic_is_deterministic_and_geometrically_coherent():
    a = synthetic_coordinates("LS6 1AB", "14")
    assert a == synthetic_coordinates("LS6 1AB", "14")
    assert a != synthetic_coordinates("LS6 1AB", "16")
    lat = [synthetic_coordinates("LS6 1AB", str(n))[0] for n in (14, 15, 16)]
    lon = [synthetic_coordinates("LS6 1AB", str(n))[1] for n in (14, 15, 16)]
    d = haversine_matrix(lat, lon)
    assert 5 < d[0, 2] < 15  # 14 et 16 : même côté, ~9 m
    assert 15 < d[0, 1] < 30  # 14 et 15 : côtés opposés
    assert math.isfinite(sum(synthetic_coordinates("ZZ", "THE LAURELS")))


# --------------------------------------------------------------------- Redis réel
async def test_redis_cache_integration(redis_url):
    cache = RedisCache(redis_url)
    assert await cache.ping()
    provider = StaticProvider({"LS61AB:14": (53.8, -1.55)})
    geo = GeocoderUK(cache, [provider], ttl_s=60)
    assert not (await geo.geocode(addr("LS6 1AB", "14"))).cached
    assert (await geo.geocode(addr("LS6 1AB", "14"))).cached
    raw = (await cache.mget(["uprn:v1:LS61AB:14"]))[0]
    assert decode_point(raw).lat == 53.8
    assert provider.calls == 1
    await cache.close()


async def test_redis_down_degrades_to_miss_without_exception(free_port):
    cache = RedisCache(f"redis://127.0.0.1:{free_port}/0")  # rien n'écoute
    assert not await cache.ping()
    geo = GeocoderUK(cache, [StaticProvider({"LS61AB:14": (1.0, 2.0)})])
    res = await geo.geocode(addr("LS6 1AB", "14"))
    assert res is not None and not res.cached
    await cache.close()


async def test_create_cache_falls_back_to_memory(free_port):
    settings = Settings(redis_url=f"redis://127.0.0.1:{free_port}/0")
    cache = await create_cache(settings)
    assert cache.name == "memory"
