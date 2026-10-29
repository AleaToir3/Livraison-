import struct
from types import SimpleNamespace

import httpx
import orjson
import pytest
from starlette.testclient import TestClient

from config import Settings
from conftest import fake_osrm_transport
from geocoder_uk import MemoryCache, StaticProvider, SyntheticProvider
from main import create_app
from vision_extractor import GeminiVisionExtractor

from test_vision import FakeModels, WEBP, extraction

SETTINGS = Settings(redis_url="", solver_workers=0, osrm_url="http://osrm", allow_synthetic_geocoding=True)


def client_for(settings=SETTINGS, vision=None, providers=None) -> TestClient:
    app = create_app(
        settings,
        http_transport=fake_osrm_transport(circuity=2.5),
        providers=providers if providers is not None else [SyntheticProvider()],
        cache=MemoryCache(),
        vision=vision,
    )
    return TestClient(app)


def stub_vision(result=None, error=None) -> GeminiVisionExtractor:
    models = FakeModels(SimpleNamespace(parsed=result or extraction(), text=""), error=error)
    return GeminiVisionExtractor("k", client=SimpleNamespace(aio=SimpleNamespace(models=models)))  # type: ignore[arg-type]


# --------------------------------------------------------------------- WebSocket
def test_ws_scan_fast_path_payload_is_tiny_and_second_scan_hits_cache():
    payload = orjson.dumps({"i": 17, "p": "LS61AB", "b": "14A"})
    assert len(payload) < 50
    with client_for() as client, client.websocket_connect("/ws/scan") as ws:
        ws.send_text(payload.decode())
        first = orjson.loads(ws.receive_text())
        assert first["i"] == 17 and first["ok"] == 1 and first["pr"] == "synthetic"
        assert first["k"] == "LS61AB:14A" and 49 < first["la"] < 61
        ws.send_text(payload.decode())
        again = orjson.loads(ws.receive_text())
        assert (again["la"], again["lo"]) == (first["la"], first["lo"])


def test_ws_caches_real_providers_and_labels_source():
    static = StaticProvider({"LS61AB:14": (53.8123456, -1.5123456)})
    with client_for(providers=[static]) as client, client.websocket_connect("/ws/scan") as ws:
        ws.send_text('{"i":1,"p":"ls6 1ab","b":"14"}')
        a = orjson.loads(ws.receive_text())
        ws.send_text('{"i":2,"p":"LS6 1AB","b":"14"}')
        b = orjson.loads(ws.receive_text())
    assert (a["src"], b["src"]) == ("static", "cache") and static.calls == 1
    assert a["la"] == pytest.approx(53.8123456)


@pytest.mark.parametrize(
    "msg,err",
    [
        ("not json", "bad_json"),
        ("[1,2]", "bad_json"),
        ('{"i":3,"p":"ZZ9 9ZZ","b":"1"}', "bad_address"),
        ('{"i":4,"p":"LS6 1AB"}', "bad_address"),
        ('{"i":5,"p":"LS6 1AB","b":"ROSE COTTAGE"}', "geocode_failed"),
    ],
)
def test_ws_errors_keep_connection_open(msg, err):
    with client_for(providers=[StaticProvider()]) as client, client.websocket_connect("/ws/scan") as ws:
        ws.send_text(msg)
        assert orjson.loads(ws.receive_text())["e"] == err
        ws.send_text('{"t":"ping"}')
        assert orjson.loads(ws.receive_text()) == {"t": "pong"}


def test_ws_binary_frame_runs_vision_fallback():
    with client_for(vision=stub_vision()) as client, client.websocket_connect("/ws/scan") as ws:
        ws.send_bytes(struct.pack(">I", 99) + WEBP)
        res = orjson.loads(ws.receive_text())
        assert res["i"] == 99 and res["ok"] == 1 and res["key"] == "LS61AB:14" and res["geo"]["la"]


def test_ws_binary_without_vision_is_reported():
    with client_for() as client, client.websocket_connect("/ws/scan") as ws:
        ws.send_bytes(struct.pack(">I", 5) + WEBP)
        assert orjson.loads(ws.receive_text()) == {"i": 5, "ok": 0, "e": "disabled"}


def test_ws_requires_api_key_when_configured():
    secured = Settings(redis_url="", solver_workers=0, api_key="s3cret")
    with client_for(secured) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/scan"):
                pass
        with client.websocket_connect("/ws/scan?key=s3cret") as ws:
            ws.send_text('{"t":"ping"}')
            assert orjson.loads(ws.receive_text())["t"] == "pong"


# --------------------------------------------------------------------- extract-image
def test_extract_image_endpoint():
    with client_for(vision=stub_vision()) as client:
        r = client.post("/api/v1/extract-image", content=WEBP, headers={"content-type": "image/webp"})
        assert r.status_code == 200
        body = r.json()
        assert body["address"]["postcode"] == "LS6 1AB" and body["key"] == "LS61AB:14" and body["geo"]["pr"] == "synthetic"
        assert set(body["ms"]) == {"vision", "geocode", "total"}


@pytest.mark.parametrize(
    "vision,body,status",
    [
        (None, WEBP, 503),
        (stub_vision(), b"GIF89a....", 415),
        (stub_vision(), WEBP + b"\x00" * (200 * 1024), 413),
        (stub_vision(extraction(readable=False)), WEBP, 422),
    ],
)
def test_extract_image_errors(vision, body, status):
    with client_for(vision=vision) as client:
        assert client.post("/api/v1/extract-image", content=body).status_code == status


def test_http_endpoints_require_api_key():
    secured = Settings(redis_url="", solver_workers=0, api_key="s3cret", osrm_url="http://osrm")
    with client_for(secured, vision=stub_vision()) as client:
        assert client.post("/api/v1/extract-image", content=WEBP).status_code == 401
        assert client.post("/api/v1/extract-image", content=WEBP, headers={"X-API-Key": "s3cret"}).status_code == 200


# --------------------------------------------------------------------- optimize-route
def route_payload(n=8, **options):
    stops = [{"id": f"p{i}", "postcode": "LS6 1AB", "building": str(2 * i + 1), "street": "ALBION AVENUE"} for i in range(n)]
    return {"depot": {"lat": 53.80, "lon": -1.55, "label": "Depot Leeds"}, "stops": stops, "options": options}


def test_optimize_route_end_to_end():
    with client_for() as client:
        r = client.post("/api/v1/optimize-route", json=route_payload(10, time_limit_ms=60))
        assert r.status_code == 200, r.text
        body = r.json()
    delivered = sorted(d["stop_id"] for c in body["plan"]["clusters"] for d in c["deliveries"])
    assert delivered == sorted(f"p{i}" for i in range(10))
    t = body["plan"]["totals"]
    assert t["parcels"] == 10
    assert t["total_s"] == pytest.approx(t["drive_s"] + t["walk_s"] + t["service_s"], abs=0.2)
    cmp = body["comparison"]
    assert cmp["saved_s"] >= 0 and cmp["chosen_total_s"] == min(cmp["candidates_s"].values())
    assert set(cmp["candidates_s"]) == {"VAN_STRICT", "HYBRID_POCKETS", "HYBRID_SPEC"}
    assert cmp["van_only_total_s"] == cmp["candidates_s"]["VAN_STRICT"]
    assert body["matrix"]["size"] == 11 and body["geocode"]["synthetic"] == 10
    assert {"geocode", "osrm", "matrix_numpy", "solver_wall", "total"} <= set(body["timings_ms"])
    assert body["unresolved"] == [] and body["depot"]["label"] == "Depot Leeds"


def test_optimize_route_reports_unresolved_and_accepts_explicit_coordinates():
    payload = route_payload(3)
    payload["stops"].append({"id": "gps", "lat": 53.8001, "lon": -1.5501, "label": "Colis déjà géocodé"})
    payload["stops"].append({"id": "bad", "postcode": "LS6 1AB", "building": "ROSE COTTAGE"})
    with client_for(providers=[StaticProvider({"LS61AB:1": (53.8, -1.55), "LS61AB:3": (53.8002, -1.5502), "LS61AB:5": (53.8004, -1.5504)})]) as client:
        body = client.post("/api/v1/optimize-route", json=payload).json()
    assert body["unresolved"] == ["bad"]
    labels = {d["stop_id"]: d["label"] for c in body["plan"]["clusters"] for d in c["deliveries"]}
    assert labels["gps"] == "Colis déjà géocodé" and set(labels) == {"p0", "p1", "p2", "gps"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["stops"].clear(),
        lambda p: p["stops"].append({"id": "p0", "lat": 53.8, "lon": -1.5}),  # id dupliqué
        lambda p: p["stops"].append({"id": "x", "lat": 53.8}),  # lat sans lon
        lambda p: p["stops"].append({"id": "y"}),  # ni adresse ni coordonnées
        lambda p: p["depot"].update(lat=10.0),  # hors Royaume-Uni
    ],
)
def test_optimize_route_validation(mutate):
    payload = route_payload(2)
    mutate(payload)
    with client_for() as client:
        assert client.post("/api/v1/optimize-route", json=payload).status_code == 422


def test_optimize_route_all_unresolved_and_osrm_down():
    payload = {"depot": {"lat": 53.8, "lon": -1.55}, "stops": [{"id": "a", "postcode": "LS6 1AB", "building": "9"}]}
    with client_for(providers=[StaticProvider()]) as client:
        r = client.post("/api/v1/optimize-route", json=payload)
        assert r.status_code == 422 and r.json()["detail"]["unresolved"] == ["a"]

    app = create_app(
        SETTINGS,
        http_transport=httpx.MockTransport(lambda r: httpx.Response(500, json={"code": "Error"})),
        providers=[SyntheticProvider()],
        cache=MemoryCache(),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/optimize-route", json=route_payload(3)).status_code == 503


def test_healthz_is_light_by_default_and_deep_on_demand():
    with client_for(vision=stub_vision()) as client:
        light = client.get("/healthz").json()
        deep = client.get("/healthz?deep=1").json()
    assert light == {"status": "ok", "cache": "memory", "providers": ["synthetic"], "vision": True}
    assert deep == {**light, "osrm": True}


def test_optimize_route_reports_approximate_positions():
    with client_for() as client:  # le fournisseur synthétique renvoie des points fictifs
        body = client.post("/api/v1/optimize-route", json=route_payload(4)).json()
    assert body["geocode"]["approximate"] == 4 and body["geocode"]["synthetic"] == 4
