"""回補作業共用：請求計數、耗時、新增列數、時間預算、DB 內的作業狀態（檢查點）。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

from pipeline import httputil
from pipeline.coverage import max_rss_mb
from pipeline.dates import now_iso

COUNT_TABLES = ("daily_prices", "corporate_actions", "monthly_revenues", "income_statements", "cash_flow_statements",
                "material_infos", "trading_halts")


class Budget:
    """--budget-min：時間到就收尾（呼叫端標 partial）。None／0＝不限。"""

    def __init__(self, minutes: float | None):
        self.minutes = minutes if minutes else None
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def left(self) -> float | None:
        return None if self.minutes is None else self.minutes * 60 - self.elapsed()

    def expired(self, reserve_s: float = 0.0) -> bool:
        left = self.left()
        return left is not None and left <= reserve_s


class Tally:
    """作業摘要：實際請求數（依主機）、耗時、各表新增列數、峰值 RSS。"""

    def __init__(self, conn: sqlite3.Connection, tables: tuple[str, ...] = COUNT_TABLES):
        self.conn = conn
        self.tables = tables
        self.t0 = time.monotonic()
        self.req0 = dict(httputil.STATS)
        self.rows0 = self._rows()

    def _rows(self) -> dict[str, int]:
        out = {}
        for t in self.tables:
            try:
                out[t] = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                pass
        return out

    def summary(self) -> dict:
        req = {h: n - self.req0.get(h, 0) for h, n in httputil.STATS.items() if n - self.req0.get(h, 0)}
        rows = self._rows()
        return {
            "requests": sum(req.values()),
            "requests_by_host": req,
            "elapsed_s": round(time.monotonic() - self.t0, 1),
            "rows_added": {t: rows[t] - self.rows0.get(t, 0) for t in rows if rows[t] != self.rows0.get(t, 0)},
            "peak_rss_mb": max_rss_mb(),
        }


def state_get(conn: sqlite3.Connection, job: str, key: str) -> sqlite3.Row | tuple | None:
    return conn.execute("SELECT status, attempts, detail FROM backfill_state WHERE job=? AND key=?", (job, key)).fetchone()


def state_set(conn: sqlite3.Connection, job: str, key: str, status: str, detail: str | None = None, *, bump: bool = False) -> None:
    conn.execute(
        """INSERT INTO backfill_state(job,key,status,attempts,detail,updated_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(job,key) DO UPDATE SET status=excluded.status,
             attempts=backfill_state.attempts + ?, detail=excluded.detail, updated_at=excluded.updated_at""",
        (job, key, status, 1 if bump else 0, detail, now_iso(), 1 if bump else 0),
    )


def state_init(conn: sqlite3.Connection, job: str, key: str, status: str, detail: str | None = None) -> bool:
    """只在尚無此鍵時寫入（例：重訊每日快照『自哪天起累積』的起始日）。回傳是否新寫入。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO backfill_state(job,key,status,attempts,detail,updated_at) VALUES(?,?,?,0,?,?)",
        (job, key, status, detail, now_iso()))
    return cur.rowcount > 0


def state_keys(conn: sqlite3.Connection, job: str, status: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM backfill_state WHERE job=? AND status=?", (job, status))}


def finish_with_summary(conn: sqlite3.Connection, run_id: int, status: str, out: dict, failed: list[str] | None = None,
                        error: str | None = None) -> None:
    from pipeline.db import finish_job

    finish_job(conn, run_id, status, [json.dumps(out, ensure_ascii=False, default=str)[:6000]], failed or [], error=error)


def stop_before_from_budget(minutes: float | None) -> datetime | None:
    if not minutes:
        return None
    from pipeline.deadline import TZ

    return datetime.now(TZ) + timedelta(minutes=minutes)


def material_since(conn: sqlite3.Connection) -> str | None:
    """重訊整批快照『自哪天起累積』（YYYY-MM-DD）。沒有按日全市場的官方歷史端點，所以更早的日期是「資料不足」。
    網站顯示：資料不足（重訊自 MM/DD 起累積）。"""
    r = state_get(conn, "material_snapshot", "since")
    return r[2] if r else None
