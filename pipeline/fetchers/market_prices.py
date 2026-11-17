"""全市場日行情：TWSE MI_INDEX、TPEx dailyQuotes（整批，不逐檔 STOCK_DAY）。"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pipeline import config
from pipeline.coverage import max_rss_mb, record_summary
from pipeline.dates import ad_slash, ad_yyyymmdd, now_iso, parse_int, parse_number, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.deadline import should_stop
from pipeline.fetchers.calendar import recent_trade_days
from pipeline.fetchers.price_fields import is_official_halt, load_halt_coverage, parse_change, trade_flags
from pipeline.fetchers.prices import CONTENT_KEYS, _upsert_price
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.market_prices")

MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={yyyymmdd}&type=ALLBUT0999"
DAILY_QUOTES_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={slash}&id=&response=json"

# 比對既有 sample30 列時使用的數值鍵（不含 source_table）
VALUE_KEYS = [
    "open_twd", "high_twd", "low_twd", "close_twd", "change", "change_twd",
    "volume_shares", "amount_twd", "transactions",
]
CLASS_KEYS = ["change", "change_twd", "change_sign", "change_note", "is_halted", "is_no_trade"]


def _universe(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """SELECT symbol, market FROM securities
           WHERE is_current=1 AND COALESCE(is_common,1)=1"""
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _nums_equal(a, b, *, tol: float = 1e-6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def _ohlc_change_equal(old: sqlite3.Row, row: dict, keys: list[str]) -> bool:
    return all(_nums_equal(old[k] if k in old.keys() else None, row.get(k)) for k in keys)


def _payload_from_old(old: sqlite3.Row, updates: dict) -> dict:
    skip = {"version", "checksum", "is_current"}
    payload = {k: old[k] for k in old.keys() if k not in skip}
    payload.update(updates)
    return payload


def _upsert_preserving(conn: sqlite3.Connection, row: dict) -> str:
    """既有 STOCK_DAY／tradingStock 列保留來源與量額；分類欄（停牌／無成交／無比價）可更新。"""
    old = conn.execute(
        "SELECT * FROM daily_prices WHERE symbol=? AND price_date=? AND is_current=1",
        (row["symbol"], row["price_date"]),
    ).fetchone()
    if old is None:
        return _upsert_price(conn, row)
    src = old["source_table"] if "source_table" in old.keys() else None
    if src not in ("STOCK_DAY", "tradingStock"):
        return _upsert_price(conn, row)
    keys = VALUE_KEYS
    if src == "tradingStock":
        # dailyQuotes 量／額／筆數因口徑（股／元 vs 張／仟元；是否含鉅額）可能略差；OHLC＋漲跌相同即保留既有列
        keys = ["open_twd", "high_twd", "low_twd", "close_twd", "change", "change_twd"]
    # 無比價時新列 change 為 NULL、舊列可能是 0：仍保留來源，只更新分類／漲跌欄
    class_updates = {k: row.get(k) for k in CLASS_KEYS}
    if _ohlc_change_equal(old, row, keys):
        return _upsert_price(conn, _payload_from_old(old, class_updates))
    # OHLC 一致但漲跌因 X／除息由 0 改為 NULL（或符號補齊）
    ohlc_keys = ["open_twd", "high_twd", "low_twd", "close_twd"]
    if _ohlc_change_equal(old, row, ohlc_keys):
        return _upsert_price(conn, _payload_from_old(old, class_updates))
    return _upsert_price(conn, row)


def parse_mi_index(
    data: dict,
    universe: dict[str, str],
    price_date: str,
    url: str,
    exact: set[tuple[str, str]] | None = None,
    intervals: dict[str, list[tuple[str, str]]] | None = None,
) -> list[dict]:
    tables = data.get("tables") or []
    quotes = None
    for t in tables:
        fields = t.get("fields") or []
        if fields and fields[0] == "證券代號" and "收盤價" in fields:
            quotes = t
            break
    if not quotes:
        return []
    fields = quotes["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    out: list[dict] = []
    now = now_iso()
    for rec in quotes.get("data") or []:
        if not rec:
            continue
        sym = str(rec[idx["證券代號"]]).strip()
        if sym not in universe or universe[sym] != "TWSE":
            continue
        close = parse_number(rec[idx["收盤價"]])
        chg, sign, note = parse_change(
            rec[idx["漲跌(+/-)"]] if "漲跌(+/-)" in idx else "",
            rec[idx["漲跌價差"]] if "漲跌價差" in idx else None,
        )
        no_trade, halted = trade_flags(close, sym, price_date, exact or set(), intervals or {})
        out.append(
            {
                "symbol": sym,
                "market": "TWSE",
                "price_date": price_date,
                "open_twd": parse_number(rec[idx["開盤價"]]),
                "high_twd": parse_number(rec[idx["最高價"]]),
                "low_twd": parse_number(rec[idx["最低價"]]),
                "close_twd": close,
                "change": chg,
                "change_twd": chg,
                "change_sign": sign,
                "change_note": note,
                "is_halted": halted,
                "is_no_trade": no_trade,
                "next_ref_price_twd": None,
                "volume_shares": parse_int(rec[idx["成交股數"]]),
                "amount_twd": parse_int(rec[idx["成交金額"]]),
                "transactions": parse_int(rec[idx["成交筆數"]]),
                "avg_twd": None,
                "pe_ratio": parse_number(rec[idx["本益比"]]) if "本益比" in idx else None,
                "raw_volume": rec[idx["成交股數"]],
                "raw_amount": rec[idx["成交金額"]],
                "source": url,
                "source_table": "MI_INDEX",
                "announced_at": None,
                "announced_at_note": "來源無公告時間；MI_INDEX 與 STOCK_DAY 同口徑（股／元）；次日參考價：來源無此欄",
                "fetched_at": now,
                "unit": "volume=股;amount=新台幣元",
                "source_unit": "股;元",
                "unit_conversion": "none",
            }
        )
    return out


def parse_daily_quotes(
    data: dict,
    universe: dict[str, str],
    price_date: str,
    url: str,
    exact: set[tuple[str, str]] | None = None,
    intervals: dict[str, list[tuple[str, str]]] | None = None,
) -> list[dict]:
    tables = data.get("tables") or []
    quotes = None
    for t in tables:
        title = t.get("title") or ""
        fields = t.get("fields") or []
        if "上櫃股票行情" in title or (fields and fields[0] == "代號" and "成交股數" in fields):
            quotes = t
            break
    if not quotes:
        return []
    fields = quotes["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    out: list[dict] = []
    now = now_iso()
    for rec in quotes.get("data") or []:
        if not rec:
            continue
        sym = str(rec[idx["代號"]]).strip()
        if sym not in universe or universe[sym] != "TPEx":
            continue
        close = parse_number(rec[idx["收盤"]])
        chg, sign, note = parse_change(rec[idx["漲跌"]])
        no_trade, halted = trade_flags(close, sym, price_date, exact or set(), intervals or {})
        out.append(
            {
                "symbol": sym,
                "market": "TPEx",
                "price_date": price_date,
                "open_twd": parse_number(rec[idx["開盤"]]),
                "high_twd": parse_number(rec[idx["最高"]]),
                "low_twd": parse_number(rec[idx["最低"]]),
                "close_twd": close,
                "change": chg,
                "change_twd": chg,
                "change_sign": sign,
                "change_note": note,
                "is_halted": halted,
                "is_no_trade": no_trade,
                "next_ref_price_twd": parse_number(
                    rec[idx["次日 參考價"]] if "次日 參考價" in idx else (rec[idx["次日參考價"]] if "次日參考價" in idx else None)
                ),
                "volume_shares": parse_int(rec[idx["成交股數"]]),
                "amount_twd": parse_int(rec[idx["成交金額(元)"]] if "成交金額(元)" in idx else rec[idx.get("成交金額", 9)]),
                "transactions": parse_int(rec[idx["成交筆數"]]),
                "avg_twd": parse_number(rec[idx["均價"]]) if "均價" in idx else None,
                "pe_ratio": None,
                "raw_volume": rec[idx["成交股數"]],
                "raw_amount": rec[idx["成交金額(元)"]] if "成交金額(元)" in idx else None,
                "source": url,
                "source_table": "dailyQuotes",
                "announced_at": None,
                "announced_at_note": "dailyQuotes 單位為股／元（較 tradingStock 張／仟元精確）；既有 tradingStock 列不覆寫",
                "fetched_at": now,
                "unit": "volume=股;amount=新台幣元",
                "source_unit": "股;元",
                "unit_conversion": "none",
            }
        )
    return out


def _load_ckpt(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_ckpt(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def fetch_one_day(
    conn: sqlite3.Connection,
    iso: str,
    universe: dict[str, str],
    http_events: list,
) -> dict:
    """抓單一交易日兩市場。回傳 {twse: n, tpex: n, failed: []}。"""
    from datetime import date as date_cls

    y, m, d = map(int, iso.split("-"))
    dt = date_cls(y, m, d)
    out = {"twse": 0, "tpex": 0, "failed": [], "page_twse": 0, "page_tpex": 0}
    exact, intervals = load_halt_coverage(conn)
    # TWSE
    url = MI_INDEX_URL.format(yyyymmdd=ad_yyyymmdd(dt))
    try:
        resp = twse.request("GET", url)
        http_events.append({"src": "TWSE", "url": url, "http": resp.status_code, "date": iso})
        data = resp.json()
        if data.get("stat") != "OK":
            msg = str(data.get("stat"))
            touch_source(conn, f"twse_mi_index_{iso}", url=url, ok=False, http_status=resp.status_code, error=msg)
            out["failed"].append(f"TWSE:{iso}:{msg}")
        else:
            rows = parse_mi_index(data, universe, iso, url, exact, intervals)
            out["page_twse"] = len(rows)
            n = 0
            for i, row in enumerate(rows, 1):
                _upsert_preserving(conn, row)
                n += 1
                if i % 200 == 0:
                    conn.commit()
            conn.commit()
            out["twse"] = n
            touch_source(conn, "twse_mi_index", url=url, ok=True, http_status=200, data_date=iso)
            touch_source(conn, f"twse_mi_index_{iso}", url=url, ok=True, http_status=200, data_date=iso)
    except FetchError as e:
        http_events.append({"src": "TWSE", "url": url, "http": e.status, "date": iso, "error": str(e)})
        touch_source(conn, f"twse_mi_index_{iso}", url=url, ok=False, http_status=e.status, error=str(e))
        out["failed"].append(f"TWSE:{iso}:{e}")
        log.error("MI_INDEX %s %s", iso, e)

    # TPEx
    url2 = DAILY_QUOTES_URL.format(slash=ad_slash(dt))
    try:
        resp = tpex.request("GET", url2)
        http_events.append({"src": "TPEx", "url": url2, "http": resp.status_code, "date": iso})
        if resp.status_code != 200:
            raise FetchError(f"http={resp.status_code}", status=resp.status_code)
        data = resp.json()
        stat = (data.get("stat") or "").lower()
        if stat not in ("ok", ""):
            touch_source(conn, f"tpex_daily_quotes_{iso}", url=url2, ok=False, http_status=resp.status_code, error=str(data.get("stat")))
            out["failed"].append(f"TPEx:{iso}:{data.get('stat')}")
        else:
            rows = parse_daily_quotes(data, universe, iso, url2, exact, intervals)
            out["page_tpex"] = len(rows)
            n = 0
            for i, row in enumerate(rows, 1):
                _upsert_preserving(conn, row)
                n += 1
                if i % 200 == 0:
                    conn.commit()
            conn.commit()
            out["tpex"] = n
            touch_source(conn, "tpex_daily_quotes", url=url2, ok=True, http_status=200, data_date=iso)
            touch_source(conn, f"tpex_daily_quotes_{iso}", url=url2, ok=True, http_status=200, data_date=iso)
    except FetchError as e:
        http_events.append({"src": "TPEx", "url": url2, "http": e.status, "date": iso, "error": str(e)})
        touch_source(conn, f"tpex_daily_quotes_{iso}", url=url2, ok=False, http_status=e.status, error=str(e))
        out["failed"].append(f"TPEx:{iso}:{e}")
        log.error("dailyQuotes %s %s", iso, e)
    return out


def fetch_market_prices(
    conn: sqlite3.Connection,
    *,
    trade_days: int,
    stop_before: datetime | None = None,
    checkpoint_path: Path | None = None,
    run_id: int | None = None,
    resume: bool = True,
) -> dict:
    """依交易日整批回補／增量。支援檢查點與時限中止。"""
    end = today() - timedelta(days=1)
    days = recent_trade_days(conn, trade_days + 5, end=end)
    days = days[-trade_days:] if len(days) >= trade_days else days
    universe = _universe(conn)
    twse_n = sum(1 for m in universe.values() if m == "TWSE")
    tpex_n = sum(1 for m in universe.values() if m == "TPEx")
    ck_path = checkpoint_path or config.CHECKPOINT_ALL
    ck = _load_ckpt(ck_path) if resume else {}
    prices_ck = ck.setdefault("prices", {})
    done = set(prices_ck.get("done_dates") or [])
    http_events: list = []
    failed: list[str] = []
    ok_dates: list[str] = []
    skipped_done = 0
    stopped = False
    peak_rss = max_rss_mb()

    for iso in days:
        if should_stop(stop_before):
            stopped = True
            log.warning("stop-before reached before %s rss=%.1fMB", iso, max_rss_mb())
            break
        if iso in done:
            skipped_done += 1
            continue
        st = fetch_one_day(conn, iso, universe, http_events)
        failed.extend(st["failed"])
        if st["twse"] or st["tpex"]:
            ok_dates.append(iso)
            done.add(iso)
            prices_ck["done_dates"] = sorted(done)
            prices_ck["last_date"] = iso
            prices_ck["updated_at"] = now_iso()
            _save_ckpt(ck_path, ck)
        peak_rss = max(peak_rss, max_rss_mb())
        log.info(
            "prices day %s twse=%s tpex=%s failed=%s rss=%.1fMB",
            iso, st["twse"], st["tpex"], st["failed"], peak_rss,
        )

    # coverage：以「至少有一筆行情」的宇宙成員計成功
    def _covered(market: str) -> int:
        return conn.execute(
            """SELECT COUNT(DISTINCT p.symbol)
               FROM daily_prices p
               JOIN securities s ON s.symbol=p.symbol AND s.is_current=1
               WHERE p.is_current=1 AND s.market=? AND COALESCE(s.is_common,1)=1
                 AND p.price_date>=? AND p.price_date<=?""",
            (market, days[0] if days else None, days[-1] if days else None),
        ).fetchone()[0] if days else 0

    d0, d1 = (days[0], days[-1]) if days else (None, None)
    cov_twse = _covered("TWSE")
    cov_tpex = _covered("TPEx")
    notes = (
        f"ok_dates={len(ok_dates)} skipped_done={skipped_done} stopped={stopped} "
        f"peak_rss_mb={peak_rss} target_days={len(days)}"
    )
    record_summary(
        conn, run_id=run_id, universe="all", data_kind="prices", market="TWSE",
        expected=twse_n, success=cov_twse, failed=max(twse_n - cov_twse, 0), excluded=0,
        date_start=d0, date_end=d1, notes=notes,
        http_events=[e for e in http_events if e.get("src") == "TWSE"][-20:],
    )
    record_summary(
        conn, run_id=run_id, universe="all", data_kind="prices", market="TPEx",
        expected=tpex_n, success=cov_tpex, failed=max(tpex_n - cov_tpex, 0), excluded=0,
        date_start=d0, date_end=d1, notes=notes,
        http_events=[e for e in http_events if e.get("src") == "TPEx"][-20:],
    )
    return {
        "days": days,
        "ok_dates": ok_dates,
        "failed": failed,
        "skipped_done": skipped_done,
        "stopped": stopped,
        "peak_rss_mb": peak_rss,
        "covered": {"TWSE": cov_twse, "TPEx": cov_tpex, "expected": {"TWSE": twse_n, "TPEx": tpex_n}},
        "http_events": http_events,
    }
