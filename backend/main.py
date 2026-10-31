"""Serveur FastAPI ultra-basse latence : scan WebSocket, fallback image, optimisation de tournée.

    WS   /ws/scan                 scan instantané (payload ~50 octets) -> coordonnées de la porte
    POST /api/v1/extract-image    fallback vision : ROI WebP -> adresse UK normalisée + coordonnées
    POST /api/v1/optimize-route   géocodage groupé + matrice hybride + OR-Tools + ParkingClusters
    GET  /healthz

Protocole WebSocket (clés courtes pour tenir dans ~50 octets) :
    client -> serveur (texte)   {"i": 17, "p": "LS6 1AB", "b": "14A"}      # i = id de scan, p = postcode,
                                 optionnels : "f" sous-bâtiment (FLAT 2), "s" rue
    client -> serveur (binaire) 4 octets (id de scan, big-endian) + image WebP du ROI
    client -> serveur (texte)   {"t": "ping"}                              # -> {"t": "pong"}
    serveur -> client           {"i": 17, "ok": 1, "la": 53.80012, "lo": -1.55231, "src": "cache",
                                 "pr": "uprn", "k": "LS61AB:14A", "ms": 0.31}
                                 {"i": 17, "ok": 0, "e": "bad_address" | "geocode_failed" | ...}
"""

from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import numpy as np
import orjson
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from config import UK_LAT_MAX, UK_LAT_MIN, UK_LON_MAX, UK_LON_MIN, Settings, get_settings
from geocoder_uk import (
    CacheBackend,
    GeocodeProvider,
    GeocoderUK,
    GeoResult,
    build_providers,
    create_cache,
)
from hybrid_matrix import HybridMatrixBuilder, OSRMClient, OSRMError
from routing_engine import PlanConfig, RoutingEngine, SolverPool, StopMeta
from uk_address_parser import ParsedAddress, parse_structured
from vision_extractor import GeminiVisionExtractor, VisionError, VisionResult

log = logging.getLogger("main")


class ORJSONResponse(Response):
    """Réponse JSON sérialisée par orjson (la classe de FastAPI du même nom est dépréciée)."""

    media_type = "application/json"

    def render(self, content: object) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)


# =============================================================================================
# Services partagés
# =============================================================================================
@dataclass(slots=True)
class Services:
    settings: Settings
    http: httpx.AsyncClient
    geocoder: GeocoderUK
    osrm: OSRMClient
    matrix: HybridMatrixBuilder
    engine: RoutingEngine
    pool: SolverPool
    vision: GeminiVisionExtractor | None


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    providers: list[GeocodeProvider] | None = None,
    cache: CacheBackend | None = None,
    vision: GeminiVisionExtractor | None = None,
) -> FastAPI:
    """Fabrique l'application. Les paramètres nommés servent aux tests (transport HTTP simulé, etc.)."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        # httpx / httpcore journalisent les URL complètes, donc les clés API en query string.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        http = httpx.AsyncClient(
            http2=True,  # HTTP/2 négocié (ALPN) vers les API HTTPS ; OSRM local reste en HTTP/1.1 keep-alive
            transport=http_transport,
            limits=httpx.Limits(
                max_connections=cfg.http_max_connections, max_keepalive_connections=cfg.http_max_keepalive
            ),
            timeout=httpx.Timeout(cfg.osrm_timeout_s),
        )
        cache_backend = cache or await create_cache(cfg)
        geocoder = GeocoderUK(
            cache=cache_backend,
            providers=providers if providers is not None else build_providers(cfg, http),
            ttl_s=cfg.cache_ttl_s,
            cache_prefix=cfg.cache_prefix,
        )
        osrm = OSRMClient(http, cfg.osrm_url, timeout_s=cfg.osrm_timeout_s, max_table_size=cfg.osrm_max_table_size)
        pool = SolverPool(cfg.solver_workers)
        await pool.warmup()
        extractor = vision or (
            GeminiVisionExtractor(cfg.gemini_api_key, cfg.gemini_model, cfg.gemini_timeout_s)
            if cfg.gemini_api_key
            else None
        )
        app.state.svc = Services(
            settings=cfg,
            http=http,
            geocoder=geocoder,
            osrm=osrm,
            matrix=HybridMatrixBuilder(osrm, cfg),
            engine=RoutingEngine(
                pool,
                PlanConfig(cfg.service_time_s, cfg.max_cluster_size, cfg.walk_max_distance_m, cfg.pocket_van_close_s),
                cfg.solver_time_limit_ms,
            ),
            pool=pool,
            vision=extractor,
        )
        if cfg.is_production and not cfg.api_key:
            log.warning("APP_ENV=production sans API_KEY : l'API est ouverte à tout le réseau")
        log.info(
            "prêt: cache=%s fournisseurs=%s vision=%s solveurs=%d",
            cache_backend.name,
            [p.name for p in geocoder.providers],
            "oui" if extractor else "non",
            cfg.solver_workers,
        )
        try:
            yield
        finally:
            pool.shutdown()
            await geocoder.close()
            await http.aclose()

    app = FastAPI(
        title="UK Last-Mile Hybrid Router",
        version="1.0.0",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if cfg.is_production else "/openapi.json",
    )

    def services(request: Request) -> Services:
        return request.app.state.svc

    async def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        if not _authorised(cfg, x_api_key):
            raise HTTPException(status_code=401, detail="clé API invalide")

    # ------------------------------------------------------------------ santé
    @app.get("/healthz")
    async def healthz(deep: bool = False, svc: Services = Depends(services)) -> ORJSONResponse:
        """Contrôle de santé. Léger par défaut (appelé toutes les quelques secondes par l'hébergeur) ;
        `?deep=1` teste aussi le moteur routier, sans que l'hébergeur ne martèle un OSRM public."""
        body = {
            "status": "ok",
            "cache": svc.geocoder.cache.name,
            "providers": [p.name for p in svc.geocoder.providers],
            "vision": svc.vision is not None,
        }
        if deep:
            body["osrm"] = await svc.osrm.ping()
        return ORJSONResponse(body)

    # ------------------------------------------------------------------ WebSocket de scan
    @app.websocket("/ws/scan")
    async def ws_scan(ws: WebSocket) -> None:
        svc: Services = ws.app.state.svc
        if not _authorised(cfg, ws.query_params.get("key") or ws.headers.get("x-api-key")):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    reply = await _scan_text(svc, message["text"])
                elif message.get("bytes") is not None:
                    reply = await _scan_binary(svc, message["bytes"])
                else:
                    continue
                await ws.send_text(orjson.dumps(reply).decode())
        except WebSocketDisconnect:
            pass

    # ------------------------------------------------------------------ fallback image
    @app.post("/api/v1/extract-image", dependencies=[Depends(require_api_key)])
    async def extract_image(request: Request, svc: Services = Depends(services)) -> ORJSONResponse:
        limit = cfg.max_image_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(status_code=413, detail=f"image > {limit} octets")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(status_code=413, detail=f"image > {limit} octets")
        try:
            return ORJSONResponse(await _extract_and_geocode(svc, bytes(body)))
        except VisionError as exc:
            status = {"disabled": 503, "bad_image": 415, "timeout": 504, "upstream": 502, "unreadable": 422}[exc.kind]
            raise HTTPException(status_code=status, detail={"kind": exc.kind, "message": str(exc)}) from exc

    # ------------------------------------------------------------------ optimisation de tournée
    @app.post("/api/v1/optimize-route", dependencies=[Depends(require_api_key)])
    async def optimize_route(req: OptimizeRequest, svc: Services = Depends(services)) -> ORJSONResponse:
        t_total = time.perf_counter()
        if len(req.stops) + 1 > svc.osrm.max_table_size:
            raise HTTPException(status_code=422, detail=f"au plus {svc.osrm.max_table_size - 1} arrêts par tournée")

        # 1. Géocodage groupé (1 MGET Redis + appels API uniquement pour les manquants).
        t = time.perf_counter()
        pending = [
            (i, parse_structured(s.postcode, s.building, s.sub_building, s.street))
            for i, s in enumerate(req.stops)
            if s.lat is None
        ]
        batch = await svc.geocoder.geocode_many([p for _, p in pending])
        coords: dict[int, tuple[float, float]] = {
            i: (s.lat, s.lon) for i, s in enumerate(req.stops) if s.lat is not None and s.lon is not None
        }
        parsed_by_index = dict(pending)
        for (i, _), result in zip(pending, batch.results):
            if result is not None:
                coords[i] = (result.point.lat, result.point.lon)
        geocode_ms = (time.perf_counter() - t) * 1000.0

        kept = [i for i in range(len(req.stops)) if i in coords]
        unresolved = [req.stops[i].id for i in range(len(req.stops)) if i not in coords]
        if not kept:
            raise HTTPException(status_code=422, detail={"message": "aucune adresse géocodable", "unresolved": unresolved})

        metas = [
            StopMeta(
                id=req.stops[i].id,
                label=req.stops[i].label or _display(parsed_by_index.get(i)) or req.stops[i].id,
                lat=coords[i][0],
                lon=coords[i][1],
            )
            for i in kept
        ]
        lat = np.array([req.depot.lat, *(m.lat for m in metas)], dtype=np.float64)
        lon = np.array([req.depot.lon, *(m.lon for m in metas)], dtype=np.float64)

        # 2. Matrices (OSRM + NumPy) puis 3. solveur + regroupement.
        try:
            hm = await svc.matrix.build(lat, lon, return_to_depot=req.options.return_to_depot)
        except OSRMError as exc:
            raise HTTPException(status_code=503, detail=f"moteur routier indisponible: {exc}") from exc
        outcome = await svc.engine.plan(hm, metas, time_limit_ms=req.options.time_limit_ms)

        body = outcome.to_dict()
        body["depot"] = {"lat": req.depot.lat, "lon": req.depot.lon, "label": req.depot.label}
        body["return_to_depot"] = req.options.return_to_depot
        body["unresolved"] = unresolved
        body["matrix"] = {
            "size": hm.size,
            "walk_arcs": hm.walk_arcs,
            "unreachable_pairs": hm.unreachable_pairs,
        }
        body["geocode"] = {
            "requested": len(pending),
            "cache_hits": batch.cache_hits,
            "unresolved": batch.unresolved,
            "synthetic": sum(1 for r in batch.results if r is not None and r.point.precision == "synthetic"),
            # position du centre du code postal ou fictive : la marche entre portes n'a alors pas de sens
            "approximate": sum(
                1 for r in batch.results if r is not None and r.point.precision in ("synthetic", "postcode")
            ),
        }
        body["timings_ms"].update(
            {
                "geocode": geocode_ms,
                "matrix_numpy": hm.numpy_ms,
                "osrm": hm.osrm_ms,
                "matrix_total": hm.total_ms,
                "total": (time.perf_counter() - t_total) * 1000.0,
            }
        )
        body["timings_ms"] = {k: round(v, 2) for k, v in body["timings_ms"].items()}
        return ORJSONResponse(body)

    return app


# =============================================================================================
# Modèles de requête
# =============================================================================================
class DepotIn(BaseModel):
    lat: float = Field(ge=UK_LAT_MIN, le=UK_LAT_MAX)
    lon: float = Field(ge=UK_LON_MIN, le=UK_LON_MAX)
    label: str = Field(default="Dépôt", max_length=120)


class StopIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str | None = Field(default=None, max_length=200)
    postcode: str | None = Field(default=None, max_length=12)
    building: str | None = Field(default=None, max_length=80)
    sub_building: str | None = Field(default=None, max_length=40)
    street: str | None = Field(default=None, max_length=120)
    lat: float | None = Field(default=None, ge=UK_LAT_MIN, le=UK_LAT_MAX)
    lon: float | None = Field(default=None, ge=UK_LON_MIN, le=UK_LON_MAX)

    @model_validator(mode="after")
    def _needs_position_or_address(self) -> "StopIn":
        has_coords = self.lat is not None and self.lon is not None
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat et lon vont ensemble")
        has_address = bool(self.postcode) and bool(self.building or (self.sub_building and self.street))
        if not has_coords and not has_address:
            raise ValueError("fournir lat+lon, ou postcode + (building | sub_building + street)")
        return self


class OptionsIn(BaseModel):
    time_limit_ms: int = Field(default=150, ge=20, le=1000)
    return_to_depot: bool = True  # False : tournée ouverte, elle s'arrête au dernier arrêt (pas de retour au point de départ)


class OptimizeRequest(BaseModel):
    depot: DepotIn
    stops: list[StopIn] = Field(min_length=1)
    options: OptionsIn = Field(default_factory=OptionsIn)

    @model_validator(mode="after")
    def _unique_ids(self) -> "OptimizeRequest":
        ids = [s.id for s in self.stops]
        if len(set(ids)) != len(ids):
            raise ValueError("les id d'arrêts doivent être uniques")
        return self


# =============================================================================================
# Aides
# =============================================================================================
def _authorised(cfg: Settings, provided: str | None) -> bool:
    return not cfg.api_key or (provided is not None and hmac.compare_digest(provided, cfg.api_key))


def _display(parsed: ParsedAddress | None) -> str | None:
    return parsed.display() if parsed is not None else None


def _geo_payload(result: GeoResult) -> dict:
    p = result.point
    return {
        "la": round(p.lat, 7),
        "lo": round(p.lon, 7),
        "src": "cache" if result.cached else p.source,
        "pr": p.precision,
    }


async def _scan_text(svc: Services, text: str) -> dict:
    """Fast-path : postcode + bâtiment -> coordonnées, sans image."""
    t0 = time.perf_counter()
    try:
        data = orjson.loads(text)
    except orjson.JSONDecodeError:
        return {"ok": 0, "e": "bad_json"}
    if not isinstance(data, dict):
        return {"ok": 0, "e": "bad_json"}
    if data.get("t") == "ping":
        return {"t": "pong"}
    scan_id = data.get("i") if isinstance(data.get("i"), int) else None
    fields = [data.get(k) for k in ("p", "b", "f", "s")]
    if not isinstance(fields[0], str) or not isinstance(fields[1], str) or any(
        f is not None and not isinstance(f, str) for f in fields[2:]
    ):
        return {"i": scan_id, "ok": 0, "e": "bad_address"}
    parsed = parse_structured(*fields)
    if not parsed.is_fast_path_valid:
        return {"i": scan_id, "ok": 0, "e": "bad_address"}
    result = await svc.geocoder.geocode(parsed)
    if result is None:
        return {"i": scan_id, "ok": 0, "e": "geocode_failed"}
    return {
        "i": scan_id,
        "ok": 1,
        **_geo_payload(result),
        "k": parsed.cache_key,
        "ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


async def _scan_binary(svc: Services, blob: bytes) -> dict:
    """Trame binaire : 4 octets d'id de scan (big-endian) + image WebP du ROI."""
    if len(blob) <= 4:
        return {"ok": 0, "e": "bad_image"}
    scan_id = int.from_bytes(blob[:4], "big")
    image = blob[4:]
    if len(image) > svc.settings.max_image_bytes:
        return {"i": scan_id, "ok": 0, "e": "too_large"}
    try:
        body = await _extract_and_geocode(svc, image)
    except VisionError as exc:
        return {"i": scan_id, "ok": 0, "e": exc.kind}
    return {"i": scan_id, **body}


async def _extract_and_geocode(svc: Services, image: bytes) -> dict:
    if svc.vision is None:
        raise VisionError("disabled", "GEMINI_API_KEY non configurée")
    t0 = time.perf_counter()
    vision: VisionResult = await svc.vision.extract(image)
    t1 = time.perf_counter()
    result = await svc.geocoder.geocode(vision.parsed)
    t2 = time.perf_counter()
    return {
        "ok": 1,
        "address": vision.parsed.to_dict(),
        "key": vision.parsed.cache_key,
        "confidence": vision.raw.confidence,
        "geo": _geo_payload(result) if result is not None else None,
        "ms": {
            "vision": round(vision.ms, 1),
            "geocode": round((t2 - t1) * 1000.0, 2),
            "total": round((t2 - t0) * 1000.0, 1),
        },
    }


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        loop="uvloop",
        http="httptools",
        ws_max_size=256 * 1024,
        log_level=get_settings().log_level.lower(),
    )
