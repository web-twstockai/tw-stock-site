"""一次性作業（可重跑、冪等）：上櫃成交量口徑改為 dailyQuotes（含鉅額）並補鉅額欄位。

用法（Actions）：python -m pipeline.run --job tpex_volume_fix [--fix-from YYYY-MM-DD] [--fix-to YYYY-MM-DD] [--max-dates N]

做三件事：
1. 找出 DB 內現行、來源為 tradingStock 的上櫃列（sample30 上櫃，約 1,760 列）涉及的交易日；
   每個日期以 dailyQuotes（1 個請求）取官方含鉅額值，版本化覆寫（舊列 is_current=0，新列 change_note 標明換源）。
   已完成（該日現有列全為 dailyQuotes／incl_block）的日期略過 → 重跑不會重複請求或重複升版。
2. 取不到 dailyQuotes 的日期：保留原 tradingStock 列（volume_basis=excl_block、source_table=tradingStock），列入回報，不補造。
3. 以鉅額歷史彙總（名稱比對）就地補齊「現行 dailyQuotes 列中 block 欄位仍為 NULL」的列（只填 NULL、不改量額與版本）。

限流（302→/errors、429）立即停止，主機熔斷，剩餘日期留待下次。TPEx 請求間隔 ≥5 秒（config.TPEX_INTERVAL）。
"""
from __future__ import annotations

import logging
import sqlite3

from pipeline import config
from pipeline.db import checksum
from pipeline.fetchers import tpex_block
from pipeline.fetchers.prices import CONTENT_KEYS, fetch_tpex_quotes_days

log = logging.getLogger("pipeline.tpex_volume_fix")


def target_dates(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None) -> tuple[list[str], list[str]]:
    """回傳 (有 tradingStock 現行列的交易日, 這些列涉及的代號)。"""
    q = "SELECT DISTINCT price_date FROM daily_prices WHERE is_current=1 AND market='TPEx' AND source_table='tradingStock'"
    args: list = []
    if date_from:
        q += " AND price_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND price_date<=?"
        args.append(date_to)
    dates = [r[0] for r in conn.execute(q + " ORDER BY price_date", args)]
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily_prices WHERE is_current=1 AND market='TPEx' AND source_table='tradingStock' ORDER BY symbol")]
    return dates, syms


def fill_block_in_place(conn: sqlite3.Connection, ctx: tpex_block.BlockContext, *, date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """現行 dailyQuotes 列中 block_source IS NULL 者，依逐筆／歷史彙總補 block 欄位（就地，只填 NULL；量額與版本不變）。"""
    q = ("SELECT * FROM daily_prices WHERE is_current=1 AND market='TPEx' AND source_table='dailyQuotes' "
         "AND block_source IS NULL")
    args: list = []
    if date_from:
        q += " AND price_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND price_date<=?"
        args.append(date_to)
    filled = untouched = 0
    cur = conn.execute(q, args)
    cols = [d[0] for d in cur.description]
    batch = []
    for tup in cur.fetchall():
        row = dict(zip(cols, tup))
        res = ctx.lookup(row["price_date"], row["symbol"])
        if res is None:
            untouched += 1
            continue
        row["block_shares"], row["block_amount_twd"], row["block_source"] = res
        batch.append((row["block_shares"], row["block_amount_twd"], row["block_source"],
                      checksum(row, CONTENT_KEYS), row["symbol"], row["price_date"], row["version"]))
        filled += 1
    conn.executemany(
        "UPDATE daily_prices SET block_shares=?, block_amount_twd=?, block_source=?, checksum=? "
        "WHERE symbol=? AND price_date=? AND version=? AND block_source IS NULL",
        batch,
    )
    conn.commit()
    return {"filled": filled, "undetermined": untouched}


def run_fix(conn: sqlite3.Connection, *, date_from: str | None = None, date_to: str | None = None,
            max_dates: int | None = None) -> dict:
    dates, syms = target_dates(conn, date_from, date_to)
    out: dict = {"target_dates": len(dates), "symbols": syms, "fetched_days": 0, "failed_days": [], "rate_limited": False,
                 "block_fill": None, "left_tradingStock_dates": []}
    if dates:
        tp = fetch_tpex_quotes_days(conn, syms, dates, max_days=max_dates)
        out["fetched_days"] = tp["fetched_days"]
        out["skipped_days"] = tp["skipped_days"]
        out["failed_days"] = tp["failed_days"]
        out["rate_limited"] = tp["rate_limited"]
        out["failed"] = tp["failed"]
    ctx = tpex_block.get_context(conn, fetch=not out["rate_limited"])
    if not out["rate_limited"]:
        out["block_fill"] = fill_block_in_place(conn, ctx, date_from=date_from, date_to=date_to)
    # 仍是 tradingStock（不含鉅額）的列：標註來源與口徑，不補造
    out["left_tradingStock_dates"] = [r[0] for r in conn.execute(
        "SELECT DISTINCT price_date FROM daily_prices WHERE is_current=1 AND market='TPEx' AND source_table='tradingStock' "
        "AND price_date>=? AND price_date<=? ORDER BY price_date", (date_from or "0000-00-00", date_to or "9999-99-99"))]
    out["left_tradingStock_rows"] = conn.execute(
        "SELECT COUNT(*) FROM daily_prices WHERE is_current=1 AND market='TPEx' AND source_table='tradingStock'").fetchone()[0]
    out["block_requests"] = ctx.requests
    return out


def main_job(*, date_from: str | None = None, date_to: str | None = None, max_dates: int | None = None) -> int:
    from pipeline.db import connect, finish_job, init_db, start_job
    from pipeline.dates import today
    from pipeline.deadline import assert_no_other_pipeline
    import json

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, "tpex_volume_fix", "sample30", today().isoformat())
    try:
        out = run_fix(conn, date_from=date_from, date_to=date_to, max_dates=max_dates)
        ok = not out["failed_days"] and not out["rate_limited"] and not out["left_tradingStock_dates"]
        status = "success" if ok else "partial"
        err = None if ok else (
            f"rate_limited={out['rate_limited']} failed_days={len(out['failed_days'])} "
            f"left_tradingStock_rows={out['left_tradingStock_rows']}")
        finish_job(conn, run_id, status, [json.dumps(out, ensure_ascii=False)[:4000]], out.get("failed") or [], error=err)
        log.info("tpex_volume_fix %s %s", status, out)
        return 0
    except Exception as e:   # noqa: BLE001
        finish_job(conn, run_id, "failed", [], [str(e)], error=str(e))
        log.exception("tpex_volume_fix")
        return 2
    finally:
        conn.close()
