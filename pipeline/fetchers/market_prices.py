"""全市場日行情：TWSE MI_INDEX、TPEx dailyQuotes（整批，不逐檔 STOCK_DAY）。"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pipeline import config
from pipeline.coverage import max_rss_mb, record_summary
from pipeline.dates import ad_slash, ad_yyyymmdd, include_today_prices, now_iso, parse_int, parse_number, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.deadline import should_stop
from pipeline.fetchers.calendar import recent_trade_days
from pipeline.fetchers.price_fields import is_official_halt, load_halt_coverage, parse_change, trade_flags
from pipeline.fetchers.prices import CONTENT_KEYS, _upsert_price
from pipeline.httputil import FetchError, RateLimited, tpex, twse
from pipeline.fetchers import tpex_block

log = logging.getLogger("pipeline.market_prices")

MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={yyyymmdd}&type=ALLBUT0999"
DAILY_QUOTES_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={slash}&id=&response=json"

# 比對既有 sample30 列時使用的數值鍵（不含 source_table）
VALUE_KEYS = [
    "open_twd", "high_twd", "low_twd", "close_twd", "change", "change_twd",
    "volume_shares", "amount_twd", "transactions",
]
CLASS_KEYS = ["change", "change_twd", "change_sign", "change_note", "is_halted", "is_no_trade"]
SWITCH_NOTE = "來源從 tradingStock 換成 dailyQuotes（含鉅額）"


def _add_switch_note(row: dict) -> None:
    cur = (row.get("change_note") or "").strip()
    if SWITCH_NOTE not in cur:
        row["change_note"] = f"{cur}；{SWITCH_NOTE}" if cur else SWITCH_NOTE


def _universe(conn: sqlite3.Connection, include_delisted: str | None = None) -> dict[str, str]:
    """現行普通股（symbol→market）。include_delisted＝'YYYY-MM-DD'：另加 delistings 內「終止日 ≥ 該日」的 4 位數字代號（歷史回補用，
    降低倖存者偏差；只收回補窗口內下市者，避免把更早下市的股票算進分母）；現行清單優先，代號重用時以現行為準。"""
    rows = conn.execute(
        """SELECT symbol, market FROM securities
           WHERE is_current=1 AND COALESCE(is_common,1)=1"""
    ).fetchall()
    uni = {r[0]: r[1] for r in rows}
    if include_delisted:
        try:
            for sym, mkt in conn.execute("SELECT symbol, market FROM delistings WHERE delist_date>=? AND symbol GLOB '[1-9][0-9][0-9][0-9]'", (include_delisted,)):
                uni.setdefault(sym, mkt)
        except sqlite3.OperationalError:    # 舊庫尚無 delistings
            pass
    return uni


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
    if row.get("source_table") == "dailyQuotes":
        # 2026-11-17：上櫃正式來源改為 dailyQuotes（含鉅額）。既有 tradingStock（不含鉅額）列以版本化覆寫（舊列 is_current=0），
        # 新列 change_note 標明換源；之後重跑時沿用同一註記，才不會每次都升版（冪等）。
        if src == "tradingStock" or (src == "dailyQuotes" and SWITCH_NOTE in (old["change_note"] or "")):
            _add_switch_note(row)
        return _upsert_price(conn, row)
    if src not in ("STOCK_DAY", "tradingStock"):
        return _upsert_price(conn, row)
    keys = VALUE_KEYS
    if src == "tradingStock":
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
                "volume_basis": "incl_block",
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
                "volume_basis": "incl_block",   # 實測含鉅額（2026-11-17：dailyQuotes＝tradingStock＋鉅額，殘差<500 股；與 highlight 全市場加總一致）
                "announced_at": None,
                "announced_at_note": "dailyQuotes 單位為股／元，成交量含鉅額（volume_basis=incl_block）；鉅額見 block_shares／block_amount_twd",
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


def fetch_tpex_day(
    conn: sqlite3.Connection,
    iso: str,
    universe: dict[str, str],
    http_events: list,
    *,
    exact: set | None = None,
    intervals: dict | None = None,
    block_ctx: "tpex_block.BlockContext | None" = None,
) -> dict:
    """抓上櫃 dailyQuotes 單一交易日（1 個請求），寫入 universe 內的上櫃列（含鉅額欄位）。
    限流（RateLimited）時不重試，回傳 rate_limited=True 讓呼叫端停止。"""
    from datetime import date as date_cls

    y, m, d = map(int, iso.split("-"))
    dt = date_cls(y, m, d)
    out = {"tpex": 0, "page_tpex": 0, "failed": [], "rate_limited": False}
    if exact is None or intervals is None:
        exact, intervals = load_halt_coverage(conn)
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
            ctx = block_ctx if block_ctx is not None else tpex_block.get_context(conn)
            tpex_block.attach_block(rows, iso, ctx)
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
    except RateLimited as e:
        out["rate_limited"] = True
        http_events.append({"src": "TPEx", "url": url2, "http": e.status, "date": iso, "error": str(e)})
        touch_source(conn, f"tpex_daily_quotes_{iso}", url=url2, ok=False, http_status=e.status, error=str(e))
        out["failed"].append(f"TPEx:{iso}:{e}")
        log.error("dailyQuotes %s %s", iso, e)
    except FetchError as e:
        http_events.append({"src": "TPEx", "url": url2, "http": e.status, "date": iso, "error": str(e)})
        touch_source(conn, f"tpex_daily_quotes_{iso}", url=url2, ok=False, http_status=e.status, error=str(e))
        out["failed"].append(f"TPEx:{iso}:{e}")
        log.error("dailyQuotes %s %s", iso, e)
    return out


def fetch_twse_day(
    conn: sqlite3.Connection,
    iso: str,
    universe: dict[str, str],
    http_events: list,
    *,
    exact: set | None = None,
    intervals: dict | None = None,
) -> dict:
    """抓上市 MI_INDEX 單一交易日（1 個請求），寫入 universe 內的上市列。回傳 {twse, page_twse, failed, blocked}。"""
    from datetime import date as date_cls

    y, m, d = map(int, iso.split("-"))
    dt = date_cls(y, m, d)
    out = {"twse": 0, "page_twse": 0, "failed": [], "blocked": False}
    if exact is None or intervals is None:
        exact, intervals = load_halt_coverage(conn)
    url = MI_INDEX_URL.format(yyyymmdd=ad_yyyymmdd(dt))
    try:
        resp = twse.request("GET", url)
        http_events.append({"src": "TWSE", "url": url, "http": resp.status_code, "date": iso})
        data = resp.json()
        if data.get("stat") != "OK":
            msg = str(data.get("stat"))
            touch_source(conn, f"twse_mi_index_{iso}", url=url, ok=False, http_status=resp.status_code, error=msg)
            out["failed"].append(f"TWSE:{iso}:{msg}")
            out["no_data"] = True
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
        out["blocked"] = bool(e.blocked)
        out["rate_limited"] = isinstance(e, RateLimited)
        log.error("MI_INDEX %s %s", iso, e)
    return out


def fetch_one_day(
    conn: sqlite3.Connection,
    iso: str,
    universe: dict[str, str],
    http_events: list,
) -> dict:
    """抓單一交易日兩市場。回傳 {twse: n, tpex: n, failed: []}。"""
    out = {"twse": 0, "tpex": 0, "failed": [], "page_twse": 0, "page_tpex": 0}
    exact, intervals = load_halt_coverage(conn)
    tw = fetch_twse_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
    out["twse"], out["page_twse"] = tw["twse"], tw["page_twse"]
    out["failed"].extend(tw["failed"])

    tp = fetch_tpex_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
    out["tpex"], out["page_tpex"] = tp["tpex"], tp["page_tpex"]
    out["failed"].extend(tp["failed"])
    out["rate_limited"] = tp.get("rate_limited", False)
    return out


def market_day_rows(conn: sqlite3.Connection, iso: str, universe: dict[str, str] | None = None) -> dict[str, int]:
    """該日各市場現行普通股列數（用來判斷該日該市場是否已是「全市場」）。
    給 universe（含視窗內下市股）時，改數「universe 內」的列，使分子分母同一口徑（下市股當日有成交就算進去）。"""
    if universe is not None:
        out: dict[str, int] = {}
        for (sym,) in conn.execute("SELECT symbol FROM daily_prices WHERE is_current=1 AND price_date=?", (iso,)):
            m = universe.get(sym)
            if m:
                out[m] = out.get(m, 0) + 1
        return out
    return {m: n for m, n in conn.execute(
        """SELECT s.market, COUNT(*) FROM daily_prices p
           JOIN securities s ON s.symbol=p.symbol AND s.is_current=1 AND COALESCE(s.is_common,1)=1
           WHERE p.is_current=1 AND p.price_date=? GROUP BY s.market""", (iso,))}


def markets_needed(conn: sqlite3.Connection, iso: str, universe: dict[str, str]) -> list[str]:
    """該日還缺哪些市場（現行列數 < FULL_DAY_RATIO × 該市場現行普通股數）。檢查點在 DB（列數），不靠 logs/ 檔案。"""
    have = market_day_rows(conn, iso, universe)
    need = []
    for m in ("TWSE", "TPEx"):
        total = sum(1 for v in universe.values() if v == m)
        if have.get(m, 0) < config.FULL_DAY_RATIO * total:
            need.append(m)
    return need


def fetch_market_prices(
    conn: sqlite3.Connection,
    *,
    trade_days: int,
    stop_before: datetime | None = None,
    checkpoint_path: Path | None = None,
    run_id: int | None = None,
    resume: bool = True,
    use_db_state: bool = False,
) -> dict:
    """依交易日整批回補／增量。支援檢查點與時限中止。
    use_db_state=True（雲端 all 模式）：不用 logs/ 檢查點檔，改以 DB 內「該日該市場現行列數」判斷是否已完成，
    只抓還缺的市場（TWSE／TPEx 各 1 個請求），Actions 臨時機器也能續跑、冪等。"""
    # 終點：台北時間過 config.TODAY_PRICES_AFTER 含今天，否則到昨天（2026-11-24 修正，原本一律到昨天）
    t0 = today()
    end = t0 if include_today_prices() else t0 - timedelta(days=1)
    days = recent_trade_days(conn, trade_days + 5, end=end)
    days = days[-trade_days:] if len(days) >= trade_days else days
    today_iso = t0.isoformat()
    pending_today: list[str] = []
    universe = _universe(conn)
    twse_n = sum(1 for m in universe.values() if m == "TWSE")
    tpex_n = sum(1 for m in universe.values() if m == "TPEx")
    ck_path = checkpoint_path or config.CHECKPOINT_ALL
    ck = _load_ckpt(ck_path) if (resume and not use_db_state) else {}
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
        if use_db_state:
            need = markets_needed(conn, iso, universe)
            if not need:
                skipped_done += 1
                continue
            st = {"twse": 0, "tpex": 0, "failed": [], "rate_limited": False}
            exact, intervals = load_halt_coverage(conn)
            if iso == today_iso:
                # 當日：先抓上櫃（較晚公布；未公布時回 ok 但 0 列），上櫃未公布就不抓上市，避免只有半個市場。
                # 任一市場官方尚未公布 → 不寫入、不算失敗，記 pending_today，下次 run 再抓。
                if "TPEx" in need:
                    tp = fetch_tpex_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
                    st["tpex"] = tp["tpex"]
                    st["rate_limited"] = tp.get("rate_limited", False)
                    if tp.get("rate_limited") or tp.get("failed"):
                        st["failed"].extend(tp.get("failed") or [])
                    elif not tp.get("page_tpex"):
                        pending_today.append("TPEx")
                if "TWSE" in need and "TPEx" not in pending_today and not st["failed"] and not st["rate_limited"]:
                    tw = fetch_twse_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
                    st["twse"] = tw["twse"]
                    if tw.get("no_data"):
                        pending_today.append("TWSE")
                    else:
                        st["failed"].extend(tw["failed"])
                if pending_today:
                    log.info("prices day %s：官方尚未公布 %s，本次不寫入、不算失敗", iso, "／".join(pending_today))
            else:
                if "TWSE" in need:
                    tw = fetch_twse_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
                    st["twse"] = tw["twse"]
                    st["failed"].extend(tw["failed"])
                if "TPEx" in need:
                    tp = fetch_tpex_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
                    st["tpex"] = tp["tpex"]
                    st["failed"].extend(tp["failed"])
                    st["rate_limited"] = tp.get("rate_limited", False)
        elif iso in done:
            skipped_done += 1
            continue
        else:
            st = fetch_one_day(conn, iso, universe, http_events)
        failed.extend(st["failed"])
        if st.get("rate_limited"):
            stopped = True
            log.error("prices: 上櫃限流，停止後續日期（已熔斷主機；稍後重跑可續）")
            break
        if st["twse"] or st["tpex"]:
            ok_dates.append(iso)
            if not use_db_state:
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
        f"peak_rss_mb={peak_rss} target_days={len(days)} end={end.isoformat()}"
        + (f" pending_today={'/'.join(pending_today)}" if pending_today else "")
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
        "end": end.isoformat(),
        "pending_today": pending_today,
        "ok_dates": ok_dates,
        "failed": failed,
        "skipped_done": skipped_done,
        "stopped": stopped,
        "peak_rss_mb": peak_rss,
        "covered": {"TWSE": cov_twse, "TPEx": cov_tpex, "expected": {"TWSE": twse_n, "TPEx": tpex_n}},
        "http_events": http_events,
    }
