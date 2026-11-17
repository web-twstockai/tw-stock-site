"""全市場公司行動回補（Phase 3，冪等、可續跑）：除權息、減資恢復買賣、面額變更。

用法（Actions）：python -m pipeline.run --job corp_backfill [--fix-from 2026-03-02] [--max-enrich 60] [--budget-min 30]
        python -m pipeline.run --job corp_backfill --max-enrich 0     # 只跑區間查詢（7 個請求）

- 擋頁規則：區間端點每個只送 1 次請求；某主機被擋即停止該主機區間請求，並在 backfill_state 記時間，60 分鐘內不再送。
- 6 個官方區間查詢（TWSE TWT49U／TWTAUU／TWTB8U(+B7U)、TPEx exDailyQ／revivt／pvChgRslt），每個 1 個請求涵蓋整段；
  各自獨立 try，失敗只記錄、不影響其他。
- MOPS t05st09_2 補強上市「權／權息」的配股比例：逐代號 1 個請求，--max-enrich 限量（以代號計；試過的往後排），0＝不補強。
- upsert 全部是版本化冪等寫入；重跑同值 unchanged。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime

from pipeline import config
from pipeline.dates import now_iso, today
from pipeline.fetchers import corporate_actions as ca
from pipeline.fetchers.market_prices import _universe
from pipeline.httputil import FetchError, RateLimited, single_shot
from pipeline.runstats import Budget, Tally, finish_with_summary, state_get, state_set

log = logging.getLogger("pipeline.corp_backfill")
JOB = "corp_backfill"


def _blocked_recently(conn: sqlite3.Connection, host: str) -> float:
    """該主機區間端點最近一次被擋距今幾分鐘（沒有紀錄或已恢復成功＝回傳很大的數）。"""
    r = state_get(conn, JOB, f"block:{host}")
    if not r or r[0] != "blocked" or not r[2]:
        return 1e9
    try:
        t = datetime.fromisoformat(r[2])
    except ValueError:
        return 1e9
    return (datetime.now(t.tzinfo) - t).total_seconds() / 60.0


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None, max_enrich: int | None = 60,
                 budget_min: float | None = None) -> dict:
    """擋頁規則（2026-11-17 起）：
    - 區間端點每個只送 1 次請求（httputil.single_shot，不重試）。
    - 某主機任一區間端點被擋（3xx／擋頁／限流）→ 本次停止該主機所有區間請求，並把時間記進 backfill_state(block:<主機>)；
      之後 RANGE_BLOCK_COOLDOWN_MIN（60）分鐘內任何執行（含 Actions 換機器）都不再送該主機的區間請求。
    - 另一主機（TPEx）不受影響；MOPS 配股補強另計。"""
    tally = Tally(conn)
    budget = Budget(budget_min)
    start = date.fromisoformat(date_from or config.ALL_BACKFILL_START)
    end = today()
    syms = set(_universe(conn, include_delisted=start.isoformat()))
    n_cur = conn.execute("SELECT COUNT(*) FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1").fetchone()[0]
    out: dict = {"window": [start.isoformat(), end.isoformat()], "universe": len(syms), "universe_current": n_cur,
                 "universe_delisted_in_window": len(syms) - n_cur, "ranges": {}, "failed": [],
                 "stopped": None, "stopped_hosts": [], "skipped": {}}
    if not conn.execute("SELECT 1 FROM delistings LIMIT 1").fetchone():
        out["warnings"] = ["delistings 是空的：視窗內下市股的除權息會被過濾掉；請先跑 delist_backfill 再跑 corp_backfill"]
        log.warning("corp_backfill: %s", out["warnings"][0])
    steps = [
        ("TWSE", "twse_ex", ca.fetch_twse_exrights),
        ("TWSE", "twse_cr_restore", ca.fetch_twse_twtauu),
        ("TWSE", "twse_par_value", ca.fetch_twse_twtb8u),
        ("TPEx", "tpex_ex", ca.fetch_tpex_exrights),
        ("TPEx", "tpex_cr_restore", ca.fetch_tpex_revivt),
        ("TPEx", "tpex_par_value", ca.fetch_tpex_pv_chg),
    ]
    stopped_hosts: set[str] = set()
    for host, name, fn in steps:
        if budget.expired():
            out["stopped"] = "budget"
            break
        if host in stopped_hosts:
            out["skipped"][name] = "host_stopped_after_block"
            continue
        ago = _blocked_recently(conn, host)
        if ago < config.RANGE_BLOCK_COOLDOWN_MIN:
            stopped_hosts.add(host)
            out["stopped_hosts"].append(host)
            out["skipped"][name] = f"cooldown:blocked_{ago:.0f}min_ago<{config.RANGE_BLOCK_COOLDOWN_MIN}"
            continue
        try:
            with single_shot():
                out["ranges"][name] = fn(conn, syms, start, end)
            if state_get(conn, JOB, f"block:{host}"):
                state_set(conn, JOB, f"block:{host}", "ok", now_iso())
        except (RateLimited, FetchError) as e:
            out["failed"].append(f"{name}:{'rate_limited:' if isinstance(e, RateLimited) else ''}{e}")
            if isinstance(e, RateLimited) or "redirect/http=" in str(e) or e.blocked:
                stopped_hosts.add(host)
                out["stopped_hosts"].append(host)
                state_set(conn, JOB, f"block:{host}", "blocked", now_iso(), bump=True)
                conn.commit()
        except Exception as e:  # noqa: BLE001
            out["failed"].append(f"{name}:{type(e).__name__}:{e}")
            log.exception("corp_backfill %s", name)
    conn.commit()
    if max_enrich != 0 and not budget.expired():
        try:
            out["mops_div_enrich"] = ca.enrich_twse_stock_dividends_from_mops(conn, syms, max_items=max_enrich)
        except Exception as e:  # noqa: BLE001
            out["failed"].append(f"enrich:{e}")
            log.exception("corp_backfill enrich")
    cs: dict = {}
    for m, k, sc, n in conn.execute(
            "SELECT market, action_kind, scope, symbol_count FROM corporate_actions_coverage WHERE coverage_start=? ORDER BY 1,2,3", (start.isoformat(),)):
        cs.setdefault(f"{m}/{k}", {})[sc] = n          # 同一區間可有多個 scope 列（主鍵含 scope）；值＝symbol_count
    out["coverage_scope"] = cs
    out["pending_ratio_rows"] = ca.pending_ratio_rows(conn)
    if (out.get("mops_div_enrich") or {}).get("stopped") and not out["stopped"]:
        out["stopped"] = "mops:" + out["mops_div_enrich"]["stopped"]
    if out["stopped_hosts"] and not out["stopped"]:
        out["stopped"] = "blocked:" + ",".join(out["stopped_hosts"])
    out["tally"] = tally.summary()
    return out


def main_job(*, date_from: str | None = None, max_enrich: int | None = 60, budget_min: float | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, date_from=date_from, max_enrich=max_enrich, budget_min=budget_min)
        enr = out.get("mops_div_enrich") or {}
        pending_left = max((enr.get("pending") or 0) - (enr.get("filled") or 0), 0) if enr else 0
        ok = not out["failed"] and not out["stopped"]
        status = "success" if ok else "partial"
        out["enrich_pending_after"] = pending_left
        finish_with_summary(conn, run_id, status, out, out["failed"], None if ok else f"stopped={out['stopped']} failed={len(out['failed'])}")
        log.info("corp_backfill %s %s", status, out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("corp_backfill")
        return 2
    finally:
        conn.close()
