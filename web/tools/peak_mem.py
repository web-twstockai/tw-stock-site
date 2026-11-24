"""量測一個指令（含它 fork 出來的所有子行程）的記憶體峰值與時間；可設上限，超過就整棵行程樹砍掉。

只用標準函式庫（讀 /proc），本機沒有 /usr/bin/time 也能用。用法（在 web/ 下）：
  .venv/bin/python tools/peak_mem.py --cap-mb 2500 --json /tmp/m.json -- .venv/bin/python tools/export_static.py --mode v2 ...
量到的值（每 0.2 秒取樣一次）：
- peak_pss_mb：同一時刻整棵行程樹 PSS 合計的最大值（fork 共用的頁面依比例分攤，最接近「機器實際要多少記憶體」）
- peak_rss_sum_mb：同一時刻 RSS 合計的最大值（共用頁面重複計算，偏高的上界；Actions runner 約 7 GB／16 GB 時拿這個保守比）
- peak_single_rss_mb：單一行程 RSS 的最大值（主行程或最胖的工作行程）
- peak_procs：同時存在的行程數
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time


def _children_map() -> dict:
    kids: dict = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                s = f.read().decode("utf-8", "replace")
            ppid = int(s[s.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def tree(pid: int) -> list[int]:
    kids = _children_map()
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


def mem_kb(pid: int) -> tuple[int, int]:
    """(rss_kb, pss_kb)；行程已結束回 (0, 0)。"""
    rss = pss = 0
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for ln in f:
                if ln.startswith("Rss:"):
                    rss = int(ln.split()[1])
                elif ln.startswith("Pss:"):
                    pss = int(ln.split()[1])
    except OSError:
        pass
    return rss, pss


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap-mb", type=float, default=0, help="PSS 合計超過就砍掉整棵行程樹（0＝不設）")
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--json", default="")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        ap.error("缺少要量測的指令")
    t0 = time.time()
    p = subprocess.Popen(cmd, start_new_session=True)
    peak = {"pss": 0, "rss": 0, "single": 0, "procs": 0}
    timeline = []
    killed = False
    while p.poll() is None:
        pids = tree(p.pid)
        rs = [mem_kb(x) for x in pids]
        pss, rss = sum(x[1] for x in rs), sum(x[0] for x in rs)
        peak["pss"] = max(peak["pss"], pss)
        peak["rss"] = max(peak["rss"], rss)
        peak["single"] = max([peak["single"]] + [x[0] for x in rs])
        peak["procs"] = max(peak["procs"], len(pids))
        el = time.time() - t0
        if not timeline or el - timeline[-1][0] >= 5:
            timeline.append((round(el, 1), round(pss / 1024), len(pids)))
        if a.cap_mb and pss / 1024 > a.cap_mb:
            print(f"[peak_mem] PSS 合計 {pss / 1024:.0f} MB 超過上限 {a.cap_mb:.0f} MB，砍掉行程樹", file=sys.stderr)
            killed = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            break
        time.sleep(a.interval)
    rc = p.wait()
    res = {"cmd": " ".join(cmd), "exit_code": rc, "killed_by_cap": killed, "seconds": round(time.time() - t0, 1),
           "peak_pss_mb": round(peak["pss"] / 1024), "peak_rss_sum_mb": round(peak["rss"] / 1024),
           "peak_single_rss_mb": round(peak["single"] / 1024), "peak_procs": peak["procs"],
           "timeline_s_pssmb_procs": timeline}
    print("[peak_mem] " + json.dumps({k: v for k, v in res.items() if k != "timeline_s_pssmb_procs"}, ensure_ascii=False),
          file=sys.stderr)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    return 3 if killed else rc


if __name__ == "__main__":
    sys.exit(main())
