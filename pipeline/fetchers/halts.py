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


def fetch_tpex(conn: sqlite3.Connection) -> int:
    url = "https://www.tpex.org.tw/openapi/v1/tpex_spendi_history"
    resp = tpex.request("GET", url)
    data = resp.json()
    n = 0
    for rec in data or []:
        sym = str(rec.get("SecuritiesCompanyCode") or "").strip()
        hd = roc_ymd_to_iso(str(rec.get("DateOfSuspendedTrading") or ""))
        ht = str(rec.get("TimeOfSuspendedTrading") or "")
        rd = roc_ymd_to_iso(str(rec.get("DateOfResumedTrading") or "")) if rec.get("DateOfResumedTrading") else None
        rt = str(rec.get("TimeOfResumedTrading") or "") or None
        if not sym or not hd:
            continue
        row = {
            "market": "TPEx",
            "symbol": sym,
            "halt_date": hd,
            "halt_time": ht,
            "resume_date": rd,
            "resume_time": rt,
            "halt_type": "暫停交易",
            "source": url,
            "announced_at": None,
            "announced_at_note": "tpex_spendi_history",
            "fetched_at": now_iso(),
        }
        upsert_versioned(
            conn,
            "trading_halts",
            {"market": "TPEx", "symbol": sym, "halt_date": hd, "halt_time": ht},
            row,
            ["resume_date", "resume_time", "halt_type"],
        )
        n += 1
    touch_source(conn, "tpex_spendi_history", url=url, ok=True, http_status=200)
    conn.commit()
    return n


def fetch_all(conn: sqlite3.Connection) -> dict:
    end = today()
    start = end - timedelta(days=400)
    return {"twse": fetch_twse(conn, start, end), "tpex": fetch_tpex(conn)}
