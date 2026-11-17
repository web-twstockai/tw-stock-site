"""job_runs.error_summary 後端彙總（HANDOFF §16.3）：把「2330 營收僅 35 月（目標>=36）; 2454 營收僅 35 月…」
這類同訊息、不同代號的重複句，合併成「12 檔營收僅 35 月（目標>=36）：2330、2454…」。只重排文字，不改內容、不丟項目。"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"\s*[;；\n]\s*")
_ITEM = re.compile(r"^(\d{4,6}[A-Z]?)\s+(.+)$")


def group_errors(text: str) -> list[dict]:
    """→ [{"message", "symbols": [...], "count", "text"}]，依首次出現順序；沒有代號的句子 symbols=[]，重複句累計 count。"""
    groups: dict[str, dict] = {}
    for part in _SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        m = _ITEM.match(part)
        msg, sym = (m.group(2).strip(), m.group(1)) if m else (part, None)
        g = groups.setdefault(msg, {"message": msg, "symbols": [], "count": 0})
        g["count"] += 1
        if sym and sym not in g["symbols"]:
            g["symbols"].append(sym)
    for g in groups.values():
        if g["symbols"]:
            g["text"] = f"{len(g['symbols'])} 檔{g['message']}：{'、'.join(g['symbols'])}"
        else:
            g["text"] = g["message"] + (f"（×{g['count']}）" if g["count"] > 1 else "")
    return list(groups.values())


def summarize(text: str) -> str:
    return "；".join(g["text"] for g in group_errors(text))
