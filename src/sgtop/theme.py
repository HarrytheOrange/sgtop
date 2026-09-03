"""btop-flavored color palette, gradient meters, boxes and sparklines for a
curses screen. Degrades gracefully on 8/16-color terminals."""
from __future__ import annotations

import curses

BLOCKS = " ▁▂▃▄▅▆▇█"  # 9 levels, index 0 = blank (keeps idle sparklines quiet)

# xterm-256 ramp, green -> yellow -> red, used for gradient meters.
GRADIENT_256 = [46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196]
# accent colors per role, 256-color id.
CYAN_256 = 51
MAGENTA_256 = 213
ORANGE_256 = 215
DIM_256 = 240

# pair ids, assigned once in Theme.setup()
PAIR_BORDER = 1
PAIR_TITLE_CYAN = 2
PAIR_TITLE_MAGENTA = 3
PAIR_TITLE_ORANGE = 4
PAIR_DIM = 5
PAIR_BAD = 6
GRADIENT_PAIR_BASE = 10  # + 0..len(GRADIENT_256)-1


class Theme:
    def __init__(self) -> None:
        self.has_256 = False

    def setup(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        self.has_256 = curses.COLORS >= 256

        if self.has_256:
            curses.init_pair(PAIR_BORDER, DIM_256, -1)
            curses.init_pair(PAIR_TITLE_CYAN, CYAN_256, -1)
            curses.init_pair(PAIR_TITLE_MAGENTA, MAGENTA_256, -1)
            curses.init_pair(PAIR_TITLE_ORANGE, ORANGE_256, -1)
            curses.init_pair(PAIR_DIM, DIM_256, -1)
            curses.init_pair(PAIR_BAD, 196, -1)
            for i, color in enumerate(GRADIENT_256):
                curses.init_pair(GRADIENT_PAIR_BASE + i, color, -1)
        else:
            curses.init_pair(PAIR_BORDER, curses.COLOR_WHITE, -1)
            curses.init_pair(PAIR_TITLE_CYAN, curses.COLOR_CYAN, -1)
            curses.init_pair(PAIR_TITLE_MAGENTA, curses.COLOR_MAGENTA, -1)
            curses.init_pair(PAIR_TITLE_ORANGE, curses.COLOR_YELLOW, -1)
            curses.init_pair(PAIR_DIM, curses.COLOR_WHITE, -1)
            curses.init_pair(PAIR_BAD, curses.COLOR_RED, -1)
            # 3-step fallback gradient reusing green/yellow/red.
            curses.init_pair(GRADIENT_PAIR_BASE, curses.COLOR_GREEN, -1)
            curses.init_pair(GRADIENT_PAIR_BASE + 1, curses.COLOR_YELLOW, -1)
            curses.init_pair(GRADIENT_PAIR_BASE + 2, curses.COLOR_RED, -1)

    def gradient_steps(self) -> int:
        return len(GRADIENT_256) if self.has_256 else 3

    def gradient_pair(self, position_frac: float) -> int:
        steps = self.gradient_steps()
        idx = min(steps - 1, max(0, int(position_frac * steps)))
        return curses.color_pair(GRADIENT_PAIR_BASE + idx)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    room = w - x - 1  # leave the last column alone, ncurses errors writing into it
    if room <= 0:
        return
    try:
        win.addstr(y, x, text[:room], attr)
    except curses.error:
        pass


def draw_box(win, theme: Theme, y: int, x: int, h: int, w: int, title: str, title_pair: int) -> None:
    border = curses.color_pair(PAIR_BORDER)
    top = "╭" + "─" * max(0, w - 2) + "╮"
    safe_addstr(win, y, x, top, border)
    label = f" {title} "
    safe_addstr(win, y, x + 2, label, curses.color_pair(title_pair) | curses.A_BOLD)
    for row in range(1, h - 1):
        safe_addstr(win, y + row, x, "│", border)
        safe_addstr(win, y + row, x + w - 1, "│", border)
    safe_addstr(win, y + h - 1, x, "╰" + "─" * (w - 2) + "╯", border)


def fit_meter_width(inner_w: int, label: str, min_w: int = 10, max_w: int = 60) -> int:
    """Bar width that keeps '[' + bar + '] ' + label inside inner_w columns."""
    avail = inner_w - 3 - len(label)
    return max(min_w, min(max_w, avail))


def draw_meter(win, theme: Theme, y: int, x: int, width: int, frac: float, label: str) -> int:
    """A btop-style horizontal meter: color is a fixed left-to-right gradient,
    only the fill length changes with `frac`. Unfilled cells are dim dots.
    Returns the screen column right after the rendered label, so callers can
    place further text without overlapping it."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(width * frac))
    safe_addstr(win, y, x, "[")
    for i in range(width):
        ch = "█" if i < filled else "░"
        pair = theme.gradient_pair(i / max(1, width - 1)) if i < filled else curses.color_pair(PAIR_DIM)
        safe_addstr(win, y, x + 1 + i, ch, pair)
    tail = f"] {label}"
    safe_addstr(win, y, x + 1 + width, tail)
    return x + 1 + width + len(tail)


def sparkline(values: list[float], vmax: float | None = None) -> str:
    if not values:
        return ""
    peak = vmax if vmax is not None else (max(values) or 1.0)
    if peak <= 0:
        peak = 1.0
    out = []
    for v in values:
        lvl = int(round((v / peak) * (len(BLOCKS) - 1)))
        out.append(BLOCKS[max(0, min(len(BLOCKS) - 1, lvl))])
    return "".join(out)


def draw_sparkline(win, y: int, x: int, values: list[float], pair: int, vmax: float | None = None) -> None:
    safe_addstr(win, y, x, sparkline(values, vmax), curses.color_pair(pair))
