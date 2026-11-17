"""全市場財報回補（Phase 3，冪等、可續跑）：MOPS 綜合損益（ajax_t163sb04）與現金流量（ajax_t163sb20）。

用法（Actions）：python -m pipeline.run --job financials_backfill [--quarters 8] [--max-pages 16] [--budget-min 60]

- 每「季 × 市場（sii／otc）× 表」1 頁（含全市場），8 季共 32 頁；由新到舊。每頁都記 financial_fetch_status（ok／empty／fail／blocked、scope、attempts）。
- 續跑：同範圍（scope=all）已 ok 的頁略過；失敗頁下次只重試失敗的。
- 頁級重試：主流程後把本次失敗頁再試一次（httputil 內另有每請求 3 次嘗試、總時限 150 秒）。
- 連續 3 頁 3xx（307）／擋頁就停止（視為 MOPS 限流），留待下次；不拖住整個 job。
- --max-pages＝本次最多送出幾頁請求；--budget-min＝時間到收尾，標 partial。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from pipeline import config
from pipeline.dates import quarter_iter, today
from pipeline.fetchers import financials as fin
from pipeline.fetchers.market_prices import _universe
from pipeline.runstats import Budget, Tally, finish_with_summary

log = logging.getLogger("pipeline.financials_backfill")
JOB = "financials_backfill"


def filing_deadline(y: int, q: int) -> date:
    return {1: date(y, 5, 15), 2: date(y, 8, 14), 3: date(y, 11, 14), 4: date(y + 1, 3, 31)}[q]


def quarters_since(date_from: str, ey: int, eq: int) -> list[tuple[int, int]]:
    """申報期限 ≥ date_from 的季別（由新到舊）：--fix-from 2023-11-01 → 2023Q3 起（2023Q3 期限 2023-11-14）。"""
    d0 = date.fromisoformat(date_from)
    return [(y, q) for (y, q) in quarter_iter(ey, eq, 60) if filing_deadline(y, q) >= d0]


def run_backfill(conn: sqlite3.Connection, *, quarters: int | None = None, max_pages: int | None = None,
                 budget_min: float | None = None, date_from: str | None = None) -> dict:
    tally = Tally(conn)
    budget = Budget(budget_min)
    ey, eq = fin._latest_filed_quarter()
    qs = quarters_since(date_from, ey, eq) if date_from else quarter_iter(ey, eq, quarters or config.ALL_FINANCIAL_QUARTERS)
    symset = set(_universe(conn, include_delisted=date_from or None))
    pages = fin.build_pages(qs)
    r = fin.fetch_pages(conn, pages, symset, budget=budget, max_pages=max_pages, skip_done=True, retry_failed=True)
    r["quarters"] = [f"{y}Q{q}" for y, q in qs]
    left = [pg for pg in pages if not fin.page_done(conn, pg[2], pg[0], pg[1], pg[3], "all")]
    r["pages_left"] = len(left)
    r["universe"] = len(symset)
    r["tally"] = tally.summary()
    return r


def main_job(*, quarters: int | None = None, max_pages: int | None = None, budget_min: float | None = None,
             date_from: str | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, quarters=quarters, max_pages=max_pages, budget_min=budget_min, date_from=date_from)
        ok = out["pages_left"] == 0 and not out["failed"] and not out["stopped"]
        status = "success" if ok else "partial"
        finish_with_summary(conn, run_id, status, out, out["failed"],
                            None if ok else f"pages_left={out['pages_left']} stopped={out['stopped']} failed={len(out['failed'])}")
        log.info("financials_backfill %s %s", status, out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("financials_backfill")
        return 2
    finally:
        conn.close()
