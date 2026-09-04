"""Networking + time-series helpers. No filesystem access: everything comes
from a running sgtop-server's /api/status endpoint, so sgtop can point at
any host on the LAN that's serving one."""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30001

# Tried, in order, when the caller didn't pin a --port: sgtop-server's own
# default (30001) first, then sglang's own port (30000) — people sometimes
# run sgtop-server bound to that instead, or a deployment script overrides
# the default — then a couple of other common picks.
CANDIDATE_PORTS = [30001, 30000, 30002, 30003, 8080]


def fetch_status(host: str, port: int, timeout: float = 2.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    return data if isinstance(data, dict) and "service_up" in data else None


def find_dashboard_port(host: str, ports: list[int] = CANDIDATE_PORTS, timeout: float = 1.0) -> Optional[int]:
    """Probe candidate ports in parallel for a live sgtop-server (something
    that answers /api/status with the shape we expect, not just anything
    listening) and return the first hit in `ports` order, or None."""
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        futures = {pool.submit(fetch_status, host, p, timeout): p for p in ports}
        found: dict[int, bool] = {}
        for fut, port in futures.items():
            found[port] = fut.result() is not None
    for p in ports:
        if found.get(p):
            return p
    return None


def bucket_series(
    items: list[dict],
    ts_key: str,
    value_fn: Callable[[dict], Optional[float]],
    now: float,
    window_s: float,
    width: int,
    filter_fn: Optional[Callable[[dict], bool]] = None,
) -> list[float]:
    """Bucket `items` into `width` evenly-spaced buckets covering the last
    `window_s` seconds, averaging value_fn(item) within each bucket. Empty
    buckets come back as 0.0, so the sparkline reads as "idle", not "no data".
    """
    if width <= 0:
        return []
    buckets: list[list[float]] = [[] for _ in range(width)]
    start = now - window_s
    step = window_s / width
    for item in items:
        if filter_fn is not None and not filter_fn(item):
            continue
        ts = item.get(ts_key)
        if ts is None or ts < start:
            continue
        idx = min(width - 1, max(0, int((ts - start) / step)))
        v = value_fn(item)
        if v is not None:
            buckets[idx].append(v)
    return [sum(b) / len(b) if b else 0.0 for b in buckets]
