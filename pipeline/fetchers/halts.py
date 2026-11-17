"""停牌／暫停交易清單。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from urllib.parse import urlencode

from pipeline.dates import ad_yyyymmdd, now_iso, roc_ymd_to_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import tpex, twse

log = logging.getLogger("pipeline.halts")


def fetch_twse(conn: sqlite3.Connection, start, end) -> int:
    url = (
        "https://www.twse.com.tw/exchangeReport/TWTAWU?"
        + urlencode({"response": "json", "startDate": ad_yyyymmdd(start), "endDate": ad_yyyymmdd(end)})
    )
    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_twtawu", url=url, ok=False, error=str(data.get("stat")))
        return 0
    n = 0
    for rec in data.get("data") or []:
        sym = str(rec[1]).strip()
        hd = roc_ymd_to_iso(str(rec[3]))
        ht = str(rec[4]).strip() if len(rec) > 4 else ""
        rd = roc_ymd_to_iso(str(rec[5])) if len(rec) > 5 else None
        rt = str(rec[6]).strip() if len(rec) > 6 else None
        if not hd:
            continue
        row = {
            "market": "TWSE",
            "symbol": sym,
            "halt_date": hd,
            "halt_time": ht,
            "resume_date": rd,
            "resume_time": rt,
            "halt_type": "暫停交易",
            "source": url,
            "announced_at": None,
            "announced_at_note": "TWTAWU 暫停交易證券",
            "fetched_at": now_iso(),
        }
        upsert_versioned(
            conn,
            "trading_halts",
            {"market": "TWSE", "symbol": sym, "halt_date": hd, "halt_time": ht},
            row,
            ["resume_date", "resume_time", "halt_type"],
        )
        n += 1
    touch_source(conn, "twse_twtawu", url=url, ok=True, http_status=200)
    conn.commit()
    return n


def pair_tpex_halts(items: list[dict]) -> list[dict]:
    """櫃買「暫停交易」與「恢復交易」是**分開的兩列**（同代號、不同日期；暫停列的恢復欄為空，恢復列的暫停欄為空）。
    把每個暫停列配對到同代號「恢復日 ≥ 暫停日」中最早且尚未用過的恢復列；配不到的 resume_date 維持 None。
    items：dict(symbol, halt_date, halt_time, resume_date, resume_time)（日期為 ISO 或 None）。傳入的區間要夠長（跨年度請一起傳）。
    2026-11-17 發現舊版丟掉所有恢復列，造成 183 筆上櫃停牌沒有恢復日。"""
    halts = [dict(x) for x in items if x.get("halt_date")]
    resumes: dict[str, list[dict]] = {}
    for x in items:
        if not x.get("halt_date") and x.get("resume_date"):
            resumes.setdefault(x["symbol"], []).append(x)
    for lst in resumes.values():
        lst.sort(key=lambda r: (r["resume_date"], r.get("resume_time") or ""))
    used: set[int] = set()
    halts.sort(key=lambda h: (h["symbol"], h["halt_date"], h.get("halt_time") or ""))
    for h in halts:
        if h.get("resume_date"):
            continue
        for r in resumes.get(h["symbol"], []):
            if id(r) in used or r["resume_date"] < h["halt_date"]:
                continue
            used.add(id(r))
            h["resume_date"], h["resume_time"] = r["resume_date"], r.get("resume_time")
            break
    return halts


def _store_tpex_halts(conn: sqlite3.Connection, halts: list[dict], url: str, note: str) -> int:
    n = 0
    for h in halts:
        row = {
            "market": "TPEx", "symbol": h["symbol"], "halt_date": h["halt_date"], "halt_time": h.get("halt_time") or "",
            "resume_date": h.get("resume_date"), "resume_time": h.get("resume_time"), "halt_type": "暫停交易",
            "source": url, "announced_at": None, "announced_at_note": note, "fetched_at": now_iso(),
        }
        upsert_versioned(
            conn, "trading_halts",
            {"market": "TPEx", "symbol": h["symbol"], "halt_date": h["halt_date"], "halt_time": h.get("halt_time") or ""},
            row, ["resume_date", "resume_time", "halt_type"],
        )
        n += 1
    return n


def fetch_tpex(conn: sqlite3.Connection) -> int:
    url = "https://www.tpex.org.tw/openapi/v1/tpex_spendi_history"
    resp = tpex.request("GET", url)
    data = resp.json()
    items = []
    for rec in data or []:
        sym = str(rec.get("SecuritiesCompanyCode") or "").strip()
        if not sym:
            continue
        items.append({
            "symbol": sym,
            "halt_date": roc_ymd_to_iso(str(rec.get("DateOfSuspendedTrading") or "")) if rec.get("DateOfSuspendedTrading") else None,
            "halt_time": str(rec.get("TimeOfSuspendedTrading") or ""),
            "resume_date": roc_ymd_to_iso(str(rec.get("DateOfResumedTrading") or "")) if rec.get("DateOfResumedTrading") else None,
            "resume_time": str(rec.get("TimeOfResumedTrading") or "") or None,
        })
    n = _store_tpex_halts(conn, pair_tpex_halts(items), url, "tpex_spendi_history（暫停列與恢復列配對）")
    touch_source(conn, "tpex_spendi_history", url=url, ok=True, http_status=200)
    conn.commit()
    return n


TPEX_HALT_HIST_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/sprcHis?date={year}&cate=&response=json"


def _norm_time(t: str | None) -> str | None:
    """'8:00'／'09:00'／'080000' → 'HHMMSS'（與 openapi tpex_spendi_history 一致，避免同一筆因時間格式不同變成兩列）。"""
    t = (t or "").strip()
    if t in ("", "-"):
        return None
    if ":" in t:
        parts = t.split(":")
        try:
            return f"{int(parts[0]):02d}{int(parts[1]):02d}{int(parts[2]) if len(parts) > 2 else 0:02d}"
        except ValueError:
            return t
    return t


def _roc_slash(s: str) -> str | None:
    s = (s or "").strip()
    return None if s in ("", "-") else roc_ymd_to_iso(s)


def fetch_tpex_history(conn: sqlite3.Connection, years: list[int]) -> dict:
    """櫃買網頁版「公布暫停／恢復交易有價證券 — 歷史」（`bulletin/sprcHis?date=西元年`）：每年 1 個請求，含各年度完整歷史；
    只收「上櫃股票」類別（權證／轉換公司債／興櫃不收）。跨年度的暫停／恢復列一起配對。每個請求單次（不重試）。"""
    from pipeline.httputil import single_shot

    items: list[dict] = []
    per_year: dict[int, int] = {}
    for y in sorted(years):
        url = TPEX_HALT_HIST_URL.format(year=y)
        with single_shot():
            resp = tpex.request("GET", url)
        data = resp.json()
        tables = data.get("tables") or []
        rows = (tables[0].get("data") if tables else None) or []
        k = 0
        for r in rows:
            if len(r) < 8 or str(r[1]).strip() != "上櫃股票":
                continue
            items.append({"symbol": str(r[2]).strip(), "halt_date": _roc_slash(str(r[4])), "halt_time": _norm_time(str(r[5])) or "",
                          "resume_date": _roc_slash(str(r[6])), "resume_time": _norm_time(str(r[7]))})
            k += 1
        per_year[y] = k
        touch_source(conn, f"tpex_sprcHis_{y}", url=url, ok=True, http_status=200, notes=f"rows={k}")
    halts = pair_tpex_halts(items)
    n = _store_tpex_halts(conn, halts, TPEX_HALT_HIST_URL.format(year="YYYY"), "tpex bulletin/sprcHis 歷史（暫停列與恢復列配對）")
    conn.commit()
    return {"rows_by_year": per_year, "halts": n, "unpaired": sum(1 for h in halts if not h.get("resume_date"))}


def fetch_all(conn: sqlite3.Connection) -> dict:
    end = today()
    start = end - timedelta(days=400)
    return {"twse": fetch_twse(conn, start, end), "tpex": fetch_tpex(conn)}
