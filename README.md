# sgtop

![Linux](https://img.shields.io/badge/-Linux-grey?logo=linux)
![macOS](https://img.shields.io/badge/-macOS-black?logo=apple)
![Usage](https://img.shields.io/badge/Usage-SGLang%20monitor-yellow)
[![PyPI](https://img.shields.io/pypi/v/sgtop)](https://pypi.org/project/sgtop/)
![Python](https://img.shields.io/pypi/pyversions/sgtop)
![License](https://img.shields.io/badge/license-MIT-blue)
![Contributors](https://img.shields.io/github/contributors/HarrytheOrange/sgtop)

A [btop](https://github.com/aristocratos/btop)-style terminal dashboard for a live [SGLang](https://github.com/sgl-project/sglang) deployment. Not affiliated with the SGLang project — just a small, read-only sidecar that never touches your inference traffic.

![sgtop screenshot](docs/screenshot.png)

`nvtop` will tell you a GPU is at 98% utilization. It won't tell you whether your sglang server can actually take more concurrent requests, whether one data-parallel replica is sitting idle while another is drowning, or what your real time-to-first-token looks like. That's what this is for: concurrency vs. the capacity sglang actually computed at startup, KV-cache headroom per replica, and (if you turn on `--enable-metrics`) rolling TTFT/E2E/queue-time percentiles.

## Install

```bash
pipx install sgtop
```

Pure standard library, no dependencies, Python ≥ 3.9. `pip install --user sgtop` works too if you don't have pipx.

## Quick start

`sgtop` is just a client — it needs `sgtop-server` running somewhere with access to your sglang process:

```bash
# launch sglang, redirecting its output to a file
python -m sglang.launch_server --model-path ... --dp-size 2 --enable-metrics > server.log 2>&1 &

# point the sidecar at that log
sgtop-server --log server.log --service-port 30000 --port 30001 &

# watch it
sgtop --port 30001
```

Run `sgtop-server` on the sglang box, then `sgtop --host <that box>` from wherever you actually work. There's also a plain HTML version of the same data at `http://<host>:30001/` if you'd rather glance at it in a browser.

## About `--enable-metrics`

Skip it and you still get concurrency, KV-cache usage, throughput, and GPU stats — all parsed straight from sglang's log lines. Add it and the LATENCY panel starts showing TTFT / end-to-end / queue-time / cache-hit-rate, pulled from sglang's Prometheus endpoint. Prometheus histograms are cumulative counters, so `sgtop-server` diffs consecutive scrapes itself to give you an actual rolling 5-minute p50/p90/p99 instead of a number that just drifts toward whatever your first few requests looked like.

`q` quits. That's the whole interface.

## License

MIT
