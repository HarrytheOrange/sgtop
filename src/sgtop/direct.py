"""Direct client: talks straight to a running sglang instance, no sgtop-server
sidecar needed. Requires --enable-metrics on the sglang side for anything
beyond a bare up/down check — everything sgtop shows (concurrency, KV-cache,
decode throughput, TTFT/E2E/queue-time, cache-hit, retraction) turns out to
already be exposed on sglang's own /metrics and /get_server_info, verified
live against sglang 0.5.18:

  - /get_server_info -> internal_states[*].effective_max_running_requests_per_dp
    is the per-DP concurrency cap (all DP ranks share one config, so this is
    the same value everywhere; there's no dp_rank field on the state itself).
  - /metrics gauges sglang:num_running_reqs, num_queue_reqs, kv_used_tokens,
    gen_throughput, spec_accept_rate/length, max_total_num_tokens, context_len
    all carry a dp_rank label and (like queue_time_seconds) get reported
    identically by both TP ranks in a DP group, so we only trust tp_rank="0".

The GPU panel is filled in from a *local* nvidia-smi query — i.e. whatever
GPUs are on the machine running `sgtop` itself, not the (possibly remote)
--host being watched. That's the right behavior either way: run sgtop right
on the sglang box and you get real GPU stats for free; run it from a laptop
watching a remote sglang instance and the panel is correctly empty, since
nvidia-smi has nothing to report there. The one thing this mode still can't
give you is the error-log line — that needs sglang's own log file, which
isn't exposed over the API at all; sgtop-server covers that if you want it.
"""
from __future__ import annotations

import json
import time
import urllib.request
from collections import deque
from typing import Optional

from .localgpu import query_local_gpus
from .prom import PromTracker


class DirectClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.prom = PromTracker()
        self.gpus: list[dict] = []
        self.caps: dict[str, dict] = {}
        self._running_cap: Optional[int] = None
        self.decode_history: deque = deque(maxlen=1200)
        self.request_ts: deque = deque(maxlen=4000)
        self.start_ts: Optional[float] = None
        self.service_up = False

    def _get_json(self, path: str, timeout: float = 3.0) -> Optional[dict]:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}{path}", timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def _get_text(self, path: str, timeout: float = 3.0) -> Optional[str]:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.port}{path}", timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception:
            return None

    def _ensure_running_cap(self) -> None:
        if self._running_cap is not None:
            return
        info = self._get_json("/get_server_info")
        if not info:
            return
        for state in info.get("internal_states") or []:
            cap = state.get("effective_max_running_requests_per_dp")
            if cap:
                self._running_cap = int(cap)
                return

    def poll(self) -> None:
        now = time.time()
        metrics_text = self._get_text("/metrics")
        self.service_up = metrics_text is not None
        if not self.service_up:
            return
        if self.start_ts is None:
            self.start_ts = now
        self.prom.ingest(metrics_text, now)
        self._ensure_running_cap()
        self.gpus = query_local_gpus()

        by_dp: dict[str, dict] = {}
        for (gauge_key, group_key), value in self.prom.latest_gauge.items():
            group = dict(group_key)
            if group.get("tp_rank") not in (None, "0"):
                continue
            dp_rank = group.get("dp_rank")
            if dp_rank is None:
                continue
            by_dp.setdefault(dp_rank, {})[gauge_key] = value

        prev_running = {dp: e.get("running", 0) for dp, e in self._last_active().items()}
        for dp_rank, vals in by_dp.items():
            dp = f"DP{dp_rank}"
            max_tokens = vals.get("max_total_num_tokens")
            if max_tokens:
                self.caps[dp] = {
                    "max_running": self._running_cap or 0,
                    "max_tokens": int(max_tokens),
                    "ctx": int(vals.get("context_len", 0)),
                }
            running = int(vals.get("num_running_reqs", 0))
            entry = {
                "ts": now, "dp": dp,
                "running": running,
                "queue": int(vals.get("num_queue_reqs", 0)),
                "tokens": int(vals.get("kv_used_tokens", 0)),
                "throughput": vals.get("gen_throughput", 0.0),
                "accept_len": vals.get("spec_accept_length", 0.0),
                "accept_rate": vals.get("spec_accept_rate", 0.0),
            }
            self.decode_history.append(entry)
            # crude request-completion proxy for req/min|5min: count each drop
            # in num_running_reqs as roughly one request finishing. Not exact
            # (a batch can finish >1 at once) but good enough for a trend.
            if running < prev_running.get(dp, running):
                for _ in range(prev_running.get(dp, running) - running):
                    self.request_ts.append(now)

        cutoff = now - 900
        while self.decode_history and self.decode_history[0]["ts"] < cutoff:
            self.decode_history.popleft()
        while self.request_ts and self.request_ts[0] < cutoff:
            self.request_ts.popleft()

    def _last_active(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for item in self.decode_history:
            latest[item["dp"]] = item
        return latest

    @staticmethod
    def _reshape_prom(raw: dict, latest_gauge: dict) -> dict:
        latency: dict[str, dict] = {}
        queue_by_dp: dict[str, dict] = {}
        for (hist_key, _group_key), stats in raw.items():
            group = stats["group"]
            if group.get("tp_rank") not in (None, "0"):
                continue
            dp_rank = group.get("dp_rank")
            fields = {k: stats[k] for k in ("mean", "p50", "p90", "p99", "count")}
            if hist_key == "queue" and dp_rank is not None:
                queue_by_dp[str(dp_rank)] = fields
            elif hist_key != "queue":
                best = latency.get(hist_key)
                if best is None or stats["count"] > best["count"]:
                    latency[hist_key] = fields
        cache_hit_by_dp: dict[str, float] = {}
        retracted_by_dp: dict[str, float] = {}
        for (gauge_key, group_key), value in latest_gauge.items():
            if gauge_key not in ("cache_hit_rate", "num_retracted_reqs"):
                continue
            group = dict(group_key)
            if group.get("tp_rank") not in (None, "0"):
                continue
            dp_rank = group.get("dp_rank")
            if dp_rank is None:
                continue
            (cache_hit_by_dp if gauge_key == "cache_hit_rate" else retracted_by_dp)[str(dp_rank)] = value
        return {"latency": latency, "queue_by_dp": queue_by_dp, "cache_hit_by_dp": cache_hit_by_dp,
                "retracted_by_dp": retracted_by_dp}

    def snapshot(self) -> dict:
        now = time.time()
        active = self._last_active()
        active = {dp: v for dp, v in active.items() if now - v["ts"] <= 5}
        recent = [item for item in self.decode_history if now - item["ts"] <= 60]
        accept_rates = [item["accept_rate"] for item in recent if item.get("accept_rate")]

        prom = self._reshape_prom(self.prom.summary(now), self.prom.latest_gauge)
        prom["enabled"] = self.prom.seen_any

        summary = {
            "decode_now": sum(v.get("throughput", 0) for v in active.values()),
            "running_now": sum(v.get("running", 0) for v in active.values()),
            "queue_now": sum(v.get("queue", 0) for v in active.values()),
            "accept_rate_60s": sum(accept_rates) / len(accept_rates) if accept_rates else 0,
            "requests_1m": sum(ts >= now - 60 for ts in self.request_ts),
            "requests_5m": sum(ts >= now - 300 for ts in self.request_ts),
        }
        uptime = (now - self.start_ts) if self.start_ts else None
        return {
            "now": now, "service_up": self.service_up, "pid": None, "uptime": uptime,
            "service_port": self.port, "summary": summary, "active": active,
            "gpus": self.gpus, "caps": dict(self.caps), "prom": prom, "errors": [],
            "history": {"decode": list(self.decode_history), "prefill": [], "gpu": []},
        }
