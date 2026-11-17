"""screen_results 遷移（analysis-0.9.0 合入前）：加四欄＋change_check 的 CHECK 加 after_no_trade。

SQLite 不能改既有 CHECK，必須重建表。流程（單一交易，失敗即整體回滾，不留半套）：
  1. 已遷移（四欄齊全且 CHECK 含 after_no_trade）→ 略過（冪等）；表不存在 → 略過（init_db 會以新 DDL 建表）。
  2. 目標 DDL 取自 pipeline/schema.sql 的 screen_results（單一來源）；舊表欄位必須全包含在目標表內，否則中止（不丟資料）。
  3. CREATE screen_results_new → INSERT … SELECT（舊欄逐欄複製；四個新欄 NULL，不回填、不推算）→ DROP 舊表 → RENAME → 重建索引。
  4. 列數、PRIMARY KEY 集合須與遷移前一致；integrity_check／foreign_key_check 通過才 COMMIT。
用法：python -m pipeline.run --job screen_results_migrate
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from pipeline import config

log = logging.getLogger("pipeline.screen_results_migration")

NEW_COLS = ["window_bad_data_days", "data_note", "volume_basis", "window_volume_basis_days"]
TABLE = "screen_results"


def _target_ddl() -> tuple[str, list[str]]:
    """從 schema.sql 取 screen_results 的 CREATE TABLE（改名 screen_results_new）與其索引語句。"""
    sql = config.SCHEMA_SQL.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS screen_results \(.*?\n\);", sql, flags=re.S)
    if not m:
        raise RuntimeError("schema.sql 找不到 screen_results DDL")
    create = m.group(0).replace("CREATE TABLE IF NOT EXISTS screen_results (", "CREATE TABLE screen_results_new (", 1)
    idx = re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS \w+\s+ON screen_results\([^;]*;", sql, flags=re.S)
    if not idx:
        raise RuntimeError("schema.sql 找不到 screen_results 索引")
    return create, idx


def _table_sql(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return r[0] if r else None


def is_migrated(conn: sqlite3.Connection) -> bool:
    sql = _table_sql(conn)
    if sql is None:
        return True   # 沒有表：不需遷移（init_db 建表即是新版）
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    return all(c in cols for c in NEW_COLS) and "after_no_trade" in sql


def migrate_screen_results(conn: sqlite3.Connection) -> dict:
    """回傳摘要 dict：status＝skipped｜no_table｜migrated；rows＝列數。失敗丟例外（交易已回滾）。"""
    sql = _table_sql(conn)
    if sql is None:
        return {"status": "no_table", "rows": 0}
    if is_migrated(conn):
        n = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        return {"status": "skipped", "rows": n}

    create, idx_sql = _target_ddl()
    old_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")]
    conn.commit()
    prev_iso = conn.isolation_level
    conn.isolation_level = None   # 手動交易
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS screen_results_new")   # 前次失敗殘留（交易回滾後不會有，保險）
        conn.execute(create)
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(screen_results_new)")]
        missing = [c for c in old_cols if c not in new_cols]
        if missing:
            raise RuntimeError(f"目標表缺少舊欄位 {missing}；中止遷移（不丟資料）")
        absent_new = [c for c in NEW_COLS if c not in new_cols]
        if absent_new:
            raise RuntimeError(f"目標 DDL 缺少新欄位 {absent_new}")
        before_n = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        before_keys = conn.execute(f"SELECT as_of, symbol, params_hash, version, is_current FROM {TABLE} ORDER BY 1,2,3,4").fetchall()
        cl = ", ".join(f'"{c}"' for c in old_cols)
        conn.execute(f"INSERT INTO screen_results_new ({cl}) SELECT {cl} FROM {TABLE}")
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE screen_results_new RENAME TO {TABLE}")
        for s in idx_sql:
            conn.execute(s)
        after_n = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        after_keys = conn.execute(f"SELECT as_of, symbol, params_hash, version, is_current FROM {TABLE} ORDER BY 1,2,3,4").fetchall()
        if after_n != before_n or after_keys != before_keys:
            raise RuntimeError(f"遷移後列數／主鍵不一致 before={before_n} after={after_n}")
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if ic != "ok" or fk:
            raise RuntimeError(f"integrity_check={ic} foreign_key_check={fk[:3]}")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.isolation_level = prev_iso
    log.info("screen_results 遷移完成：%s 列，新增欄位 %s", after_n, NEW_COLS)
    return {"status": "migrated", "rows": after_n, "added_columns": NEW_COLS,
            "indexes": [re.search(r"INDEX IF NOT EXISTS (\w+)", s).group(1) for s in idx_sql]}


def main_job() -> int:
    from pipeline.dates import today
    from pipeline.db import connect, finish_job, init_db, start_job

    conn = connect()
    pre = "migrated" if is_migrated(conn) else "pending"
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_runs'").fetchone():
        init_db(conn)   # 全新空庫：先建表（其 migrate() 會跳過不存在的舊表）
    run_id = start_job(conn, "screen_results_migrate", "n/a", today().isoformat())
    try:
        out = migrate_screen_results(conn)      # 先遷移（init_db 內的 migrate() 之後只會略過）
        init_db(conn)                           # 缺表時以新 DDL 建表；其餘欄位遷移
        out["state_before_job"] = pre
        sql = _table_sql(conn) or ""
        out["check_has_after_no_trade"] = "after_no_trade" in sql
        finish_job(conn, run_id, "success", [json.dumps(out, ensure_ascii=False)], [])
        log.info("screen_results_migrate %s", out)
        return 0
    except Exception as e:   # noqa: BLE001
        finish_job(conn, run_id, "failed", [], [str(e)], error=str(e))
        log.exception("screen_results_migrate")
        return 2
    finally:
        conn.close()
