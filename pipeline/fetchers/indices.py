"""加權／櫃買指數：FMTQIK／st41 月表為歷史主來源；漲跌百分比優先取官方，否則由漲跌÷前一交易日收盤推算。"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta
from urllib.parse import urlencode

from pipeline import config
from pipeline.dates import ad_yyyymmdd, now_iso, parse_number, roc_ymd_to_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.fetchers.calendar import recent_trade_days
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.indices")


def _months(days: list[str]) -> list[date]:
    ms = sorted({d[:7] for d in days})
    out = []
    for ym in ms:
        y, m = map(int, ym.split("-"))
        out.append(date(y, m, 1))
    return out


def _sign_from_html(cell: str | None) -> float:
    if not cell:
        return 1.0
    if "green" in cell or ">-<" in cell or ">-</" in cell:
        return -1.0
    return 1.0


def _fill_derived_pct(conn: sqlite3.Connection, index_id: str) -> int:
    """對 change_pct 為空的列：change ÷ 前一交易日收盤 ×100，並標 change_pct_derived=1。"""
    rows = conn.execute(
        """SELECT price_date, close, change, change_pct, version FROM index_daily
           WHERE index_id=? AND is_current=1 ORDER BY price_date""",
        (index_id,),
    ).fetchall()
    n = 0
    prev_close = None
    for r in rows:
        pd, close, chg, pct, ver = r["price_date"], r["close"], r["change"], r["change_pct"], r["version"]
        if pct is None and chg is not None and prev_close not in (None, 0):
            derived = (float(chg) / float(prev_close)) * 100.0
            conn.execute(
                """UPDATE index_daily SET change_pct=?, change_pct_derived=1,
                   unit_conversion=COALESCE(unit_conversion,'') || CASE
                     WHEN instr(COALESCE(unit_conversion,''), 'change_pct=derived')>0 THEN ''
                     ELSE ';change_pct=change/prev_close*100' END
                   WHERE index_id=? AND price_date=? AND version=? AND is_current=1""",
                (derived, index_id, pd, ver),
            )
            n += 1
        if close is not None:
            prev_close = close
    conn.commit()
    return n


def fetch_twse_fmtqik(conn: sqlite3.Connection, month_day: date) -> int:
    """歷史月表：僅有漲跌點數，無官方百分比 → change_pct 稍後推算。"""
    url = (
        "https://www.twse.com.tw/exchangeReport/FMTQIK?"
        + urlencode({"response": "json", "date": ad_yyyymmdd(month_day)})
    )
    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_fmtqik", url=url, ok=False, error=str(data.get("stat")))
        return 0
    n = 0
    max_pd = None
    for rec in data.get("data") or []:
        pd = roc_ymd_to_iso(rec[0])
        if not pd:
            continue
        chg = parse_number(rec[5])
        row = {
            "index_id": "TAIEX",
            "index_name": "發行量加權股價指數",
            "price_date": pd,
            "close": parse_number(rec[4]),
            "change": chg,
            "change_pct": None,
            "change_pct_derived": None,
            "source": url,
            "announced_at": None,
            "announced_at_note": "FMTQIK 無漲跌百分比；稍後以 change/prev_close 推算",
            "fetched_at": now_iso(),
            "unit": "index_points",
            "source_unit": "點",
            "unit_conversion": "none",
        }
        upsert_versioned(conn, "index_daily", {"index_id": "TAIEX", "price_date": pd}, row, ["close", "change"])
        n += 1
        max_pd = pd if max_pd is None or pd > max_pd else max_pd
    touch_source(conn, "twse_fmtqik", url=url, ok=True, http_status=200, data_date=max_pd)
    conn.commit()
    return n


def fetch_tpex_st41(conn: sqlite3.Connection, month_day: date) -> int:
    """歷史月表：僅有漲/跌點數，無官方百分比。"""
    d = f"{month_day.year-1911}/{month_day.month:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_index/st41_result.php?l=zh-tw&d={d}&o=json"
    resp = tpex.request("GET", url)
    data = resp.json()
    tables = data.get("tables") or []
    n = 0
    max_pd = None
    for rec in (tables[0].get("data") if tables else []) or []:
        pd = roc_ymd_to_iso(str(rec[0]))
        if not pd:
            continue
        row = {
            "index_id": "TPEX",
            "index_name": "櫃買指數",
            "price_date": pd,
            "close": parse_number(rec[4]),
            "change": parse_number(rec[5]),
            "change_pct": None,
            "change_pct_derived": None,
            "source": url,
            "announced_at": None,
            "announced_at_note": "st41 無漲跌幅度；稍後以 change/prev_close 推算",
            "fetched_at": now_iso(),
            "unit": "index_points",
            "source_unit": "點",
            "unit_conversion": "none",
        }
        upsert_versioned(conn, "index_daily", {"index_id": "TPEX", "price_date": pd}, row, ["close", "change"])
        n += 1
        max_pd = pd if max_pd is None or pd > max_pd else max_pd
    touch_source(conn, "tpex_st41", url=url, ok=True, http_status=200, data_date=max_pd)
    conn.commit()
    return n


def enrich_official_change_pct(conn: sqlite3.Connection, as_of: str | None = None) -> dict:
    """以當日官方端點覆寫 change_pct（非推算）：
    - TWSE MI_INDEX type=IND「漲跌百分比(%)」
    - TPEx afterTrading/indexSummary「漲跌幅度(%)」
    """
    out = {"twse": 0, "tpex": 0, "failed": []}
    if not as_of:
        row = conn.execute(
            "SELECT MAX(price_date) FROM index_daily WHERE is_current=1"
        ).fetchone()
        as_of = row[0] if row else None
    if not as_of:
        return out
    ymd = as_of.replace("-", "")
    # TWSE
    try:
        url = (
            "https://www.twse.com.tw/exchangeReport/MI_INDEX?"
            + urlencode({"response": "json", "date": ymd, "type": "IND"})
        )
        resp = twse.request("GET", url)
        data = resp.json()
        if data.get("stat") == "OK":
            for table in data.get("tables") or []:
                for rec in table.get("data") or []:
                    if rec and rec[0] == "發行量加權股價指數":
                        sign = _sign_from_html(str(rec[2]))
                        pts = parse_number(rec[3])
                        pct = parse_number(rec[4])
                        close = parse_number(rec[1])
                        if pct is not None and sign < 0 and pct > 0:
                            pct = -pct
                        if pts is not None and sign < 0 and pts > 0:
                            pts = -pts
                        if pct is not None:
                            conn.execute(
                                """UPDATE index_daily SET change_pct=?, change_pct_derived=0,
                                   change=COALESCE(?, change), close=COALESCE(?, close),
                                   announced_at_note=?, source=?, fetched_at=?
                                   WHERE index_id='TAIEX' AND price_date=? AND is_current=1""",
                                (
                                    pct,
                                    pts,
                                    close,
                                    "change_pct 取自 MI_INDEX type=IND 漲跌百分比(%)",
                                    url,
                                    now_iso(),
                                    as_of,
                                ),
                            )
                            out["twse"] = 1
                            touch_source(conn, "twse_mi_index_ind", url=url, ok=True, http_status=200, data_date=as_of)
                        break
        else:
            out["failed"].append(f"mi_index:{data.get('stat')}")
    except Exception as e:
        out["failed"].append(f"mi_index:{e}")
        log.exception("mi_index ind")

    # TPEx
    try:
        # indexSummary 接受 2026/11/09
        d = f"{as_of[0:4]}/{as_of[5:7]}/{as_of[8:10]}"
        url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/indexSummary?date={d}"
        resp = tpex.request("GET", url)
        data = resp.json()
        tables = data.get("tables") or []
        for rec in (tables[0].get("data") if tables else []) or []:
            if rec and str(rec[0]).strip() == "櫃買指數":
                close = parse_number(rec[1])
                chg = parse_number(rec[2])
                pct = parse_number(rec[3])
                if pct is not None:
                    conn.execute(
                        """UPDATE index_daily SET change_pct=?, change_pct_derived=0,
                           change=COALESCE(?, change), close=COALESCE(?, close),
                           announced_at_note=?, source=?, fetched_at=?
                           WHERE index_id='TPEX' AND price_date=? AND is_current=1""",
                        (
                            pct,
                            chg,
                            close,
                            "change_pct 取自 TPEx indexSummary 漲跌幅度(%)",
                            url,
                            now_iso(),
                            as_of,
                        ),
                    )
                    out["tpex"] = 1
                    touch_source(conn, "tpex_index_summary", url=url, ok=True, http_status=200, data_date=as_of)
                break
    except Exception as e:
        out["failed"].append(f"tpex_indexSummary:{e}")
        log.exception("tpex indexSummary")
    conn.commit()
    return out


def fetch_window(conn: sqlite3.Connection) -> dict:
    end = today() - timedelta(days=1)
    days = recent_trade_days(conn, config.PRICE_TRADE_DAYS, end=end)
    months = _months(days)
    stats: dict = {"twse": 0, "tpex": 0, "failed": [], "derived": {}, "official_pct": {}}
    for md in months:
        try:
            stats["twse"] += fetch_twse_fmtqik(conn, md)
        except FetchError as e:
            stats["failed"].append(f"fmtqik:{md}:{e}")
        try:
            stats["tpex"] += fetch_tpex_st41(conn, md)
        except FetchError as e:
            stats["failed"].append(f"st41:{md}:{e}")
    # 歷史列推算百分比
    stats["derived"]["TAIEX"] = _fill_derived_pct(conn, "TAIEX")
    stats["derived"]["TPEX"] = _fill_derived_pct(conn, "TPEX")
    # 最近一日嘗試官方百分比覆寫
    if days:
        stats["official_pct"] = enrich_official_change_pct(conn, days[-1] if days else None)
    return stats
