"""screen_results 遷移：重建表、保留所有列與版本、索引重建、冪等、失敗整體回滾。"""
import sqlite3

import pytest

from pipeline import config, db as pdb
from pipeline import screen_results_migration as srm

NEW = srm.NEW_COLS


def _old_schema_sql() -> str:
    """把 schema.sql 的 screen_results 還原成 0.8.0 版（無四欄、CHECK 無 after_no_trade），並移除其他表以外的語句。"""
    sql = config.SCHEMA_SQL.read_text(encoding="utf-8")
    start = sql.index("CREATE TABLE IF NOT EXISTS screen_results (")
    end = sql.index("CREATE INDEX IF NOT EXISTS ix_screen_results_excl")
    end = sql.index(";", end) + 1
    blk = sql[start:end]
    lines = [l for l in blk.splitlines() if not l.strip().startswith(tuple(NEW))]
    blk = "\n".join(lines).replace(", 'after_no_trade'", "")
    return blk


def _make_old(conn):
    conn.executescript(_old_schema_sql())
    cols = [r[1] for r in conn.execute("PRAGMA table_info(screen_results)")]
    assert not set(NEW) & set(cols)
    base = dict(as_of="2026-11-09", symbol="2330", params_hash="a" * 64, params_json="{}", status="PASS", c1_enabled=1, c2_enabled=1,
                c3_enabled=1, c4_enabled=1, c1_passed=1, c2_passed=1, c3_passed=1, c4_passed=1, conditions_json="[]",
                analysis_version="analysis-0.8.0", generated_at="2026-11-09T18:00:00+08:00", checksum="x", version=1, is_current=0,
                change_check="ok", volume_shares=123.0, market="TWSE", name="台積電", screen_run_id="7")
    rows = [dict(base), dict(base, version=2, is_current=1, volume_shares=456.0),
            dict(base, symbol="6488", market="TPEx", status="EXCLUDED", c1_passed=None, c2_passed=None, c3_passed=None, c4_passed=None,
                 exclusion_code="EXCL_NO_TRADE", change_check="no_source")]
    for r in rows:
        k = list(r)
        conn.execute(f"INSERT INTO screen_results({','.join(k)}) VALUES({','.join('?' * len(k))})", [r[c] for c in k])
    conn.commit()
    return cols


def _dump(conn, cols):
    cl = ",".join(cols)
    return conn.execute(f"SELECT {cl} FROM screen_results ORDER BY as_of,symbol,params_hash,version").fetchall()


def test_migrate_rebuilds_preserving_rows_versions_and_indexes(tmp_path):
    conn = sqlite3.connect(tmp_path / "o.db")
    cols = _make_old(conn)
    before = _dump(conn, cols)
    assert not srm.is_migrated(conn)
    out = srm.migrate_screen_results(conn)
    assert out["status"] == "migrated" and out["rows"] == 3
    assert _dump(conn, cols) == before                       # 舊欄位逐列逐值不變（含版本、is_current）
    ncols = {r[1] for r in conn.execute("PRAGMA table_info(screen_results)")}
    assert set(NEW) <= ncols and srm.is_migrated(conn)
    assert conn.execute("SELECT COUNT(*) FROM screen_results WHERE " + " OR ".join(f"{c} IS NOT NULL" for c in NEW)).fetchone()[0] == 0
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='screen_results' AND sql IS NOT NULL")}
    assert idx == {"ux_screen_results_current", "ix_screen_results_day", "ix_screen_results_symbol", "ix_screen_results_excl"}
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='screen_results_new'").fetchone()[0] == 0
    # 新 CHECK 生效：after_no_trade 可寫、volume_basis 不合法被擋；其他既有 CHECK 仍在
    conn.execute("UPDATE screen_results SET change_check='after_no_trade', volume_basis='excl_block' WHERE symbol='6488'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE screen_results SET volume_basis='bogus' WHERE symbol='6488'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE screen_results SET change_check='nope' WHERE symbol='6488'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE screen_results SET symbol='TEST1' WHERE symbol='6488'")
    conn.rollback()
    # 現行唯一索引仍有效
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO screen_results(as_of,symbol,params_hash,params_json,status,c1_enabled,c2_enabled,c3_enabled,c4_enabled,conditions_json,"
                     "analysis_version,generated_at,checksum,version,is_current) VALUES('2026-11-09','2330',?, '{}','FAIL',1,1,1,1,'[]','a','g','c',9,1)", ("a" * 64,))


def test_migrate_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "o.db")
    cols = _make_old(conn)
    srm.migrate_screen_results(conn)
    snap = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), _dump(conn, cols)
    again = srm.migrate_screen_results(conn)
    assert again["status"] == "skipped" and again["rows"] == 3
    assert (conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), _dump(conn, cols)) == snap


def test_no_table_is_skipped_and_fresh_schema_is_already_new(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    assert srm.migrate_screen_results(conn)["status"] == "no_table"
    pdb.init_db(conn)                                         # 全新庫：schema.sql 即新版
    assert srm.is_migrated(conn) and srm.migrate_screen_results(conn)["status"] == "skipped"


def test_failure_rolls_back_completely(tmp_path):
    """舊表有目標 DDL 沒有的欄位 → 中止，舊表與資料原封不動（不丟資料）。"""
    conn = sqlite3.connect(tmp_path / "o.db")
    cols = _make_old(conn)
    conn.execute("ALTER TABLE screen_results ADD COLUMN extra_unknown TEXT")
    conn.execute("UPDATE screen_results SET extra_unknown='keep me'")
    conn.commit()
    sql_before = conn.execute("SELECT sql FROM sqlite_master WHERE name='screen_results'").fetchone()[0]
    with pytest.raises(RuntimeError):
        srm.migrate_screen_results(conn)
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='screen_results'").fetchone()[0] == sql_before
    assert conn.execute("SELECT COUNT(*) FROM screen_results WHERE extra_unknown='keep me'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='screen_results_new'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ux_screen_results_current'").fetchone()[0] == 1


def test_init_db_runs_the_migration_automatically(tmp_path):
    conn = sqlite3.connect(tmp_path / "o.db")
    # 先建完整舊庫：用目前 schema 建好其他表，再把 screen_results 換成舊版
    pdb.init_db(conn)
    conn.execute("DROP TABLE screen_results")
    _make_old(conn)
    assert not srm.is_migrated(conn)
    pdb.init_db(conn)
    assert srm.is_migrated(conn) and conn.execute("SELECT COUNT(*) FROM screen_results").fetchone()[0] == 3
