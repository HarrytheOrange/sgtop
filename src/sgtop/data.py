"""Networking + time-series helpers. No filesystem access: everything comes
from a running monitor_dashboard.py's /api/status endpoint, so sgtop can
point at any host on the LAN that's serving one."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30001


def fetch_status(host: str, port: int, timeout: float = 2.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
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
