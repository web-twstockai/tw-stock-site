"""守門測試：確認測試環境不可能寫到正式庫。"""
import sqlite3
from pathlib import Path

import pytest

from pipeline import analysis_bridge, config


def test_config_points_to_tmp(isolate_db):
    assert Path(config.DB_PATH) == isolate_db
    assert analysis_bridge.db_path() == isolate_db
    assert Path(config.PRODUCTION_DB_PATH).resolve() != isolate_db.resolve()


def test_opening_real_db_for_write_fails():
    real = Path(config.PRODUCTION_DB_PATH)
    with pytest.raises(AssertionError):
        sqlite3.connect(str(real))
    with pytest.raises(AssertionError):
        sqlite3.connect(f"file:{real}?mode=rw", uri=True)
    with pytest.raises(AssertionError):
        sqlite3.connect(real)   # Path 物件


def test_opening_real_db_readonly_is_allowed():
    real = Path(config.PRODUCTION_DB_PATH)
    if not real.exists():
        pytest.skip("無正式庫檔案")
    con = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
    try:
        con.execute("SELECT 1").fetchone()
    finally:
        con.close()


def test_db_connect_helper_uses_tmp(isolate_db):
    from pipeline.db import connect
    con = connect()
    try:
        assert Path(con.execute("PRAGMA database_list").fetchone()[2]).resolve() == isolate_db.resolve()
    finally:
        con.close()
