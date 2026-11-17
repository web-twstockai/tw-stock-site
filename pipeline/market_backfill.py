"""全市場行情回補（Phase 3，冪等、可續跑）：TWSE MI_INDEX＋TPEx dailyQuotes（含鉅額口徑），由新到舊。

用法（Actions）：
  python -m pipeline.run --job market_backfill [--fix-from 2026-03-02] [--fix-to YYYY-MM-DD] [--max-dates 40] [--budget-min 80]

- 目標日期＝ trading_calendar 中 [fix-from, fix-to] 的交易日（預設 2026-03-02 ～ 昨日，與 sample30 起始日一致）。
- 檢查點在 DB：以「該日該市場現行普通股列數 ≥ FULL_DAY_RATIO × 該市場現行普通股數」判斷該市場該日已完成，已完成的不發請求；
  交易所明確回「無資料」的日期記在 backfill_state（no_data），連續 2 次仍無資料就不再重試。
- 每個日期每個缺的市場各 1 個請求（TWSE ≥3.5 秒、TPEx ≥5 秒節流）。上櫃鉅額歷史／當日各 1 個請求（整個作業共約 2 個）。
- 停止條件：--max-dates、--budget-min（時間到收尾，標 partial）、上櫃限流（302→/errors、429）、連續 3 次擋頁／錯誤。
- 寫入沿用 _upsert_preserving（版本化；既有 STOCK_DAY／tradingStock 列依原規則），重跑同值為 unchanged。
- 摘要寫入 job_runs.success_items：請求數（依主機）、耗時、新增列數、峰值 RSS、各日期結果。
"""
from __future__ import annotations

import logging
import sqlite3

from pipeline import config
from pipeline.dates import today
from pipeline.fetchers import tpex_block
from pipeline.fetchers.calendar import recent_trade_days  # noqa: F401  (保持與 market_prices 同源)
from pipeline.fetchers.market_prices import _universe, fetch_tpex_day, fetch_twse_day, markets_needed
from pipeline.fetchers.price_fields import load_halt_coverage
from pipeline.runstats import Budget, Tally, finish_with_summary, state_get, state_set

log = logging.getLogger("pipeline.market_backfill")
JOB = "market_backfill"


def target_dates(conn: sqlite3.Connection, date_from: str | None, date_to: str | None) -> list[str]:
    d0 = date_from or config.ALL_BACKFILL_START
    d1 = date_to or (today().isoformat())
    # 預設結束日＝昨日：當日資料要等收盤後才有，當日由 daily 負責
    if date_to is None:
        from datetime import timedelta
        d1 = (today() - timedelta(days=1)).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT calendar_date FROM trading_calendar WHERE is_trade_day=1 AND calendar_date>=? AND calendar_date<=? "
        "ORDER BY calendar_date DESC", (d0, d1))]


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None, date_to: str | None = None,
                 max_dates: int | None = None, budget_min: float | None = None) -> dict:
    tally = Tally(conn)
    budget = Budget(budget_min)
    universe = _universe(conn, include_delisted=date_from or config.ALL_BACKFILL_START)
    dates = target_dates(conn, date_from, date_to)
    out: dict = {"target_dates": len(dates), "universe": {m: sum(1 for v in universe.values() if v == m) for m in ("TWSE", "TPEx")},
                 "dates_done": [], "dates_failed": {}, "dates_skipped_done": 0, "dates_no_data": [],
                 "stopped": None, "rate_limited": False}
    exact, intervals = load_halt_coverage(conn)
    http_events: list = []
    ctx = None
    streak = {"TWSE": 0, "TPEx": 0}      # 各市場連續錯誤／擋頁次數（任一達 3 就停）
    processed = 0
    last_dur = 0.0
    for iso in dates:
        need = markets_needed(conn, iso, universe)
        if not need:
            out["dates_skipped_done"] += 1
            continue
        nd = state_get(conn, JOB, f"no_data:{iso}")
        if nd and nd[0] == "no_data" and nd[1] >= 2:
            out["dates_no_data"].append(iso)
            continue
        if max_dates is not None and processed >= max_dates:
            out["stopped"] = "max_dates"
            break
        if budget.expired(reserve_s=min(last_dur * 1.5, 600)):
            out["stopped"] = "budget"
            break
        t_day = budget.elapsed()
        res: dict = {}
        if "TWSE" in need:
            tw = fetch_twse_day(conn, iso, universe, http_events, exact=exact, intervals=intervals)
            res["TWSE"] = tw["twse"]
            if tw["failed"]:
                out["dates_failed"].setdefault(iso, []).extend(tw["failed"])
                if tw.get("no_data") and not tw["blocked"]:
                    state_set(conn, JOB, f"no_data:{iso}", "no_data", "TWSE", bump=True)
                    conn.commit()
                else:
                    streak["TWSE"] += 1
                if tw.get("rate_limited"):
                    out["rate_limited"] = True
                    out["stopped"] = "rate_limited:TWSE"
                    break
            else:
                streak["TWSE"] = 0
        if "TPEx" in need:
            if ctx is None:
                ctx = tpex_block.get_context(conn)
            tp = fetch_tpex_day(conn, iso, universe, http_events, exact=exact, intervals=intervals, block_ctx=ctx)
            res["TPEx"] = tp["tpex"]
            if tp["failed"]:
                out["dates_failed"].setdefault(iso, []).extend(tp["failed"])
                streak["TPEx"] += 1
            else:
                streak["TPEx"] = 0
            if tp.get("rate_limited"):
                out["rate_limited"] = True
                out["stopped"] = "rate_limited:TPEx"
                break
        processed += 1
        last_dur = budget.elapsed() - t_day
        left = markets_needed(conn, iso, universe)
        if not left:
            out["dates_done"].append(iso)
            state_set(conn, JOB, iso, "done", str(res))
        conn.commit()
        log.info("market_backfill %s need=%s wrote=%s left=%s %.1fs", iso, need, res, left, last_dur)
        worst = max(streak, key=streak.get)
        if streak[worst] >= config.REDIRECT_STREAK_STOP:
            out["stopped"] = f"consecutive_errors:{worst}:{streak[worst]}"
            break
    remaining = [d for d in dates if markets_needed(conn, d, universe)]
    out["dates_left"] = len(remaining)
    out["oldest_left"] = remaining[-1] if remaining else None
    out["newest_left"] = remaining[0] if remaining else None
    out["block_requests"] = ctx.requests if ctx is not None else 0
    out["tally"] = tally.summary()
    return out


def main_job(*, date_from: str | None = None, date_to: str | None = None, max_dates: int | None = None,
             budget_min: float | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, date_from=date_from, date_to=date_to, max_dates=max_dates, budget_min=budget_min)
        try:   # 停牌統一規則（離線）：新補的歷史日要依官方停牌＋成交推定重算停牌旗標
            from pipeline import halt_fix
            hr = halt_fix.apply_unified_rule(conn)
            out["halt_rule"] = {"by_basis": hr["resume_rule"].get("by_basis"), "flags": hr["recompute"].get("counts")}
        except Exception as e:  # noqa: BLE001
            out["halt_rule"] = {"error": str(e)}
            log.warning("halt_rule: %s", e)
        try:   # 新補的行情可能讓下市股的最後成交日可判定（離線、不產生版本）
            from pipeline.fetchers.delistings import fill_last_trade_dates
            out["last_trade"] = fill_last_trade_dates(conn)
        except Exception as e:  # noqa: BLE001
            out["last_trade"] = {"error": str(e)}
            log.warning("last_trade: %s", e)
        failed = [f"{d}:{'|'.join(v)}" for d, v in out["dates_failed"].items()]
        ok = out["dates_left"] == 0 and not failed and not out["rate_limited"]
        status = "success" if ok else "partial"
        err = None if ok else f"dates_left={out['dates_left']} stopped={out['stopped']} failed_dates={len(failed)}"
        finish_with_summary(conn, run_id, status, out, failed, err)
        log.info("market_backfill %s %s", status, out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("market_backfill")
        return 2
    finally:
        conn.close()
