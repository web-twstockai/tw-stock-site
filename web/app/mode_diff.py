"""每日模式異動：各模式符合名單比對前一交易日的新進／掉出。

只做 PASS 名單的集合差集，不改選股規則。比對資料以匯出時寫下的快照為主；
沒有快照時，匯出階段可對有上限的追蹤範圍重算前一日，超時或全市場則顯示資料不足。
網頁請求路徑不重算。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

TPE = timezone(timedelta(hours=8))
SNAPSHOT_VERSION = 1
SNAPSHOT_NAME = "mode-snapshot.json"
PREV_NAME = "mode-snapshot.prev.json"
PUBLIC_SNAPSHOT_URL = "https://web-twstockai.github.io/tw-stock-site/data/mode-snapshot.json"
DEFAULT_RECOMPUTE_MAX = 80
DEFAULT_RECOMPUTE_SEC = 90
HUB_MAX_CODES = 8  # 篩選中心摘要每側最多列出的代號數

# 匯出前 prepare_for_export() 解析到的前一日快照（請求時優先使用）
_PREPARED: Optional[dict] = None

DISCLAIMER = "歷史統計，不代表未來，非投資建議"
SIGNAL_NOTE = (
    "「新進」只表示今天新符合該模式條件，「掉出」只表示今天不再符合；"
    "這不是買進或賣出建議。歷史 5 日勝率尚未達標、或仍顯示「計算中」時，"
    "異動同樣只是條件符合與否的變化，不代表比較容易獲利。"
)
BACKFILL_NOTE = (
    "若之後回補行情或除權息調整，用價格重算的「前一日」結果可能與當初不同。"
    "本頁有快照時以當時匯出的名單為準。"
)
PENDING_REASON = (
    "尚無前一交易日快照；今日符合名單已寫入，下次更新後即可比對。"
)


def _run_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".run"


def _now_taipei() -> str:
    return datetime.now(TPE).strftime("%Y-%m-%d %H:%M")


def universe_name(src) -> str:
    try:
        return str(src.universe.name or "")
    except Exception:
        return ""


def universe_label(src) -> str:
    try:
        return str(src.universe.label or src.universe.name or "")
    except Exception:
        return ""


def extract_passes(core: dict) -> tuple[dict[str, list[str]], dict[str, str]]:
    """從 hub._core 取出各模式符合名單與狀態。不改名單內容。"""
    passes: dict[str, list[str]] = {}
    status: dict[str, str] = {}
    computed = core.get("computed") or {}
    for mid, rec in computed.items():
        if not isinstance(rec, dict):
            status[mid] = "error"
            passes[mid] = []
            continue
        if rec.get("error"):
            status[mid] = "error"
            passes[mid] = []
            continue
        # mode_state 尚未寫入 computed；以是否有 passed 為準。NA 模式通常沒進入 computed。
        codes = [str(c) for c in (rec.get("passed") or [])]
        passes[mid] = sorted(set(codes))
        status[mid] = "ok"
    return passes, status


def snapshot_payload(src, core: dict) -> dict:
    passes, status = extract_passes(core)
    return {
        "version": SNAPSHOT_VERSION,
        "as_of": core.get("as_of") or "",
        "universe": universe_name(src),
        "universe_label": universe_label(src),
        "generated_at": _now_taipei(),
        "passes": passes,
        "status": status,
    }


def validate_snapshot(data: Any) -> Optional[str]:
    """通過回傳 None，否則回傳原因（不使用該檔）。"""
    if not isinstance(data, dict):
        return "快照格式不是物件"
    if data.get("version") not in (None, SNAPSHOT_VERSION, 1):
        return f"快照版本不被支援（{data.get('version')}）"
    if not data.get("as_of"):
        return "快照沒有資料日"
    if not isinstance(data.get("passes"), dict):
        return "快照沒有各模式名單"
    return None


def read_snapshot(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if validate_snapshot(data):
        return None
    data["_path"] = str(path)
    return data


def write_snapshot(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    path.write_text(json.dumps(clean, ensure_ascii=False, separators=(",", ":")), "utf-8")
    return path


def previous_trade_day(src, today: str) -> Optional[str]:
    try:
        days = list(src._trade_days() or [])
    except Exception:
        days = []
    prev = [d for d in days if d and d < today]
    return max(prev) if prev else None


def _candidate_paths(out_dir: Optional[Path] = None) -> list[Path]:
    paths: list[Path] = []
    env = (os.environ.get("TWSITE_MODE_SNAPSHOT") or "").strip()
    if env and env.lower() not in ("none", "0", "off"):
        paths.append(Path(env))
    if out_dir is not None:
        paths.append(out_dir / "data" / SNAPSHOT_NAME)
        paths.append(out_dir / "data" / PREV_NAME)
    run = _run_dir()
    paths.append(run / SNAPSHOT_NAME)
    paths.append(run / PREV_NAME)
    # 去重但保序
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        k = str(p.resolve()) if p.exists() else str(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _fetch_url(url: str, timeout: float = 5.0) -> Optional[dict]:
    if not url or url.lower() in ("none", "0", "off"):
        return None
    try:
        req = Request(url, headers={"User-Agent": "tw-stock-site-mode-diff/1"})
        with urlopen(req, timeout=timeout) as r:  # noqa: S310 公開靜態檔
            data = json.loads(r.read().decode("utf-8"))
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return None
    if validate_snapshot(data):
        return None
    data["_path"] = url
    data["_source"] = "url"
    return data


def snapshot_url() -> Optional[str]:
    raw = os.environ.get("TWSITE_MODE_SNAPSHOT_URL")
    if raw is not None:
        s = raw.strip()
        return None if s.lower() in ("none", "0", "off", "") else s
    if os.environ.get("GITHUB_ACTIONS"):
        return PUBLIC_SNAPSHOT_URL
    return None


def pick_snapshot(today: str, universe: str, candidates: list[dict]) -> tuple[Optional[dict], str]:
    """選出資料日早於今日、追蹤範圍相符的最新快照。"""
    usable = []
    reasons = []
    for snap in candidates:
        if not snap:
            continue
        as_of = str(snap.get("as_of") or "")
        if not as_of or as_of >= today:
            reasons.append(f"{snap.get('_path', '快照')} 資料日 {as_of or '（空）'} 不是今日之前")
            continue
        su = str(snap.get("universe") or "")
        if universe and su and su != universe:
            reasons.append(f"{snap.get('_path', '快照')} 追蹤範圍是「{su}」，與本次「{universe}」不同")
            continue
        usable.append(snap)
    if usable:
        best = max(usable, key=lambda s: str(s.get("as_of") or ""))
        return best, "snapshot"
    return None, ("；".join(reasons) if reasons else "找不到可用的前一日快照")


def load_prev_snapshot(today: str, universe: str, out_dir: Optional[Path] = None) -> tuple[Optional[dict], str]:
    if _PREPARED is not None:
        snap, how = pick_snapshot(today, universe, [_PREPARED])
        if snap is not None:
            return snap, how or "snapshot"
    cands: list[dict] = []
    for p in _candidate_paths(out_dir):
        if p.is_file():
            s = read_snapshot(p)
            if s:
                cands.append(s)
    url = snapshot_url()
    if url:
        fetched = _fetch_url(url)
        if fetched:
            cands.append(fetched)
    return pick_snapshot(today, universe, cands)


def prepare_for_export(out_dir: Optional[Path], src=None, core: Optional[dict] = None) -> Optional[dict]:
    """匯出開始時呼叫：從既有匯出目錄／本機快取／（CI）公開站取出前一日快照，供後續頁面比對。

    若目標目錄即將被覆蓋，先把裡面的快照複製到 `.run`，避免遺失。
    無快照且範圍允許時，才重算前一交易日（有檔數與秒數上限）。
    """
    global _PREPARED
    _PREPARED = None
    today = (core or {}).get("as_of") or ""
    uni = universe_name(src) if src is not None else ""
    # 覆蓋前先把舊站快照搬到本機快取
    if out_dir is not None:
        for name in (SNAPSHOT_NAME, PREV_NAME):
            srcp = out_dir / "data" / name
            if srcp.is_file():
                dest = _run_dir() / (PREV_NAME if name == SNAPSHOT_NAME else name)
                try:
                    data = read_snapshot(srcp)
                    if data:
                        write_snapshot(dest, data)
                except OSError:
                    pass
    snap, how = load_prev_snapshot(today or "9999-99-99", uni, out_dir)
    if snap is None and src is not None and today:
        snap = maybe_recompute_for_export(src, today)
        how = "recompute" if snap else how
    if snap is not None:
        snap["_how"] = how
        _PREPARED = snap
        # 讓工作行程（fork）也能看到
        try:
            p = _run_dir() / PREV_NAME
            write_snapshot(p, snap)
            os.environ["TWSITE_MODE_SNAPSHOT"] = str(p)
        except OSError:
            pass
    return snap


def clear_prepared() -> None:
    global _PREPARED
    _PREPARED = None


def _recompute_allowed(src) -> tuple[bool, str]:
    flag = (os.environ.get("TWSITE_MODE_DIFF_RECOMPUTE") or "auto").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False, "已設定不重算前一交易日（TWSITE_MODE_DIFF_RECOMPUTE=0）"
    try:
        symbols = src.universe.symbols
    except Exception:
        symbols = None
    if symbols is None:
        if flag in ("1", "true", "yes", "on"):
            return True, ""
        return False, "全市場重算前一日會明顯拉長匯出時間，本次未重算"
    try:
        cap = int(os.environ.get("TWSITE_MODE_DIFF_RECOMPUTE_MAX") or DEFAULT_RECOMPUTE_MAX)
    except ValueError:
        cap = DEFAULT_RECOMPUTE_MAX
    n = len(symbols)
    if n > cap and flag not in ("1", "true", "yes", "on"):
        return False, f"追蹤範圍 {n} 檔超過重算上限 {cap} 檔，本次未重算"
    return True, ""


def maybe_recompute_for_export(src, today: str) -> Optional[dict]:
    """只給匯出路徑呼叫。成功則回傳與快照相同結構的資料。"""
    ok, why = _recompute_allowed(src)
    if not ok:
        return None
    prev = previous_trade_day(src, today)
    if not prev:
        return None
    try:
        sec = float(os.environ.get("TWSITE_MODE_DIFF_RECOMPUTE_SEC") or DEFAULT_RECOMPUTE_SEC)
    except ValueError:
        sec = DEFAULT_RECOMPUTE_SEC
    t0 = time.time()
    try:
        from . import hub as H
        core = H._compute_core(src, as_of=prev)
    except Exception:
        return None
    elapsed = time.time() - t0
    if core.get("fatal"):
        return None
    if sec > 0 and elapsed > sec:
        # 已算完但超過預算：仍採用結果（算力已經花了），註記逾時由呼叫端決定。
        # 為遵守「有上限」，超過預算就丟掉，避免之後依賴一次過長的重算。
        return None
    if (core.get("as_of") or "") != prev:
        return None
    payload = snapshot_payload(src, core)
    payload["_source"] = "recompute"
    payload["_seconds"] = round(elapsed, 2)
    return payload


def _names(src) -> dict[str, str]:
    out = {}
    try:
        for s in src.universe_stocks() or []:
            out[str(s["code"])] = s.get("name") or ""
    except Exception:
        pass
    if not out:
        try:
            for s in src.list_stocks() or []:
                out[str(s.get("code") or s.get("symbol") or "")] = s.get("name") or ""
        except Exception:
            pass
    return out


def _specs():
    from . import hub as H
    from . import modes_hub as MH
    specs = list(H.mode_specs())
    # 補上 computed 裡有、specs 沒列到的（防漏），但一般 21 個都會在
    return specs, MH


def _stock(code: str, names: dict[str, str]) -> dict:
    return {"code": code, "name": names.get(code) or "", "href": f"/stock/{code}"}


def _mode_rows(core: dict, prev: Optional[dict], names: dict[str, str],
               today: str, prev_as_of: Optional[str]) -> list[dict]:
    from .hub import mode_state
    today_passes, today_status = extract_passes(core)
    computed = core.get("computed") or {}
    prev_passes = (prev or {}).get("passes") or {}
    prev_status = (prev or {}).get("status") or {}
    rows = []
    specs, _MH = _specs()
    for spec in specs:
        mid = spec["id"]
        rec = computed.get(mid) or {}
        available = "na" not in spec  # 與成績單相同：不可計算模式不給結果連結
        item = {
            "id": mid,
            "name": spec.get("name") or mid,
            "tier": spec.get("tier") or "基本",
            "available": available,
            "href": f"/hub?mode={mid}#results" if available else "",
            "entered": [],
            "exited": [],
            "entered_preview": [],
            "exited_preview": [],
            "n_entered": 0,
            "n_exited": 0,
            "n_today": 0,
            "n_prev": 0,
            "status": "ok",
            "reason": "",
        }
        if not available:
            item["status"] = "na"
            item["reason"] = spec.get("na") or "本模式目前無法計算，不比較異動"
            rows.append(item)
            continue
        if rec.get("error") or today_status.get(mid) == "error":
            item["status"] = "error"
            item["reason"] = rec.get("error") or "本模式今日無法計算，不比較異動"
            rows.append(item)
            continue
        # 覆蓋率不足：今日沒有可用的符合名單（與篩選中心同一份 computed）
        try:
            st, _n = mode_state(rec) if rec and "coverage_ratio" in rec and "passed" in rec else ("ok", len(today_passes.get(mid) or []))
        except (KeyError, TypeError):
            st, _n = "ok", len(today_passes.get(mid) or [])
        if st == "insufficient_data":
            item["status"] = "insufficient"
            item["reason"] = "今日可評估覆蓋率未達門檻，不比較異動"
            rows.append(item)
            continue
        today_set = set(today_passes.get(mid) or [])
        # 對照日該模式若當時也是 error／無名單，passes 為空 → 全部今日符合會變成「新進」。
        # 若對照快照明確標記非 ok，則不比對。
        ps = prev_status.get(mid)
        if prev is None:
            item["status"] = "pending"
            item["reason"] = PENDING_REASON
            rows.append(item)
            continue
        if ps and ps not in ("ok",):
            item["status"] = "insufficient"
            item["reason"] = f"對照日（{prev_as_of}）此模式沒有可用名單，不比較異動"
            rows.append(item)
            continue
        prev_set = set(str(c) for c in (prev_passes.get(mid) or []))
        entered = sorted(today_set - prev_set)
        exited = sorted(prev_set - today_set)
        item.update(
            entered=[_stock(c, names) for c in entered],
            exited=[_stock(c, names) for c in exited],
            entered_preview=[_stock(c, names) for c in entered[:HUB_MAX_CODES]],
            exited_preview=[_stock(c, names) for c in exited[:HUB_MAX_CODES]],
            n_entered=len(entered),
            n_exited=len(exited),
            n_today=len(today_set),
            n_prev=len(prev_set),
            status="ok",
        )
        rows.append(item)
    return rows


def _public_missing_reason(src, today: str, how_detail: str) -> str:
    """給畫面看的原因：只寫中文，不出現檔案路徑或環境變數名稱。"""
    prev = previous_trade_day(src, today)
    parts = ["尚無可用的前一交易日快照"]
    detail = how_detail or ""
    if "範圍" in detail or "不同" in detail:
        parts.append("既有快照的追蹤範圍與本次不同")
    elif "不是今日之前" in detail:
        parts.append("找到的快照與今日同一資料日，不能當作前一交易日")
    allowed, why = _recompute_allowed(src)
    if not allowed:
        if "全市場" in why:
            parts.append("全市場重算前一日會明顯拉長匯出時間，本次未重算")
        elif "上限" in why:
            parts.append("追蹤範圍超過重算上限，本次未重算")
    # 網頁請求路徑不會重算；只有匯出階段在範圍允許時才試。沒試成就不要寫「重算未成功」。
    if prev:
        parts.append(f"應對照的前一交易日是 {prev}")
    parts.append("今日符合名單已寫入，下次更新後即可比對")
    return "；".join(parts) + "。"


def build(src, core: Optional[dict] = None) -> dict:
    """給篩選中心與／changes 用的畫面資料。請求路徑只讀快照，不重算。"""
    from . import hub as H
    if core is None:
        core = H._core(src)
    today = core.get("as_of") or "—"
    uni = universe_name(src)
    empty = {
        "today": today,
        "prev": None,
        "state": "insufficient",
        "reason": PENDING_REASON,
        "source": "none",
        "stale_note": "",
        "modes": [],
        "n_entered": 0,
        "n_exited": 0,
        "disclaimer": DISCLAIMER,
        "signal_note": SIGNAL_NOTE,
        "backfill_note": BACKFILL_NOTE,
        "universe_label": universe_label(src),
        "href": "/changes",
    }
    if core.get("fatal"):
        empty["reason"] = f"今日模式結果無法計算（{core['fatal']}），因此無法比較異動。"
        return empty
    prev, how = load_prev_snapshot(today, uni)
    names = _names(src)
    expected = previous_trade_day(src, today)
    if prev is None:
        empty["reason"] = _public_missing_reason(src, today, how)
        empty["modes"] = _mode_rows(core, None, names, today, None)
        return empty
    prev_as_of = str(prev.get("as_of") or "")
    stale = ""
    if expected and prev_as_of != expected:
        stale = f"對照的快照資料日是 {prev_as_of}，不是緊鄰的前一交易日 {expected}。"
    modes = _mode_rows(core, prev, names, today, prev_as_of)
    n_in = sum(m["n_entered"] for m in modes if m["status"] == "ok")
    n_out = sum(m["n_exited"] for m in modes if m["status"] == "ok")
    src_label = "recompute" if (prev.get("_source") == "recompute" or prev.get("_how") == "recompute") else "snapshot"
    return {
        "today": today,
        "prev": prev_as_of,
        "state": "ok",
        "reason": "",
        "source": src_label,
        "stale_note": stale,
        "modes": modes,
        "n_entered": n_in,
        "n_exited": n_out,
        "disclaimer": DISCLAIMER,
        "signal_note": SIGNAL_NOTE,
        "backfill_note": BACKFILL_NOTE,
        "universe_label": universe_label(src),
        "href": "/changes",
    }


def write_today_snapshot(dest_dir: Path, src, core: Optional[dict] = None) -> Path:
    """把今日名單寫入匯出目錄與本機 `.run`。若 `.run` 已有較早資料日的快照，先改名為 .prev。"""
    from . import hub as H
    if core is None:
        core = H._core(src)
    payload = snapshot_payload(src, core)
    dest = dest_dir / "data" / SNAPSHOT_NAME
    write_snapshot(dest, payload)
    run = _run_dir()
    current = run / SNAPSHOT_NAME
    if current.is_file():
        old = read_snapshot(current)
        if old and str(old.get("as_of") or "") and str(old.get("as_of")) < str(payload.get("as_of") or ""):
            write_snapshot(run / PREV_NAME, old)
    write_snapshot(current, payload)
    return dest
