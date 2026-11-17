"""下市（終止上市／上櫃）清單回補（冪等）：上市 1 個請求＋上櫃每年 1 個請求（2023-11 起共 4 個）。

用法：python -m pipeline.run --job delist_backfill [--fix-from 2023-11-01]
結果寫入 `delistings`（見 docs/schema.md §3.27），並把已下市代號寫入 `securities_history`、填 `last_trade_date`；並回報與現行 securities 重疊的代號（改上市櫃或代號重用）。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from pipeline import config
from pipeline.dates import today
from pipeline.fetchers import delistings
from pipeline.httputil import FetchError, RateLimited
from pipeline.runstats import Tally, finish_with_summary

log = logging.getLogger("pipeline.delist_backfill")
JOB = "delist_backfill"


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None) -> dict:
    tally = Tally(conn)
    start = date.fromisoformat(date_from or "2023-11-01")
    out: dict = {"from": start.isoformat(), "failed": [], "stopped": None}
    for name, fn in (("twse", lambda: delistings.fetch_twse(conn)),
                     ("tpex", lambda: delistings.fetch_tpex(conn, list(range(start.year, today().year + 1))))):
        try:
            out[name] = fn()
        except RateLimited as e:
            out["failed"].append(f"{name}:rate_limited:{e}")
            out["stopped"] = f"rate_limited:{name}"
            break
        except FetchError as e:
            out["failed"].append(f"{name}:{e}")
    out["securities_history"] = delistings.sync_securities_history(conn)          # 0 請求：已下市代號寫入 securities_history（valid_to＝終止日）
    out["last_trade"] = delistings.fill_last_trade_dates(conn)                    # 0 請求：只取 DB 內最後一筆官方成交日，缺行情則留空
    out["since_from"] = {r[0]: r[1] for r in conn.execute(
        "SELECT market, COUNT(*) FROM delistings WHERE delist_date>=? GROUP BY market", (start.isoformat(),))}
    out["also_in_current_list"] = [f"{r[0]}:{r[1]}:{r[2]}" for r in conn.execute(
        "SELECT d.market, d.symbol, d.delist_date FROM delistings d JOIN securities s ON s.symbol=d.symbol AND s.is_current=1 WHERE d.delist_date>=? ORDER BY 3", (start.isoformat(),))]
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
        ok = not out["failed"] and not out["stopped"]
        finish_with_summary(conn, run_id, "success" if ok else "partial", out, out["failed"], None if ok else f"stopped={out['stopped']}")
        log.info("delist_backfill %s", out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("delist_backfill")
        return 2
    finally:
        conn.close()
