"""next_ref_price_twd 回填（一次性作業，可重跑、冪等）。

規格（docs/schema.md §3.4、docs/indicators.md §8）：
  - 上櫃：官方來源是 dailyQuotes 的「次日 參考價」欄（記在該交易日那一列；無成交日也有）。tradingStock 無此欄，所以舊的 sample30
    上櫃 tradingStock 列是 NULL。
  - 上市：MI_INDEX／STOCK_DAY **沒有**此欄（標「X」不比價；TWT84U 的「開盤競價基準」有兩欄、語意不明，不採用）→ 永遠 NULL，
    並在 announced_at_note 註明來源無此欄，不推算、不以收盤價代替。
作法：
  1. 上櫃現行列 next_ref IS NULL 的日期，每日抓 1 次 dailyQuotes（≥5 秒間隔；遇限流停止），官方有值才「就地」填入
     （只填 NULL、不改量額、不升版；重算 checksum；announced_at_note 註明回填來源）；官方無值的列在 note 註明並留 NULL。
  2. 上市現行列 next_ref IS NULL 者 announced_at_note 補「次日參考價：來源無此欄」（一個 UPDATE，不請求）。
建議順序：先跑 tpex_volume_fix（dailyQuotes 版本化覆寫已含次日參考價），再跑本作業；反過來也正確，只是同一個日期會被請求兩次。
用法：python -m pipeline.run --job next_ref_fix [--fix-from D] [--fix-to D] [--max-dates N]
"""
from __future__ import annotations

import json
import logging
import sqlite3

from pipeline.dates import ad_slash, now_iso, today

log = logging.getLogger("pipeline.next_ref_fix")

NOTE_FILLED = "次日參考價取自 dailyQuotes（回填 {d}）"
NOTE_TPEX_NONE = "次日參考價：官方 dailyQuotes 該日無此值，留 NULL"
NOTE_TWSE = "次日參考價：來源無此欄（TWSE MI_INDEX／STOCK_DAY）"


def _append_note(cur: str | None, note: str) -> str:
    cur = (cur or "").strip()
    return note if not cur else (cur if note in cur else f"{cur}；{note}")


def todo_dates(conn: sqlite3.Connection, date_from=None, date_to=None) -> list[str]:
    q = ("SELECT DISTINCT price_date FROM daily_prices WHERE is_current=1 AND market='TPEx' AND next_ref_price_twd IS NULL "
         "AND COALESCE(announced_at_note,'') NOT LIKE '%次日參考價：官方 dailyQuotes 該日無此值%'")
    args: list = []
    if date_from:
        q += " AND price_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND price_date<=?"
        args.append(date_to)
    return [r[0] for r in conn.execute(q + " ORDER BY price_date", args)]


def fill_tpex_day(conn: sqlite3.Connection, iso: str) -> dict:
    from datetime import date as date_cls

    from pipeline.db import checksum
    from pipeline.fetchers.market_prices import DAILY_QUOTES_URL, parse_daily_quotes
    from pipeline.fetchers.price_fields import load_halt_coverage
    from pipeline.fetchers.prices import CONTENT_KEYS
    from pipeline.httputil import FetchError, RateLimited, tpex

    out = {"filled": 0, "none_official": 0, "absent_in_official": 0, "failed": None, "rate_limited": False}
    y, m, d = map(int, iso.split("-"))
    url = DAILY_QUOTES_URL.format(slash=ad_slash(date_cls(y, m, d)))
    need = {r["symbol"]: r for r in conn.execute(
        "SELECT * FROM daily_prices WHERE is_current=1 AND market='TPEx' AND price_date=? AND next_ref_price_twd IS NULL", (iso,))}
    if not need:
        return out
    try:
        resp = tpex.request("GET", url)
        if resp.status_code != 200:
            raise FetchError(f"http={resp.status_code}", status=resp.status_code)
        data = resp.json()
        if (data.get("stat") or "").lower() not in ("ok", ""):
            raise FetchError(f"stat={data.get('stat')}")
        exact, intervals = load_halt_coverage(conn)
        rows = parse_daily_quotes(data, {s: "TPEx" for s in need}, iso, url, exact, intervals)
    except RateLimited as e:
        out["rate_limited"] = True
        out["failed"] = str(e)
        return out
    except (FetchError, ValueError, KeyError) as e:
        out["failed"] = str(e)
        return out
    got = {r["symbol"]: r.get("next_ref_price_twd") for r in rows}
    stamp = today().isoformat()
    for sym, old in need.items():
        if sym not in got:
            out["absent_in_official"] += 1        # 官方該日表沒有此股：不動（也不註記，可能是缺口，由 halt_fix 處理）
            continue
        v = got[sym]
        payload = {k: old[k] for k in old.keys() if k not in ("version", "checksum", "is_current")}
        if v is None:
            note = _append_note(old["announced_at_note"], NOTE_TPEX_NONE)
            conn.execute("UPDATE daily_prices SET announced_at_note=? WHERE symbol=? AND price_date=? AND is_current=1", (note, sym, iso))
            out["none_official"] += 1
            continue
        payload["next_ref_price_twd"] = v
        ck = checksum(payload, CONTENT_KEYS)
        note = _append_note(old["announced_at_note"], NOTE_FILLED.format(d=stamp))
        conn.execute("UPDATE daily_prices SET next_ref_price_twd=?, checksum=?, announced_at_note=? "
                     "WHERE symbol=? AND price_date=? AND is_current=1 AND next_ref_price_twd IS NULL", (v, ck, note, sym, iso))
        out["filled"] += 1
    conn.commit()
    return out


def annotate_twse(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """UPDATE daily_prices SET announced_at_note = CASE WHEN COALESCE(announced_at_note,'')='' THEN ? ELSE announced_at_note || '；' || ? END
           WHERE is_current=1 AND market='TWSE' AND next_ref_price_twd IS NULL
             AND COALESCE(announced_at_note,'') NOT LIKE '%次日參考價%'""", (NOTE_TWSE, NOTE_TWSE))
    conn.commit()
    return cur.rowcount


def coverage(conn: sqlite3.Connection) -> dict:
    out = {}
    for m, st, n, k in conn.execute(
        "SELECT market, source_table, COUNT(*), SUM(next_ref_price_twd IS NOT NULL) FROM daily_prices WHERE is_current=1 GROUP BY 1,2"
    ):
        out[f"{m}/{st}"] = {"rows": n, "with_next_ref": k or 0, "pct": round(100.0 * (k or 0) / n, 2) if n else None}
    return out


def run_fix(conn: sqlite3.Connection, *, date_from=None, date_to=None, max_dates=None) -> dict:
    out: dict = {"coverage_before": coverage(conn)}
    dates = todo_dates(conn, date_from, date_to)
    out["todo_dates"] = len(dates)
    if max_dates is not None:
        dates = dates[:max_dates]
    tot = {"filled": 0, "none_official": 0, "absent_in_official": 0}
    failed: list[str] = []
    out["rate_limited"] = False
    out["requests"] = 0
    for iso in dates:
        st = fill_tpex_day(conn, iso)
        out["requests"] += 1
        for k in tot:
            tot[k] += st[k]
        if st["failed"]:
            failed.append(f"{iso}:{st['failed']}")
        if st["rate_limited"]:
            out["rate_limited"] = True
            log.error("next_ref_fix：限流，停止")
            break
    out.update(tot)
    out["failed"] = failed
    out["twse_annotated_rows"] = annotate_twse(conn)
    out["dates_left"] = len(todo_dates(conn, date_from, date_to))
    out["coverage_after"] = coverage(conn)
    return out


def main_job(*, date_from=None, date_to=None, max_dates=None) -> int:
    from pipeline.db import connect, finish_job, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, "next_ref_fix", "all", today().isoformat())
    try:
        out = run_fix(conn, date_from=date_from, date_to=date_to, max_dates=max_dates)
        ok = not out["failed"] and not out["rate_limited"] and out["dates_left"] == 0
        status = "success" if ok else "partial"
        err = None if ok else f"rate_limited={out['rate_limited']} failed={len(out['failed'])} dates_left={out['dates_left']}"
        finish_job(conn, run_id, status, [json.dumps(out, ensure_ascii=False)[:6000]], out["failed"], error=err)
        log.info("next_ref_fix %s %s", status, json.dumps(out, ensure_ascii=False)[:2000])
        return 0
    except Exception as e:   # noqa: BLE001
        finish_job(conn, run_id, "failed", [], [str(e)], error=str(e))
        log.exception("next_ref_fix")
        return 2
    finally:
        conn.close()
