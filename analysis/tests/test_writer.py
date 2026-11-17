"""writer.py 冪等版本化寫入測試。一律使用暫存 SQLite（tmp_path），絕不寫入 data/twstock.db。

分析表 DDL 取自 analysis/tables_draft.md；為讓測試用合成資料（代號 TEST*）可寫入暫存庫，
本測試移除 `CHECK (symbol NOT LIKE 'TEST%')`（正式 DDL 保留，另有一測試確認它會擋下合成資料）。
"""
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

import io_sqlite as I
import narrative as N
import screener as S
import synthetic_data as SD
import version as V
import writer as W

DOC = Path(__file__).resolve().parents[1] / "tables_draft.md"
TEST_CHECK = "CHECK (symbol NOT LIKE 'TEST%')"


def _ddl(allow_test=True, drop_cols=()):
    ddl = re.search(r"```sql\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S).group(1)
    if allow_test:
        ddl = ddl.replace(TEST_CHECK, "")
    for c in drop_cols:
        ddl = re.sub(rf"^\s*{c}\s+.*\n", "", ddl, flags=re.M)
    return ddl


@pytest.fixture(scope="module")
def screened(tmp_path_factory):
    db = SD.build_sqlite(tmp_path_factory.mktemp("src") / "synthetic.db")
    inp = I.load_screen_inputs(db)
    return inp, S.screen(**inp.screen_kwargs(), params=S.ScreenParams())


def _target(tmp_path, **kw):
    p = tmp_path / "analysis_tables.db"
    c = sqlite3.connect(p)
    c.executescript(_ddl(**kw))
    c.close()
    return W.connect_writable(p)


def _count(con, table, where="1=1"):
    return con.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


def test_input_max_fetched_at(screened):
    inp, r = screened
    rows = SD.tables()
    allv = [x["fetched_at"] for t in ("daily_prices", "corporate_actions", "trading_calendar", "securities")
            for x in rows[t] if x.get("fetched_at")]
    assert inp.input_max_fetched_at == max(allv)
    assert r.computed_at.endswith("+08:00") and r.analysis_version == V.ANALYSIS_VERSION
    assert I.max_fetched_at(None) is None
    import pandas as pd
    df = pd.DataFrame({"fetched_at": ["2026-11-10T09:00:00+08:00", "2026-11-10T02:00:00Z", None]})
    assert I.max_fetched_at(df) == "2026-11-10T02:00:00Z"   # 依實際時間比較（UTC 02:00 = 台北 10:00）


def test_screen_results_idempotent_and_versioned(tmp_path, screened):
    inp, r = screened
    con = _target(tmp_path)
    n = len(r.summary)
    rep = W.write_screen_results(con, r, inp.input_max_fetched_at)
    assert (rep.inserted, rep.versioned, rep.skipped) == (n, 0, 0) and rep.dropped_columns == []
    row = con.execute("SELECT computed_at, input_max_fetched_at, analysis_version, generated_at, status, "
                      "c1_passed, c3_threshold_volume_shares, price_version FROM screen_results "
                      "WHERE symbol='TEST01'").fetchone()
    assert row[0] == r.computed_at and row[1] == inp.input_max_fetched_at and row[2] == V.ANALYSIS_VERSION
    assert row[3] and row[4] == "PASS" and row[5] == 1 and row[6] > 0 and row[7] == 2
    # 重算（computed_at 不同）但內容相同 → 全部跳過、不升版
    r2 = replace(r, computed_at="2026-11-10T22:00:00+08:00")
    rep = W.write_screen_results(con, r2, "2026-11-10T21:59:00+08:00")
    assert (rep.inserted, rep.versioned, rep.skipped) == (0, 0, n)
    assert _count(con, "screen_results") == n
    # 內容改變（22:00 修正收盤）→ 該股升版，舊版保留為非現行
    s3 = r.summary.copy()
    s3.loc[s3.symbol == "TEST02", "close_twd"] += 0.05
    rep = W.write_screen_results(con, replace(r, summary=s3), inp.input_max_fetched_at)
    assert (rep.inserted, rep.versioned, rep.skipped) == (0, 1, n - 1)
    hist = con.execute("SELECT version, is_current FROM screen_results WHERE symbol='TEST02' ORDER BY version").fetchall()
    assert hist == [(1, 0), (2, 1)]
    assert _count(con, "screen_results", "is_current=1") == n
    # 排除列：條件 passed 為 NULL，exclusion_code 與 status 一致
    ex = con.execute("SELECT status, exclusion_code, c1_passed FROM screen_results WHERE symbol='TEST26'").fetchone()
    assert ex == ("EXCLUDED", "EXCL_SUSPENDED", None)


def test_screen_results_rollback_on_failure(tmp_path, screened):
    inp, r = screened
    con = _target(tmp_path)
    W.write_screen_results(con, r, inp.input_max_fetched_at)
    before = con.execute("SELECT symbol, version, checksum FROM screen_results ORDER BY symbol").fetchall()
    s = r.summary.copy()
    s.loc[s.symbol == "TEST01", "close_twd"] = 999.0        # 會升版的列
    s.loc[s.symbol == "TEST05", "status"] = "BROKEN"        # 違反 CHECK → 整批失敗
    with pytest.raises(sqlite3.IntegrityError):
        W.write_screen_results(con, replace(r, summary=s), inp.input_max_fetched_at)
    after = con.execute("SELECT symbol, version, checksum FROM screen_results ORDER BY symbol").fetchall()
    assert after == before   # 同一交易：TEST01 的升版也一起回滾


def test_formal_ddl_blocks_synthetic_and_gap_check(tmp_path, screened):
    inp, r = screened
    con = _target(tmp_path, allow_test=False)
    with pytest.raises(sqlite3.IntegrityError):
        W.write_screen_results(con, r, inp.input_max_fetched_at)
    assert _count(con, "screen_results") == 0
    gap = replace(r, params=dict(r.params, coverage_policy="gap_check"))
    with pytest.raises(ValueError, match="strict"):
        W.write_screen_results(con, gap, inp.input_max_fetched_at)


def test_refuses_production_db():
    with pytest.raises(PermissionError):
        W.connect_writable(W.PRODUCTION_DB)


def test_missing_columns_reported(tmp_path, screened):
    """資料庫尚未補欄（例如 window_halt_days 等）時只寫存在的欄位，並列出 dropped_columns。"""
    inp, r = screened
    drop = ("window_halt_days", "window_missing_days", "window_no_trade_days")
    con = _target(tmp_path, drop_cols=drop)
    rep = W.write_screen_results(con, r, inp.input_max_fetched_at)
    assert rep.inserted == len(r.summary) and sorted(rep.dropped_columns) == sorted(drop)


def test_missing_required_column_raises(tmp_path, screened):
    inp, r = screened
    con = _target(tmp_path, drop_cols=("conditions_json",))
    with pytest.raises(ValueError, match="缺少必要欄位"):
        W.write_screen_results(con, r, inp.input_max_fetched_at)
    assert _count(con, "screen_results") == 0


def test_daily_summary(tmp_path, screened):
    inp, r = screened
    con = _target(tmp_path)
    nar = N.summary_narrative(r)
    with pytest.raises(ValueError, match="先 write_screen_results"):
        W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at)
    W.write_screen_results(con, r, inp.input_max_fetched_at)
    rep = W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at)
    assert rep.inserted == 1
    row = con.execute("SELECT screen_ref_json, n_pass, n_fail, n_excluded, review_status, traceability_check, "
                      "data_date, provider, computed_at, input_max_fetched_at, analysis_version "
                      "FROM daily_summaries").fetchone()
    ref = json.loads(row[0])
    assert ref["n_rows"] == len(r.summary) and ref["screen_checksum"] == W.screen_batch_checksum(
        con, ref["as_of"], ref["params_hash"])[0]
    counts = r.summary["status"].value_counts()
    assert row[1:4] == (counts.get("PASS", 0), counts.get("FAIL", 0), counts.get("EXCLUDED", 0))
    assert row[4:7] == ("not_required", "pass", ref["as_of"]) and row[7] == W.PROVIDER_RULE
    assert row[8] and row[9] == inp.input_max_fetched_at and row[10] == V.ANALYSIS_VERSION
    assert W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at).skipped == 1
    # 篩選結果升版 → 引用的 checksum 改變 → 摘要升版
    s = r.summary.copy()
    s.loc[s.symbol == "TEST02", "close_twd"] += 0.05
    W.write_screen_results(con, replace(r, summary=s), inp.input_max_fetched_at)
    assert W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at).versioned == 1
    assert con.execute("SELECT version, is_current FROM daily_summaries ORDER BY version").fetchall() == [(1, 0), (2, 1)]


def _event(**o):
    e = dict(event_id="EV-0001", category="媒體報導", title="測試用合成新聞", first_published_at="2026-11-09T10:00:00+08:00",
             last_published_at="2026-11-09T11:00:00+08:00", news_ids_json=[1, 2], dedup_method="title_similarity",
             dedup_params_json={"jaccard_min": 0.6, "window_hours": 48}, generation_method="rule")
    e.update(o)
    return e


_SYMS = [dict(symbol="TEST01", evidence_type="內文代號", evidence="（TEST01）", source_news_id=1),
         dict(symbol="TEST02", evidence_type="公司全名", evidence="測試用合成公司", source_news_id=2)]


def test_news_event_with_symbols(tmp_path):
    con = _target(tmp_path)
    rep = W.write_news_event(con, _event(), _SYMS, input_max_fetched_at="2026-11-09T12:00:00+08:00")
    assert rep.inserted == 1 and rep.versions["EV-0001"] == 1
    assert _count(con, "news_event_symbols", "version=1") == 2
    snap = json.loads(con.execute("SELECT relation_evidence_json FROM news_events").fetchone()[0])
    assert [d["symbol"] for d in snap] == ["TEST01", "TEST02"]
    # 重跑：內容相同 → 跳過，關聯表不重複
    assert W.write_news_event(con, _event(), list(reversed(_SYMS))).skipped == 1
    assert _count(con, "news_event_symbols") == 2
    # 關聯代號改變 → 事件升版，新版本整組重寫，舊版本列保留
    rep = W.write_news_event(con, _event(), _SYMS[:1])
    assert rep.versioned == 1 and rep.versions["EV-0001"] == 2
    assert con.execute("SELECT version, count(*) FROM news_event_symbols GROUP BY version").fetchall() == [(1, 2), (2, 1)]
    cur = con.execute("SELECT s.symbol FROM news_event_symbols s JOIN news_events e ON e.event_id=s.event_id "
                      "AND e.version=s.version AND e.is_current=1").fetchall()
    assert cur == [("TEST01",)]


def test_news_event_rejects(tmp_path):
    con = _target(tmp_path)
    with pytest.raises(ValueError, match="related_symbols_json 已移除"):
        W.write_news_event(con, _event(related_symbols_json="[]"), _SYMS)
    with pytest.raises(ValueError, match="無證據"):
        W.write_news_event(con, _event(), [dict(symbol="TEST03", evidence_type="內文代號")])
    with pytest.raises(ValueError, match="evidence_type"):
        W.write_news_event(con, _event(), [dict(symbol="TEST03", evidence_type="猜測", source_news_id=1)])
    # 關聯表寫入失敗（重複主鍵）→ 事件本身也回滾
    with pytest.raises(sqlite3.IntegrityError):
        W.write_news_event(con, _event(), [_SYMS[0], dict(_SYMS[0])])
    assert _count(con, "news_events") == 0 and _count(con, "news_event_symbols") == 0


def _with_job_runs(con, ids=(41, 42)):
    con.execute("CREATE TABLE job_runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT, started_at TEXT)")
    for i in ids:
        con.execute("INSERT INTO job_runs (run_id, job_name, started_at) VALUES (?, 'daily', '2026-11-10T18:00:00+08:00')", (i,))


def test_screen_run_id_daily_summaries(tmp_path, screened):
    """screen_run_id＝產生該批 screen_results 的 job_runs.run_id；寫入 daily_summaries，不納入 checksum。"""
    inp, r = screened
    con = _target(tmp_path)
    _with_job_runs(con)
    W.write_screen_results(con, r, inp.input_max_fetched_at, screen_run_id=41)
    assert "screen_run_id" not in {x[1] for x in con.execute("PRAGMA table_info(screen_results)")}   # 草案無此欄
    nar = N.summary_narrative(r)
    W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at, screen_run_id=41)
    assert con.execute("SELECT screen_run_id FROM daily_summaries WHERE is_current=1").fetchone() == ("41",)
    # 另一個 job 重跑、內容相同 → 跳過，保留原 run_id
    rep = W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at, screen_run_id=42)
    assert rep.skipped == 1
    assert con.execute("SELECT screen_run_id FROM daily_summaries WHERE is_current=1").fetchone() == ("41",)
    # 不存在的 run_id、非正整數 → 拒絕
    for bad in (999, 0, -1, "abc", True):
        with pytest.raises(ValueError):
            W.write_daily_summary(con, nar, r, input_max_fetched_at=inp.input_max_fetched_at, screen_run_id=bad)


def test_screen_run_id_backfill_and_screen_results_column(tmp_path, screened):
    """screen_results 有 screen_run_id 欄時一併寫入；原值為 NULL 的現行列在跳過時補上。"""
    inp, r = screened
    con = _target(tmp_path)
    con.execute("ALTER TABLE screen_results ADD COLUMN screen_run_id TEXT")
    _with_job_runs(con)
    W.write_screen_results(con, r, inp.input_max_fetched_at)                       # 未提供 → NULL
    assert _count(con, "screen_results", "screen_run_id IS NULL") == len(r.summary)
    rep = W.write_screen_results(con, r, inp.input_max_fetched_at, screen_run_id=42)
    assert rep.skipped == len(r.summary) and rep.dropped_columns == []
    assert _count(con, "screen_results", "screen_run_id = '42'") == len(r.summary)
    s = r.summary.copy()
    s.loc[s.symbol == "TEST02", "close_twd"] += 0.05
    W.write_screen_results(con, replace(r, summary=s), inp.input_max_fetched_at, screen_run_id=41)
    got = dict(con.execute("SELECT version, screen_run_id FROM screen_results WHERE symbol='TEST02'").fetchall())
    assert got == {1: "42", 2: "41"}
    # 草案表（無 screen_run_id 欄）＋未建 job_runs：不檢查存在、不報 dropped
    con2 = _target(tmp_path / "b" if not (tmp_path / "b").mkdir() else tmp_path)
    assert W.write_screen_results(con2, r, inp.input_max_fetched_at, screen_run_id=7).dropped_columns == []
