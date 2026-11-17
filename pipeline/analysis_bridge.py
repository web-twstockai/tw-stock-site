"""呼叫研究分析 writer（只改 pipeline，不改 analysis/）。"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger("pipeline.analysis")

from pipeline import config

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis"


def db_path() -> Path:
    """分析 writer 使用的 DB：與管線同一個路徑（config.DB_PATH；測試／副本可用環境變數 PIPELINE_DB_PATH 覆寫），不再寫死正式庫。"""
    return Path(config.DB_PATH)


def run_screen_and_write(conn: sqlite3.Connection, run_id: int, symbols: list[str] | None = None) -> dict:
    """行情入庫後：screen → write_screen_results → summary_narrative → write_daily_summary。
    analysis-0.6.0：直接傳 screen_run_id=job_runs.run_id；失敗只回報，不中斷行情。
    僅寫最近有行情的交易日；不回補歷史。
    """
    out: dict = {"ok": False, "screen": None, "summary": None, "error": None, "interface_notes": []}
    if not (ANALYSIS / "writer.py").exists():
        out["error"] = "analysis/writer.py missing"
        return out
    sys.path.insert(0, str(ANALYSIS))
    try:
        from io_sqlite import load_screen_inputs  # type: ignore
        from screener import ScreenParams, screen  # type: ignore
        from narrative import summary_narrative  # type: ignore
        import writer as W  # type: ignore
    except Exception as e:
        out["error"] = f"import_analysis:{e}"
        return out

    try:
        dbp = db_path()
        inputs = load_screen_inputs(str(dbp), as_of=None, symbols=symbols)
        as_of = str(inputs.as_of)[:10]
        row = conn.execute(
            "SELECT is_trade_day FROM trading_calendar WHERE calendar_date=?", (as_of,)
        ).fetchone()
        if row is not None and int(row[0]) != 1:
            out["ok"] = True
            out["skipped"] = f"non_trading_day:{as_of}"
            return out

        result = screen(**inputs.screen_kwargs(), params=ScreenParams())
        # 只有真正的正式庫才允許 production 寫入；其他路徑（測試／副本）一律不給 allow_production
        wcon = W.connect_writable(str(dbp), allow_production=dbp.resolve() == config.PRODUCTION_DB_PATH.resolve())
        try:
            r1 = W.write_screen_results(
                wcon, result, inputs.input_max_fetched_at, screen_run_id=run_id
            )
            out["screen"] = {
                "inserted": r1.inserted,
                "versioned": r1.versioned,
                "skipped": r1.skipped,
                "dropped_columns": list(r1.dropped_columns or []),
                "as_of": as_of,
                "n_pass": int((result.summary["status"] == "PASS").sum()),
                "screen_run_id": run_id,
            }
            if r1.dropped_columns:
                out["interface_notes"].append(f"dropped_columns:{list(r1.dropped_columns)}")
            n = summary_narrative(result)
            r2 = W.write_daily_summary(
                wcon,
                n,
                result,
                input_max_fetched_at=inputs.input_max_fetched_at,
                news_event_refs=(),
                screen_run_id=run_id,
            )
            out["summary"] = {
                "inserted": r2.inserted,
                "versioned": r2.versioned,
                "skipped": r2.skipped,
                "screen_run_id": run_id,
            }
        finally:
            wcon.close()
        out["ok"] = True
    except Exception as e:
        log.exception("analysis writer failed")
        out["error"] = str(e)
    return out
