"""測試安全網：所有測試一律不得寫到正式庫 data/twstock.db。

- 每個測試自動把 config.DB_PATH、環境變數 PIPELINE_DB_PATH、LOG_DIR 指向 tmp_path；
- 把 sqlite3.connect 換成守門版：開啟「真正的 data/twstock.db」且不是唯讀（URI mode=ro）→ 立即 AssertionError；
- 整個測試階段結束時，若正式庫存在，檢查大小與 sha256 與開始時相同。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline import config  # noqa: E402

REAL_DB = {Path(config.PRODUCTION_DB_PATH).resolve(), (ROOT / "data" / "twstock.db").resolve()}
_REAL_CONNECT = sqlite3.connect


def _target_of(database, uri: bool) -> tuple[Path | None, bool]:
    """回傳 (解析後的檔案路徑或 None, 是否唯讀)。"""
    s = str(database)
    if s == ":memory:" or s == "":
        return None, False
    if uri and s.startswith("file:"):
        from urllib.parse import parse_qs, unquote, urlparse
        u = urlparse(s)
        q = parse_qs(u.query)
        return Path(unquote(u.path)).resolve(), q.get("mode", [""])[0] == "ro"
    return Path(s).resolve(), False


def _guarded_connect(database, *args, **kwargs):
    uri = bool(kwargs.get("uri", args[5] if len(args) > 5 else False))
    path, readonly = _target_of(database, uri)
    if path is not None and path in REAL_DB and not readonly:
        raise AssertionError(f"測試中禁止開啟正式庫的寫入連線：{path}（請使用 tmp 庫或 URI mode=ro）")
    return _REAL_CONNECT(database, *args, **kwargs)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    existing = {p: (p.stat().st_size, _sha256(p)) for p in REAL_DB if p.exists()}
    yield
    for p, (size, sha) in existing.items():
        assert p.stat().st_size == size and _sha256(p) == sha, f"正式庫在測試期間被改動：{p}"


@pytest.fixture(autouse=True)
def isolate_db(monkeypatch, tmp_path):
    tmp_db = tmp_path / "test_twstock.db"
    monkeypatch.setenv("PIPELINE_DB_PATH", str(tmp_db))
    monkeypatch.setattr(config, "DB_PATH", tmp_db)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sqlite3, "connect", _guarded_connect)
    # 退避時間縮短、清掉行程內狀態（熔斷、鉅額 context）
    monkeypatch.setenv("PIPELINE_RATE_LIMIT_BACKOFF", "2")
    monkeypatch.setenv("PIPELINE_RATE_LIMIT_BACKOFF_MAX", "8")
    from pipeline import httputil
    from pipeline.fetchers import tpex_block
    httputil.reset_breakers()
    tpex_block.reset_context()
    yield tmp_db
    httputil.reset_breakers()
    tpex_block.reset_context()
