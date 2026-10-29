"""Géocodage UK précision porte (UPRN / rooftop) avec cache RAM.

Chaîne de résolution pour une adresse `POSTCODE + BUILDING_ID` :

    Redis (< 0.2 ms)  ->  Ideal Postcodes (PAF/UPRN)  ->  OS Places (UPRN)  ->  Google (ROOFTOP)
                      ->  point synthétique déterministe (tests / démo uniquement)

Optimisations :
  * lecture groupée (`MGET`) de tout un lot d'adresses en un seul aller-retour Redis ;
  * les API PAF renvoient *toutes* les adresses d'un code postal : on les écrit toutes dans le
    cache, les scans suivants du même code postal sont donc des hits ;
  * single-flight : N colis du même code postal déclenchent 1 seul appel API.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence
from urllib.parse import quote

import httpx
import orjson

from config import Settings
from uk_address_parser import (
    ParsedAddress,
    compact_postcode,
    make_cache_key,
    normalize_building_token,
)

log = logging.getLogger("geocoder_uk")


# =============================================================================================
# Modèles
# =============================================================================================
@dataclass(frozen=True, slots=True)
class GeoPoint:
    lat: float
    lon: float
    source: str  # ideal_postcodes | os_places | google_rooftop | static | synthetic
    precision: str  # uprn | rooftop | synthetic
    uprn: str | None = None


@dataclass(frozen=True, slots=True)
class GeoResult:
    point: GeoPoint
    cached: bool


@dataclass(slots=True)
class GeocodeBatch:
    results: list[GeoResult | None]
    cache_hits: int = 0
    resolved: int = 0
    unresolved: int = 0
    elapsed_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class AddressRecord:
    """Une ligne PAF/UPRN telle que renvoyée par un fournisseur."""

    postcode: str
    building_number: str | None
    building_name: str | None
    sub_building: str | None
    street: str | None
    lat: float
    lon: float
    uprn: str | None


def record_keys(record: AddressRecord) -> list[str]:
    """Clés de cache (niveau bâtiment) d'une ligne fournisseur."""
    keys: list[str] = []
    for token in (record.building_number, record.building_name):
        norm = normalize_building_token(token)
        if norm:
            keys.append(make_cache_key(record.postcode, norm))
    if not keys and record.sub_building and record.street:
        sub = normalize_building_token(record.sub_building)
        street = normalize_building_token(record.street)
        keys.append(make_cache_key(record.postcode, f"{sub}@{street}"))
    return keys


def points_from_records(records: Sequence[AddressRecord], source: str, precision: str) -> dict[str, GeoPoint]:
    """Index {clé de cache -> point}. Les lignes sans sous-bâtiment (le bâtiment lui-même) priment."""
    ordered = sorted(records, key=lambda r: r.sub_building is not None)
    points: dict[str, GeoPoint] = {}
    for rec in ordered:
        point = GeoPoint(rec.lat, rec.lon, source, precision, rec.uprn)
        for key in record_keys(rec):
            points.setdefault(key, point)
    return points


# =============================================================================================
# Caches
# =============================================================================================
class CacheBackend(Protocol):
    name: str

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]: ...
    async def mset(self, items: Mapping[str, bytes], ttl_s: int) -> None: ...
    async def close(self) -> None: ...


class MemoryCache:
    """Cache LRU en processus (repli si Redis est indisponible, tests, benchmark hors-ligne)."""

    name = "memory"

    def __init__(self, max_items: int = 200_000) -> None:
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._max = max_items

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        now = time.monotonic()
        out: list[bytes | None] = []
        for key in keys:
            entry = self._data.get(key)
            if entry is None or entry[0] < now:
                if entry is not None:
                    del self._data[key]
                out.append(None)
            else:
                self._data.move_to_end(key)
                out.append(entry[1])
        return out

    async def mset(self, items: Mapping[str, bytes], ttl_s: int) -> None:
        expires = time.monotonic() + ttl_s
        for key, value in items.items():
            self._data[key] = (expires, value)
            self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def close(self) -> None:
        self._data.clear()


class RedisCache:
    """Cache Redis asynchrone. Toute erreur réseau = miss (jamais d'exception vers l'appelant)."""

    name = "redis"
    _COOLDOWN_S = 5.0

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(
            redis_url,
            socket_timeout=0.25,
            socket_connect_timeout=0.5,
            socket_keepalive=True,
            health_check_interval=30,
            max_connections=64,
        )
        self._down_until = 0.0

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:  # noqa: BLE001 - toute erreur de transport = indisponible
            log.warning("Redis indisponible: %s", exc)
            return False

    def _tripped(self) -> bool:
        return time.monotonic() < self._down_until

    def _trip(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + self._COOLDOWN_S
        log.warning("Redis en erreur (%s), cache ignoré %.0f s", exc, self._COOLDOWN_S)

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        if not keys or self._tripped():
            return [None] * len(keys)
        try:
            return list(await self._redis.mget(list(keys)))
        except Exception as exc:  # noqa: BLE001
            self._trip(exc)
            return [None] * len(keys)

    async def mset(self, items: Mapping[str, bytes], ttl_s: int) -> None:
        if not items or self._tripped():
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            for key, value in items.items():
                pipe.set(key, value, ex=ttl_s)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self._trip(exc)

    async def close(self) -> None:
        await self._redis.aclose()


async def create_cache(settings: Settings) -> CacheBackend:
    """Redis si joignable, sinon cache mémoire (avec avertissement)."""
    if settings.redis_url:
        cache = RedisCache(settings.redis_url)
        if await cache.ping():
            return cache
        await cache.close()
        log.warning("Redis injoignable (%s): repli sur le cache mémoire du processus", settings.redis_url)
    return MemoryCache(settings.memory_cache_max_items)


def encode_point(point: GeoPoint) -> bytes:
    return orjson.dumps([point.lat, point.lon, point.source, point.precision, point.uprn])


def decode_point(raw: bytes) -> GeoPoint:
    lat, lon, source, precision, uprn = orjson.loads(raw)
    return GeoPoint(lat, lon, source, precision, uprn)


# =============================================================================================
# Fournisseurs
# =============================================================================================
class GeocodeProvider(Protocol):
    name: str
    postcode_level: bool  # True : un appel renvoie tout le code postal (single-flight par postcode)
    cacheable: bool

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]: ...


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class IdealPostcodesProvider:
    """Ideal Postcodes (PAF + UPRN). GET /v1/postcodes/{postcode}."""

    name = "ideal_postcodes"
    postcode_level = True
    cacheable = True
    BASE_URL = "https://api.ideal-postcodes.co.uk/v1/postcodes/"

    def __init__(self, client: httpx.AsyncClient, api_key: str, timeout_s: float) -> None:
        self._client = client
        self._key = api_key
        self._timeout = timeout_s

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        assert address.postcode is not None
        try:
            resp = await self._client.get(
                self.BASE_URL + quote(compact_postcode(address.postcode)),
                params={"api_key": self._key},
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            payload = orjson.loads(resp.content)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s: échec (%s)", self.name, exc.__class__.__name__)
            return {}
        records: list[AddressRecord] = []
        for item in payload.get("result", []):
            lat, lon = _float(item.get("latitude")), _float(item.get("longitude"))
            if lat is None or lon is None:
                continue
            records.append(
                AddressRecord(
                    postcode=address.postcode,
                    building_number=item.get("building_number") or None,
                    building_name=item.get("building_name") or None,
                    sub_building=item.get("sub_building_name") or None,
                    street=item.get("thoroughfare") or item.get("dependant_thoroughfare") or None,
                    lat=lat,
                    lon=lon,
                    uprn=str(item["uprn"]) if item.get("uprn") else None,
                )
            )
        return points_from_records(records, self.name, "uprn")


class OSPlacesProvider:
    """Ordnance Survey Places API (UPRN, coordonnées WGS84). Pagine jusqu'à 500 adresses."""

    name = "os_places"
    postcode_level = True
    cacheable = True
    BASE_URL = "https://api.os.uk/search/places/v1/postcode"
    PAGE = 100
    MAX_PAGES = 5

    def __init__(self, client: httpx.AsyncClient, api_key: str, timeout_s: float) -> None:
        self._client = client
        self._key = api_key
        self._timeout = timeout_s

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        assert address.postcode is not None
        records: list[AddressRecord] = []
        offset = 0
        try:
            for _ in range(self.MAX_PAGES):
                resp = await self._client.get(
                    self.BASE_URL,
                    params={
                        "postcode": compact_postcode(address.postcode),
                        "dataset": "DPA",
                        "output_srs": "WGS84",
                        "maxresults": self.PAGE,
                        "offset": offset,
                        "key": self._key,
                    },
                    timeout=self._timeout,
                )
                if resp.status_code in (400, 404):
                    break
                resp.raise_for_status()
                payload = orjson.loads(resp.content)
                results = payload.get("results") or []
                for item in results:
                    dpa = item.get("DPA") or {}
                    lat, lon = _float(dpa.get("LAT")), _float(dpa.get("LNG"))
                    if lat is None or lon is None:
                        continue
                    records.append(
                        AddressRecord(
                            postcode=address.postcode,
                            building_number=dpa.get("BUILDING_NUMBER") or None,
                            building_name=dpa.get("BUILDING_NAME") or None,
                            sub_building=dpa.get("SUB_BUILDING_NAME") or None,
                            street=dpa.get("THOROUGHFARE_NAME") or dpa.get("DEPENDENT_THOROUGHFARE_NAME") or None,
                            lat=lat,
                            lon=lon,
                            uprn=str(dpa["UPRN"]) if dpa.get("UPRN") else None,
                        )
                    )
                total = int((payload.get("header") or {}).get("totalresults", 0))
                offset += self.PAGE
                if not results or offset >= total:
                    break
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s: échec (%s)", self.name, exc.__class__.__name__)
            return {}
        return points_from_records(records, self.name, "uprn")


class GoogleRooftopProvider:
    """Google Geocoding API : n'accepte que les résultats `ROOFTOP` non partiels."""

    name = "google_rooftop"
    postcode_level = False
    cacheable = True
    URL = "https://maps.googleapis.com/maps/api/geocode/json"

    def __init__(self, client: httpx.AsyncClient, api_key: str, timeout_s: float) -> None:
        self._client = client
        self._key = api_key
        self._timeout = timeout_s

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        key = address.cache_key
        if key is None or address.postcode is None:
            return {}
        head = " ".join(p for p in (address.sub_building, address.building_number or address.building_name, address.street) if p)
        try:
            resp = await self._client.get(
                self.URL,
                params={
                    "address": f"{head}, {address.postcode}, UK",
                    "components": f"country:GB|postal_code:{address.postcode}",
                    "key": self._key,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = orjson.loads(resp.content)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s: échec (%s)", self.name, exc.__class__.__name__)
            return {}
        if payload.get("status") != "OK" or not payload.get("results"):
            return {}
        top = payload["results"][0]
        geometry = top.get("geometry") or {}
        loc = geometry.get("location") or {}
        lat, lon = _float(loc.get("lat")), _float(loc.get("lng"))
        if lat is None or lon is None or geometry.get("location_type") != "ROOFTOP" or top.get("partial_match"):
            return {}
        return {key: GeoPoint(lat, lon, self.name, "rooftop", None)}


class PostcodesIoProvider:
    """postcodes.io (gratuit, sans clé) : renvoie le CENTRE du code postal, jamais la porte.

    Sert de repli pour tester sans abonnement : la précision est étiquetée `postcode` (≈ 15 maisons, jusqu'à
    ~100 m d'écart). Résultat non mis en cache Redis (une vraie clé UPRN doit pouvoir le remplacer) ; une
    mémoire locale évite de rappeler l'API pour chaque colis du même code postal.
    """

    name = "postcodes_io"
    postcode_level = False  # un point par adresse demandée ; la mémoire ci-dessous évite les appels répétés
    cacheable = False
    BASE_URL = "https://api.postcodes.io/postcodes/"
    MEMO_TTL_S = 24 * 3600
    MEMO_MAX = 5000

    def __init__(self, client: httpx.AsyncClient, timeout_s: float) -> None:
        self._client = client
        self._timeout = timeout_s
        self._memo: dict[str, tuple[float, tuple[float, float] | None]] = {}

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        key, postcode = address.cache_key, address.postcode
        if key is None or postcode is None:
            return {}
        now = time.monotonic()
        hit = self._memo.get(postcode)
        if hit is not None and hit[0] > now:
            coords = hit[1]
        else:
            coords = await self._fetch(postcode)
            if len(self._memo) >= self.MEMO_MAX:
                self._memo.pop(next(iter(self._memo)))
            self._memo[postcode] = (now + self.MEMO_TTL_S, coords)
        if coords is None:
            return {}
        return {key: GeoPoint(coords[0], coords[1], self.name, "postcode", None)}

    async def _fetch(self, postcode: str) -> tuple[float, float] | None:
        try:
            resp = await self._client.get(self.BASE_URL + quote(compact_postcode(postcode)), timeout=self._timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            result = orjson.loads(resp.content).get("result") or {}
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("%s: échec (%s)", self.name, exc.__class__.__name__)
            return None
        lat, lon = _float(result.get("latitude")), _float(result.get("longitude"))
        return (lat, lon) if lat is not None and lon is not None else None


# Centroïdes approximatifs des zones postales les plus courantes (point d'ancrage du fallback synthétique).
_AREA_CENTROIDS: dict[str, tuple[float, float]] = {
    "LS": (53.8008, -1.5491), "M": (53.4808, -2.2426), "B": (52.4862, -1.8904),
    "L": (53.4084, -2.9916), "BS": (51.4545, -2.5879), "E": (51.5390, -0.0300),
    "EC": (51.5155, -0.0922), "N": (51.5650, -0.1100), "NW": (51.5430, -0.1700),
    "SE": (51.4700, -0.0500), "SW": (51.4650, -0.1600), "W": (51.5100, -0.2200),
    "WC": (51.5200, -0.1200), "EH": (55.9533, -3.1883), "G": (55.8642, -4.2518),
    "CF": (51.4816, -3.1791), "NE": (54.9783, -1.6178), "S": (53.3811, -1.4701),
    "NG": (52.9548, -1.1581), "LE": (52.6369, -1.1398), "CV": (52.4068, -1.5197),
    "OX": (51.7520, -1.2577), "CB": (52.2053, 0.1218), "YO": (53.9600, -1.0873),
    "BT": (54.5973, -5.9301), "AB": (57.1497, -2.0943), "DD": (56.4620, -2.9707),
    "PL": (50.3755, -4.1427), "EX": (50.7184, -3.5339), "SO": (50.9097, -1.4044),
    "PO": (50.8198, -1.0880), "BN": (50.8225, -0.1372), "RG": (51.4543, -0.9781),
    "MK": (52.0406, -0.7594), "NR": (52.6309, 1.2974), "HU": (53.7457, -0.3367),
    "BD": (53.7960, -1.7594), "WF": (53.6830, -1.4977), "SK": (53.4106, -2.1575),
    "PR": (53.7632, -2.7031), "ST": (53.0027, -2.1794), "DE": (52.9225, -1.4746),
    "SA": (51.6214, -3.9436), "LL": (53.3241, -3.8276),
}
_DEFAULT_CENTROID = (52.4862, -1.8904)  # Birmingham : centre géographique approximatif de l'Angleterre


def synthetic_coordinates(postcode: str, building_id: str) -> tuple[float, float]:
    """Point déterministe (même entrée => mêmes coordonnées) géométriquement cohérent :

    * le code postal ancre une "rue virtuelle" à ≤ 1,2 km du centroïde de sa zone ;
    * les numéros se suivent tous les ~9 m le long de la rue, impairs d'un côté, pairs de l'autre ;
    * un nom de maison est placé par hachage sur la même rue.
    Ces coordonnées sont fictives : elles servent aux tests et à la démo, jamais à livrer.
    """
    outward = postcode.split()[0]
    area = "".join(ch for ch in outward if ch.isalpha())[:2]
    base_lat, base_lon = _AREA_CENTROIDS.get(area) or _AREA_CENTROIDS.get(area[:1]) or _DEFAULT_CENTROID

    h = hashlib.blake2b(postcode.encode(), digest_size=12).digest()
    u1 = int.from_bytes(h[0:4], "big") / 2**32
    u2 = int.from_bytes(h[4:8], "big") / 2**32
    u3 = int.from_bytes(h[8:12], "big") / 2**32
    radius = 1200.0 * math.sqrt(u1)
    theta = 2 * math.pi * u2
    east, north = radius * math.cos(theta), radius * math.sin(theta)
    bearing = math.pi * u3  # orientation de la rue virtuelle

    digits = ""
    for ch in building_id:
        if ch.isdigit():
            digits += ch
        else:
            break
    if digits:
        n = int(digits)
        along = ((n // 2) % 30) * 9.0
        side = 11.0 if n % 2 else -11.0
    else:
        hb = hashlib.blake2b(building_id.encode(), digest_size=4).digest()
        along = (int.from_bytes(hb, "big") % 270) * 1.0
        side = 11.0 if hb[0] % 2 else -11.0
    east += along * math.cos(bearing) - side * math.sin(bearing)
    north += along * math.sin(bearing) + side * math.cos(bearing)

    lat = base_lat + north / 111_320.0
    lon = base_lon + east / (111_320.0 * math.cos(math.radians(base_lat)))
    return round(lat, 7), round(lon, 7)


class SyntheticProvider:
    """Dernier recours déterministe (tests, démo). Jamais mis en cache, toujours étiqueté `synthetic`."""

    name = "synthetic"
    postcode_level = False
    cacheable = False

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        key, bid = address.cache_key, address.building_id
        if key is None or bid is None or address.postcode is None:
            return {}
        lat, lon = synthetic_coordinates(address.postcode, bid)
        return {key: GeoPoint(lat, lon, self.name, "synthetic", None)}


class StaticProvider:
    """Table {clé de cache -> (lat, lon)} : jeux de test et benchmark (émule un fournisseur UPRN)."""

    name = "static"
    postcode_level = False
    cacheable = True

    def __init__(self, table: Mapping[str, tuple[float, float]] | None = None) -> None:
        self._table: dict[str, tuple[float, float]] = dict(table or {})
        self.calls = 0

    def register(self, key: str, lat: float, lon: float) -> None:
        self._table[key] = (lat, lon)

    async def lookup(self, address: ParsedAddress) -> dict[str, GeoPoint]:
        self.calls += 1
        key = address.cache_key
        if key is None or key not in self._table:
            return {}
        lat, lon = self._table[key]
        return {key: GeoPoint(lat, lon, self.name, "uprn", None)}


def build_providers(settings: Settings, client: httpx.AsyncClient) -> list[GeocodeProvider]:
    providers: list[GeocodeProvider] = []
    if settings.ideal_postcodes_api_key:
        providers.append(IdealPostcodesProvider(client, settings.ideal_postcodes_api_key, settings.geocode_timeout_s))
    if settings.os_places_api_key:
        providers.append(OSPlacesProvider(client, settings.os_places_api_key, settings.geocode_timeout_s))
    if settings.google_maps_api_key:
        providers.append(GoogleRooftopProvider(client, settings.google_maps_api_key, settings.geocode_timeout_s))
    if settings.postcodes_io_enabled:
        providers.append(PostcodesIoProvider(client, settings.geocode_timeout_s))
    if settings.allow_synthetic_geocoding:
        providers.append(SyntheticProvider())
    if not providers:
        log.error("Aucun fournisseur de géocodage configuré: toutes les adresses non cachées échoueront")
    return providers


# =============================================================================================
# Géocodeur
# =============================================================================================
@dataclass(slots=True)
class GeocoderUK:
    cache: CacheBackend
    providers: list[GeocodeProvider]
    ttl_s: int = 30 * 24 * 3600
    cache_prefix: str = "uprn:v1"
    _inflight: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)

    def _redis_key(self, key: str) -> str:
        return f"{self.cache_prefix}:{key}"

    async def geocode(self, address: ParsedAddress) -> GeoResult | None:
        return (await self.geocode_many([address])).results[0]

    async def geocode_many(self, addresses: Sequence[ParsedAddress]) -> GeocodeBatch:
        """Géocode un lot : 1 `MGET`, puis résolution concurrente des seuls manquants."""
        t0 = time.perf_counter()
        keys = [a.cache_key for a in addresses]
        unique: dict[str, ParsedAddress] = {}
        for key, address in zip(keys, addresses):
            if key is not None:
                unique.setdefault(key, address)

        points: dict[str, GeoResult] = {}
        raw = await self.cache.mget([self._redis_key(k) for k in unique])
        missing: list[ParsedAddress] = []
        for (key, address), blob in zip(unique.items(), raw):
            if blob is not None:
                try:
                    points[key] = GeoResult(decode_point(blob), cached=True)
                    continue
                except (ValueError, TypeError):
                    log.warning("entrée de cache corrompue ignorée: %s", key)
            missing.append(address)
        hits = len(points)

        if missing:
            resolved = await asyncio.gather(*(self._resolve(a) for a in missing))
            for address, point in zip(missing, resolved):
                if point is not None and address.cache_key is not None:
                    points[address.cache_key] = GeoResult(point, cached=False)

        results = [points.get(k) if k is not None else None for k in keys]
        unresolved = sum(r is None for r in results)
        return GeocodeBatch(
            results=results,
            cache_hits=hits,
            resolved=len(results) - unresolved,
            unresolved=unresolved,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )

    async def _resolve(self, address: ParsedAddress) -> GeoPoint | None:
        key = address.cache_key
        assert key is not None
        for provider in self.providers:
            try:
                found = await self._provider_lookup(provider, address)
            except Exception:  # noqa: BLE001 - un fournisseur défaillant ne doit pas bloquer la chaîne
                log.exception("fournisseur %s en erreur", provider.name)
                continue
            point = found.get(key)
            if point is not None:
                return point
        return None

    async def _call_provider(self, provider: GeocodeProvider, address: ParsedAddress) -> dict[str, GeoPoint]:
        found = await provider.lookup(address)
        if provider.cacheable and found:
            await self.cache.mset({self._redis_key(k): encode_point(p) for k, p in found.items()}, self.ttl_s)
        return found

    async def _provider_lookup(self, provider: GeocodeProvider, address: ParsedAddress) -> dict[str, GeoPoint]:
        """Single-flight : un seul appel sortant (et une seule écriture cache) par fournisseur et
        par code postal (fournisseurs PAF) ou par adresse (Google)."""
        scope = address.postcode if provider.postcode_level else address.cache_key
        flight = (provider.name, scope or "")
        task = self._inflight.get(flight)
        if task is None:
            task = asyncio.ensure_future(self._call_provider(provider, address))
            self._inflight[flight] = task
            task.add_done_callback(lambda _t, f=flight: self._inflight.pop(f, None))
        return await asyncio.shield(task)

    async def close(self) -> None:
        await self.cache.close()

