#!/usr/bin/env python3
"""Exporte `web/data.json` : les données de la démo web, produites par le VRAI pipeline.

Rien n'est écrit à la main : colis, étiquettes OCR, plans (VAN_STRICT, HYBRID_POCKETS, HYBRID_SPEC) et temps
d'exécution viennent de `benchmark_uk_speed.py` (parseur, géocodeur, matrice NumPy, OR-Tools, ParkingClusters).

    python scripts/export_demo.py            # écrit web/data.json
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import platform
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

import benchmark_uk_speed as bench  # noqa: E402
from uk_address_parser import parse_uk_address  # noqa: E402

STAGES = [
    ("parse", "Parseur UK (50 étiquettes)"),
    ("geocode", "Géocodage, cache chaud (50 adresses)"),
    ("matrix_numpy", "Matrice NumPy (Haversine + fusion)"),
    ("osrm", "Appel OSRM Table API (simulé)"),
    ("solver_hybrid", "Solveur OR-Tools (dans le worker)"),
    ("solver_wall", "Solveur OR-Tools (mur, 2 résolutions en parallèle)"),
    ("pockets", "Rattachement des poches"),
    ("clustering", "Regroupement en ParkingClusters"),
    ("total", "Total adresses → plan"),
]


async def build_payload(runs: int = 15) -> dict:
    world = await bench.build_world("traps", seed=7, workers=2)
    b = world.bench
    await world.pool.warmup()
    geocoder = bench.GeocoderUK(cache=world.cache, providers=[b.provider], ttl_s=600, cache_prefix="export")
    await bench.run_once(b, geocoder, world.glitched)  # passe à froid
    results = [await bench.run_once(b, geocoder, world.glitched) for _ in range(runs)]
    last = results[-1]
    outcome = last.outcome

    parcels = []
    for i, (p, label, parsed) in enumerate(zip(b.parcels, b.labels, last.parsed)):
        lat, lon = p.latlon
        parcels.append(
            {
                "pid": p.pid,
                "trap": p.trap,
                "street": p.street,
                "x": p.x,
                "y": p.y,
                "label": parsed.display(),
                "text": label,
                "key": p.truth_key,
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "damaged": i in bench.DAMAGED,
                "ocr_glitch": i in world.glitched,
                # ce que le parseur local produit sur le texte brut (None = chemin lent Gemini)
                "fast_path_key": parse_uk_address(label).cache_key,
            }
        )

    timings = {}
    for key, title in STAGES:
        values = [r.timings[key] for r in results]
        timings[key] = {
            "title": title,
            "median": round(statistics.median(values), 3),
            "p95": round(float(np.percentile(values, 95)), 3),
        }

    nodes = {name: list(xy) for name, xy in bench.NODES.items()}
    gap = [nodes["F1"], nodes["F2"]]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "machine": {"cores": os.cpu_count(), "python": platform.python_version()},
        "disclaimer": (
            "Scénario et adresses fictifs. OSRM est un moteur simulé en mémoire (graphe de "
            f"{len(nodes)} carrefours) : les temps de route ne sont pas ceux d'OSRM sur la carte de Grande-Bretagne."
        ),
        "depot": list(bench.DEPOT_XY),
        "world": {
            "nodes": nodes,
            "segments": [
                {"name": s.name, "u": s.u, "v": s.v, "mph": s.mph, "oneway": s.oneway} for s in bench.SEGMENTS
            ],
            "filters": [
                {
                    "x": (gap[0][0] + gap[1][0]) / 2,
                    "y": (gap[0][1] + gap[1][1]) / 2,
                    "label": "Filtre LTN (bollards) : fermé aux véhicules, ouvert aux piétons",
                }
            ],
        },
        "parcels": parcels,
        "geocode": {p["key"]: [p["lat"], p["lon"]] for p in parcels},
        "chosen": outcome.plan.strategy,
        "plans": {plan.strategy: plan.to_dict() for plan in outcome.candidates},
        "comparison": outcome.to_dict()["comparison"],
        "service_time_s": world.settings.service_time_s,
        "timings_ms": timings,
        "gain_stability_s": sorted({round(r.outcome.saved_s, 1) for r in results}),
        "runs": runs,
    }


def main() -> int:
    payload = asyncio.run(build_payload())
    out = ROOT / "web" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    cmp = payload["comparison"]
    print(f"{out.relative_to(ROOT)} : {out.stat().st_size / 1024:.0f} Ko · plan retenu {payload['chosen']} · "
          f"gain {cmp['saved_s']} s ({cmp['saved_pct']} %) · candidats {cmp['candidates_s']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
