# sgtop

![Linux](https://img.shields.io/badge/-Linux-grey?logo=linux)
![macOS](https://img.shields.io/badge/-macOS-black?logo=apple)
![Usage](https://img.shields.io/badge/Usage-SGLang%20monitor-yellow)
[![PyPI](https://img.shields.io/pypi/v/sgtop)](https://pypi.org/project/sgtop/)
![Python](https://img.shields.io/pypi/pyversions/sgtop)
![License](https://img.shields.io/badge/license-MIT-blue)
![Contributors](https://img.shields.io/github/contributors/HarrytheOrange/sgtop)

A [btop](https://github.com/aristocratos/btop)-style terminal dashboard for a live [SGLang](https://github.com/sgl-project/sglang) deployment. Not affiliated with the SGLang project — just a small, read-only tool that never touches your inference traffic.

![sgtop screenshot](docs/screenshot.png)

`nvtop` will tell you a GPU is at 98% utilization. It won't tell you whether your sglang server can actually take more concurrent requests, whether one data-parallel replica is sitting idle while another is drowning, or what your real time-to-first-token looks like. That's what this is for: concurrency vs. the capacity sglang actually computed at startup, KV-cache headroom per replica, and (with `--enable-metrics`) rolling TTFT/E2E/queue-time percentiles.

## Install

```bash
pipx install sgtop
```

Pure standard library, no dependencies, Python ≥ 3.9. `pip install --user sgtop` works too if you don't have pipx.

## Quick start

Point it at a running sglang instance — that's it, nothing else to run:

```bash
sgtop --host <sglang-host> --port 30000
```

Leave out `--port` entirely and it'll probe a handful of common ports for you. For the LATENCY panel (TTFT / end-to-end / queue-time / cache-hit-rate), start sglang with `--enable-metrics`; without it you still get concurrency, KV-cache usage, and decode throughput straight off sglang's own API. Prometheus histograms are cumulative counters, so sgtop diffs consecutive scrapes itself to give you an actual rolling 5-minute p50/p90/p99, not a number that just drifts toward whatever your first few requests looked like.

## The optional sidecar

sglang's API can't tell you GPU temperature or show you a recent-errors line — for that there's `sgtop-server`, a small sidecar you can run on the sglang box that also tails its log and `nvidia-smi`:

```bash
python -m sglang.launch_server --model-path ... --enable-metrics > server.log 2>&1 &
sgtop-server --log server.log --service-port 30000 --port 30001 &
sgtop --port 30001    # or --mode proxy to skip auto-detection
```

It also serves a plain HTML version of the same data at `http://<host>:30001/`. Everything else — concurrency, KV-cache, latency — works exactly the same with or without it.

`q` quits. That's the whole interface.

## License

MIT
