from __future__ import annotations

import argparse
import curses

from .app import App
from .data import DEFAULT_HOST, DEFAULT_PORT, find_dashboard_port


def main() -> None:
    ap = argparse.ArgumentParser(
        description="btop-style terminal monitor for a live SGLang deployment. "
        "Reads a running sgtop-server's /api/status endpoint — no local "
        "file access, so it works against any host on the LAN serving one."
    )
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"dashboard host (default: {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=None,
                     help=f"dashboard port. If omitted, sgtop probes a few common ports "
                          f"(default: {DEFAULT_PORT}, then {30000}, ...) and uses whichever answers.")
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1.0)")
    args = ap.parse_args()

    port = args.port
    if port is None:
        print(f"sgtop: no --port given, probing {args.host} for a live dashboard...")
        port = find_dashboard_port(args.host)
        if port is not None:
            print(f"sgtop: found one on port {port}")
        else:
            port = DEFAULT_PORT
            print(f"sgtop: none responded, falling back to default port {port}")

    def _run(stdscr):
        App(stdscr, args.host, port, args.interval).run()

    curses.wrapper(_run)


if __name__ == "__main__":
    main()
