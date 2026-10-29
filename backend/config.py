"""Configuration centrale du backend (lue une seule fois depuis l'environnement).

Toutes les constantes métier (vitesse piétonne, seuils en mètres, URLs, clés API)
vivent ici. Aucune autre module ne lit `os.environ` directement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

# --- Constantes physiques ------------------------------------------------------
EARTH_RADIUS_M = 6_371_008.8  # rayon moyen terrestre (IUGG), en mètres

# Boîte englobante du Royaume-Uni (Shetland et Cornouailles inclus), en degrés.
UK_LAT_MIN, UK_LAT_MAX = 49.8, 60.95
UK_LON_MIN, UK_LON_MAX = -8.7, 1.85


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    # --- Environnement --------------------------------------------------------
    app_env: str = "development"
    log_level: str = "INFO"
    api_key: str = ""  # vide = pas d'authentification (dev). En prod : X-API-Key / ?key=

    # --- Modèle piéton "livreur opportuniste" ---------------------------------
    walk_speed_mps: float = 1.65  # course légère
    walk_fixed_s: float = 10.0  # arrêt / traversée de rue / franchissement de bollard
    walk_max_distance_m: float = 160.0  # seuil vol d'oiseau pour un WALK_SHORTCUT
    max_cluster_size: int = 8  # nb max de portes dans un ParkingCluster
    pocket_van_close_s: float = 30.0  # deux colis à moins de 30 s de van (dans les 2 sens) = même "poche"
    service_time_s: float = 30.0  # remise d'un colis (identique van strict / hybride)

    # --- Solveur OR-Tools -------------------------------------------------------
    solver_time_limit_ms: int = 150
    solver_workers: int = 2  # processus du pool (0 = thread par défaut, pour tests)

    # --- OSRM -----------------------------------------------------------------
    osrm_url: str = "http://osrm-uk:5000"
    osrm_timeout_s: float = 3.0
    osrm_max_table_size: int = 250  # doit être <= --max-table-size du serveur OSRM
    osrm_unreachable_cost_s: float = 6 * 3600.0  # remplace les `null` de la Table API

    # --- Redis / cache UPRN -----------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    cache_ttl_s: int = 30 * 24 * 3600
    cache_prefix: str = "uprn:v1"
    memory_cache_max_items: int = 200_000

    # --- Géocodage UK -----------------------------------------------------------
    ideal_postcodes_api_key: str = ""
    os_places_api_key: str = ""
    google_maps_api_key: str = ""
    geocode_timeout_s: float = 0.8
    # Repli gratuit : position du CENTRE du code postal (précision "postcode", ~15 maisons), sans clé API.
    postcodes_io_enabled: bool = True
    # Si aucun fournisseur réel ne répond, un point synthétique déterministe est
    # renvoyé (tests, démo). Jamais activé par défaut en production.
    allow_synthetic_geocoding: bool = True

    # --- Vision fallback (Gemini) -------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_s: float = 4.0
    max_image_bytes: int = 128 * 1024  # un ROI WebP q70 fait ~15 Ko ; marge large

    # --- Réseau HTTP sortant ------------------------------------------------------
    http_max_connections: int = 100
    http_max_keepalive: int = 50

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @classmethod
    def from_env(cls) -> "Settings":
        app_env = _env_str("APP_ENV", "development")
        return cls(
            app_env=app_env,
            log_level=_env_str("LOG_LEVEL", "INFO"),
            api_key=_env_str("API_KEY"),
            walk_speed_mps=_env_float("WALK_SPEED_MPS", 1.65),
            walk_fixed_s=_env_float("WALK_FIXED_S", 10.0),
            walk_max_distance_m=_env_float("WALK_MAX_DISTANCE_M", 160.0),
            max_cluster_size=_env_int("MAX_CLUSTER_SIZE", 8),
            pocket_van_close_s=_env_float("POCKET_VAN_CLOSE_S", 30.0),
            service_time_s=_env_float("SERVICE_TIME_S", 30.0),
            solver_time_limit_ms=_env_int("SOLVER_TIME_LIMIT_MS", 150),
            solver_workers=_env_int("SOLVER_WORKERS", 2),
            osrm_url=_env_str("OSRM_URL", "http://osrm-uk:5000").rstrip("/"),
            osrm_timeout_s=_env_float("OSRM_TIMEOUT_S", 3.0),
            osrm_max_table_size=_env_int("OSRM_MAX_TABLE_SIZE", 250),
            osrm_unreachable_cost_s=_env_float("OSRM_UNREACHABLE_COST_S", 6 * 3600.0),
            redis_url=_env_str("REDIS_URL", "redis://redis:6379/0"),
            cache_ttl_s=_env_int("CACHE_TTL_S", 30 * 24 * 3600),
            memory_cache_max_items=_env_int("MEMORY_CACHE_MAX_ITEMS", 200_000),
            ideal_postcodes_api_key=_env_str("IDEAL_POSTCODES_API_KEY"),
            os_places_api_key=_env_str("OS_PLACES_API_KEY"),
            google_maps_api_key=_env_str("GOOGLE_MAPS_API_KEY"),
            geocode_timeout_s=_env_float("GEOCODE_TIMEOUT_S", 0.8),
            postcodes_io_enabled=_env_bool("POSTCODES_IO_ENABLED", True),
            allow_synthetic_geocoding=_env_bool(
                "ALLOW_SYNTHETIC_GEOCODING", app_env.lower() != "production"
            ),
            gemini_api_key=_env_str("GEMINI_API_KEY") or _env_str("GOOGLE_API_KEY"),
            gemini_model=_env_str("GEMINI_MODEL", "gemini-2.5-flash"),
            gemini_timeout_s=_env_float("GEMINI_TIMEOUT_S", 4.0),
            max_image_bytes=_env_int("MAX_IMAGE_BYTES", 128 * 1024),
            http_max_connections=_env_int("HTTP_MAX_CONNECTIONS", 100),
            http_max_keepalive=_env_int("HTTP_MAX_KEEPALIVE", 50),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
