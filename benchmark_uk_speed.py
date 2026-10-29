#!/usr/bin/env python3
"""Benchmark & validation terrain UK : 50 colis, pièges réels, pipeline de bout en bout.

    python benchmark_uk_speed.py                        # OSRM simulé en processus (hors-ligne)
    python benchmark_uk_speed.py --osrm-url http://localhost:5000 --redis-url redis://localhost:6379/0

Le scénario (quartier fictif, adresses fictives) contient les pièges classiques du dernier kilomètre
britannique : rue coupée par des bollards LTN, maisons face à face sur une avenue à sens unique,
impasses adjacentes, appartements d'un même immeuble et *House Names*.

Pipeline exécuté à chaque passe (code de production, rien de réécrit ici) :
    texte OCR -> parseur UK -> [Gemini simulé si étiquette déchirée] -> géocodage (cache) ->
    OSRM Table API + matrice NumPy hybride -> OR-Tools (M_hybrid || M_van) -> 3 plans candidats
    (VAN_STRICT, HYBRID_SPEC, HYBRID_POCKETS) chiffrés avec retour à pied jusqu'au van -> le plus court

HONNÊTETÉ DES MESURES
  * Sans --osrm-url, « OSRM » est un petit moteur simulé (graphe de rues du scénario, sens uniques,
    bollards, pénalités de carrefour) servi par un vrai client HTTP asynchrone, mais en mémoire :
    son temps de réponse n'est PAS celui d'OSRM + CH sur la carte de Grande-Bretagne.
  * Avec --osrm-url, les temps OSRM sont réels mais le scénario reste fictif : les coordonnées ne
    correspondent pas à de vraies rues, donc seuls les temps (pas les gains) ont un sens.
  * Les fournisseurs d'adresses (UPRN) et Gemini sont simulés : leur latence réseau n'est pas mesurée.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import orjson  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Route  # noqa: E402

from config import Settings  # noqa: E402
from geocoder_uk import GeocoderUK, MemoryCache, StaticProvider, create_cache  # noqa: E402
from hybrid_matrix import HybridMatrix, HybridMatrixBuilder, OSRMClient, fuse_matrices, haversine_matrix, walk_time_matrix  # noqa: E402
from routing_engine import PlanConfig, PlanOutcome, RoutePlan, RoutingEngine, SolverPool, StopMeta  # noqa: E402
from uk_address_parser import ParsedAddress, make_cache_key, parse_uk_address  # noqa: E402
from vision_extractor import GeminiVisionExtractor, UKAddressExtraction  # noqa: E402

# =============================================================================================
# 1. Géométrie locale et réseau routier simulé
# =============================================================================================
ORIGIN_LAT, ORIGIN_LON = 53.8190, -1.5780  # quartier fictif (nord de Leeds)
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LAT))
MPH = 0.44704


def to_latlon(x: float, y: float) -> tuple[float, float]:
    return ORIGIN_LAT + y / M_PER_DEG_LAT, ORIGIN_LON + x / M_PER_DEG_LON


def to_xy(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    return np.stack([(np.asarray(lon) - ORIGIN_LON) * M_PER_DEG_LON, (np.asarray(lat) - ORIGIN_LAT) * M_PER_DEG_LAT], axis=-1)


@dataclass(frozen=True)
class Segment:
    name: str
    u: str
    v: str
    mph: float
    oneway: bool = False  # True : circulation u -> v uniquement


NODES: dict[str, tuple[float, float]] = {
    # ceinture : Kingsway Road (sud), Northgate (nord), Station Road (ouest), Mill Road (est)
    "A0": (0, 0), "B": (900, 0), "E": (1800, 0),
    "P1": (0, 300), "P2": (0, 600), "P3": (0, 900), "N0": (0, 1200),
    "S1": (1800, 300), "S2": (1800, 600), "S3": (1800, 900), "N4": (1800, 1200),
    "N2": (900, 1200), "N3": (1350, 1200),
    # intérieur : Albion Avenue (y=300, sens unique), Park Lane (y=600), Cranbrook Road (x=900)
    "Q1": (900, 300), "Q2": (900, 600), "R1": (1200, 300),
    "F1": (900, 655), "F2": (900, 665),  # les deux côtés du filtre modal à bollards de Cranbrook Road
    # lotissements desservis par de longues routes d'accès (Primrose Lane 600 m, Lavender Lane 450 m)
    "PJ": (1200, 600), "Z1": (1200, 430),  # Primrose Close : cul-de-sac sud, tête en Z1
    "Z2": (1200, 390),  # Bluebell Mews : cul-de-sac nord depuis Albion Avenue, tête en Z2 (40 m de Z1)
    "Z3": (1350, 1000),  # Foxglove Mews : cul-de-sac sud depuis Northgate
    "LJ": (1350, 900), "Z4": (1350, 964),  # Lavender Close : tête en Z4 (36 m de Z3)
}
SEGMENTS: list[Segment] = [
    *(Segment("Kingsway Road", u, v, 30) for u, v in (("A0", "B"), ("B", "E"))),
    *(Segment("Northgate", u, v, 30) for u, v in (("N0", "N2"), ("N2", "N3"), ("N3", "N4"))),
    *(Segment("Station Road", u, v, 30) for u, v in (("A0", "P1"), ("P1", "P2"), ("P2", "P3"), ("P3", "N0"))),
    *(Segment("Mill Road", u, v, 30) for u, v in (("E", "S1"), ("S1", "S2"), ("S2", "S3"), ("S3", "N4"))),
    *(Segment("Park Lane", u, v, 30) for u, v in (("P2", "Q2"), ("Q2", "PJ"))),
    # Cranbrook Road : coupée entre F1 et F2 (bollards LTN) -> deux tronçons sans lien pour un véhicule.
    *(Segment("Cranbrook Road", u, v, 20) for u, v in (("B", "Q1"), ("Q1", "Q2"), ("Q2", "F1"), ("F2", "N2"))),
    *(Segment("Albion Avenue", u, v, 20, oneway=True) for u, v in (("P1", "Q1"), ("Q1", "R1"), ("R1", "S1"))),
    Segment("Primrose Lane", "S2", "PJ", 20),
    Segment("Primrose Close", "PJ", "Z1", 20),
    Segment("Bluebell Mews", "R1", "Z2", 20),
    Segment("Foxglove Mews", "N3", "Z3", 20),
    Segment("Lavender Lane", "S3", "LJ", 20),
    Segment("Lavender Close", "LJ", "Z4", 20),
]


class RoadNetwork:
    """Graphe routier minimal : plus courts chemins (Floyd-Warshall) + accrochage des portes aux rues."""

    def __init__(self, nodes: dict[str, tuple[float, float]], segments: list[Segment], junction_s: float = 4.0) -> None:
        names = list(nodes)
        index = {n: i for i, n in enumerate(names)}
        xy = np.array([nodes[n] for n in names], dtype=float)
        self.su = np.array([index[s.u] for s in segments])
        self.sv = np.array([index[s.v] for s in segments])
        self.seg_origin = xy[self.su]
        self.seg_vec = xy[self.sv] - self.seg_origin
        self.seg_len = np.hypot(self.seg_vec[:, 0], self.seg_vec[:, 1])
        self.seg_speed = np.array([s.mph * MPH for s in segments])
        self.fwd = np.ones(len(segments), dtype=bool)
        self.bwd = np.array([not s.oneway for s in segments])
        self.junction_s = junction_s
        v = len(names)
        dist = np.full((v, v), np.inf)
        np.fill_diagonal(dist, 0.0)
        for k in range(len(segments)):
            w = self.seg_len[k] / self.seg_speed[k] + junction_s
            dist[self.su[k], self.sv[k]] = min(dist[self.su[k], self.sv[k]], w)
            if self.bwd[k]:
                dist[self.sv[k], self.su[k]] = min(dist[self.sv[k], self.su[k]], w)
        for k in range(v):
            dist = np.minimum(dist, dist[:, k : k + 1] + dist[k : k + 1, :])
        self.dist = dist

    def snap(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Segment le plus proche de chaque point et abscisse relative t ∈ [0, 1] sur ce segment."""
        p = pts[:, None, :]
        t = np.clip(((p - self.seg_origin[None]) * self.seg_vec[None]).sum(-1) / (self.seg_len**2)[None], 0.0, 1.0)
        proj = self.seg_origin[None] + t[..., None] * self.seg_vec[None]
        d = np.hypot(*(p - proj).transpose(2, 0, 1))
        s = d.argmin(axis=1)
        return s, t[np.arange(len(pts)), s]

    def drive_times(self, pts: np.ndarray) -> np.ndarray:
        """Matrice des temps de conduite (s) entre points accrochés à la rue, sens uniques respectés."""
        s, t = self.snap(pts)
        sec = self.seg_len[s] / self.seg_speed[s]
        inf = np.inf
        exit_nodes = np.stack([self.sv[s], self.su[s]], axis=1)
        exit_costs = np.stack([np.where(self.fwd[s], (1 - t) * sec, inf), np.where(self.bwd[s], t * sec, inf)], axis=1)
        entry_nodes = np.stack([self.su[s], self.sv[s]], axis=1)
        entry_costs = np.stack(
            [np.where(self.fwd[s], self.junction_s + t * sec, inf), np.where(self.bwd[s], self.junction_s + (1 - t) * sec, inf)],
            axis=1,
        )
        via_nodes = self.dist[exit_nodes[:, None, :, None], entry_nodes[None, :, None, :]]
        cost = (exit_costs[:, None, :, None] + via_nodes + entry_costs[None, :, None, :]).min(axis=(2, 3))
        same = s[:, None] == s[None, :]
        dt = (t[None, :] - t[:, None]) * sec[:, None]  # temps le long de u -> v de i vers j
        forward = np.where(same & self.fwd[s][:, None] & (dt >= 0), dt, inf)
        backward = np.where(same & self.bwd[s][:, None] & (dt <= 0), -dt, inf)
        cost = np.minimum(cost, np.minimum(forward, backward))
        np.fill_diagonal(cost, 0.0)
        return cost


def make_sim_osrm(network: RoadNetwork) -> Starlette:
    """Sert la Table API d'OSRM (`/table/v1/driving/lon,lat;lon,lat`) depuis le réseau simulé."""

    async def table(request):  # type: ignore[no-untyped-def]
        pairs = [tuple(map(float, c.split(","))) for c in request.path_params["coords"].split(";")]
        lon = np.array([p[0] for p in pairs])
        lat = np.array([p[1] for p in pairs])
        cost = network.drive_times(to_xy(lat, lon))
        rows = [[round(float(v), 1) if math.isfinite(v) else None for v in row] for row in cost]
        return Response(orjson.dumps({"code": "Ok", "durations": rows}), media_type="application/json")

    async def nearest(request):  # type: ignore[no-untyped-def]
        return Response(orjson.dumps({"code": "Ok"}), media_type="application/json")

    return Starlette(
        routes=[
            Route("/table/v1/driving/{coords:path}", table),
            Route("/nearest/v1/driving/{coords:path}", nearest),
        ]
    )


# =============================================================================================
# 2. Scénario : 50 colis, étiquettes OCR réalistes
# =============================================================================================
@dataclass(frozen=True)
class Parcel:
    pid: str
    trap: str  # ONE_WAY | FLATS | LTN | CUL_DE_SAC | HOUSE_NAME | PLAIN
    street: str
    postcode: str
    number: str | None
    name: str | None
    sub: str | None
    x: float
    y: float
    recipient: str

    @property
    def truth_key(self) -> str:
        return make_cache_key(self.postcode, self.number or self.name or "")

    @property
    def latlon(self) -> tuple[float, float]:
        return to_latlon(self.x, self.y)


# (rue, piège, code postal, numéro | nom de maison, appartement, x, y)
_RAW = [
    # Albion Avenue : sens unique vers l'est, maisons face à face (côté nord y=312 impair, côté sud y=288 pair)
    *[("ALBION AVENUE", "ONE_WAY", "LS6 1AD", n, None, x, 312) for n, x in (("21", 300), ("23", 360), ("25", 420), ("29", 540))],
    ("ALBION AVENUE", "FLATS", "LS6 1AD", "27", "FLAT 1", 480, 312),
    ("ALBION AVENUE", "FLATS", "LS6 1AD", "27", "FLAT 2", 480, 312),
    *[("ALBION AVENUE", "ONE_WAY", "LS6 1AE", n, None, x, 288) for n, x in (("22", 300), ("24", 360), ("26", 420), ("28", 480), ("30", 540), ("32", 600))],
    # Cranbrook Road : filtre modal à bollards en (900, 660). Rive sud (accès Park Lane) : 5 colis ;
    # rive nord (accès par Northgate uniquement, 540 m de rue + la ceinture) : 2 colis.
    ("CRANBROOK ROAD", "LTN", "LS6 2BA", "2", None, 888, 590),
    ("CRANBROOK ROAD", "LTN", "LS6 2BA", "4", None, 888, 620),
    ("CRANBROOK ROAD", "LTN", "LS6 2BA", "1", None, 912, 600),
    ("CRANBROOK ROAD", "LTN", "LS6 2BA", "3", None, 912, 630),
    ("CRANBROOK ROAD", "LTN", "LS6 2BA", "THE GABLES", None, 912, 648),
    ("CRANBROOK ROAD", "LTN", "LS6 2BB", "21", None, 912, 676),
    ("CRANBROOK ROAD", "LTN", "LS6 2BB", "22", None, 888, 684),
    # Bluebell Mews (accès Albion Avenue, 5 colis) / Primrose Close (accès Park Lane puis Primrose Lane, 2 colis) :
    # impasses dos à dos, 40 m entre les deux culs-de-sac
    ("BLUEBELL MEWS", "HOUSE_NAME", "LS6 2QB", "THE LAURELS", None, 1188, 382),
    ("BLUEBELL MEWS", "CUL_DE_SAC", "LS6 2QB", "1", None, 1188, 332),
    ("BLUEBELL MEWS", "CUL_DE_SAC", "LS6 2QB", "3", None, 1188, 356),
    ("BLUEBELL MEWS", "CUL_DE_SAC", "LS6 2QB", "2", None, 1212, 345),
    ("BLUEBELL MEWS", "CUL_DE_SAC", "LS6 2QB", "4", None, 1212, 370),
    ("PRIMROSE CLOSE", "HOUSE_NAME", "LS6 2QA", "ROSE COTTAGE", None, 1212, 442),
    ("PRIMROSE CLOSE", "CUL_DE_SAC", "LS6 2QA", "5", None, 1188, 450),
    # Foxglove Mews (accès Northgate, 4 colis) / Lavender Close (accès Mill Road puis Lavender Lane, 2 colis) :
    # seconde paire d'impasses, 36 m entre les culs-de-sac
    ("FOXGLOVE MEWS", "HOUSE_NAME", "LS6 3FA", "FOXGLOVE LODGE", None, 1362, 1008),
    ("FOXGLOVE MEWS", "CUL_DE_SAC", "LS6 3FA", "1", None, 1338, 1040),
    ("FOXGLOVE MEWS", "CUL_DE_SAC", "LS6 3FA", "2", None, 1362, 1075),
    ("FOXGLOVE MEWS", "CUL_DE_SAC", "LS6 3FA", "3", None, 1338, 1110),
    ("LAVENDER CLOSE", "CUL_DE_SAC", "LS6 3LA", "4", None, 1362, 958),
    ("LAVENDER CLOSE", "CUL_DE_SAC", "LS6 3LA", "3", None, 1338, 950),
    # Rues ouvertes à double sens, colis plus espacés
    *[("KINGSWAY ROAD", "PLAIN", "LS6 1NA", str(n), None, x, -12 if n % 2 else 12) for n, x in ((45, 1300), (47, 1360), (49, 1420), (50, 1480), (52, 1540))],
    *[("STATION ROAD", "PLAIN", "LS6 4SR", str(n), None, 12 if n % 2 else -12, y) for n, y in ((17, 420), (18, 450), (19, 480), (20, 520))],
    *[("MILL ROAD", "PLAIN", "LS6 4ML", str(n), None, 1812 if n % 2 else 1788, y) for n, y in ((111, 430), (112, 450), (113, 480), (114, 520))],
    *[("PARK LANE", "PLAIN", "LS6 1PL", str(n), None, x, 612 if n % 2 else 588) for n, x in ((7, 300), (8, 340), (10, 400))],
    ("NORTHGATE", "PLAIN", "LS6 4NG", "3", None, 100, 1212),
    ("NORTHGATE", "PLAIN", "LS6 4NG", "5", None, 160, 1212),
]
# Scénario témoin : 50 colis sur des rues ouvertes à double sens, sans aucun piège (le gain attendu est ~0).
_CONTROL = [
    *[("KINGSWAY ROAD", "PLAIN", "LS6 1NA", str(n), None, x, -12 if n % 2 else 12)
      for n, x in zip(range(31, 41), (140, 180, 220, 260, 300, 1000, 1040, 1080, 1120, 1160))],
    *[("NORTHGATE", "PLAIN", "LS6 4NG", str(n), None, x, 1212 if n % 2 else 1188)
      for n, x in zip(range(11, 21), (200, 240, 280, 320, 360, 1400, 1440, 1480, 1520, 1560))],
    *[("STATION ROAD", "PLAIN", "LS6 4SR", str(n), None, 12 if n % 2 else -12, y)
      for n, y in zip(range(41, 51), (420, 460, 500, 540, 580, 880, 920, 960, 1000, 1040))],
    *[("MILL ROAD", "PLAIN", "LS6 4ML", str(n), None, 1812 if n % 2 else 1788, y)
      for n, y in zip(range(61, 71), (100, 140, 180, 220, 260, 620, 660, 700, 740, 780))],
    *[("PARK LANE", "PLAIN", "LS6 1PL", str(n), None, x, 612 if n % 2 else 588)
      for n, x in zip(range(81, 91), (520, 560, 600, 640, 680, 1040, 1080, 1120, 1160, 1200))],
]
DEPOT_XY = (100.0, -10.0)  # dépôt fictif sur Kingsway Road
RECIPIENTS = [
    "MR J SMITH", "MRS A PATEL", "MISS L BROWN", "J O'NEILL", "MS P JONES", "DR K LEE", "MR T WALKER", "MRS D KHAN",
    "A HUSSAIN", "MS C WILLIAMS", "MR R TAYLOR", "MISS E DAVIES", "MR S EVANS", "MRS H THOMAS", "G ROBERTS", "MS N WRIGHT",
    "MR O JOHNSON", "MRS F GREEN", "B HALL", "MS I WOOD",
]
CARRIERS = ["ROYAL MAIL", "DPD", "EVRI", "AMAZON", "YODEL", "DHL"]
DAMAGED = {6, 33}  # étiquettes déchirées : le code postal est perdu -> chemin lent (Gemini)


def build_parcels(scenario: str = "traps") -> list[Parcel]:
    out = []
    for i, (street, trap, pc, who, sub, x, y) in enumerate(_RAW if scenario == "traps" else _CONTROL):
        is_number = re.fullmatch(r"\d+[A-Z]?(-\d+)?", who) is not None
        out.append(
            Parcel(f"P{i + 1:02d}", trap, street, pc, who if is_number else None, None if is_number else who, sub, x, y,
                   RECIPIENTS[i % len(RECIPIENTS)])
        )
    return out


def render_label(p: Parcel, i: int, seed: int) -> str:
    """Texte OCR bruité d'une étiquette (transporteur, suivi, téléphone, poids, bloc expéditeur...)."""
    rng = random.Random(seed * 1000 + i)
    digits = lambda n: "".join(rng.choice("0123456789") for _ in range(n))  # noqa: E731
    if p.number:
        street_lines = [f"{p.number.replace('-', ' - ') if i % 5 == 0 else p.number} {p.street}"]
    else:
        street_lines = [p.name, p.street] if i % 2 == 0 else [f"{p.name}, {p.street}"]
    if p.sub:
        street_lines = [f"{p.sub}, {street_lines[0]}"] if i % 2 == 0 else [p.sub, *street_lines]
    town = "HEADINGLEY\nLEEDS" if i % 3 == 0 else "LEEDS"
    pc = p.postcode
    if i in DAMAGED:
        return f"{p.recipient}\n{street_lines[-1][:6]}...\n~~ TORN ~~\n{digits(10)}GB"
    carrier = CARRIERS[i % len(CARRIERS)]
    body = "\n".join(street_lines)
    if carrier == "ROYAL MAIL":
        return f"ROYAL MAIL\nTRACKED 48\nSIGNED FOR\n{p.recipient}\n{body}\n{town}\n{pc}\nJD0{digits(9)}GB"
    if carrier == "DPD":
        return f"DPD\nDeliver to:\n{p.recipient}\n{body}\n{town.splitlines()[-1]} {pc}\nTel: 07700 9{digits(5)}\nRef {digits(10)}\n{rng.randint(1, 9)}.{rng.randint(1, 9)}KG"
    if carrier == "EVRI":
        return f"EVRI\n{p.recipient}\n{body}\n{town}\n{pc}\n{rng.randint(1, 9)} KG\nFROM: ONLINE SHOP LTD\n12 FACTORY ROAD\nMANCHESTER M4 5AB"
    if carrier == "AMAZON":
        return f"amazon\nShip to:\n{p.recipient}\n{body}\n{town.splitlines()[-1]} {pc}\nOrder #{digits(3)}-{digits(7)}-{digits(7)}"
    if carrier == "YODEL":
        return f"YODEL\n{p.recipient.title()}\n{', '.join(street_lines)}, {town.splitlines()[-1].title()}, {pc}\nPARCEL 1 OF 1"
    return f"DHL EXPRESS\nTo: {p.recipient}\n{body}\n{pc} {town.splitlines()[-1]}\nWEIGHT {rng.randint(1, 9)}.{rng.randint(1, 9)} KG"


def inject_ocr_glitches(labels: list[str], parcels: list[Parcel], count: int = 3) -> list[int]:
    """Confond un chiffre du code postal avec une lettre (1->l, 2->Z) : testé par la réparation OCR."""
    glitched: list[int] = []
    for i, (p, label) in enumerate(zip(parcels, labels)):
        if len(glitched) == count:
            break
        if i in DAMAGED or p.postcode[4] not in "12" or i % 4 != 1:
            continue
        bad = p.postcode[:4] + {"1": "l", "2": "Z"}[p.postcode[4]] + p.postcode[5:]
        labels[i] = label.replace(p.postcode, bad)
        glitched.append(i)
    return glitched


# =============================================================================================
# 3. Pipeline chronométré
# =============================================================================================
WEBP_STUB = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(24)


class TruthGemini:
    """Faux client Gemini : renvoie l'adresse vraie du colis (aucun appel réseau)."""

    def __init__(self) -> None:
        self.queue: list[UKAddressExtraction] = []
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model, contents, config):  # type: ignore[no-untyped-def]
        return SimpleNamespace(parsed=self.queue.pop(0), text="")


def truth_extraction(p: Parcel) -> UKAddressExtraction:
    return UKAddressExtraction(
        readable=True, recipient_name=p.recipient, sub_building=p.sub, building_number=p.number, building_name=p.name,
        street=p.street, locality=None, post_town="LEEDS", postcode=p.postcode, confidence=0.9,
    )


@dataclass
class Bench:
    parcels: list[Parcel]
    labels: list[str]
    settings: Settings
    provider: StaticProvider
    gemini: TruthGemini
    vision: GeminiVisionExtractor
    builder: HybridMatrixBuilder
    engine: RoutingEngine
    depot: tuple[float, float]


@dataclass
class RunResult:
    timings: dict[str, float]
    outcome: PlanOutcome
    hm: HybridMatrix
    metas: list[StopMeta]
    parsed: list[ParsedAddress]
    fast_path: int
    geocode_hits: int
    glitches_repaired: int


async def run_once(b: Bench, geocoder: GeocoderUK, glitched: list[int]) -> RunResult:
    t0 = time.perf_counter()
    parsed = [parse_uk_address(label) for label in b.labels]
    t_parse = time.perf_counter()

    slow = [i for i, a in enumerate(parsed) if not a.is_fast_path_valid]
    for i in slow:  # chemin lent : ROI WebP -> Gemini (simulé)
        b.gemini.queue.append(truth_extraction(b.parcels[i]))
        parsed[i] = (await b.vision.extract(WEBP_STUB)).parsed
    t_slow = time.perf_counter()

    batch = await geocoder.geocode_many(parsed)
    t_geo = time.perf_counter()
    missing = [b.parcels[i].pid for i, r in enumerate(batch.results) if r is None]
    if missing:
        raise RuntimeError(f"colis non géocodés: {missing}")

    metas = [StopMeta(p.pid, a.display(), r.point.lat, r.point.lon) for p, a, r in zip(b.parcels, parsed, batch.results)]  # type: ignore[union-attr]
    lat = np.array([b.depot[0], *(m.lat for m in metas)])
    lon = np.array([b.depot[1], *(m.lon for m in metas)])
    hm = await b.builder.build(lat, lon)
    t_matrix = time.perf_counter()
    outcome = await b.engine.plan(hm, metas)
    t_end = time.perf_counter()

    timings = {
        "parse": (t_parse - t0) * 1000,
        "vision_stub": (t_slow - t_parse) * 1000,
        "geocode": (t_geo - t_slow) * 1000,
        "matrix_numpy": hm.numpy_ms,
        "osrm": hm.osrm_ms,
        "matrix_wall": (t_matrix - t_geo) * 1000,
        "solver_wall": outcome.timings_ms["solver_wall"],
        "solver_hybrid": outcome.timings_ms["solver_hybrid"],
        "solver_van": outcome.timings_ms["solver_van"],
        "pockets": outcome.timings_ms["pockets"],
        "clustering": outcome.timings_ms["clustering"],
        "total": (t_end - t_slow) * 1000,  # adresses déjà extraites -> plan
        "total_with_parse": (t_end - t0) * 1000,
    }
    return RunResult(timings, outcome, hm, metas, parsed, len(parsed) - len(slow), batch.cache_hits, len(glitched))


def validate(b: Bench, run: RunResult) -> list[tuple[str, bool, str]]:
    """Contrôles de cohérence terrain ; chaque ligne = (nom, ok, détail)."""
    checks: list[tuple[str, bool, str]] = []
    keys_ok = sum(a.cache_key == p.truth_key for a, p in zip(run.parsed, b.parcels))
    checks.append(("adresses reconnues = vérité terrain", keys_ok == len(b.parcels), f"{keys_ok}/{len(b.parcels)}"))
    wanted = sorted(p.pid for p in b.parcels)
    for plan in run.outcome.candidates:
        ids = sorted(d.stop_id for c in plan.clusters for d in c.deliveries)
        recomputed = _recompute(plan, run.hm, b.engine._cfg)  # noqa: SLF001
        ok = ids == wanted and abs(recomputed - plan.totals.total_s) < 0.5
        checks.append((f"plan {plan.strategy}: 50 colis livrés une fois, durée recalculée = annoncée", ok, f"{recomputed:.0f} s"))
    chosen, ref = run.outcome.plan.totals.total_s, run.outcome.van_plan.totals.total_s
    checks.append(("plan retenu <= van strict", chosen <= ref + 1e-6, f"{chosen:.0f} s <= {ref:.0f} s"))
    return checks


def _recompute(plan: RoutePlan, hm: HybridMatrix, cfg: PlanConfig) -> float:
    pos, total = 0, 0.0
    for c in plan.clusters:
        total += hm.m_van[pos, c.park_node]
        nodes = [d.node for d in c.deliveries]
        if len(nodes) > 1:
            total += sum(hm.t_walk[a, bb] for a, bb in zip(nodes, nodes[1:])) + hm.t_walk[nodes[-1], nodes[0]]
        total += len(nodes) * cfg.service_time_s
        pos = c.park_node
    return float(total + hm.m_van[pos, 0])


# =============================================================================================
# 4. Rapport
# =============================================================================================
def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def print_plan(b: Bench, run: RunResult) -> None:
    plan = run.outcome.plan
    trap_of = {p.pid: p.trap for p in b.parcels}
    print(f"\n━━ TOURNÉE GROUPÉE PAR PARKING CLUSTERS ({plan.strategy}) ━━")
    print(f"   Dépôt → {len(plan.clusters)} arrêts du van pour {plan.totals.parcels} colis\n")
    for c in plan.clusters:
        n = len(c.deliveries)
        if c.kind == "SINGLE":
            d = c.deliveries[0]
            print(f" #{c.index + 1:02d}  GARER  {d.label:<44} conduite {fmt_dur(c.drive_s):>7}   [{trap_of[d.stop_id]}]")
            continue
        print(f" #{c.index + 1:02d}  GARER  {c.park_label:<44} conduite {fmt_dur(c.drive_s):>7}")
        print(f"      └ {n} portes à pied, boucle {fmt_dur(c.walk_loop_s)} (retour au van inclus) :")
        for k, d in enumerate(c.deliveries):
            where = "devant le van" if k == 0 else f"+{fmt_dur(d.walk_from_previous_s)} à pied"
            print(f"          {k + 1}. {d.label:<44} {where:<16} [{trap_of[d.stop_id]}]")
        print(f"          ↩ reprendre le van                                    {fmt_dur(c.return_walk_s)}")
    print(f"      ⮐ retour au dépôt {fmt_dur(plan.drive_back_s)}")


def print_shortcuts(b: Bench, run: RunResult, top: int = 8) -> None:
    hm = run.hm
    gain = np.where(hm.walk_mask, hm.m_van - hm.t_walk, -np.inf)
    order = np.dstack(np.unravel_index(np.argsort(-gain, axis=None), gain.shape))[0]
    labels = ["DÉPÔT", *(m.label for m in run.metas)]
    print("\n━━ PIÈGES UK DÉTECTÉS : plus gros WALK_SHORTCUT de la matrice ━━")
    seen: set[frozenset[int]] = set()
    shown = 0
    for i, j in order:
        if not np.isfinite(gain[i, j]) or shown == top:
            break
        pair = frozenset((int(i), int(j)))
        if pair in seen:
            continue
        seen.add(pair)
        shown += 1
        print(f"   {labels[i]:<38} → {labels[j]:<38} {hm.d_haversine[i, j]:5.0f} m   à pied {fmt_dur(hm.t_walk[i, j]):>6}  vs  van {fmt_dur(hm.m_van[i, j]):>6}")
    total = int(hm.walk_mask.sum())
    print(f"   … {total} arcs WALK_SHORTCUT sur {hm.size * (hm.size - 1)} couples ({100 * total / (hm.size * (hm.size - 1)):.1f} %)")


def print_comparison(run: RunResult, parcels: int, service_s: float) -> None:
    o = run.outcome
    van, hyb = o.van_plan, o.plan
    print("\n━━ VAN STRICT vs MODE HYBRIDE OPPORTUNISTE ━━")
    print(f"   {'':<30}{'Van strict':>14}{'Hybride (' + hyb.strategy + ')':>34}")
    rows = [
        ("Conduite", van.totals.drive_s, hyb.totals.drive_s),
        ("Marche (retour au van inclus)", van.totals.walk_s, hyb.totals.walk_s),
        (f"Service ({parcels} × {service_s:.0f} s)", van.totals.service_s, hyb.totals.service_s),
    ]
    for name, a, c in rows:
        print(f"   {name:<30}{fmt_dur(a):>14}{fmt_dur(c):>34}")
    print(f"   {'DURÉE TOTALE':<30}{fmt_dur(van.totals.total_s):>14}{fmt_dur(hyb.totals.total_s):>34}")
    print(f"   {'Arrêts du van':<30}{van.totals.van_stops:>14}{hyb.totals.van_stops:>34}")
    print("\n   Les trois plans candidats, chiffrés avec retour à pied jusqu'au van :")
    for c in o.candidates:
        mark = "◀ retenu" if c is o.plan else ""
        print(f"     {c.strategy:<16}{fmt_dur(c.totals.total_s):>10}   conduite {fmt_dur(c.totals.drive_s):>7}  marche {fmt_dur(c.totals.walk_s):>6}  arrêts {c.totals.van_stops:>2}  {mark}")
    print(f"     (objectifs OR-Tools : M_van {o.objective_van}, M_hybrid {o.objective_hybrid} ; M_hybrid ignore le retour au van)")
    print(f"\n   ➜ Temps économisé vs van strict : {fmt_dur(o.saved_s)} ({o.saved_pct:.1f} % de la tournée, "
          f"{100 * o.saved_s / van.totals.drive_s:.1f} % de la conduite)")


STAGES = [
    ("Parseur UK (50 étiquettes)", "parse", "—"),
    ("Géocodage (cache chaud)", "geocode", "< 1 ms/adresse"),
    ("Matrice NumPy (Haversine+fusion)", "matrix_numpy", "< 1 ms"),
    ("Appel OSRM Table API", "osrm", "< 10 ms (100×100)"),
    ("Solveur OR-Tools (dans le worker)", "solver_hybrid", "150 ms"),
    ("Solveur OR-Tools (mur, ×2 en //)", "solver_wall", "150 ms + IPC"),
    ("Rattachement des poches (worker)", "pockets", "—"),
    ("Regroupement ParkingClusters", "clustering", "—"),
    ("TEMPS TOTAL (adresses → plan)", "total", "—"),
]


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q))


def print_timings(runs: list[RunResult], cold: RunResult, simulated: bool, redis_mode: str) -> None:
    warm = runs
    print(f"\n━━ TEMPS D'EXÉCUTION (ms) : {len(warm)} passes à cache chaud + 1 passe à froid ━━")
    print(f"   {'étape':<38}{'médiane':>9}{'p95':>9}{'max':>9}   objectif cahier des charges")
    for label, key, target in STAGES:
        vals = [r.timings[key] for r in warm]
        note = ""
        if key == "osrm" and simulated:
            note = "  (OSRM SIMULÉ : non représentatif)"
        print(f"   {label:<38}{statistics.median(vals):>9.2f}{pct(vals, 95):>9.2f}{max(vals):>9.2f}   {target}{note}")
    print(f"\n   Passe à froid : géocodage {cold.timings['geocode']:.2f} ms (fournisseur UPRN simulé + écriture {redis_mode}), "
          f"chemin lent Gemini simulé {cold.timings['vision_stub']:.2f} ms, total {cold.timings['total']:.1f} ms")
    wall = [r.timings["total_with_parse"] for r in warm]
    print(f"   Pipeline complet avec parsing : médiane {statistics.median(wall):.1f} ms, p95 {pct(wall, 95):.1f} ms")


def numpy_scaling() -> None:
    """Micro-benchmark de la partie NumPy seule pour 51, 101 et 201 nœuds (100×100 du cahier des charges)."""
    print("\n━━ MATRICE NUMPY SEULE : passage à l'échelle (Haversine + T_walk + fusion) ━━")
    rng = np.random.default_rng(0)
    for n in (51, 101, 201):
        lat, lon = 53.8 + rng.random(n) * 0.01, -1.55 + rng.random(n) * 0.012
        m_van = rng.random((n, n)) * 600
        times = []
        for _ in range(200):
            t = time.perf_counter()
            d = haversine_matrix(lat, lon)
            tw = walk_time_matrix(d, 1.65, 10.0)
            fuse_matrices(m_van, d, tw, 160.0)
            times.append((time.perf_counter() - t) * 1000)
        print(f"   {n:>3}×{n:<3}  médiane {statistics.median(times):6.3f} ms   p95 {pct(times, 95):6.3f} ms   (objectif < 1 ms)")


# =============================================================================================
# 5. Orchestration
# =============================================================================================
async def main_async(args: argparse.Namespace) -> int:
    parcels = build_parcels(args.scenario)
    assert len(parcels) == 50, len(parcels)
    labels = [render_label(p, i, args.seed) for i, p in enumerate(parcels)]
    glitched = inject_ocr_glitches(labels, parcels)

    settings = Settings(
        redis_url=args.redis_url or "", solver_workers=args.workers, osrm_url=args.osrm_url or "http://osrm-sim",
        osrm_max_table_size=250, allow_synthetic_geocoding=False,
    )
    simulated = not args.osrm_url
    network = RoadNetwork(NODES, SEGMENTS)
    transport = httpx.ASGITransport(app=make_sim_osrm(network)) if simulated else None
    http = httpx.AsyncClient(transport=transport, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10), timeout=3.0)

    provider = StaticProvider()
    for p in parcels:
        provider.register(p.truth_key, *p.latlon)
    cache = await create_cache(settings) if args.redis_url else MemoryCache()
    gemini = TruthGemini()
    osrm = OSRMClient(http, settings.osrm_url, timeout_s=settings.osrm_timeout_s, max_table_size=settings.osrm_max_table_size)
    pool = SolverPool(args.workers)
    bench = Bench(
        parcels=parcels, labels=labels, settings=settings, provider=provider, gemini=gemini,
        vision=GeminiVisionExtractor("bench", client=gemini),  # type: ignore[arg-type]
        builder=HybridMatrixBuilder(osrm, settings),
        engine=RoutingEngine(
            pool,
            PlanConfig(settings.service_time_s, settings.max_cluster_size, settings.walk_max_distance_m, settings.pocket_van_close_s),
            settings.solver_time_limit_ms,
        ),
        depot=to_latlon(*DEPOT_XY),
    )

    print("=" * 100)
    print(" BENCHMARK UK LAST-MILE : 50 colis, pièges LTN / sens unique / impasses / House Names")
    print("=" * 100)
    print(f" Machine        : {os.cpu_count()} cœurs · Python {sys.version.split()[0]} · event loop {asyncio.get_running_loop().__class__.__module__.split('.')[0]}")
    print(f" Routage        : {f'OSRM SIMULÉ en processus (graphe de {len(NODES)} carrefours, sens uniques, bollards)' if simulated else 'OSRM réel ' + args.osrm_url}")
    print(f" Cache UPRN     : {cache.name} · fournisseur d'adresses simulé · Gemini simulé (aucun appel réseau)")
    print(f" Solveur        : OR-Tools PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH, {settings.solver_time_limit_ms} ms, pool de {args.workers} processus")
    print(f" Étiquettes     : {len(parcels)} · carriers {', '.join(CARRIERS)} · {len(DAMAGED)} déchirées (chemin lent) · {len(glitched)} avec confusion OCR du code postal")

    await pool.warmup()
    prefix = f"bench:{uuid.uuid4().hex[:8]}"

    def new_geocoder(tag: str) -> GeocoderUK:
        return GeocoderUK(cache=cache, providers=[provider], ttl_s=600, cache_prefix=f"{prefix}:{tag}")

    cold_geocoder = new_geocoder("run")
    cold = await run_once(bench, cold_geocoder, glitched)
    warm_runs = [await run_once(bench, cold_geocoder, glitched) for _ in range(args.runs)]
    last = warm_runs[-1]

    fast = last.fast_path
    print(f"\n Chemin rapide  : {fast}/{len(parcels)} étiquettes ({100 * fast / len(parcels):.0f} %) résolues localement, "
          f"{len(parcels) - fast} via ROI WebP → Gemini (simulé) ; payload WebSocket type ≈ "
          f"{len(orjson.dumps({'i': 17, 'p': 'LS61AB', 'b': '14A'}))} octets")

    if not args.no_plan:
        print_plan(bench, last)
    print_shortcuts(bench, last)
    print_comparison(last, len(parcels), settings.service_time_s)
    print_timings(warm_runs, cold, simulated, cache.name)
    numpy_scaling()

    print("\n━━ VALIDATION TERRAIN ━━")
    ok_all = True
    for name, ok, detail in validate(bench, last):
        ok_all &= ok
        print(f"   {'✓' if ok else '✗'} {name:<52} {detail}")

    savings = [r.outcome.saved_s for r in warm_runs]
    print(f"\n   Stabilité du gain sur {len(warm_runs)} passes (le solveur est limité par le temps, donc non déterministe) : "
          f"min {fmt_dur(min(savings))} · médiane {fmt_dur(statistics.median(savings))} · max {fmt_dur(max(savings))}")
    print("\n   LIMITES")
    if args.scenario == "traps":
        print("   · Scénario et adresses FICTIFS, construits pour contenir des pièges : 3 « poches » de 2 colis derrière un filtre LTN ou")
        print("     une impasse à longue route d'accès. Le gain dépend entièrement de cette densité ; `--scenario control` (50 colis sans")
        print("     piège) donne 0 s, ce qui montre que le moteur n'invente pas de gain.")
    else:
        print("   · Scénario témoin : 50 colis sur des rues ouvertes. Aucun piège, donc aucun gain attendu (le plan hybride doit égaler le van strict).")
    print("   · Marche à vol d'oiseau : suppose un passage piéton entre les portes (pas de rivière, voie ferrée ou clôture modélisées).")
    print("   · OSRM / Gemini / fournisseur UPRN simulés sauf indication contraire en tête de rapport ; latences réseau non mesurées.")

    pool.shutdown()
    await cold_geocoder.close()
    await http.aclose()
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=12, help="passes chronométrées à cache chaud (défaut 12)")
    ap.add_argument("--osrm-url", default="", help="URL d'un vrai OSRM (sinon simulation en processus)")
    ap.add_argument("--redis-url", default="", help="Redis réel pour le cache UPRN (sinon mémoire)")
    ap.add_argument("--workers", type=int, default=2, help="processus du pool OR-Tools (0 = thread, bloque la boucle)")
    ap.add_argument("--seed", type=int, default=7, help="graine des numéros de suivi / téléphones des étiquettes")
    ap.add_argument("--scenario", choices=("traps", "control"), default="traps",
                    help="traps : pièges LTN/sens unique/impasses (défaut) ; control : 50 colis sans piège (gain attendu ~0)")
    ap.add_argument("--no-plan", action="store_true", help="n'imprime pas le détail de la tournée")
    args = ap.parse_args()
    try:
        import uvloop

        return uvloop.run(main_async(args))
    except ImportError:
        return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
