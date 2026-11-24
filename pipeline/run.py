"""單一入口：python -m pipeline.run --job daily|evening --symbols sample30|all"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from pipeline import config
from pipeline.coverage import max_rss_mb, rebuild_universes, write_coverage_json, record_summary
from pipeline.db import connect, finish_job, init_db, start_job
from pipeline.dates import today
from pipeline.deadline import assert_no_other_pipeline, parse_stop_before, should_stop
from pipeline.fetchers import calendar, corporate_actions, financials, halts, indices, material, prices, revenue, securities
from pipeline.fetchers import market_prices, material_batch
from pipeline.quality import check_db
from pipeline import analysis_bridge

LOG_DIR = config.LOG_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / f"pipeline_{today().isoformat()}.log", encoding="utf-8"),
        ],
    )


def resolve_symbols(conn, mode: str) -> list[str]:
    if mode == "sample30":
        return list(config.SAMPLE30)
    rows = conn.execute(
        """SELECT symbol FROM securities
           WHERE is_current=1 AND COALESCE(is_common,1)=1
           ORDER BY symbol"""
    ).fetchall()
    return [r[0] for r in rows]


def run_job(job: str, symbols_mode: str, *, stop_before_s: str | None = None, budget_min: float | None = None) -> int:
    setup_logging()
    log = logging.getLogger("pipeline.run")
    assert_no_other_pipeline()
    stop_before = parse_stop_before(stop_before_s)
    if budget_min and stop_before is None:
        # --budget-min：換算成 stop-before；時間到時沿用既有的「interrupted、可續跑」語意
        from pipeline.runstats import stop_before_from_budget
        stop_before = stop_before_from_budget(budget_min)
    if stop_before:
        log.info("stop-before=%s", stop_before.isoformat())
    conn = connect()
    init_db(conn)
    symbols = resolve_symbols(conn, symbols_mode)
    # sample30 需先有 securities；若空則先抓清單
    if symbols_mode == "sample30" and not conn.execute("SELECT 1 FROM securities LIMIT 1").fetchone():
        log.info("securities empty → fetch ISIN first")
        securities.fetch_and_store(conn)

    run_id = start_job(conn, job, symbols_mode, today().isoformat())
    from pipeline.runstats import Tally
    tally = Tally(conn)
    success: list[str] = []
    failed: list[str] = []
    try:
        # 共用前置
        try:
            calendar.fetch_and_store(conn)
            success.append("calendar")
        except Exception as e:
            failed.append(f"calendar:{e}")
            log.exception("calendar")

        if job in ("daily", "evening"):
            try:
                skip = os.environ.get("PIPELINE_SKIP_ISIN") == "1"
                if skip:
                    # 雲端（GitHub Actions）isin.twse.com.tw 約 13 KB/s、10 MB 要 12 分鐘以上：預設略過，沿用 DB 內的清單。
                    # 但 DB 內清單不足（空庫／壞檔）時忽略旗標照抓，絕不在 0 檔下「成功」。
                    can_skip, cnts = securities.should_skip_isin(conn)
                    if not can_skip:
                        log.warning("PIPELINE_SKIP_ISIN=1 但 DB 內普通股清單不足 %s（門檻 %s）→ 忽略旗標，照抓 isin",
                                    cnts, securities.MIN_COMMON_TO_SKIP)
                        skip = False
                if skip:
                    st = f"skipped(PIPELINE_SKIP_ISIN=1，沿用 DB 內證券清單 {cnts})"
                    log.info("securities: %s", st)
                    securities.note_isin_skipped(conn, "雲端略過 isin")
                else:
                    st = securities.fetch_and_store(conn)
                success.append(f"securities:{st}")
                try:
                    ust = rebuild_universes(conn)
                    success.append(f"universes:{ust.get('counts')}")
                except Exception as ue:
                    log.warning("rebuild_universes: %s", ue)
            except Exception as e:
                failed.append(f"securities:{e}")
                log.exception("securities")

            # refresh symbol markets after securities
            symbols = resolve_symbols(conn, symbols_mode)

            try:
                # daily：只抓近幾日增量；evening：補滿觀察窗
                # sample30 維持逐檔 STOCK_DAY／tradingStock（不破壞既有列與冪等）
                # all 用 MI_INDEX／dailyQuotes 整批
                if symbols_mode == "all":
                    depth = config.DAILY_PRICE_TRADE_DAYS if job == "daily" else config.ALL_PRICE_TRADE_DAYS
                    if should_stop(stop_before):
                        raise InterruptedError("stop-before before prices")
                    st = market_prices.fetch_market_prices(
                        conn, trade_days=depth, stop_before=stop_before,
                        run_id=run_id, resume=True, use_db_state=True,   # 檢查點在 DB（Actions 臨時機器不保留 logs/）
                    )
                    success.append(
                        f"prices:batch:ok_dates={len(st.get('ok_dates') or [])}:days={depth}:rss={st.get('peak_rss_mb')}"
                        f":end={st.get('end')}" + (f":pending_today={'/'.join(st['pending_today'])}" if st.get('pending_today') else "")
                    )
                    failed.extend(st.get("failed") or [])
                    if st.get("stopped"):
                        raise InterruptedError("stop-before during prices")
                else:
                    depth = config.DAILY_PRICE_TRADE_DAYS if job == "daily" else config.EVENING_PRICE_TRADE_DAYS
                    st = prices.fetch_sample_prices(conn, symbols, trade_days=depth)
                    success.append(f"prices:{st.get('ok')}:days={depth}")
                    failed.extend(st.get("failed") or [])
            except InterruptedError:
                raise
            except Exception as e:
                failed.append(f"prices:{e}")
                log.exception("prices")

            # analysis-0.5.0：daily 在行情入庫後寫選股／摘要（writer 失敗不影響行情）
            if job == "daily":
                try:
                    # all 模式傳 None＝analysis 讀 securities 全部普通股（不展開 1,956 個代號參數）
                    ar = analysis_bridge.run_screen_and_write(conn, run_id, None if symbols_mode == "all" else symbols)
                    if ar.get("ok"):
                        success.append(f"analysis:{ar.get('screen')}")
                        if ar.get("interface_notes"):
                            success.append(f"analysis_notes:{ar['interface_notes']}")
                    else:
                        failed.append(f"analysis:{ar.get('error')}")
                        log.error("analysis writer: %s", ar.get("error"))
                except Exception as e:
                    failed.append(f"analysis:{e}")
                    log.exception("analysis")

            try:
                st = corporate_actions.fetch_all(
                    conn, symbols, max_enrich=config.ENRICH_PER_RUN if symbols_mode == "all" else None)
                success.append(f"corp:{st}")
            except Exception as e:
                failed.append(f"corp:{e}")
                log.exception("corp")

            try:
                st = halts.fetch_all(conn)
                success.append(f"halts:{st}")
            except Exception as e:
                failed.append(f"halts:{e}")
                log.exception("halts")

            try:
                # 停牌統一規則（離線、0 請求）：推定恢復日＋重算停牌旗標；必須在 halts 與行情入庫之後
                from pipeline import halt_fix
                hr = halt_fix.apply_unified_rule(conn)
                success.append(f"halt_rule:{hr['resume_rule'].get('by_basis')}:flags={hr['recompute'].get('counts')}")
            except Exception as e:
                failed.append(f"halt_rule:{e}")
                log.exception("halt_rule")

            try:
                st = indices.fetch_window(conn)
                success.append(f"indices:{st}")
            except Exception as e:
                failed.append(f"indices:{e}")
                log.exception("indices")

        if job == "evening":
            # 2026-11-24 籌碼增量：最近 3 個交易日未完成的資料集各 1 個請求（正常每交易日 4 個：TWSE T86＋MI_MARGN、TPEx 法人＋融資融券），
            # TDCC 股權分散最新已存週距今 ≥8 天才抓 opendata（約每週 1 個）。融資融券約 21:00 後才公布，所以只放 evening（22:00）。
            try:
                from pipeline.fetchers import chips
                st = chips.fetch_recent(conn, days=3, stop_before=stop_before)
                success.append(f"chips:requests={st.get('requests_by_host')}:tdcc={(st.get('tdcc') or {}).get('status')}")
                if st.get("stopped_hosts"):
                    failed.append(f"chips:stopped_hosts={st['stopped_hosts']}")
            except Exception as e:
                failed.append(f"chips:{e}")
                log.exception("chips")

        if job == "evening" or job == "daily":
            # daily：營收只試近 2 月、重訊短窗；財報歷史回補僅 evening（避免每次重抓季報）
            try:
                if job == "daily":
                    rev_n = config.DAILY_REVENUE_MONTHS
                elif symbols_mode == "all":
                    rev_n = config.ALL_EVENING_REVENUE_MONTHS   # evening 只重抓最近 3 個月；歷史由 revenue_backfill
                else:
                    rev_n = config.REVENUE_MONTHS
                if should_stop(stop_before):
                    raise InterruptedError("stop-before before revenue")
                st = revenue.fetch_all(conn, symbols, max_months=rev_n)
                success.append(f"revenue:months={len(st.get('months') or [])}:target={rev_n}")
                failed.extend(st.get("failed") or [])
            except Exception as e:
                failed.append(f"revenue:{e}")
                log.exception("revenue")

            if job == "evening":
                try:
                    st = financials.fetch_all(
                        conn, symbols, quarters=config.ALL_EVENING_FINANCIAL_QUARTERS if symbols_mode == "all" else None)
                    success.append(f"financials:cf={st.get('cf')},inc={st.get('income')}")
                    failed.extend(st.get("failed") or [])
                except Exception as e:
                    failed.append(f"financials:{e}")
                    log.exception("financials")
            else:
                success.append("financials:skipped_on_daily")

            try:
                if symbols_mode == "all":
                    if should_stop(stop_before):
                        raise InterruptedError("stop-before before material")
                    # 全市場只用整批快照，不用逐檔 t05st01（約 1900 檔會超時／被封）
                    st = material_batch.fetch_snapshots(conn, symbols, run_id=run_id)
                    success.append(f"material:snapshot:rows={st.get('rows')}:{st.get('note')}")
                    from pipeline.runstats import state_init
                    # 重訊整批快照只有近期、沒有歷史：記下「自哪天起累積」，網站據此標示（之前為「資料不足」）
                    if state_init(conn, "material_snapshot", "since", "info", today().isoformat()):
                        conn.commit()
                    failed.extend(st.get("failed") or [])
                else:
                    mat_days = config.DAILY_MATERIAL_DAYS if job == "daily" else config.MATERIAL_DAYS
                    st = material.fetch_all(conn, symbols, days=mat_days)
                    success.append(f"material:rows={st.get('rows')}:days={mat_days}")
                    failed.extend(st.get("failed") or [])
            except InterruptedError:
                raise
            except Exception as e:
                failed.append(f"material:{e}")
                log.exception("material")

        if symbols_mode == "all":
            try:
                write_coverage_json(conn)
            except Exception:
                log.exception("coverage json")
        report = check_db(conn, symbols if symbols_mode == "sample30" else symbols[:30])
        # gaps（如營收<36）與 errors 皆不得標成無條件 success
        if failed or report.errors or report.gaps:
            status = "partial" if success else "failed"
        elif not report.ok:
            status = "partial" if success else "failed"
        else:
            status = "success"
        success.append("summary:" + json.dumps(tally.summary(), ensure_ascii=False))
        finish_job(conn, run_id, status, success, failed, error="; ".join(report.errors + report.gaps[:20]))
        log.info("job done status=%s success=%s failed=%s rss=%.1fMB", status, success, failed, max_rss_mb())
        log.info("quality errors=%s warnings=%s gaps=%s", report.errors, report.warnings[:10], report.gaps[:30])
        # write quality summary
        qpath = LOG_DIR / f"quality_{today().isoformat()}.txt"
        qpath.write_text(
            "\n".join(
                ["# Quality Report", f"ok={report.ok}", "", "## errors"]
                + report.errors
                + ["", "## warnings"]
                + report.warnings
                + ["", "## gaps"]
                + report.gaps
            ),
            encoding="utf-8",
        )
        return 0 if status in ("success", "partial") else 1
    except (InterruptedError, KeyboardInterrupt) as e:
        log.warning("interrupted: %s rss=%.1fMB", e, max_rss_mb())
        try:
            if symbols_mode == "all":
                write_coverage_json(conn)
        except Exception:
            log.exception("coverage json on interrupt")
        finish_job(
            conn, run_id, "interrupted", success, failed + [str(e)],
            error=f"interrupted: {e}",
        )
        return 3
    except Exception as e:
        finish_job(conn, run_id, "failed", success, failed + [str(e)], error=str(e))
        log.exception("fatal")
        return 2
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="台股官方資料管線")
    p.add_argument("--job", choices=["daily", "evening", "tpex_volume_fix", "screen_results_migrate", "halt_fix", "next_ref_fix",
                                      "market_backfill", "corp_backfill", "revenue_backfill", "financials_backfill", "halts_backfill", "delist_backfill",
                                      "calendar_backfill", "chips_backfill", "tdcc_backfill"], required=False)
    p.add_argument("--symbols", choices=["sample30", "all"], required=False)
    p.add_argument("--fix-from", default=None, help="起始日 YYYY-MM-DD：tpex_volume_fix／halt_fix／next_ref_fix／market_backfill／corp_backfill／revenue_backfill／financials_backfill／halts_backfill／delist_backfill／calendar_backfill／chips_backfill／tdcc_backfill（歷史回補到 2023-11-01 用）")
    p.add_argument("--fix-to", default=None, help="tpex_volume_fix／halt_fix／next_ref_fix／market_backfill／chips_backfill／tdcc_backfill：結束日 YYYY-MM-DD（market_backfill／chips_backfill 預設昨日）")
    p.add_argument("--max-dates", type=int, default=None, help="tpex_volume_fix／next_ref_fix／market_backfill：本次最多處理幾個交易日（market_backfill 每日每缺的市場各 1 個請求）；halt_fix：最多處理幾個全市場日；chips_backfill：最多處理幾個交易日（每日最多 4 個請求）；tdcc_backfill：最多處理幾週")
    p.add_argument("--budget-min", type=float, default=None, help="時間預算（分鐘），到時收尾並標 partial（market_backfill／chips_backfill／tdcc_backfill）；daily／evening 換算成 stop-before（interrupted，可續跑）")
    p.add_argument("--months", type=int, default=None, help="revenue_backfill：回補最近幾個月（預設 24）")
    p.add_argument("--quarters", type=int, default=None, help="financials_backfill：回補最近幾季（預設 8）")
    p.add_argument("--max-pages", type=int, default=None, help="revenue_backfill／financials_backfill：本次最多送出幾頁請求")
    p.add_argument("--max-enrich", type=int, default=60, help="corp_backfill：MOPS 配股補強最多幾個代號（0＝只跑區間查詢）")
    p.add_argument("--no-fetch", action="store_true", help="halt_fix：不抓官方快照（TWSE 停止買賣 API、tpex_cmode；預設各 1 個請求）；halts_backfill：不打上市 TWTAWU")
    p.add_argument("--init-db-only", action="store_true", help="僅建立 schema")
    p.add_argument(
        "--stop-before",
        default=None,
        help="ISO 時刻（Asia/Taipei）；到期中止並將 job 標 interrupted，便於續跑。",
    )
    args = p.parse_args(argv)
    if args.init_db_only:
        conn = connect()
        init_db(conn)
        conn.close()
        print(f"initialized {config.DB_PATH}")
        return 0
    if args.job == "tpex_volume_fix":
        from pipeline import tpex_volume_fix
        setup_logging()
        return tpex_volume_fix.main_job(date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates)
    if args.job == "screen_results_migrate":
        from pipeline import screen_results_migration
        setup_logging()
        return screen_results_migration.main_job()
    if args.job == "halt_fix":
        from pipeline import halt_fix
        setup_logging()
        return halt_fix.main_job(fetch=not args.no_fetch, date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates)
    if args.job == "next_ref_fix":
        from pipeline import next_ref_fix
        setup_logging()
        return next_ref_fix.main_job(date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates)
    if args.job == "market_backfill":
        from pipeline import market_backfill
        setup_logging()
        return market_backfill.main_job(date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates, budget_min=args.budget_min)
    if args.job == "corp_backfill":
        from pipeline import corp_backfill
        setup_logging()
        return corp_backfill.main_job(date_from=args.fix_from, max_enrich=args.max_enrich, budget_min=args.budget_min)
    if args.job == "revenue_backfill":
        from pipeline import revenue_backfill
        setup_logging()
        return revenue_backfill.main_job(months=args.months, max_pages=args.max_pages, budget_min=args.budget_min, date_from=args.fix_from)
    if args.job == "financials_backfill":
        from pipeline import financials_backfill
        setup_logging()
        return financials_backfill.main_job(quarters=args.quarters, max_pages=args.max_pages, budget_min=args.budget_min, date_from=args.fix_from)
    if args.job == "halts_backfill":
        from pipeline import halts_backfill
        setup_logging()
        return halts_backfill.main_job(date_from=args.fix_from, twse=not args.no_fetch)
    if args.job == "delist_backfill":
        from pipeline import delist_backfill
        setup_logging()
        return delist_backfill.main_job(date_from=args.fix_from)
    if args.job == "calendar_backfill":
        from pipeline import calendar_backfill
        setup_logging()
        return calendar_backfill.main_job(date_from=args.fix_from)
    if args.job == "chips_backfill":
        from pipeline import chips_backfill
        setup_logging()
        return chips_backfill.main_job(date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates, budget_min=args.budget_min)
    if args.job == "tdcc_backfill":
        from pipeline import chips_backfill
        setup_logging()
        return chips_backfill.main_tdcc(scope=args.symbols, date_from=args.fix_from, date_to=args.fix_to, max_dates=args.max_dates,
                                        budget_min=args.budget_min)
    if not args.job or not args.symbols:
        p.error("--job 與 --symbols 為必要參數（除非使用 --init-db-only）")
    return run_job(args.job, args.symbols, stop_before_s=args.stop_before, budget_min=args.budget_min)


if __name__ == "__main__":
    raise SystemExit(main())
