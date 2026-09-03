from __future__ import annotations

import argparse
import curses

from .app import App
from .data import DEFAULT_HOST, DEFAULT_PORT


def main() -> None:
    ap = argparse.ArgumentParser(
        description="btop-style terminal monitor for a live SGLang deployment. "
        "Reads a running sgtop-server's /api/status endpoint — no local "
        "file access, so it works against any host on the LAN serving one."
    )
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"dashboard host (default: {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"dashboard port (default: {DEFAULT_PORT})")
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1.0)")
    args = ap.parse_args()

    def _run(stdscr):
        App(stdscr, args.host, args.port, args.interval).run()

    curses.wrapper(_run)


if __name__ == "__main__":
    main()
