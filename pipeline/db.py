"""SQLite 連線、DDL、job_runs／source_status、版本化 upsert。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from pipeline import config
from pipeline.dates import now_iso


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    sql = config.SCHEMA_SQL.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    migrate(conn)
    # 2026-11-24：籌碼資料表（三大法人／融資融券／集保股權分散＋抓取狀態、覆蓋表；pipeline/chips_schema.sql）。
    # CREATE ... IF NOT EXISTS，可重跑、不動既有表、不重新版本化任何既有列
    chips_sql = Path(__file__).resolve().parent / "chips_schema.sql"
    if chips_sql.exists():
        conn.executescript(chips_sql.read_text(encoding="utf-8"))
        conn.commit()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    if col not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


COVERAGE_DDL = """CREATE TABLE corporate_actions_coverage (
  market TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  coverage_start TEXT NOT NULL,
  coverage_end TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  notes TEXT,
  scope TEXT NOT NULL DEFAULT 'unknown',
  symbol_count INTEGER,
  PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source, scope)
)"""


def _migrate_coverage_pk(conn: sqlite3.Connection) -> None:
    """corporate_actions_coverage 主鍵加入 scope（可重跑；已含 scope 即略過）。
    舊列 scope 為 NULL 時：corporate_actions 內的代號全在追蹤清單 SAMPLE30 內 → 'sample30'（舊資料是 sample30 過濾寫入）；否則 'unknown'。
    只重建覆蓋表本身，不碰 corporate_actions 內容（不重新版本化）。"""
    info = list(conn.execute("PRAGMA table_info(corporate_actions_coverage)"))
    if not info or "scope" in {r[1] for r in info if r[5]}:
        return
    syms = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM corporate_actions")}
    legacy = "sample30" if syms <= set(config.SAMPLE30) else "unknown"
    conn.execute("UPDATE corporate_actions_coverage SET scope=? WHERE scope IS NULL", (legacy,))
    conn.execute("DROP TABLE IF EXISTS corporate_actions_coverage_old")
    conn.execute("ALTER TABLE corporate_actions_coverage RENAME TO corporate_actions_coverage_old")
    conn.execute(COVERAGE_DDL)
    conn.execute(
        """INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,notes,scope,symbol_count)
           SELECT market,action_kind,coverage_start,coverage_end,source,fetched_at,notes,COALESCE(scope,'unknown'),symbol_count
           FROM corporate_actions_coverage_old"""
    )
    conn.execute("DROP TABLE corporate_actions_coverage_old")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """可重跑的欄位／表升級。"""
    mig = Path(__file__).resolve().parent / "migrate.sql"
    if mig.exists():
        conn.executescript(mig.read_text(encoding="utf-8"))
    # daily_prices.change（含正負號）
    _add_col(conn, "daily_prices", "change", "REAL")
    _add_col(conn, "daily_prices", "is_halted", "INTEGER")
    _add_col(conn, "daily_prices", "is_no_trade", "INTEGER")
    _add_col(conn, "daily_prices", "change_note", "TEXT")
    _add_col(conn, "daily_prices", "next_ref_price_twd", "REAL")
    _add_col(conn, "daily_prices", "source_table", "TEXT")
    # 2026-11-17：上櫃成交量口徑（含鉅額）與鉅額欄位
    _add_col(conn, "daily_prices", "block_shares", "INTEGER")
    _add_col(conn, "daily_prices", "block_amount_twd", "INTEGER")
    _add_col(conn, "daily_prices", "block_source", "TEXT")
    _add_col(conn, "daily_prices", "volume_basis", "TEXT CHECK (volume_basis IN ('incl_block','excl_block'))")
    # 以 source_table 決定性回填舊列口徑（只填 NULL、可重跑；不動內容欄位、fetched_at、checksum）
    conn.execute(
        """UPDATE daily_prices SET volume_basis = CASE
               WHEN source_table IN ('STOCK_DAY','MI_INDEX','dailyQuotes') THEN 'incl_block'
               WHEN source_table = 'tradingStock' THEN 'excl_block' END
           WHERE volume_basis IS NULL AND source_table IN ('STOCK_DAY','MI_INDEX','dailyQuotes','tradingStock')"""
    )
    _add_col(conn, "index_daily", "change_pct_derived", "INTEGER")
    # corporate_actions extras
    _add_col(conn, "corporate_actions", "cash_subscription_ratio", "REAL")
    _add_col(conn, "corporate_actions", "share_exchange_ratio", "REAL")
    # financials
    for tbl in ("income_statements", "cash_flow_statements"):
        _add_col(conn, tbl, "report_basis", "TEXT")
        _add_col(conn, tbl, "period_type", "TEXT")
        _add_col(conn, tbl, "derived", "INTEGER DEFAULT 0")
        _add_col(conn, tbl, "derivation_method", "TEXT")
    # material url
    _add_col(conn, "material_infos", "url", "TEXT")
    # Phase 3b：月營收抓取狀態加範圍（舊 'ok' 是 sample30 過濾後的列數，不可當全市場已完成）
    _add_col(conn, "revenue_fetch_status", "scope", "TEXT")
    _add_col(conn, "revenue_fetch_status", "page_rows", "INTEGER")
    # Phase 3b：停牌統一規則（對齊研究分析 0.9.1）：沒有恢復日的紀錄，依成交推定恢復日；不進 CONTENT_KEYS、就地更新
    _add_col(conn, "trading_halts", "resume_date_inferred", "TEXT")
    _add_col(conn, "trading_halts", "resume_basis", "TEXT")
    _add_col(conn, "trading_halts", "resume_basis_note", "TEXT")
    # Phase 3b／fix1：除權息覆蓋表加範圍並把 scope 放進主鍵（sample30／subset／all 對同一區間不互相覆蓋）
    _add_col(conn, "corporate_actions_coverage", "scope", "TEXT")
    _add_col(conn, "corporate_actions_coverage", "symbol_count", "INTEGER")
    _migrate_coverage_pk(conn)
    # fix1：delistings.last_trade_*、securities_history.valid_*（只加欄、就地回填；不重新版本化任何表）
    _add_col(conn, "delistings", "last_trade_date", "TEXT")
    _add_col(conn, "delistings", "last_trade_basis", "TEXT")
    for col in ("valid_from", "valid_to", "valid_basis"):
        _add_col(conn, "securities_history", col, "TEXT")
    conn.commit()
    from pipeline.fetchers.delistings import fill_last_trade_dates

    fill_last_trade_dates(conn)
    # analysis tables (pipeline 不寫內容；欄位遷移冪等)
    for tbl in ("screen_results", "news_events", "daily_summaries"):
        _add_col(conn, tbl, "computed_at", "TEXT")
        _add_col(conn, tbl, "input_max_fetched_at", "TEXT")
    for col in ("window_halt_days", "window_missing_days", "window_no_trade_days"):
        _add_col(conn, "screen_results", col, "INTEGER")
    _add_col(conn, "screen_results", "screen_run_id", "TEXT")
    for col, decl in (
        ("data_date", "TEXT"),
        ("provider", "TEXT"),
        ("screen_run_id", "TEXT"),
    ):
        _add_col(conn, "daily_summaries", col, decl)
    conn.commit()
    # analysis-0.9.0：screen_results 加四欄＋change_check CHECK 加 after_no_trade（需重建表；已遷移則略過；失敗整體回滾後丟例外）
    from pipeline.screen_results_migration import migrate_screen_results

    migrate_screen_results(conn)


def checksum(obj: dict[str, Any], keys: Iterable[str]) -> str:
    payload = {k: obj.get(k) for k in keys}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def start_job(conn: sqlite3.Connection, job_name: str, scope: str, data_date: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO job_runs(job_name,started_at,status,data_date,symbols_scope,retry_count) VALUES(?,?,?,?,?,0)",
        (job_name, now_iso(), "running", data_date, scope),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_job(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    success: list[str],
    failed: list[str],
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE job_runs SET finished_at=?, status=?, success_items=?, failed_items=?, error_summary=?
           WHERE run_id=?""",
        (now_iso(), status, json.dumps(success, ensure_ascii=False), json.dumps(failed, ensure_ascii=False), error, run_id),
    )
    conn.commit()


def touch_source(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    url: str | None = None,
    ok: bool,
    http_status: int | None = None,
    data_date: str | None = None,
    error: str | None = None,
    notes: str | None = None,
) -> None:
    now = now_iso()
    if not ok and error and str(error).startswith("rate_limited"):
        notes = "rate_limited: 限流（對方限流或本機主機熔斷；已暫停請求，稍後重試）"
    conn.execute(
        """INSERT INTO source_status(source_id,source_url,last_success_at,last_attempt_at,last_http_status,
              last_data_date,last_error,ok,notes,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET
             source_url=COALESCE(excluded.source_url, source_status.source_url),
             last_attempt_at=excluded.last_attempt_at,
             last_http_status=excluded.last_http_status,
             last_data_date=COALESCE(excluded.last_data_date, source_status.last_data_date),
             last_error=excluded.last_error,
             ok=excluded.ok,
             notes=COALESCE(excluded.notes, source_status.notes),
             updated_at=excluded.updated_at,
             last_success_at=CASE WHEN excluded.ok=1 THEN excluded.last_success_at ELSE source_status.last_success_at END
        """,
        (
            source_id,
            url,
            now if ok else None,
            now,
            http_status,
            data_date,
            error,
            1 if ok else 0,
            notes,
            now,
        ),
    )
    if ok:   # 成功後清掉先前的「限流」註記
        conn.execute("UPDATE source_status SET notes=NULL WHERE source_id=? AND notes LIKE 'rate_limited%'", (source_id,))
    conn.commit()


def upsert_versioned(
    conn: sqlite3.Connection,
    table: str,
    natural_key: dict[str, Any],
    row: dict[str, Any],
    content_keys: list[str],
    *,
    history_table: str | None = None,
) -> str:
    """回傳 'inserted' | 'unchanged' | 'updated'。保留舊版 is_current=0。"""
    row = dict(row)
    row["checksum"] = checksum(row, content_keys)
    row.setdefault("fetched_at", now_iso())
    row.setdefault("is_current", 1)
    where = " AND ".join(f"{k}=?" for k in natural_key)
    params = list(natural_key.values())
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE {where} AND is_current=1",
        params,
    )
    old = cur.fetchone()
    if old is None:
        row.setdefault("version", 1)
        cols = list(row.keys())
        conn.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [row[c] for c in cols],
        )
        return "inserted"

    # 用「現行 content_keys」重算舊列 checksum 再比對，避免歷史鍵集合／enrich 路徑
    # 寫入的 stored checksum 與本次不一致而誤升版（fetched_at／source URL 不在 keys 內）。
    old_payload = {k: old[k] for k in old.keys()}
    old_ck = checksum(old_payload, content_keys)
    if old_ck == row["checksum"]:
        # 內容未變：不更新 fetched_at（否則 daily_prices.fetched_at 變動會流入
        # screen_results.price_fetched_at 並觸發分析表誤升版）。僅在 stored checksum
        # 與現行 content_keys 不一致時回寫 checksum。
        if old["checksum"] != row["checksum"]:
            conn.execute(
                f"UPDATE {table} SET checksum=? WHERE {where} AND is_current=1",
                [row["checksum"], *params],
            )
        return "unchanged"

    new_ver = int(old["version"]) + 1
    # archive
    if history_table == "securities_history" and table == "securities":
        cols = [c for c in old.keys() if c != "is_current"]
        conn.execute(
            f"""INSERT INTO securities_history({','.join(cols)}, archived_at)
                VALUES({','.join('?' for _ in cols)}, ?)""",
            [old[c] for c in cols] + [now_iso()],
        )
        conn.execute(f"DELETE FROM {table} WHERE {where}", params)
    else:
        conn.execute(
            f"UPDATE {table} SET is_current=0 WHERE {where} AND is_current=1",
            params,
        )

    row["version"] = new_ver
    row["is_current"] = 1
    cols = list(row.keys())
    conn.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return "updated"
