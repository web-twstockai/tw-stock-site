"""日行情：上市 STOCK_DAY 逐月；上櫃 tradingStock 逐月。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date
from urllib.parse import urlencode

from pipeline import config
from pipeline.dates import ad_slash, ad_yyyymmdd, now_iso, parse_int, parse_number, roc_ymd_to_iso
from pipeline.db import touch_source, upsert_versioned
from pipeline.fetchers.calendar import recent_trade_days
from pipeline.fetchers.price_fields import load_halt_coverage, parse_change, trade_flags
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.prices")

CONTENT_KEYS = [
    "open_twd", "high_twd", "low_twd", "close_twd", "change", "change_twd",
    "change_sign", "change_note", "is_halted", "is_no_trade", "next_ref_price_twd",
    "volume_shares", "amount_twd", "transactions", "source_table",
]


def _months_covering(days: list[str]) -> list[date]:
    months = sorted({d[:7] for d in days})
    out = []
    for ym in months:
        y, m = map(int, ym.split("-"))
        out.append(date(y, m, 1))
    return out


def _upsert_price(conn: sqlite3.Connection, row: dict) -> str:
    return upsert_versioned(
        conn,
        "daily_prices",
        {"symbol": row["symbol"], "price_date": row["price_date"]},
        row,
        CONTENT_KEYS,
    )


def fetch_twse_stock_day(conn: sqlite3.Connection, symbol: str, month_day: date) -> int:
    url = (
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY?"
        + urlencode({"response": "json", "date": ad_yyyymmdd(month_day), "stockNo": symbol})
    )
    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_stock_day", url=url, ok=False, http_status=resp.status_code, error=str(data.get("stat")))
        return 0
    exact, intervals = load_halt_coverage(conn)
    n = 0
    max_pd = None
    for rec in data.get("data") or []:
        # 日期,成交股數,成交金額,開盤,最高,最低,收盤,漲跌價差,成交筆數,註記
        pd = roc_ymd_to_iso(rec[0])
        if not pd:
            continue
        close = parse_number(rec[6])
        chg, sign, note = parse_change(rec[7])
        no_trade, halted = trade_flags(close, symbol, pd, exact, intervals)
        row = {
            "symbol": symbol,
            "market": "TWSE",
            "price_date": pd,
            "open_twd": parse_number(rec[3]),
            "high_twd": parse_number(rec[4]),
            "low_twd": parse_number(rec[5]),
            "close_twd": close,
            "change": chg,
            "change_twd": chg,
            "change_sign": sign,
            "change_note": note,
            "is_halted": halted,
            "is_no_trade": no_trade,
            "next_ref_price_twd": None,
            "volume_shares": parse_int(rec[1]),
            "amount_twd": parse_int(rec[2]),
            "transactions": parse_int(rec[8]),
            "avg_twd": None,
            "pe_ratio": None,
            "raw_volume": rec[1],
            "raw_amount": rec[2],
            "source": url,
            "source_table": "STOCK_DAY",
            "announced_at": None,
            "announced_at_note": "來源無公告時間；次日參考價：來源無此欄",
            "fetched_at": now_iso(),
            "unit": "volume=股;amount=新台幣元",
            "source_unit": "股;元",
            "unit_conversion": "none",
        }
        _upsert_price(conn, row)
        n += 1
        max_pd = pd if max_pd is None or pd > max_pd else max_pd
    # last_data_date＝成功入庫的最新行情日（非請求參數）
    touch_source(conn, "twse_stock_day", url=url, ok=True, http_status=200, data_date=max_pd)
    conn.commit()
    return n


def fetch_tpex_trading_stock(conn: sqlite3.Connection, symbol: str, month_day: date) -> int:
    url = (
        "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?"
        + urlencode({"code": symbol, "date": ad_slash(month_day), "id": "", "response": "json"})
    )
    resp = tpex.request("GET", url)
    data = resp.json()
    tables = data.get("tables") or []
    if not tables:
        touch_source(conn, "tpex_trading_stock", url=url, ok=False, http_status=resp.status_code, error="no tables")
        return 0
    exact, intervals = load_halt_coverage(conn)
    n = 0
    max_pd = None
    for rec in tables[0].get("data") or []:
        # 日期,成交張數,成交仟元,開盤,最高,最低,收盤,漲跌,筆數
        pd = roc_ymd_to_iso(rec[0])
        if not pd:
            continue
        lots = parse_number(rec[1])
        thou = parse_number(rec[2])
        vol = int(lots * 1000) if lots is not None else None
        amt = int(thou * 1000) if thou is not None else None
        close = parse_number(rec[6])
        chg, sign, note = parse_change(rec[7])
        no_trade, halted = trade_flags(close, symbol, pd, exact, intervals)
        row = {
            "symbol": symbol,
            "market": "TPEx",
            "price_date": pd,
            "open_twd": parse_number(rec[3]),
            "high_twd": parse_number(rec[4]),
            "low_twd": parse_number(rec[5]),
            "close_twd": close,
            "change": chg,
            "change_twd": chg,
            "change_sign": sign,
            "change_note": note,
            "is_halted": halted,
            "is_no_trade": no_trade,
            "next_ref_price_twd": None,
            "volume_shares": vol,
            "amount_twd": amt,
            "transactions": parse_int(rec[8]),
            "avg_twd": None,
            "pe_ratio": None,
            "raw_volume": rec[1],
            "raw_amount": rec[2],
            "source": url,
            "source_table": "tradingStock",
            "announced_at": None,
            "announced_at_note": "來源無公告時間；次日參考價：來源無此欄（tradingStock 無此欄，見 dailyQuotes）",
            "fetched_at": now_iso(),
            "unit": "volume=股;amount=新台幣元",
            "source_unit": "成交張數;成交仟元",
            "unit_conversion": "shares=lots*1000; amount=thousand_twd*1000",
        }
        _upsert_price(conn, row)
        n += 1
        max_pd = pd if max_pd is None or pd > max_pd else max_pd
    touch_source(conn, "tpex_trading_stock", url=url, ok=True, http_status=200, data_date=max_pd)
    conn.commit()
    return n


def fetch_sample_prices(conn: sqlite3.Connection, symbols: list[str], *, trade_days: int | None = None) -> dict:
    from datetime import timedelta
    from pipeline.dates import today as _today
    # 觀察窗以「最近已收盤交易日」為終點（不含尚未收盤的今天）
    depth = trade_days if trade_days is not None else config.PRICE_TRADE_DAYS
    end = _today() - timedelta(days=1)
    days = recent_trade_days(conn, depth + 5, end=end)
    days = days[-depth:] if len(days) >= depth else days
    if len(days) < depth:
        log.warning("calendar trade days only %s (want %s)", len(days), depth)
    months = _months_covering(days)
    # 多抓一個月緩衝
    if months:
        first = months[0]
        y, m = first.year, first.month - 1
        if m == 0:
            y, m = y - 1, 12
        from datetime import date as _date
        months = [_date(y, m, 1)] + months
    want = set(days)
    result = {"months": len(months), "ok": [], "failed": []}
    market_map = {r["symbol"]: r["market"] for r in conn.execute("SELECT symbol, market FROM securities")}
    for sym in symbols:
        mkt = market_map.get(sym)
        if not mkt:
            # fallback from sample lists
            mkt = "TWSE" if sym in config.SAMPLE30_TWSE else "TPEx"
        try:
            total = 0
            for md in months:
                if mkt == "TWSE":
                    total += fetch_twse_stock_day(conn, sym, md)
                else:
                    total += fetch_tpex_trading_stock(conn, sym, md)
            # trim count in window
            have = conn.execute(
                "SELECT COUNT(*) FROM daily_prices WHERE symbol=? AND is_current=1 AND price_date>=? AND price_date<=?",
                (sym, days[0], days[-1]),
            ).fetchone()[0]
            result["ok"].append(f"{sym}:{have}")
            log.info("prices %s rows_in_window≈%s fetched_rows=%s", sym, have, total)
        except FetchError as e:
            result["failed"].append(f"{sym}:{e}")
            log.error("prices fail %s %s", sym, e)
    return result
