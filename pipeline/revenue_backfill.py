"""全市場月營收回補（Phase 3，冪等、可續跑）：MOPS NAS 月表（sii／otc），每市場 × 月 1 頁（含全市場）。

用法（Actions）：python -m pipeline.run --job revenue_backfill [--months 24] [--max-pages 24] [--budget-min 60]

- 目標＝最近 N 個「資料月已結束」的月份，由新到舊；資料月尚未結束的月份記 not_yet_due、不發請求。
- 續跑（scope 修正）：只有 revenue_fetch_status 為 status=ok、scope='all'、且抓取時已過公告期限（due）的頁才算完成。
  舊的 sample30 'ok'（scope NULL／'sample30'，列數是過濾後的 20／10）不算，會重抓；公告期內抓到的（可能不全）下次也會重抓。
- 連續 3 頁 3xx／擋頁就停止；--max-pages＝本次最多送出幾頁；--budget-min＝時間到收尾標 partial。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from pipeline import config
from pipeline.dates import month_iter, revenue_month_phase, today
from pipeline.fetchers import revenue as rev
from pipeline.fetchers.market_prices import _universe
from pipeline.httputil import FetchError, RateLimited
from pipeline.runstats import Budget, Tally, finish_with_summary

log = logging.getLogger("pipeline.revenue_backfill")
JOB = "revenue_backfill"


def target_months(months: int) -> list[str]:
    end = today()
    ym = f"{end.year:04d}-{end.month:02d}"
    return [m for m in month_iter(ym, months + 2) if revenue_month_phase(m, end) != "too_early"][:months]


def page_done(conn: sqlite3.Connection, market: str, ym: str) -> bool:
    r = conn.execute("SELECT status, scope, fetched_at FROM revenue_fetch_status WHERE market=? AND revenue_month=?",
                     (market, ym)).fetchone()
    if not r or r[0] != "ok" or r[1] != "all":
        return False
    fetched = date.fromisoformat(str(r[2])[:10])
    return revenue_month_phase(ym, fetched) == "due"


def months_since(date_from: str) -> int:
    """從 date_from 所在月到「上個月」（含）的月數（--fix-from 用；本月營收未到公告期不抓）。"""
    d = date.fromisoformat(date_from)
    t = today()
    return (t.year - d.year) * 12 + (t.month - d.month)


def run_backfill(conn: sqlite3.Connection, *, months: int | None = None, max_pages: int | None = None,
                 budget_min: float | None = None, date_from: str | None = None) -> dict:
    tally = Tally(conn)
    budget = Budget(budget_min)
    if date_from:
        months = months_since(date_from)
    ms = target_months(months or config.ALL_REVENUE_MONTHS)
    symset = set(_universe(conn, include_delisted=(ms[-1] + "-01") if ms else None))
    out: dict = {"months": ms, "pages_total": len(ms) * 2, "fetched": 0, "skipped_done": 0, "rows": 0, "failed": [],
                 "stopped": None, "universe": len(symset)}
    streak = 0
    for ym in ms:
        for market in ("TWSE", "TPEx"):
            if page_done(conn, market, ym):
                out["skipped_done"] += 1
                continue
            if max_pages is not None and out["fetched"] >= max_pages:
                out["stopped"] = "max_pages"
                break
            if budget.expired(reserve_s=60):
                out["stopped"] = "budget"
                break
            try:
                n = rev.fetch_month(conn, ym, symset, market)
                out["fetched"] += 1
                out["rows"] += n
                streak = 0
            except RateLimited as e:
                out["fetched"] += 1
                out["failed"].append(f"{market}:{ym}:{e}")
                out["stopped"] = "rate_limited"
                break
            except FetchError as e:
                out["fetched"] += 1
                out["failed"].append(f"{market}:{ym}:{e}")
                streak = streak + 1 if ("redirect/http=" in str(e) or e.blocked) else 0
                if streak >= config.REDIRECT_STREAK_STOP:
                    out["stopped"] = f"consecutive_redirects:{streak}"
                    break
        if out["stopped"]:
            break
    out["pages_left"] = sum(1 for ym in ms for mk in ("TWSE", "TPEx") if not page_done(conn, mk, ym))
    out["tally"] = tally.summary()
    return out


def main_job(*, months: int | None = None, max_pages: int | None = None, budget_min: float | None = None,
             date_from: str | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, months=months, max_pages=max_pages, budget_min=budget_min, date_from=date_from)
        ok = out["pages_left"] == 0 and not out["failed"] and not out["stopped"]
        status = "success" if ok else "partial"
        finish_with_summary(conn, run_id, status, out, out["failed"],
                            None if ok else f"pages_left={out['pages_left']} stopped={out['stopped']} failed={len(out['failed'])}")
        log.info("revenue_backfill %s %s", status, out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("revenue_backfill")
        return 2
    finally:
        conn.close()
