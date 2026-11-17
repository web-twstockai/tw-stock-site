"""停牌歷史回補（冪等）：先 1 個 openapi 請求（含權證，修補舊版漏掉的恢復列），再用櫃買網頁版 bulletin/sprcHis 每年 1 個請求（只收上櫃股票；暫停列與恢復列配對），
上市 TWTAWU 1 個請求（--no-twse 可略過；本機請勿打，留給 Actions）。完成後離線套用統一停牌規則（0 請求）。

用法：python -m pipeline.run --job halts_backfill --fix-from 2023-11-01 [--no-fetch（＝不打上市）]
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from pipeline import config
from pipeline.dates import today
from pipeline.fetchers import halts
from pipeline.httputil import FetchError, RateLimited, single_shot
from pipeline.runstats import Tally, finish_with_summary

log = logging.getLogger("pipeline.halts_backfill")
JOB = "halts_backfill"


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None, twse: bool = True) -> dict:
    tally = Tally(conn)
    start = date.fromisoformat(date_from or config.ALL_BACKFILL_START)
    end = today()
    out: dict = {"window": [start.isoformat(), end.isoformat()], "failed": [], "stopped": None}
    years = list(range(start.year, end.year + 1))
    try:
        out["tpex_openapi"] = halts.fetch_tpex(conn)          # 1 請求：openapi 近年全類別（含權證）；修補舊版丟掉恢復列的 183 筆
        out["tpex"] = halts.fetch_tpex_history(conn, years)
    except RateLimited as e:
        out["failed"].append(f"tpex:rate_limited:{e}")
        out["stopped"] = "rate_limited:tpex"
    except FetchError as e:
        out["failed"].append(f"tpex:{e}")
    if twse and not out["stopped"]:
        try:
            with single_shot():
                out["twse"] = halts.fetch_twse(conn, start, end)
        except (RateLimited, FetchError) as e:
            out["failed"].append(f"twse:{e}")
            out["stopped"] = "blocked:twse"
    from pipeline import halt_fix
    hr = halt_fix.apply_unified_rule(conn)
    out["halt_rule"] = {"by_basis": hr["resume_rule"].get("by_basis"), "flags": hr["recompute"].get("counts")}
    out["tally"] = tally.summary()
    return out


def main_job(*, date_from: str | None = None, twse: bool = True) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, date_from=date_from, twse=twse)
        ok = not out["failed"] and not out["stopped"]
        finish_with_summary(conn, run_id, "success" if ok else "partial", out, out["failed"], None if ok else f"stopped={out['stopped']}")
        log.info("halts_backfill %s", out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("halts_backfill")
        return 2
    finally:
        conn.close()
