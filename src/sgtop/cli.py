from __future__ import annotations

import argparse
import curses

from .app import App
from .data import DEFAULT_HOST, DEFAULT_PORT, ProxyClient, find_dashboard, full_port_scan, probe_mode
from .direct import DirectClient
from .fleet import FleetClient, parse_fleet_spec
from .fleet_app import FleetApp


def main() -> None:
    ap = argparse.ArgumentParser(
        description="btop-style terminal monitor for a live SGLang deployment. "
        "Talks straight to sglang's own API by default (--enable-metrics gets you "
        "the full picture) — no sidecar required. Also works against the optional "
        "sgtop-server sidecar, which additionally gives you a GPU panel."
    )
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"target host (default: {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, default=None,
                     help=f"target port. If omitted, sgtop probes a few common ports "
                          f"(default: {DEFAULT_PORT}, then 30001, ...) and uses whichever answers.")
    ap.add_argument("--mode", choices=["auto", "direct", "proxy"], default="auto",
                     help="auto (default): detect whether --port is a plain sglang instance or a "
                          "sgtop-server sidecar. direct: talk straight to sglang's own API "
                          "(/metrics, /get_server_info). proxy: talk to sgtop-server's /api/status.")
    ap.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds (default: 1.0)")
    ap.add_argument("--no-full-scan", action="store_true",
                     help="don't fall back to scanning every TCP port (1-65535) when none of the "
                          "common candidate ports answer — just give up and show 'offline' like before")
    ap.add_argument("--fleet", metavar="SPEC", default=None,
                     help="Watch several machines at once instead of one. SPEC is either "
                          "'name=host:port,name=host:port,...' inline, or a path to a text file with "
                          "one 'name=host:port' per line (# comments ok). Each entry is auto-probed "
                          "for direct vs proxy mode, same as single-host mode. All other flags except "
                          "--interval are ignored in this mode.")
    args = ap.parse_args()

    if args.fleet:
        specs = parse_fleet_spec(args.fleet)
        if not specs:
            raise SystemExit("sgtop: --fleet given but no servers parsed from it")
        client = FleetClient(specs)
        for m in client.members:
            print(f"sgtop: {m.name} -> {m.host}:{m.port} probed as {m.mode}")

        def _run_fleet(stdscr):
            FleetApp(stdscr, client, args.interval).run()

        curses.wrapper(_run_fleet)
        return

    port = args.port
    mode = None if args.mode == "auto" else args.mode

    if port is None:
        print(f"sgtop: no --port given, probing {args.host} for sglang or sgtop-server...")
        port, mode = find_dashboard(args.host)
        if port is not None:
            print(f"sgtop: found {mode} on port {port}")
        elif args.no_full_scan:
            port, mode = DEFAULT_PORT, "direct"
            print(f"sgtop: none of the common ports answered, falling back to default port {port}")
        else:
            print(f"sgtop: none of the common ports answered — scanning all 65535 TCP ports on "
                  f"{args.host} (this can take up to a minute; pass --no-full-scan to skip)...")
            port, mode = full_port_scan(args.host, progress_cb=lambda msg: print(f"sgtop: {msg}"))
            if port is not None:
                print(f"sgtop: found {mode} on port {port}")
            else:
                port, mode = DEFAULT_PORT, "direct"
                print(f"sgtop: nothing found anywhere, falling back to default port {port}")
    elif mode is None:
        mode = probe_mode(args.host, port) or "direct"

    client = ProxyClient(args.host, port) if mode == "proxy" else DirectClient(args.host, port)

    def _run(stdscr):
        App(stdscr, client, args.host, port, args.interval).run()

    curses.wrapper(_run)


if __name__ == "__main__":
    main()
