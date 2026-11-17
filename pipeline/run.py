"""單一入口：python -m pipeline.run --job daily|evening --symbols sample30|all"""
from __future__ import annotations

import argparse
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


def run_job(job: str, symbols_mode: str, *, stop_before_s: str | None = None) -> int:
    setup_logging()
    log = logging.getLogger("pipeline.run")
    assert_no_other_pipeline()
    stop_before = parse_stop_before(stop_before_s)
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
                if os.environ.get("PIPELINE_SKIP_ISIN") == "1":
                    # 雲端（GitHub Actions）isin.twse.com.tw 約 13 KB/s、10 MB 要 12 分鐘以上：預設略過，沿用 DB 內的清單
                    st = "skipped(PIPELINE_SKIP_ISIN=1，沿用 DB 內證券清單)"
                    log.info("securities: %s", st)
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
                        run_id=run_id, resume=True,
                    )
                    success.append(
                        f"prices:batch:ok_dates={len(st.get('ok_dates') or [])}:days={depth}:rss={st.get('peak_rss_mb')}"
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
            # all 宇宙暫不跑分析（記憶體；不影響 sample30 排程）
            if job == "daily" and symbols_mode != "all":
                try:
                    ar = analysis_bridge.run_screen_and_write(conn, run_id, symbols)
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
                st = corporate_actions.fetch_all(conn, symbols)
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
                st = indices.fetch_window(conn)
                success.append(f"indices:{st}")
            except Exception as e:
                failed.append(f"indices:{e}")
                log.exception("indices")

        if job == "evening" or job == "daily":
            # daily：營收只試近 2 月、重訊短窗；財報歷史回補僅 evening（避免每次重抓季報）
            try:
                if job == "daily":
                    rev_n = config.DAILY_REVENUE_MONTHS
                elif symbols_mode == "all":
                    rev_n = config.ALL_REVENUE_MONTHS
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
                    st = financials.fetch_all(conn, symbols)
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
    p.add_argument("--job", choices=["daily", "evening"], required=False)
    p.add_argument("--symbols", choices=["sample30", "all"], required=False)
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
    if not args.job or not args.symbols:
        p.error("--job 與 --symbols 為必要參數（除非使用 --init-db-only）")
    return run_job(args.job, args.symbols, stop_before_s=args.stop_before)


if __name__ == "__main__":
    raise SystemExit(main())
