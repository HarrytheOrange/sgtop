"""Local nvidia-smi sampling — shared by sgtop-server and sgtop's direct
client. "Local" is the operative word: this only ever sees the GPUs on
whatever machine the calling process itself is running on, since nvidia-smi
has no concept of a remote host. sgtop's direct mode uses this so that
running `sgtop` right on the sglang box gets a GPU panel with zero extra
processes; it's a no-op (empty list) anywhere nvidia-smi isn't on PATH,
which is exactly what you want when viewing a *remote* sglang instance from
a machine with no GPUs of its own.
"""
from __future__ import annotations

import subprocess

_COMMAND = [
    "nvidia-smi",
    "--query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,temperature.gpu",
    "--format=csv,noheader,nounits",
]


def query_local_gpus(timeout: float = 2.0) -> list[dict]:
    gpus: list[dict] = []
    try:
        output = subprocess.run(_COMMAND, capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return gpus
    for row in output.splitlines():
        fields = [part.strip() for part in row.split(",")]
        if len(fields) != 8:
            continue
        try:
            gpus.append({
                "index": int(fields[0]), "name": fields[1], "util": float(fields[2]),
                "mem_util": float(fields[3]), "mem_used": float(fields[4]),
                "mem_total": float(fields[5]), "power": float(fields[6]), "temp": float(fields[7]),
            })
        except ValueError:
            continue
    return gpus
