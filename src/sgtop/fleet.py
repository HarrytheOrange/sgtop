"""Fleet mode: watch several sglang deployments on different LAN machines
from one terminal.

What each remote box needs to run, and why:
  - sglang itself, started with --enable-metrics, reachable on the LAN
    (0.0.0.0, not 127.0.0.1). That alone is enough for concurrency, KV-cache,
    decode throughput, TTFT/E2E/queue-time and cache-hit — DirectClient talks
    straight to it, same as single-host `sgtop` does.
  - To also get that box's GPU stats and its recent-errors line, it needs
    `sgtop-server --log <path to sglang's log> --port 30001` (or any port)
    running too, since nvidia-smi and the log file are only readable on the
    machine they're on — nothing remote can query them directly. Point fleet
    mode at that port and it uses ProxyClient instead, which carries GPU +
    errors alongside everything DirectClient already has.

Fleet mode probes each host once at startup to pick whichever of the two it
finds (preferring sgtop-server, since it's a strict superset), the same way
single-host `sgtop` does with --mode auto.
"""
from __future__ import annotations

import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from .data import ProxyClient, probe_mode
from .direct import DirectClient

HISTORY_S = 600


@dataclass
class FleetMember:
    name: str
    host: str
    port: int
    mode: str
    client: object
    history: deque = field(default_factory=lambda: deque(maxlen=1200))
    last_seen: Optional[float] = None

    def poll(self) -> None:
        try:
            self.client.poll()
        except Exception:
            pass
        snap = self.client.snapshot()
        now = time.time()
        up = bool(snap and snap.get("service_up"))

        running = cap = queue = 0
        decode = accept_rate = 0.0
        gpus: list[dict] = []
        errors = 0
        cache_hits: list[float] = []
        if up:
            self.last_seen = now
            summary = snap.get("summary", {})
            caps = snap.get("caps", {})
            running = summary.get("running_now", 0)
            queue = summary.get("queue_now", 0)
            decode = summary.get("decode_now", 0.0)
            accept_rate = summary.get("accept_rate_60s", 0.0)
            cap = sum(c.get("max_running", 0) for c in caps.values())
            gpus = snap.get("gpus", [])
            errors = len(snap.get("errors", []))
            cache_hits = list((snap.get("prom", {}).get("cache_hit_by_dp", {}) or {}).values())

        self.history.append({
            "ts": now, "up": up, "running": running, "cap": cap, "queue": queue,
            "decode": decode, "accept_rate": accept_rate,
        })
        cutoff = now - HISTORY_S
        while self.history and self.history[0]["ts"] < cutoff:
            self.history.popleft()

        self.up = up
        self.running, self.cap, self.queue = running, cap, queue
        self.decode, self.accept_rate = decode, accept_rate
        self.gpus, self.errors = gpus, errors
        self.cache_hit = sum(cache_hits) / len(cache_hits) if cache_hits else None


def _build_member(name: str, host: str, port: int, probe_timeout: float) -> FleetMember:
    mode = probe_mode(host, port, timeout=probe_timeout) or "direct"
    client = ProxyClient(host, port) if mode == "proxy" else DirectClient(host, port)
    return FleetMember(name=name, host=host, port=port, mode=mode, client=client)


class FleetClient:
    """Same poll()/snapshot() shape as ProxyClient/DirectClient — CLI code
    doesn't need to care whether it's holding one host or a fleet."""

    def __init__(self, specs: list[tuple[str, str, int]], probe_timeout: float = 2.0) -> None:
        with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
            self.members: list[FleetMember] = list(pool.map(
                lambda s: _build_member(s[0], s[1], s[2], probe_timeout), specs))
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(self.members)))

    def poll(self) -> None:
        list(self._pool.map(lambda m: m.poll(), self.members))

    def snapshot(self) -> list[FleetMember]:
        return self.members


def parse_fleet_spec(spec: str) -> list[tuple[str, str, int]]:
    """`spec` is either 'name=host:port,name=host:port,...' inline, or a path
    to a text file with one 'name=host:port' per line (# comments, blank
    lines ok). Returns [(name, host, port), ...]."""
    if os.path.isfile(spec):
        with open(spec) as f:
            lines = [ln.split("#", 1)[0].strip() for ln in f]
        entries = [ln for ln in lines if ln]
    else:
        entries = [e.strip() for e in spec.split(",") if e.strip()]

    out: list[tuple[str, str, int]] = []
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"bad fleet entry {entry!r}, expected name=host:port")
        name, addr = entry.split("=", 1)
        if ":" not in addr:
            raise ValueError(f"bad fleet entry {entry!r}, expected name=host:port")
        host, port_s = addr.rsplit(":", 1)
        out.append((name.strip(), host.strip(), int(port_s.strip())))
    return out
