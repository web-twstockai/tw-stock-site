"""回補時限與單一路程互斥。"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from pipeline.dates import TZ, now_iso

DEFAULT_STOP = os.environ.get("PIPELINE_STOP_BEFORE", "")


def parse_stop_before(s: str | None) -> datetime | None:
    if not s:
        s = DEFAULT_STOP
    if not s:
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def should_stop(stop_before: datetime | None) -> bool:
    if stop_before is None:
        return False
    return datetime.now(TZ) >= stop_before


def other_pipeline_pids() -> list[int]:
    """其他 python -m pipeline.run 行程（不含自己）。"""
    me = os.getpid()
    out: list[int] = []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,cmd"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if "pipeline.run" not in line and "-m pipeline" not in line:
                continue
            if "rg " in line or "grep " in line:
                continue
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid != me and pid != os.getppid():
                out.append(pid)
    except OSError:
        pass
    return out


def assert_no_other_pipeline() -> None:
    pids = other_pipeline_pids()
    if pids:
        raise RuntimeError(f"another pipeline.run is running: pids={pids} at {now_iso()}")
