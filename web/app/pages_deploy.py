"""資料狀態頁「公開站」區塊：讀 deploy_pages.sh 寫的發布紀錄（web/logs/pages_last_deploy.json，唯讀）。

公開站（GitHub Pages）只在推送成功時更新；匯出自檢或推送失敗時停在上一版，這裡讓使用者看得到。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "logs" / "pages_last_deploy.json"
PUBLIC_URL = "https://web-twstockai.github.io/tw-stock-site/"
PAGES_REPO = Path(__file__).resolve().parents[2] / "build" / "pages-repo"   # deploy_pages.sh 的部署工作目錄（唯讀）
STATUS_TEXT = {"ok": "已推送", "no_change": "內容無變更（未推送）", "export_failed": "匯出失敗，未推送",
               "check_failed": "匯出自檢未通過，未推送", "push_failed": "推送失敗"}


def _short(ts) -> str:
    s = str(ts or "")
    return s[:16].replace("T", " ") if len(s) >= 16 else (s or "—")


def view(local_data_date: str | None, path: Path | None = None) -> dict:
    p = path or Path(os.environ.get("TWSITE_PAGES_RECORD") or DEFAULT_FILE)   # 測試可用環境變數指到暫存紀錄
    out = {"url": PUBLIC_URL, "has_record": False, "error": "", "public_data_date": "", "pushed_at": "",
           "behind": None, "last_attempt": None, "inferred": False, "generated": "", "attempt_failed": False, "local_data_date": local_data_date or ""}
    if not p.exists():
        # 尚無發布紀錄：退回讀部署工作目錄（每次部署會先對齊遠端，內容＝最後推送版）的 build-info.json，標示為推定
        try:
            b = json.loads((PAGES_REPO / "build-info.json").read_text("utf-8")) if path is None and \
                not os.environ.get("TWSITE_PAGES_RECORD") else None
        except Exception:
            b = None
        if b and b.get("data_date"):
            out.update(public_data_date=b["data_date"], generated=_short(b.get("generated_at_taipei")), inferred=True)
            if local_data_date:
                out["behind"] = out["public_data_date"] < local_data_date
        return out
    try:
        rec = json.loads(p.read_text("utf-8"))
    except Exception as e:
        out["error"] = f"發布紀錄無法讀取：{type(e).__name__}: {e}"
        return out
    ok = rec.get("last_success") or {}
    la = rec.get("last_attempt") or {}
    out["has_record"] = bool(ok or la)
    if ok:
        out.update(public_data_date=ok.get("data_date") or "", pushed_at=_short(ok.get("pushed_at")),
                   universe_label=ok.get("universe_label") or "", generated=_short(ok.get("generated_at_taipei")),
                   confirmed_at=_short(ok.get("confirmed_at")) if ok.get("confirmed_at") else "")
        if local_data_date and out["public_data_date"]:
            out["behind"] = out["public_data_date"] < local_data_date
    if la:
        out["last_attempt"] = {"at": _short(la.get("at")), "status": la.get("status") or "",
                               "status_text": STATUS_TEXT.get(la.get("status"), la.get("status") or ""),
                               "reason": la.get("reason") or "", "data_date": la.get("data_date") or ""}
        out["attempt_failed"] = la.get("status") not in ("ok", "no_change")
    return out
