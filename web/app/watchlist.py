"""自選股清單資料：給 /api/watchlist 與靜態匯出用。

清單本身存在使用者瀏覽器（localStorage），這裡只匯出追蹤範圍內每檔的今日價、
符合模式、月營收年增，讓靜態站也能顯示。不改選股規則；缺值標明、不補造。
"""
from __future__ import annotations

from typing import Optional

from .data_source import DataSource, DataUnavailable
from .hub import TIER_CLASS, _core, _num, mode_specs, mode_state
from .revenue_page import YOY_TEXT

MISSING_REV = "資料不足（本站資料庫尚無此月營收）"
# 清單頁腳註（F-001 允許的完整句；不得單獨出現「尚未公告」）
REV_FOOTNOTE = "營收以本站已入庫資料為準；沒有該月列時顯示資料不足，不代表公司尚未公告。"
DISCLAIMER = "這份清單由你在瀏覽器自行加入，不是推薦清單。符合模式只代表今天的條件判斷，不代表優劣。歷史統計，不代表未來，非投資建議。"
STORAGE_NOTE = "清單只存在這個瀏覽器，不需要帳號、也不傳到伺服器；清除瀏覽器資料或換裝置就會消失。"


def _signed_change(r: dict):
    """官方漲跌（元，含正負）。change 優先，否則 change_twd＋change_sign；無比價／缺值回 None（不當成 0）。"""
    if r.get("change") is not None:
        try:
            return float(r["change"])
        except (TypeError, ValueError):
            return None
    if r.get("change_twd") is not None:
        try:
            v = float(r["change_twd"])
        except (TypeError, ValueError):
            return None
        return -abs(v) if (str(r.get("change_sign") or "")).strip() == "-" else v
    return None


def _is_no_compare(r: dict) -> bool:
    note = str(r.get("change_note") or "")
    sign = str(r.get("change_sign") or "").strip()
    return "無比價" in note or sign == "X"


def _yoy_text(rec: Optional[dict]) -> str:
    if not rec:
        return MISSING_REV
    ys, yr = rec.get("yoy_status") or "", rec.get("yoy_reason") or ""
    if ys == "ok" and rec.get("yoy_pct") is not None:
        return ""
    if yr in YOY_TEXT:
        return YOY_TEXT[yr]
    if ys:
        return "資料不足" if ys != "ok" else "analysis 未提供年增率"
    return MISSING_REV


def _modes_by_code(core: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if core.get("fatal"):
        return out
    computed = core.get("computed") or {}
    for m in mode_specs():
        if "na" in m:
            continue
        c = computed.get(m["id"])
        if not c or "error" in c:
            continue
        state, _n = mode_state(c)
        if state == "insufficient_data":
            continue
        item = dict(id=m["id"], name=m["name"], tier=m["tier"],
                    tier_class=TIER_CLASS.get(m["tier"], "t1"))
        for code in c.get("passed") or []:
            out.setdefault(str(code), []).append(item)
    return out


def _quotes(src: DataSource, as_of: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not as_of or as_of == "—":
        return out
    try:
        rows = src.get_source_rows("daily_prices", {"price_date": as_of}, limit=1_000_000)
    except (DataUnavailable, KeyError):
        return out
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym:
            continue
        close = _num(r.get("close_twd"))
        out[sym] = dict(close=close, change=_signed_change(r), no_compare=_is_no_compare(r))
    return out


def apply_page_flags(data: dict, page_codes, href_fn=None, mode_href_fn=None) -> dict:
    """匯出後依實際有個股頁的代號標記 has_page，並改寫靜態站相對路徑（不刪列）。"""
    pages = set(page_codes or [])
    for code, row in (data.get("stocks") or {}).items():
        ok = code in pages
        row["has_page"] = ok
        row["href"] = (href_fn(code) if href_fn else f"/stock/{code}") if ok else ""
    if mode_href_fn and data.get("links"):
        modes = data["links"].get("modes") or {}
        data["links"]["modes"] = {mid: mode_href_fn(mid, path) for mid, path in modes.items()}
        if "hub" in data["links"]:
            data["links"]["hub"] = mode_href_fn("hub", data["links"]["hub"]) or data["links"]["hub"]
        if "watchlist" in data["links"]:
            data["links"]["watchlist"] = mode_href_fn("watchlist", data["links"]["watchlist"]) or "watchlist/"
    return data


def clip_to(data: dict, keep_codes) -> dict:
    """只保留匯出範圍內的股票（測試縮小範圍、或工作集合）。"""
    keep = set(keep_codes or [])
    data["stocks"] = {c: r for c, r in (data.get("stocks") or {}).items() if c in keep}
    data["count"] = len(data["stocks"])
    return data


def payload(src: DataSource, page_codes=None) -> dict:
    """追蹤範圍內每檔的精簡列。page_codes 為 None 時（線上站）視為都有個股頁。"""
    try:
        listed = list(src.universe_stocks())
    except Exception:
        listed = []
    core: dict = {}
    try:
        core = _core(src)
    except Exception as e:
        core = dict(fatal=f"{type(e).__name__}：{e}")

    as_of = core.get("as_of") or (src.meta() or {}).get("data_date") or "—"
    month = core.get("revenue_month")
    price = core.get("price") or {}
    yoy_map = core.get("yoy") or {}
    modes = _modes_by_code(core)
    quotes = _quotes(src, as_of)
    pages = None if page_codes is None else set(page_codes)

    stocks = {}
    for s in listed:
        code = s["code"]
        p, q, y = price.get(code) or {}, quotes.get(code) or {}, yoy_map.get(code)
        close = q.get("close")
        if close is None:
            close = p.get("close")
        rec = dict(
            code=code,
            name=s.get("name") or p.get("name") or code,
            market=s.get("market") or p.get("market") or "—",
            close=close,
            change=q.get("change"),
            change_pct=p.get("change_pct"),
            no_compare=bool(q.get("no_compare")),
            modes=modes.get(code) or [],
            yoy=(y or {}).get("yoy_pct") if y and y.get("yoy_status") == "ok" else None,
            yoy_status=(y or {}).get("yoy_status") or "",
            yoy_text=_yoy_text(y),
            yoy_month=month,
            has_page=True if pages is None else code in pages,
            href=(f"/stock/{code}" if pages is None or code in pages else ""),
        )
        stocks[code] = rec

    mode_links = {m["id"]: f"/hub?mode={m['id']}" for m in mode_specs() if "na" not in m}
    return dict(
        as_of=as_of,
        yoy_month=month,
        count=len(stocks),
        stocks=stocks,
        modes_error=core.get("fatal") or "",
        links={"watchlist": "/watchlist", "hub": "/hub", "modes": mode_links},
        disclaimer=DISCLAIMER,
        storage_note=STORAGE_NOTE,
        revenue_footnote=REV_FOOTNOTE,
    )
