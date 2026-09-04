"""Networking + time-series helpers.

sgtop can talk to two different things on {host}:{port}:
  - "direct" mode: a plain sglang instance, straight off its own HTTP API
    (/get_server_info, /metrics — no log file, no sidecar, nothing extra
    needs to be running). This is the default now.
  - "proxy" mode: sgtop-server, the optional sidecar that also gives you a
    GPU panel and the recent-errors line, neither of which sglang's own API
    exposes.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30000

# Tried, in order, when the caller didn't pin a --port: sglang's own default
# port first (direct mode needs nothing else running), then sgtop-server's
# default, then a couple of other common picks.
CANDIDATE_PORTS = [30000, 30001, 30002, 30003, 8080]


def fetch_status(host: str, port: int, timeout: float = 2.0) -> Optional[dict]:
    """sgtop-server's /api/status, already in the shape the UI wants."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=timeout) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    return data if isinstance(data, dict) and "service_up" in data else None


def probe_mode(host: str, port: int, timeout: float = 1.0) -> Optional[str]:
    """Return "proxy" if sgtop-server answers here, "direct" if a plain
    sglang instance does, else None."""
    if fetch_status(host, port, timeout) is not None:
        return "proxy"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/get_server_info", timeout=timeout) as r:
            data = json.loads(r.read())
        if isinstance(data, dict) and "max_running_requests" in data:
            return "direct"
    except Exception:
        pass
    return None


def find_dashboard(host: str, ports: list[int] = CANDIDATE_PORTS,
                    timeout: float = 1.0) -> tuple[Optional[int], Optional[str]]:
    """Probe candidate ports in parallel; return (port, mode) for the first
    hit in `ports` order, or (None, None)."""
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        futures = {pool.submit(probe_mode, host, p, timeout): p for p in ports}
        found: dict[int, Optional[str]] = {}
        for fut, port in futures.items():
            found[port] = fut.result()
    for p in ports:
        if found.get(p):
            return p, found[p]
    return None, None


async def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def _scan_open_ports(host: str, ports: range, timeout: float, concurrency: int,
                            progress_cb: Optional[Callable[[int, int], None]] = None) -> list[int]:
    sem = asyncio.Semaphore(concurrency)
    open_ports: list[int] = []
    done = 0
    total = len(ports)

    async def check(p: int) -> None:
        nonlocal done
        async with sem:
            if await _tcp_open(host, p, timeout):
                open_ports.append(p)
        done += 1
        if progress_cb and done % 2000 == 0:
            progress_cb(done, total)

    await asyncio.gather(*(check(p) for p in ports))
    return sorted(open_ports)


def full_port_scan(
    host: str,
    port_range: range = range(1, 65536),
    tcp_timeout: float = 0.5,
    concurrency: int = 1000,
    verify_timeout: float = 2.0,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Last-resort fallback when none of CANDIDATE_PORTS answer: TCP-connect
    scan the whole port range for anything listening, then check each hit
    for either sgtop-server or a plain sglang instance — a listening port
    isn't necessarily either (could be ssh, some unrelated service)."""
    def _tcp_progress(done: int, total: int) -> None:
        if progress_cb:
            progress_cb(f"scanning ports... {done}/{total}")

    open_ports = asyncio.run(_scan_open_ports(host, port_range, tcp_timeout, concurrency, _tcp_progress))
    if progress_cb:
        progress_cb(f"{len(open_ports)} open TCP port(s) found, checking each one...")
    for p in open_ports:
        mode = probe_mode(host, p, timeout=verify_timeout)
        if mode:
            return p, mode
    return None, None


class ProxyClient:
    """Talks to sgtop-server's /api/status. Same poll()/snapshot() shape as
    direct.DirectClient so App doesn't care which one it's holding."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._last: Optional[dict] = None

    def poll(self) -> None:
        status = fetch_status(self.host, self.port)
        if status is not None:
            self._last = status

    def snapshot(self) -> Optional[dict]:
        return self._last


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
