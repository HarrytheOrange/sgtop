from __future__ import annotations

import curses
import math
import time
from datetime import datetime

from . import theme as th
from .app import fmt_secs, human_uptime  # noqa: F401  (fmt_secs kept for parity/future use)

CARD_H = 7
CARD_MIN_W = 56
CARD_MAX_W = 100


def fleet_grid_layout(n: int, term_w: int, rows_available: int) -> tuple[int, int, int]:
    if n <= 0:
        return 1, 0, CARD_MAX_W
    max_rows_that_fit = max(1, rows_available // (CARD_H + 1))
    max_cols_by_width = max(1, term_w // (CARD_MIN_W + 2))
    cols = 1
    while cols < min(n, max_cols_by_width) and math.ceil(n / cols) > max_rows_that_fit:
        cols += 1
    rows = math.ceil(n / cols)
    card_w = min(CARD_MAX_W, max(CARD_MIN_W, (term_w - (cols - 1) * 2) // cols))
    return cols, rows, card_w


def age_str(last_seen: float | None, now: float) -> str:
    if last_seen is None:
        return "never seen"
    s = now - last_seen
    return "just now" if s < 2 else f"{s:.0f}s ago"


class FleetApp:
    def __init__(self, stdscr, client, interval: float):
        self.stdscr = stdscr
        self.client = client
        self.interval = interval
        self.theme = th.Theme()

    def run(self) -> None:
        curses.curs_set(0)
        self.theme.setup()
        self.stdscr.nodelay(True)
        self.stdscr.timeout(int(self.interval * 1000))
        while True:
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return
            self.client.poll()
            self.render(self.client.snapshot())

    def render(self, members: list) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        now = time.time()

        self._header(0, w, members)

        rows_for_grid = h - 3
        cols, rows, card_w = fleet_grid_layout(len(members), w, rows_for_grid)
        for i, m in enumerate(members):
            col, row = i % cols, i // cols
            x = col * (card_w + 2)
            y = 2 + row * (CARD_H + 1)
            self._card(y, x, card_w, m, now)

        self._footer(h - 1, w, members)
        stdscr.refresh()

    def _header(self, y: int, w: int, members: list) -> None:
        stdscr = self.stdscr
        up_count = sum(1 for m in members if getattr(m, "up", False))
        clock = datetime.now().strftime("%H:%M:%S")
        th.safe_addstr(stdscr, y, 0, "sgtop", curses.A_BOLD | curses.color_pair(th.PAIR_TITLE_CYAN))
        th.safe_addstr(stdscr, y, 6, " fleet", curses.A_DIM)
        right = f"{up_count}/{len(members)} online   {clock}"
        th.safe_addstr(stdscr, y, max(0, w - len(right)), right)

    def _footer(self, y: int, w: int, members: list) -> None:
        total_running = sum(getattr(m, "running", 0) for m in members)
        total_cap = sum(getattr(m, "cap", 0) for m in members)
        total_decode = sum(getattr(m, "decode", 0.0) for m in members)
        left = f"q quit   fleet totals: concurrency {total_running}/{total_cap}   decode {total_decode:.0f} tok/s"
        th.safe_addstr(self.stdscr, y, 0, left, curses.A_DIM)

    def _card(self, y: int, x: int, w: int, m, now: float) -> None:
        stdscr = self.stdscr
        up = getattr(m, "up", False)
        pair = th.PAIR_TITLE_CYAN if up else th.PAIR_BAD
        th.draw_box(stdscr, self.theme, y, x, CARD_H, w, m.name, pair)
        inner_w = w - 4

        dot = "●" if up else "○"
        status = f"{dot} {m.host}:{m.port} ({m.mode})   {age_str(m.last_seen, now)}"
        th.safe_addstr(stdscr, y + 1, x + 2, status[:inner_w], curses.color_pair(pair))

        if not up:
            th.safe_addstr(stdscr, y + 3, x + 2, "offline / unreachable", curses.color_pair(th.PAIR_BAD))
            return

        running, cap, queue = getattr(m, "running", 0), getattr(m, "cap", 0), getattr(m, "queue", 0)
        if cap:
            pct = running / cap * 100
            label = f"concurrency {running}/{cap} ({pct:4.1f}%) q={queue}"
            bar_w = th.fit_meter_width(inner_w, label)
            th.draw_meter(stdscr, self.theme, y + 2, x + 2, bar_w, running / cap, label)
        else:
            th.safe_addstr(stdscr, y + 2, x + 2, "cap unknown", curses.color_pair(th.PAIR_DIM))

        decode = getattr(m, "decode", 0.0)
        thr_label = f"decode {decode:7.1f} tok/s"
        history = list(getattr(m, "history", []))
        spark_w = max(0, inner_w - len(thr_label) - 1)
        if spark_w >= 4 and history:
            series = [it["decode"] for it in history[-spark_w:]]
            series = [0.0] * (spark_w - len(series)) + series
            th.draw_sparkline(stdscr, y + 3, x + 2, series, pair)
            th.safe_addstr(stdscr, y + 3, x + 2 + spark_w + 1, thr_label)
        else:
            th.safe_addstr(stdscr, y + 3, x + 2, thr_label)

        gpus = getattr(m, "gpus", [])
        if gpus:
            mem_used = sum(g["mem_used"] for g in gpus)
            mem_total = sum(g["mem_total"] for g in gpus)
            max_temp = max(g["temp"] for g in gpus)
            per_gpu = " ".join(f"{g['index']}:{g['util']:.0f}%" for g in gpus)
            gpu_line = f"GPU {per_gpu}  mem {mem_used/1024:.1f}/{mem_total/1024:.1f}GB  {max_temp:.0f}C"
        elif m.mode == "direct":
            gpu_line = "GPU n/a (direct mode — run sgtop-server there for GPU stats)"
        else:
            gpu_line = "GPU n/a"
        th.safe_addstr(stdscr, y + 4, x + 2, gpu_line[:inner_w])

        cache_hit = getattr(m, "cache_hit", None)
        errors = getattr(m, "errors", 0)
        accept_rate = getattr(m, "accept_rate", 0.0)
        ch_txt = f"{cache_hit*100:.0f}%" if cache_hit is not None else "n/a"
        tail = f"cache-hit={ch_txt}  accept={accept_rate*100:.0f}%  errors(5m)={errors}"
        th.safe_addstr(stdscr, y + 5, x + 2, tail[:inner_w])
