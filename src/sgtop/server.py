#!/usr/bin/env python3
"""sgtop-server: read-only real-time dashboard for an sglang deployment.

Tails sglang's own log file and nvidia-smi into a small JSON API that `sgtop`
(the terminal UI) reads, plus a plain HTML page at the same address."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean, median
from urllib.parse import urlparse


DECODE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<dp>DP\d+) TP\d+\].*?"
    r"Decode batch, #running-req: (?P<running>\d+), #full token: (?P<tokens>\d+).*?"
    r"accept len: (?P<accept_len>[\d.]+), accept rate: (?P<accept_rate>[\d.]+).*?"
    r"gen throughput \(token/s\): (?P<throughput>[\d.]+), #queue-req: (?P<queue>\d+)"
)
PREFILL_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<dp>DP\d+) TP\d+\].*?"
    r"Prefill batch, #new-seq: (?P<seq>\d+), #new-token: (?P<new_tokens>\d+), "
    r"#cached-token: (?P<cached>\d+).*?#pending-token: (?P<pending>\d+).*?"
    r"input throughput \(token/s\): (?P<throughput>[\d.]+)"
)
REQUEST_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*?POST /v1/(?:messages|chat/completions).*? 200 OK"
)
ERROR_RE = re.compile(r"Traceback|Exception|ERROR|Failed to parse", re.IGNORECASE)
CAP_RE = re.compile(
    r"\[(?P<ts>[\d-]+ [\d:]+) (?P<dp>DP\d+) TP\d+\].*?"
    r"max_total_num_tokens=(?P<max_tokens>\d+).*?"
    r"max_running_requests=(?P<max_running>\d+).*?"
    r"context_len=(?P<ctx>\d+)"
)
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def epoch(value: str) -> float:
    return datetime.strptime(value, TS_FORMAT).timestamp()


# ---------------------------------------------------------------------------
# Optional: sglang's own Prometheus /metrics endpoint (only present when the
# service is started with --enable-metrics). Gives per-request latency
# histograms (TTFT / inter-token / end-to-end / queue time) that the plain
# text log doesn't have. Verified against sglang 0.5.17's
# srt/observability/metrics_collector.py:
#   - time_to_first_token_seconds / inter_token_latency_seconds /
#     e2e_request_latency_seconds live in TokenizerMetricsCollector, whose
#     labels are just {model_name, engine_type[, is_streaming]} — there is
#     NO dp_rank label, so with DP=2 these are already a whole-server
#     aggregate across both replicas, not split per DP.
#   - queue_time_seconds and cache_hit_rate live in SchedulerMetricsCollector,
#     one per DP replica, and ARE labeled with dp_rank (only the TP-rank-0
#     scheduler of each DP group reports by default, so no double counting).
# Prometheus histograms are cumulative counters since process start, so we
# diff consecutive scrapes to get "in this window" counts, same idea as
# Prometheus's own rate()/increase(). This parsing/tracking logic is shared
# with sgtop's direct-client mode (see direct.py) via prom.py.
from .prom import GAUGE_METRICS, HIST_METRICS, PromTracker  # noqa: E402


class Monitor:
    def __init__(self, log_path: str, service_port: int, interval: float):
        self.log_path = Path(log_path)
        self.service_port = service_port
        self.interval = interval
        self.lock = threading.Lock()
        self.decode = deque(maxlen=4000)
        self.prefill = deque(maxlen=4000)
        self.requests = deque(maxlen=4000)
        self.errors = deque(maxlen=30)
        self.gpu_history = deque(maxlen=1800)
        self.gpus: list[dict] = []
        self.caps: dict[str, dict] = {}
        self.prom = PromTracker()
        self.metrics_url = f"http://127.0.0.1:{service_port}/metrics"
        self.pid: int | None = None
        self.service_up = False
        self.log_offset = 0
        self.log_inode: int | None = None
        self.started_at = time.time()

    def start(self) -> None:
        self._bootstrap_log()
        threading.Thread(target=self._loop, name="sglang-monitor", daemon=True).start()

    def _bootstrap_log(self) -> None:
        try:
            stat = self.log_path.stat()
            self.log_inode = stat.st_ino
            self.log_offset = max(0, stat.st_size - 4 * 1024 * 1024)
            self._read_log()
            self._scan_caps_full()
        except OSError:
            pass

    def _scan_caps_full(self) -> None:
        # The "max_total_num_tokens=..." startup line is printed once per DP
        # replica and may sit further back than the tail window read above,
        # so scan the whole file once for it regardless of file size.
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    match = CAP_RE.search(raw)
                    if match:
                        item = match.groupdict()
                        with self.lock:
                            self.caps[item["dp"]] = {
                                "max_tokens": int(item["max_tokens"]),
                                "max_running": int(item["max_running"]),
                                "ctx": int(item["ctx"]),
                            }
        except OSError:
            pass

    def _loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self._read_log()
                self._sample_system()
                self._prune()
            except Exception as exc:  # keep monitoring even if one sample fails
                with self.lock:
                    self.errors.append({"ts": time.time(), "text": f"monitor: {exc}"})
            delay = max(0.2, self.interval - (time.monotonic() - started))
            time.sleep(delay)

    def _read_log(self) -> None:
        stat = self.log_path.stat()
        if self.log_inode != stat.st_ino or stat.st_size < self.log_offset:
            self.log_inode = stat.st_ino
            self.log_offset = 0
        if stat.st_size == self.log_offset:
            return
        with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self.log_offset)
            lines = handle.readlines()
            self.log_offset = handle.tell()
        cutoff = time.time() - 900
        with self.lock:
            for raw in lines:
                line = raw.strip().replace("\r", "")
                match = DECODE_RE.search(line)
                if match:
                    item = match.groupdict()
                    ts = epoch(item["ts"])
                    if ts >= cutoff:
                        self.decode.append({
                            "ts": ts, "dp": item["dp"],
                            "throughput": float(item["throughput"]),
                            "running": int(item["running"]), "queue": int(item["queue"]),
                            "tokens": int(item["tokens"]),
                            "accept_len": float(item["accept_len"]),
                            "accept_rate": float(item["accept_rate"]),
                        })
                    continue
                match = PREFILL_RE.search(line)
                if match:
                    item = match.groupdict()
                    ts = epoch(item["ts"])
                    if ts >= cutoff:
                        self.prefill.append({
                            "ts": ts, "dp": item["dp"],
                            "throughput": float(item["throughput"]),
                            "new_tokens": int(item["new_tokens"]),
                            "cached": int(item["cached"]), "pending": int(item["pending"]),
                        })
                    continue
                match = REQUEST_RE.search(line)
                if match:
                    ts = epoch(match.group("ts"))
                    if ts >= cutoff:
                        self.requests.append(ts)
                    continue
                if ERROR_RE.search(line) and line.startswith("["):
                    match = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    ts = epoch(match.group(1)) if match else time.time()
                    if ts >= cutoff:
                        self.errors.append({"ts": ts, "text": line[-300:]})
                    continue
                match = CAP_RE.search(line)
                if match:
                    item = match.groupdict()
                    self.caps[item["dp"]] = {
                        "max_tokens": int(item["max_tokens"]),
                        "max_running": int(item["max_running"]),
                        "ctx": int(item["ctx"]),
                    }

    def _find_pid(self) -> int | None:
        needle = f"--port {self.service_port}"
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
                if "sglang.launch_server" in cmd and needle in cmd:
                    return int(entry.name)
            except (OSError, ValueError):
                continue
        return None

    def _sample_system(self) -> None:
        now = time.time()
        pid = self._find_pid()
        try:
            with socket.create_connection(("127.0.0.1", self.service_port), timeout=0.4):
                service_up = True
        except OSError:
            service_up = False

        gpus = []
        command = [
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=2, check=True).stdout
            for row in output.splitlines():
                fields = [part.strip() for part in row.split(",")]
                if len(fields) == 8:
                    gpus.append({
                        "index": int(fields[0]), "name": fields[1], "util": float(fields[2]),
                        "mem_util": float(fields[3]), "mem_used": float(fields[4]),
                        "mem_total": float(fields[5]), "power": float(fields[6]), "temp": float(fields[7]),
                    })
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

        metrics_text = None
        if service_up:
            try:
                with urllib.request.urlopen(self.metrics_url, timeout=1.5) as resp:
                    metrics_text = resp.read().decode("utf-8", errors="replace")
            except Exception:
                pass  # --enable-metrics not on, or endpoint not up yet — fine, just no data this tick

        with self.lock:
            self.pid = pid
            self.service_up = service_up
            if gpus:
                self.gpus = gpus
                self.gpu_history.append({"ts": now, "util": [gpu["util"] for gpu in gpus]})
            if metrics_text is not None:
                self.prom.ingest(metrics_text, now)

    def _prune(self) -> None:
        cutoff = time.time() - 900
        with self.lock:
            for collection in (self.decode, self.prefill, self.requests, self.gpu_history):
                while collection and (collection[0]["ts"] if isinstance(collection[0], dict) else collection[0]) < cutoff:
                    collection.popleft()

    def _uptime(self, pid: int | None) -> float | None:
        if not pid:
            return None
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            start_ticks = int(fields[21])
            boot_seconds = float(Path("/proc/uptime").read_text().split()[0])
            return max(0.0, boot_seconds - start_ticks / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _reshape_prom(raw: dict, latest_gauge: dict) -> dict:
        # ttft/itl/e2e carry no dp_rank (see the PromTracker docstring), so if
        # more than one label combo shows up (e.g. streaming vs non-streaming)
        # we just keep whichever has more samples rather than trying to merge
        # percentiles across sub-populations.
        # Observed live (not just in server_args): every TP rank in a DP group
        # reports the same per-request scheduler stats in lockstep (identical
        # counts on tp_rank=0 and tp_rank=1), not just a single "stats logging
        # rank" as the upstream comment implies. Pin to tp_rank="0" so we
        # don't double count or pick a duplicate non-deterministically.
        latency: dict[str, dict] = {}
        queue_by_dp: dict[str, dict] = {}
        for (hist_key, _group_key), stats in raw.items():
            group = stats["group"]
            if group.get("tp_rank") not in (None, "0"):
                continue
            dp_rank = group.get("dp_rank")
            fields = {k: stats[k] for k in ("mean", "p50", "p90", "p99", "count")}
            if hist_key == "queue" and dp_rank is not None:
                queue_by_dp[str(dp_rank)] = fields
            elif hist_key != "queue":
                best = latency.get(hist_key)
                if best is None or stats["count"] > best["count"]:
                    latency[hist_key] = fields
        cache_hit_by_dp: dict[str, float] = {}
        for (gauge_key, group_key), value in latest_gauge.items():
            if gauge_key != "cache_hit_rate":
                continue
            group = dict(group_key)
            if group.get("tp_rank") not in (None, "0"):
                continue
            dp_rank = group.get("dp_rank")
            if dp_rank is not None:
                cache_hit_by_dp[str(dp_rank)] = value
        return {"latency": latency, "queue_by_dp": queue_by_dp, "cache_hit_by_dp": cache_hit_by_dp}

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            decode = list(self.decode)
            prefill = list(self.prefill)
            requests = list(self.requests)
            errors = list(self.errors)
            gpu_history = list(self.gpu_history)
            gpus = list(self.gpus)
            pid = self.pid
            service_up = self.service_up
            caps = dict(self.caps)
            prom_raw = self.prom.summary(now)
            prom_gauges = dict(self.prom.latest_gauge)
            metrics_enabled = self.prom.seen_any

        active: dict[str, dict] = {}
        for item in decode:
            active[item["dp"]] = item
        active = {key: value for key, value in active.items() if now - value["ts"] <= 5}
        recent_decode = [item for item in decode if now - item["ts"] <= 60]
        recent_prefill = [item for item in prefill if now - item["ts"] <= 60]
        normal_prefill = [item["throughput"] for item in recent_prefill if 100 <= item["throughput"] <= 20000]
        per_request = [item["throughput"] / item["running"] for item in recent_decode if item["running"]]
        stable = [item for item in recent_decode if item["throughput"] >= 50]

        summary = {
            "decode_now": sum(item["throughput"] for item in active.values()),
            "running_now": sum(item["running"] for item in active.values()),
            "queue_now": sum(item["queue"] for item in active.values()),
            "per_request_60s": mean(per_request) if per_request else 0,
            "stable_decode_60s": sum(mean([item["throughput"] for item in stable if item["dp"] == dp])
                                         for dp in {item["dp"] for item in stable}) if stable else 0,
            "accept_rate_60s": mean([item["accept_rate"] for item in recent_decode]) if recent_decode else 0,
            "prefill_60s": median(normal_prefill) if normal_prefill else 0,
            "requests_1m": sum(ts >= now - 60 for ts in requests),
            "requests_5m": sum(ts >= now - 300 for ts in requests),
        }
        prom = self._reshape_prom(prom_raw, prom_gauges)
        prom["enabled"] = metrics_enabled
        return {
            "now": now, "service_up": service_up, "pid": pid, "uptime": self._uptime(pid),
            "service_port": self.service_port, "summary": summary, "active": active,
            "gpus": gpus, "caps": caps, "prom": prom,
            "errors": [item for item in errors if item["ts"] >= now - 300],
            "history": {
                "decode": [item for item in decode if item["ts"] >= now - 300],
                "prefill": [item for item in prefill if item["ts"] >= now - 300],
                "gpu": [item for item in gpu_history if item["ts"] >= now - 300],
            },
        }


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SGLang 实时看板</title>
<style>
:root{--bg:#07111f;--panel:#0d1b2a;--line:#20354a;--text:#e8f0f7;--muted:#89a2b8;--cyan:#42d9c8;--blue:#65a9ff;--amber:#ffc857;--red:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#102d42 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
.wrap{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}.title{font:700 25px/1.2 system-ui,sans-serif;letter-spacing:.3px}.sub{color:var(--muted);margin-top:6px}.status{display:flex;gap:9px;align-items:center;background:#0b1926;border:1px solid var(--line);padding:8px 12px;border-radius:999px}.dot{width:10px;height:10px;border-radius:50%;background:var(--red);box-shadow:0 0 12px var(--red)}.dot.up{background:var(--cyan);box-shadow:0 0 12px var(--cyan)}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.card,.panel{background:linear-gradient(145deg,rgba(16,36,53,.96),rgba(9,23,36,.96));border:1px solid var(--line);border-radius:12px}.card{padding:16px}.label{color:var(--muted);font-size:12px}.value{font:700 25px/1.3 system-ui,sans-serif;margin-top:8px}.unit{font-size:12px;color:var(--muted);font-weight:500}.grid{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-top:12px}.panel{padding:16px}.panel h2{font:650 15px system-ui,sans-serif;margin:0 0 12px}.chart{width:100%;height:230px;display:block}.gpu-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.gpu{background:#0a1825;border:1px solid #1a3043;border-radius:9px;padding:12px}.gpu-head{display:flex;justify-content:space-between}.bar{height:7px;background:#152b3d;border-radius:5px;overflow:hidden;margin:9px 0}.fill{height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));transition:width .3s}.details{color:var(--muted);font-size:12px;display:flex;justify-content:space-between}.foot{color:var(--muted);margin-top:12px;display:flex;justify-content:space-between}.error{color:var(--red);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{color:var(--muted);padding:25px 0;text-align:center}
@media(max-width:1000px){.cards{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}}@media(max-width:600px){.wrap{padding:14px}.cards{grid-template-columns:repeat(2,1fr)}.gpu-grid{grid-template-columns:1fr}.title{font-size:20px}}
</style></head><body><div class="wrap">
<div class="top"><div><div class="title">SGLang · 实时看板</div><div class="sub">由 sgtop-server 从日志 + /metrics 抓取,只读</div></div><div class="status"><span id="dot" class="dot"></span><span id="state">连接中</span></div></div>
<div class="cards">
 <div class="card"><div class="label">实时生成吞吐</div><div class="value" id="decodeNow">—</div></div>
 <div class="card"><div class="label">单请求速度 · 60s</div><div class="value" id="perReq">—</div></div>
 <div class="card"><div class="label">稳定吞吐 · 60s</div><div class="value" id="stable">—</div></div>
 <div class="card"><div class="label">Prefill 中位数 · 60s</div><div class="value" id="prefill">—</div></div>
 <div class="card"><div class="label">并发 / 排队</div><div class="value" id="rq">—</div></div>
 <div class="card"><div class="label">NEXTN 接受率 · 60s</div><div class="value" id="accept">—</div></div>
</div>
<div class="grid"><section class="panel"><h2>最近 5 分钟生成吞吐 · tokens/s</h2><canvas id="decodeChart" class="chart"></canvas></section><section class="panel"><h2>GPU 实时状态</h2><div id="gpus" class="gpu-grid"><div class="empty">等待数据</div></div></section></div>
<div class="grid"><section class="panel"><h2>最近 5 分钟 GPU SM 利用率</h2><canvas id="gpuChart" class="chart"></canvas></section><section class="panel"><h2>运行信息</h2><div id="info" class="empty">等待数据</div><div id="errors"></div></section></div>
<div class="foot"><span>每 2 秒自动刷新 · 只读监控，不产生推理流量</span><span id="updated">—</span></div>
</div><script>
const $=id=>document.getElementById(id), num=(v,d=0)=>Number(v||0).toFixed(d);
function setVal(id,v,unit){$(id).innerHTML=`${v} <span class="unit">${unit}</span>`}
function chart(canvas, series, maxY, suffix){
 const ratio=devicePixelRatio||1, box=canvas.getBoundingClientRect(); canvas.width=box.width*ratio; canvas.height=box.height*ratio;
 const c=canvas.getContext('2d'); c.scale(ratio,ratio); const w=box.width,h=box.height,p={l:43,r:12,t:8,b:25}; c.clearRect(0,0,w,h);
 c.strokeStyle='#20354a';c.fillStyle='#7891a7';c.font='11px monospace';c.lineWidth=1;
 for(let i=0;i<=4;i++){let y=p.t+(h-p.t-p.b)*i/4;c.beginPath();c.moveTo(p.l,y);c.lineTo(w-p.r,y);c.stroke();c.fillText(num(maxY*(1-i/4)),3,y+4)}
 const now=Date.now()/1000,min=now-300; c.fillText('-5m',p.l,h-6);c.fillText('now',w-p.r-23,h-6);
 for(const s of series){c.strokeStyle=s.color;c.lineWidth=2;c.beginPath();let begun=false;for(const q of s.points){if(q.x<min)continue;let x=p.l+(q.x-min)/300*(w-p.l-p.r),y=p.t+(1-Math.min(q.y,maxY)/maxY)*(h-p.t-p.b);if(!begun){c.moveTo(x,y);begun=true}else c.lineTo(x,y)}c.stroke()}
}
async function refresh(){try{
 const d=await fetch('/api/status',{cache:'no-store'}).then(r=>r.json()),s=d.summary;
 $('dot').className='dot '+(d.service_up?'up':'');$('state').textContent=d.service_up?`在线 · PID ${d.pid}`:'服务离线';
 setVal('decodeNow',num(s.decode_now,0),'tok/s');setVal('perReq',num(s.per_request_60s,1),'tok/s');setVal('stable',num(s.stable_decode_60s,0),'tok/s');setVal('prefill',num(s.prefill_60s,0),'tok/s');setVal('rq',`${s.running_now} / ${s.queue_now}`,'运行 / 排队');setVal('accept',num(s.accept_rate_60s*100,1),'%');
 $('gpus').innerHTML=d.gpus.map(g=>`<div class="gpu"><div class="gpu-head"><b>GPU ${g.index}</b><span>${num(g.util)}%</span></div><div class="bar"><div class="fill" style="width:${g.util}%"></div></div><div class="details"><span>${num(g.mem_used/1024,1)}/${num(g.mem_total/1024,1)} GB</span><span>${num(g.power)} W · ${num(g.temp)}°C</span></div></div>`).join('')||'<div class="empty">无 GPU 数据</div>';
 const up=d.uptime||0,h=Math.floor(up/3600),m=Math.floor(up%3600/60);$('info').innerHTML=`<div class="details"><span>服务端口</span><b>:${d.service_port}</b></div><div class="details"><span>运行时间</span><b>${h}h ${m}m</b></div><div class="details"><span>完成请求</span><b>${s.requests_1m}/min · ${s.requests_5m}/5min</b></div>`;
 $('errors').innerHTML=d.errors.length?`<div class="error" title="${d.errors.at(-1).text.replaceAll('"','&quot;')}">近 5 分钟错误：${d.errors.length} · ${d.errors.at(-1).text}</div>`:'<div class="sub">近 5 分钟无错误</div>';
 const ds=['DP0','DP1'].map((dp,i)=>({color:['#42d9c8','#65a9ff'][i],points:d.history.decode.filter(x=>x.dp===dp).map(x=>({x:x.ts,y:x.throughput}))}));let maxD=Math.max(200,...d.history.decode.map(x=>x.throughput))*1.1;chart($('decodeChart'),ds,maxD,'tok/s');
 const colors=['#42d9c8','#65a9ff','#ffc857','#e889ff'];const gs=(d.gpus||[]).map((g,i)=>({color:colors[i],points:d.history.gpu.map(x=>({x:x.ts,y:x.util[i]||0}))}));chart($('gpuChart'),gs,100,'%');
 $('updated').textContent='更新于 '+new Date(d.now*1000).toLocaleTimeString();
 }catch(e){$('state').textContent='看板连接失败';$('dot').className='dot'}}
refresh();setInterval(refresh,2000);addEventListener('resize',refresh);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    monitor: Monitor

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            body = json.dumps(self.monitor.snapshot(), ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only sidecar dashboard for an sglang deployment. Tails "
        "sglang's own stdout/stderr log (redirected to a file) for per-DP decode/"
        "prefill/queue stats, samples nvidia-smi, and — if the sglang process was "
        "started with --enable-metrics — scrapes its Prometheus /metrics endpoint "
        "for TTFT/E2E/queue-time/cache-hit-rate. Exposes it all as a small HTML "
        "page and a JSON API (/api/status) that `sgtop` (or your own tooling) "
        "reads. Run this next to (or with network access to) your sglang server."
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind address for this dashboard's own HTTP server")
    parser.add_argument("--port", type=int, default=30001, help="port for this dashboard's own HTTP server")
    parser.add_argument("--service-port", type=int, default=30000, help="port sglang's launch_server is listening on")
    parser.add_argument("--log", required=True,
                         help="path to sglang's stdout/stderr log file (redirect it there when you launch sglang, "
                              "e.g. `python -m sglang.launch_server ... > server.log 2>&1`)")
    parser.add_argument("--interval", type=float, default=2.0, help="how often to sample nvidia-smi and /metrics, in seconds")
    args = parser.parse_args()
    monitor = Monitor(args.log, args.service_port, args.interval)
    monitor.start()
    Handler.monitor = monitor
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"sgtop-server listening on http://{args.host}:{args.port}  "
          f"(sglang log: {args.log}, sglang port: {args.service_port})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
