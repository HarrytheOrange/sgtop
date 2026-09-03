from __future__ import annotations

import curses
import time
from datetime import datetime

from . import theme as th
from .data import bucket_series, fetch_status

SPARK_WIDTH = 40
WINDOW_S = 300  # matches the dashboard's own history retention


def human_uptime(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def fmt_secs(value: float | None) -> str:
    if value is None:
        return "  n/a"
    return f"{value*1000:4.0f}ms" if value < 1 else f"{value:5.2f}s"


class App:
    def __init__(self, stdscr, host: str, port: int, interval: float):
        self.stdscr = stdscr
        self.host = host
        self.port = port
        self.interval = interval
        self.theme = th.Theme()
        self.last_status: dict | None = None

    def run(self) -> None:
        curses.curs_set(0)
        self.theme.setup()
        self.stdscr.nodelay(True)
        self.stdscr.timeout(int(self.interval * 1000))
        while True:
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return
            status = fetch_status(self.host, self.port)
            if status is not None:
                self.last_status = status
            self.render(self.last_status)

    # -- rendering -----------------------------------------------------
    def render(self, status: dict | None) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        boxw = max(50, min(w - 2, 110))

        self._header(0, boxw, status)

        if status is None or not status.get("service_up"):
            th.safe_addstr(stdscr, 3, 2, "waiting for sglang-dashboard at "
                            f"{self.host}:{self.port} ...", curses.color_pair(th.PAIR_BAD))
            self._footer(h - 1, boxw)
            stdscr.refresh()
            return

        now = status["now"]
        caps = status.get("caps", {})
        active = status.get("active", {})
        hist_decode = status.get("history", {}).get("decode", [])
        hist_gpu = status.get("history", {}).get("gpu", [])

        y = 2
        total_running = total_cap = total_tok = total_tok_cap = 0
        for i, dp in enumerate(sorted(set(active) | set(caps))):
            pair = th.PAIR_TITLE_CYAN if i % 2 == 0 else th.PAIR_TITLE_MAGENTA
            y = self._dp_panel(y, boxw, dp, pair, active.get(dp, {}), caps.get(dp, {}), hist_decode, now)
            cap = caps.get(dp, {})
            a = active.get(dp, {})
            total_running += a.get("running", 0)
            total_cap += cap.get("max_running", 0)
            total_tok += a.get("tokens", 0)
            total_tok_cap += cap.get("max_tokens", 0)

        y = self._total_panel(y, boxw, total_running, total_cap, status["summary"])
        y = self._latency_panel(y, boxw, status.get("prom", {}))
        y = self._gpu_panel(y, boxw, status.get("gpus", []), hist_gpu, now)

        errors = status.get("errors", [])
        if errors:
            th.safe_addstr(stdscr, y, 0, f"⚠ {len(errors)} error(s) in the last 5min — see logs/deploy.log",
                            curses.color_pair(th.PAIR_BAD))
            y += 1

        self._footer(h - 1, boxw)
        stdscr.refresh()

    def _header(self, y: int, boxw: int, status: dict | None) -> None:
        stdscr = self.stdscr
        up = bool(status and status.get("service_up"))
        dot = "●" if up else "○"
        dot_pair = th.PAIR_TITLE_CYAN if up else th.PAIR_BAD
        state = f"online · pid {status.get('pid')}" if up and status else "offline"
        uptime = human_uptime(status.get("uptime")) if status else "—"
        clock = datetime.now().strftime("%H:%M:%S")

        th.safe_addstr(stdscr, y, 0, "sgtop", curses.A_BOLD | curses.color_pair(th.PAIR_TITLE_CYAN))
        th.safe_addstr(stdscr, y, 6, " SGLang concurrency monitor", curses.A_DIM)
        right = f"{dot} {state}   up {uptime}   {clock}"
        th.safe_addstr(stdscr, y, max(0, boxw - len(right)), dot, curses.color_pair(dot_pair))
        th.safe_addstr(stdscr, y, max(0, boxw - len(right)) + 2, right[2:])

    def _footer(self, y: int, boxw: int) -> None:
        th.safe_addstr(self.stdscr, y, 0, "q quit", curses.A_DIM)

    def _dp_panel(self, y: int, boxw: int, dp: str, title_pair: int, a: dict, cap: dict,
                  hist_decode: list, now: float) -> int:
        stdscr = self.stdscr
        h = 6
        th.draw_box(stdscr, self.theme, y, 0, h, boxw, dp, title_pair)
        inner_w = boxw - 4
        running, queue, tokens = a.get("running", 0), a.get("queue", 0), a.get("tokens", 0)
        max_running, max_tokens = cap.get("max_running", 0), cap.get("max_tokens", 0)

        if max_running:
            label = f"concurrency {running}/{max_running} ({running/max_running*100:4.1f}%) q={queue}"
            bar_w = th.fit_meter_width(inner_w, label)
            th.draw_meter(stdscr, self.theme, y + 1, 2, bar_w, running / max_running, label)
        else:
            th.safe_addstr(stdscr, y + 1, 2, "concurrency: cap unknown (waiting for startup log line)",
                            curses.color_pair(th.PAIR_DIM))

        if max_tokens:
            label = f"kv-cache {tokens}/{max_tokens} ({tokens/max_tokens*100:4.1f}%)"
            bar_w = th.fit_meter_width(inner_w, label)
            th.draw_meter(stdscr, self.theme, y + 2, 2, bar_w, tokens / max_tokens, label)

        thr_series = bucket_series(hist_decode, "ts", lambda it: it["throughput"], now, WINDOW_S,
                                    SPARK_WIDTH, filter_fn=lambda it: it.get("dp") == dp)
        th.draw_sparkline(stdscr, y + 3, 2, thr_series, title_pair)
        th.safe_addstr(stdscr, y + 3, 2 + SPARK_WIDTH + 2,
                        f"decode {a.get('throughput', 0):7.1f} tok/s (last 5min)")

        th.safe_addstr(stdscr, y + 4, 2,
                        f"accept_len={a.get('accept_len', 0):.2f}  accept_rate={a.get('accept_rate', 0)*100:5.1f}%")
        return y + h + 1

    def _total_panel(self, y: int, boxw: int, running: int, cap: int, summary: dict) -> int:
        stdscr = self.stdscr
        h = 4
        th.draw_box(stdscr, self.theme, y, 0, h, boxw, "TOTAL", th.PAIR_TITLE_ORANGE)
        inner_w = boxw - 4
        if cap:
            label = f"concurrency {running}/{cap} ({running/cap*100:4.1f}%)"
            bar_w = th.fit_meter_width(inner_w, label)
            th.draw_meter(stdscr, self.theme, y + 1, 2, bar_w, running / cap, label)
        th.safe_addstr(stdscr, y + 2, 2,
                        f"req/min={summary.get('requests_1m', 0):<4d} req/5min={summary.get('requests_5m', 0):<5d} "
                        f"accept_rate(60s)={summary.get('accept_rate_60s', 0)*100:5.1f}%")
        return y + h + 1

    def _latency_panel(self, y: int, boxw: int, prom: dict) -> int:
        """TTFT / E2E / queue-time / cache-hit — everything that needs sglang
        started with --enable-metrics. TTFT and E2E have no per-DP label
        (sglang aggregates them server-wide), queue-time and cache-hit do."""
        stdscr = self.stdscr
        h = 5
        th.draw_box(stdscr, self.theme, y, 0, h, boxw, "LATENCY (--enable-metrics, 5min)", th.PAIR_TITLE_MAGENTA)

        if not prom.get("enabled"):
            th.safe_addstr(stdscr, y + 2, 2,
                            "not enabled — add --enable-metrics to serve517.sh and restart to see this",
                            curses.color_pair(th.PAIR_DIM))
            return y + h + 1

        latency = prom.get("latency", {})
        queue_by_dp = prom.get("queue_by_dp", {})
        cache_by_dp = prom.get("cache_hit_by_dp", {})

        def stat_row(row: int, label: str, s: dict | None) -> None:
            if not s:
                th.safe_addstr(stdscr, row, 2, f"{label:<5} no samples yet", curses.color_pair(th.PAIR_DIM))
                return
            th.safe_addstr(stdscr, row, 2,
                            f"{label:<5} mean={fmt_secs(s['mean'])}  p50={fmt_secs(s['p50'])}  "
                            f"p90={fmt_secs(s['p90'])}  p99={fmt_secs(s['p99'])}  n={s['count']:.0f}")

        stat_row(y + 1, "TTFT", latency.get("ttft"))
        stat_row(y + 2, "E2E", latency.get("e2e"))

        parts = []
        for dp in sorted(set(queue_by_dp) | set(cache_by_dp)):
            q = queue_by_dp.get(dp)
            qtxt = f"p50={fmt_secs(q['p50'])} p90={fmt_secs(q['p90'])}" if q else "no samples"
            ch = cache_by_dp.get(dp)
            chtxt = f" cache-hit={ch*100:4.1f}%" if ch is not None else ""
            parts.append(f"DP{dp} queue {qtxt}{chtxt}")
        th.safe_addstr(stdscr, y + 3, 2, "   ".join(parts))
        return y + h + 1

    def _gpu_panel(self, y: int, boxw: int, gpus: list, hist_gpu: list, now: float) -> int:
        stdscr = self.stdscr
        n = max(1, len(gpus))
        h = n + 2
        th.draw_box(stdscr, self.theme, y, 0, h, boxw, "GPUs", th.PAIR_TITLE_CYAN)
        inner_w = boxw - 4
        bar_w = min(24, max(6, inner_w - 55))
        for i, g in enumerate(gpus):
            row = y + 1 + i
            label = f"GPU{g['index']}"
            th.safe_addstr(stdscr, row, 2, label)
            end_x = th.draw_meter(stdscr, self.theme, row, 2 + len(label) + 1, bar_w, g["util"] / 100,
                                   f"{g['util']:5.1f}%")
            th.safe_addstr(stdscr, row, end_x + 2,
                            f"mem={g['mem_used']:6.0f}/{g['mem_total']:6.0f}MiB  "
                            f"{g['temp']:3.0f}C  {g['power']:5.1f}W")
        return y + h + 1
