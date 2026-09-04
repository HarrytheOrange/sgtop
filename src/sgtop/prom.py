"""Shared Prometheus text-exposition parsing + windowed histogram tracking
for sglang's /metrics endpoint. Used by both sgtop-server (which scrapes it
server-side alongside log-tailing) and sgtop's direct client (which scrapes
it itself when talking straight to sglang, no sidecar involved).

Prometheus histograms/counters are cumulative since process start, so
PromTracker diffs consecutive scrapes to get "in this window" counts —
the same idea as Prometheus's own rate()/increase() — instead of reporting
a number that just drifts toward whatever the first few requests looked
like.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

PROM_LINE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?P<labels>[^}]*)\})?\s+'
    r'(?P<value>[-+0-9.eE]+|NaN|\+Inf|-Inf)\s*$'
)
PROM_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

HIST_METRICS = {
    "sglang:time_to_first_token_seconds": "ttft",
    "sglang:inter_token_latency_seconds": "itl",
    "sglang:e2e_request_latency_seconds": "e2e",
    "sglang:queue_time_seconds": "queue",
}
# name in /metrics -> short key. Covers both sgtop-server's LATENCY panel
# (cache_hit_rate, num_retracted_reqs) and sgtop's direct-client mode, which
# needs the rest to reconstruct concurrency/KV/throughput without any log
# file at all.
GAUGE_METRICS = {
    "sglang:cache_hit_rate": "cache_hit_rate",
    "sglang:num_retracted_reqs": "num_retracted_reqs",
    "sglang:num_running_reqs": "num_running_reqs",
    "sglang:num_queue_reqs": "num_queue_reqs",
    "sglang:kv_used_tokens": "kv_used_tokens",
    "sglang:kv_available_tokens": "kv_available_tokens",
    "sglang:max_total_num_tokens": "max_total_num_tokens",
    "sglang:gen_throughput": "gen_throughput",
    "sglang:spec_accept_rate": "spec_accept_rate",
    "sglang:spec_accept_length": "spec_accept_length",
    "sglang:context_len": "context_len",
}


def parse_prometheus_text(text: str):
    """Yield (metric_name, labels_dict, value) for each exposition line."""
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        labels = {}
        raw_labels = match.group("labels")
        if raw_labels:
            for lm in PROM_LABEL_RE.finditer(raw_labels):
                labels[lm.group(1)] = lm.group(2)
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        yield match.group("name"), labels, value


def quantile_from_cumulative(buckets: dict, total: float, q: float) -> float | None:
    """Approximate a quantile from Prometheus-style cumulative "le" buckets,
    the same linear-interpolation approach histogram_quantile() uses."""
    if total <= 0:
        return None

    def bound(le: str) -> float:
        return float("inf") if le == "+Inf" else float(le)

    items = sorted(((bound(le), count) for le, count in buckets.items()), key=lambda t: t[0])
    target = q * total
    prev_bound, prev_count = 0.0, 0.0
    for b, count in items:
        if count >= target:
            if b == float("inf"):
                return prev_bound
            if count == prev_count:
                return b
            frac = (target - prev_count) / (count - prev_count)
            return prev_bound + frac * (b - prev_bound)
        prev_bound, prev_count = b, count
    return prev_bound


class PromTracker:
    """Scrapes/diffs sglang's Prometheus histograms into short rolling
    windows so the dashboard can show recent p50/p90/p99, not just an
    all-time average."""

    WINDOW_S = 300
    KEEP_SAMPLES = 300  # ~ WINDOW_S at a >=1s poll interval

    def __init__(self) -> None:
        self._prev: dict = {}  # (hist_key, group_key) -> {"buckets": {le: n}, "sum": s, "count": c}
        self._deltas: dict = defaultdict(lambda: deque(maxlen=self.KEEP_SAMPLES))
        self.latest_gauge: dict = {}  # (gauge_key, group_key) -> value
        self.seen_any = False

    @staticmethod
    def _group_key(labels: dict, drop: tuple = ()) -> tuple:
        return tuple(sorted((k, v) for k, v in labels.items() if k not in drop and k != "le"))

    def ingest(self, text: str, now: float) -> None:
        buckets_by_series: dict = defaultdict(dict)
        sums: dict = {}
        counts: dict = {}
        for name, labels, value in parse_prometheus_text(text):
            for full_name, hist_key in HIST_METRICS.items():
                if name == full_name + "_bucket":
                    self.seen_any = True
                    key = (hist_key, self._group_key(labels))
                    buckets_by_series[key][labels.get("le", "+Inf")] = value
                elif name == full_name + "_sum":
                    sums[(hist_key, self._group_key(labels))] = value
                elif name == full_name + "_count":
                    counts[(hist_key, self._group_key(labels))] = value
            for full_name, gauge_key in GAUGE_METRICS.items():
                if name == full_name:
                    self.seen_any = True
                    self.latest_gauge[(gauge_key, self._group_key(labels))] = value

        for key, buckets in buckets_by_series.items():
            s = sums.get(key, 0.0)
            c = counts.get(key, 0.0)
            prev = self._prev.get(key)
            self._prev[key] = {"buckets": buckets, "sum": s, "count": c}
            if prev is None:
                continue
            # A cumulative value going backwards means the process restarted;
            # treat the new cumulative snapshot itself as the delta.
            reset = c < prev["count"]
            delta_buckets = {
                le: (cum if reset else max(0.0, cum - prev["buckets"].get(le, 0.0)))
                for le, cum in buckets.items()
            }
            delta_sum = s if reset else max(0.0, s - prev["sum"])
            delta_count = c if reset else max(0.0, c - prev["count"])
            if delta_count > 0:
                self._deltas[key].append(
                    {"ts": now, "buckets": delta_buckets, "sum": delta_sum, "count": delta_count}
                )

    def summary(self, now: float, window_s: float | None = None) -> dict:
        window_s = window_s or self.WINDOW_S
        out: dict = {}
        for key, dq in self._deltas.items():
            hist_key, group_key = key
            agg_buckets: dict = defaultdict(float)
            agg_sum = agg_count = 0.0
            for item in dq:
                if item["ts"] < now - window_s:
                    continue
                for le, v in item["buckets"].items():
                    agg_buckets[le] += v
                agg_sum += item["sum"]
                agg_count += item["count"]
            if agg_count <= 0:
                continue
            out[key] = {
                "group": dict(group_key),
                "mean": agg_sum / agg_count,
                "p50": quantile_from_cumulative(agg_buckets, agg_count, 0.5),
                "p90": quantile_from_cumulative(agg_buckets, agg_count, 0.9),
                "p99": quantile_from_cumulative(agg_buckets, agg_count, 0.99),
                "count": agg_count,
            }
        return out
