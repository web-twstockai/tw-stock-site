"""全市場覆蓋統計：寫入 coverage_summaries、universe_members、logs/coverage_all.json。"""
from __future__ import annotations

import json
import logging
import resource
import sqlite3
from collections import defaultdict
from pathlib import Path

from pipeline import config
from pipeline.dates import now_iso, today

log = logging.getLogger("pipeline.coverage")


def max_rss_mb() -> float:
    # Linux ru_maxrss 為 KB
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


def rebuild_universes(conn: sqlite3.Connection) -> dict:
    """從 securities / securities_excluded 重建 sample30 與 all。"""
    as_of = today().isoformat()
    fetched = now_iso()
    conn.execute("DELETE FROM universe_members")
    # all = 現行普通股
    conn.execute(
        """INSERT INTO universe_members(universe_id,symbol,market,included,exclude_reason,as_of,source,fetched_at)
           SELECT 'all', symbol, market, 1, NULL, ?, source, ?
           FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1""",
        (as_of, fetched),
    )
    # sample30
    for i, sym in enumerate(config.SAMPLE30):
        row = conn.execute(
            "SELECT market, source FROM securities WHERE symbol=? AND is_current=1",
            (sym,),
        ).fetchone()
        market = row[0] if row else ( "TWSE" if sym in config.SAMPLE30_TWSE else "TPEx")
        source = row[1] if row else None
        conn.execute(
            """INSERT INTO universe_members(universe_id,symbol,market,included,exclude_reason,as_of,source,fetched_at)
               VALUES('sample30',?,?,1,NULL,?,?,?)""",
            (sym, market, as_of, source, fetched),
        )
    conn.commit()
    cur = conn.execute(
        """SELECT universe_id, market, COUNT(*) FROM universe_members
           WHERE included=1 GROUP BY 1,2"""
    ).fetchall()
    stats = {(r[0], r[1]): r[2] for r in cur}
    log.info("universes rebuilt as_of=%s stats=%s", as_of, stats)
    return {"as_of": as_of, "counts": {f"{a}:{b}": n for (a, b), n in stats.items()}}


def record_summary(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    universe: str,
    data_kind: str,
    market: str,
    expected: int,
    success: int,
    failed: int,
    excluded: int,
    exclude_reasons: dict | list | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    notes: str | None = None,
    http_events: list | dict | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO coverage_summaries(
             run_id,universe,data_kind,market,expected_count,success_count,failed_count,
             excluded_count,exclude_reasons_json,date_start,date_end,notes,http_events_json,fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id,
            universe,
            data_kind,
            market,
            expected,
            success,
            failed,
            excluded,
            json.dumps(exclude_reasons, ensure_ascii=False) if exclude_reasons is not None else None,
            date_start,
            date_end,
            notes,
            json.dumps(http_events, ensure_ascii=False) if http_events is not None else None,
            now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def write_coverage_json(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    path = path or config.COVERAGE_ALL_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    members = {}
    for r in conn.execute(
        """SELECT universe_id, market, included, COUNT(*) 
           FROM universe_members GROUP BY 1,2,3"""
    ):
        members.setdefault(r[0], {}).setdefault(r[1] or "?", {})[("in" if r[2] else "out")] = r[3]
    # latest coverage per kind+market
    latest = []
    for r in conn.execute(
        """SELECT coverage_id,run_id,universe,data_kind,market,expected_count,success_count,
                  failed_count,excluded_count,exclude_reasons_json,date_start,date_end,notes,fetched_at
           FROM coverage_summaries
           WHERE universe='all'
           ORDER BY coverage_id DESC LIMIT 80"""
    ):
        keys = [
            "coverage_id","run_id","universe","data_kind","market","expected","success",
            "failed","excluded","exclude_reasons","date_start","date_end","notes","fetched_at",
        ]
        rec = dict(zip(keys, r))
        if rec["exclude_reasons"]:
            try:
                rec["exclude_reasons"] = json.loads(rec["exclude_reasons"])
            except json.JSONDecodeError:
                pass
        latest.append(rec)
    # excluded reasons from securities_excluded (top)
    excl = conn.execute(
        """SELECT COALESCE(reason, exclude_reason, '(none)'), COUNT(*) 
           FROM securities_excluded GROUP BY 1 ORDER BY 2 DESC LIMIT 20"""
    ).fetchall() if _has(conn, "securities_excluded") else []
    # figure exclude column
    payload = {
        "generated_at": now_iso(),
        "universe_members": members,
        "securities_excluded_top_reasons": [{"reason": a, "n": b} for a, b in _excluded_reasons(conn)],
        "latest_coverage": latest,
        "max_rss_mb_writer": max_rss_mb(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s", path)
    return path


def _has(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _excluded_reasons(conn) -> list[tuple[str, int]]:
    if not _has(conn, "securities_excluded"):
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(securities_excluded)")}
    col = "reason" if "reason" in cols else ("exclude_reason" if "exclude_reason" in cols else None)
    if not col:
        n = conn.execute("SELECT COUNT(*) FROM securities_excluded").fetchone()[0]
        return [("(unspecified)", n)]
    return list(
        conn.execute(
            f"SELECT COALESCE({col},'(none)'), COUNT(*) FROM securities_excluded GROUP BY 1 ORDER BY 2 DESC LIMIT 20"
        )
    )
