"""交易日曆歷史回補（冪等）：holidaySchedule（openapi）只有當年，歷史年度改用 FMTQIK（上市月表）＋st41（上櫃月表）交叉。
daily／evening 已處理「當年與前一年」，本作業補更早的年度：--fix-from 2023-11-01 → 2023（11–12 月）、2024。

每個月 2 個請求（FMTQIK、st41）；2023-11～2024-12 共 14 個月＝28 個請求。
用法：python -m pipeline.run --job calendar_backfill --fix-from 2023-11-01
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from pipeline.dates import today
from pipeline.fetchers import calendar as cal
from pipeline.runstats import Tally, finish_with_summary

log = logging.getLogger("pipeline.calendar_backfill")
JOB = "calendar_backfill"


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None) -> dict:
    tally = Tally(conn)
    start = date.fromisoformat(date_from or "2023-11-01")
    last_year = today().year - 2         # 當年與前一年由 daily／evening 的 calendar 處理
    out: dict = {"from": start.isoformat(), "years": [], "rows": 0, "failed": []}
    for y in range(start.year, last_year + 1):
        try:
            out["rows"] += cal.fetch_and_store(conn, y, start=start)
            out["years"].append(y)
        except Exception as e:  # noqa: BLE001
            out["failed"].append(f"{y}:{e}")
            log.exception("calendar_backfill %s", y)
            break
    r = conn.execute("SELECT MIN(calendar_date), MAX(calendar_date), SUM(is_trade_day), COUNT(*) FROM trading_calendar WHERE calendar_date>=?",
                     (start.isoformat(),)).fetchone()
    out["calendar"] = {"min": r[0], "max": r[1], "trade_days": r[2], "rows": r[3]}
    out["tally"] = tally.summary()
    return out


def main_job(*, date_from: str | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, date_from=date_from)
        ok = not out["failed"]
        finish_with_summary(conn, run_id, "success" if ok else "partial", out, out["failed"], None if ok else "failed years")
        log.info("calendar_backfill %s", out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        return 2
    finally:
        conn.close()
