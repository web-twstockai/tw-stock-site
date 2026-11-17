"""公開站發布紀錄（deploy_pages.sh 呼叫；網站資料狀態頁讀取顯示「公開站資料日期」）。

用法：pages_record.py --status ok|no_change|export_failed|check_failed|push_failed [--out 匯出目錄]
                      [--reason 文字] [--commit SHA] [--file web/logs/pages_last_deploy.json]
- 每次都更新 last_attempt；status=ok（已推送）才更新 last_success（公開站目前的版本）。
- no_change：內容與公開站相同（未推送），公開站資料日期＝本次資料日期，也更新 last_success.data_date 確認時間。
- 原子寫入（暫存檔＋rename），不含權杖或任何秘密。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
DEFAULT_FILE = WEB / "logs" / "pages_last_deploy.json"
TPE = dt.timezone(dt.timedelta(hours=8))
STATUSES = ("ok", "no_change", "export_failed", "check_failed", "push_failed")


def _build_info(out: Path | None) -> dict:
    try:
        return json.loads((out / "build-info.json").read_text("utf-8")) if out else {}
    except Exception:
        return {}


def record(status: str, out: Path | None = None, reason: str = "", commit: str = "", path: Path = DEFAULT_FILE,
           now: dt.datetime | None = None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status 必須是 {STATUSES}")
    now = (now or dt.datetime.now(TPE)).astimezone(TPE)
    try:
        rec = json.loads(path.read_text("utf-8"))
    except Exception:
        rec = {}
    info = _build_info(out)
    attempt = {"at": now.isoformat(timespec="seconds"), "status": status, "reason": reason[:500],
               "data_date": info.get("data_date"), "generated_at_taipei": info.get("generated_at_taipei"),
               "universe_label": info.get("universe_label") or "", "universe_count": len(info.get("tracking_universe")
                                                                                        or info.get("universe") or [])}
    rec["last_attempt"] = attempt
    if status == "ok":
        rec["last_success"] = dict(attempt, pushed_at=attempt["at"], commit=commit)
    elif status == "no_change" and rec.get("last_success"):
        rec["last_success"]["confirmed_at"] = attempt["at"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pages_last_deploy-", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True, choices=STATUSES)
    ap.add_argument("--out")
    ap.add_argument("--reason", default="")
    ap.add_argument("--commit", default="")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    a = ap.parse_args(argv)
    record(a.status, Path(a.out) if a.out else None, a.reason, a.commit, Path(a.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
