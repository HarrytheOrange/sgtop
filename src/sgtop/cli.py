from __future__ import annotations

import argparse
import curses

from .app import App
from .data import DEFAULT_HOST, DEFAULT_PORT, find_dashboard_port, full_port_scan


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
    ap.add_argument("--no-full-scan", action="store_true",
                     help="don't fall back to scanning every TCP port (1-65535) when none of the "
                          "common candidate ports answer — just give up and show 'offline' like before")
    args = ap.parse_args()

    port = args.port
    if port is None:
        print(f"sgtop: no --port given, probing {args.host} for a live dashboard...")
        port = find_dashboard_port(args.host)
        if port is not None:
            print(f"sgtop: found one on port {port}")
        elif args.no_full_scan:
            port = DEFAULT_PORT
            print(f"sgtop: none of the common ports answered, falling back to default port {port}")
        else:
            print(f"sgtop: none of the common ports answered — scanning all 65535 TCP ports on "
                  f"{args.host} for one (this can take up to a minute; pass --no-full-scan to skip)...")
            port = full_port_scan(args.host, progress_cb=lambda msg: print(f"sgtop: {msg}"))
            if port is not None:
                print(f"sgtop: found sgtop-server on port {port}")
            else:
                port = DEFAULT_PORT
                print(f"sgtop: nothing found anywhere, falling back to default port {port}")

    def _run(stdscr):
        App(stdscr, args.host, port, args.interval).run()

    curses.wrapper(_run)


if __name__ == "__main__":
    main()
