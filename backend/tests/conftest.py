import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import orjson
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from hybrid_matrix import haversine_matrix  # noqa: E402


def fake_osrm_transport(speed_mps: float = 8.9, circuity: float = 1.4, one_way_penalty=None) -> httpx.MockTransport:
    """OSRM minimal : durée = distance vol d'oiseau * circuité / vitesse (asymétrique si demandé)."""

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/table/v1/driving/([^?]+)", request.url.path)
        if m is None:
            if "/nearest/" in request.url.path:
                return httpx.Response(200, json={"code": "Ok"})
            return httpx.Response(404, json={"code": "InvalidUrl"})
        pairs = [tuple(map(float, c.split(","))) for c in m.group(1).split(";")]
        lon = np.array([p[0] for p in pairs])
        lat = np.array([p[1] for p in pairs])
        d = haversine_matrix(lat, lon) * circuity / speed_mps
        if one_way_penalty is not None:
            d = one_way_penalty(d)
        return httpx.Response(200, content=orjson.dumps({"code": "Ok", "durations": d.tolist()}))

    return httpx.MockTransport(handler)


@pytest.fixture
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def redis_url(free_port):
    """Vrai redis-server éphémère (test d'intégration du cache)."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server absent")
    proc = subprocess.Popen(
        [binary, "--port", str(free_port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", free_port)) == 0:
                break
        time.sleep(0.05)
    yield f"redis://127.0.0.1:{free_port}/0"
    proc.terminate()
    proc.wait(timeout=5)
