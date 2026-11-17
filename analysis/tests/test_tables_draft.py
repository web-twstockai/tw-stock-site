"""tables_draft.md 的 DDL 可執行性與關鍵約束（只在記憶體 SQLite，不碰 data/）。"""
import re
import sqlite3
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[1] / "tables_draft.md"
H = "a" * 64


@pytest.fixture()
def con():
    m = re.search(r"```sql\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)
    c = sqlite3.connect(":memory:")
    c.executescript(m.group(1))
    yield c
    c.close()


def _sr(**o):
    r = dict(as_of="2026-11-09", symbol="2330", params_hash=H, params_json="{}", status="FAIL", exclusion_code=None,
             c1_enabled=1, c2_enabled=1, c3_enabled=1, c4_enabled=1, c1_passed=0, conditions_json="[]",
             analysis_version="analysis-0.5.0", computed_at="2026-11-09T18:29:00+08:00", generated_at="2026-11-09T18:30:00+08:00",
             checksum="x")
    r.update(o)
    return r


def _ins(con, table, row):
    con.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


def test_three_tables_created(con):
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"screen_results", "news_events", "news_event_symbols", "daily_summaries"}


def test_news_event_symbols_constraints(con):
    ok = dict(event_id="EV-1", version=1, symbol="2330", evidence_type="官方公告代號", source_news_id=5)
    _ins(con, "news_event_symbols", ok)
    for bad in (dict(evidence_type="猜測"), dict(source_news_id=None), dict(symbol="TEST01")):
        with pytest.raises(sqlite3.IntegrityError):
            _ins(con, "news_event_symbols", dict(ok, symbol=bad.pop("symbol", "2317"), **bad))
    _ins(con, "news_event_symbols", dict(ok, version=2))   # 新版本另存一組
    assert con.execute("SELECT count(*) FROM news_event_symbols WHERE symbol='2330'").fetchone()[0] == 2


def test_screen_results_constraints(con):
    _ins(con, "screen_results", _sr())
    with pytest.raises(sqlite3.IntegrityError):  # 現行列唯一
        _ins(con, "screen_results", _sr(version=2))
    con.execute("UPDATE screen_results SET is_current=0")
    _ins(con, "screen_results", _sr(version=2))
    for bad in (dict(symbol="TEST01"), dict(coverage_policy="gap_check"), dict(status="EXCLUDED"),
                dict(status="PASS"), dict(params_hash="abc")):
        with pytest.raises(sqlite3.IntegrityError):
            _ins(con, "screen_results", _sr(symbol=bad.pop("symbol", "2317"), **bad))


def test_news_events_constraints(con):
    ok = dict(event_id="EV-1", category="媒體報導", title="t", first_published_at="2026-11-09T10:00:00+08:00",
              last_published_at="2026-11-09T11:00:00+08:00", news_ids_json="[1,2]", dedup_method="title_similarity",
              dedup_params_json='{"jaccard_min":0.6}', analysis_version="v", computed_at="g", generated_at="g", checksum="c")
    _ins(con, "news_events", ok)
    for bad in (dict(category="利多"), dict(news_ids_json="[]"), dict(category="官方已公告事實"),
                dict(inference_text="可能…")):
        with pytest.raises(sqlite3.IntegrityError):
            _ins(con, "news_events", dict(ok, event_id="EV-x", **bad))


def test_daily_summaries_constraints(con):
    ok = dict(summary_date="2026-11-09", params_hash=H, screen_ref_json="{}", text="今日 0 檔", facts_json="[]",
              n_pass=0, n_fail=0, n_excluded=13, generation_method="rule", traceability_check="pass",
              review_status="not_required", analysis_version="v", computed_at="g", generated_at="g", checksum="c")
    _ins(con, "daily_summaries", ok)
    for bad in (dict(generation_method="ai"),                                  # 缺 prompt 版本
                dict(generation_method="ai", ai_prompt_version="p1", review_status="not_required"),
                dict(traceability_check="fail", review_status="approved", reviewed_by="x", reviewed_at="y"),
                dict(review_status="approved")):                                # 核可須有審核人與時間
        with pytest.raises(sqlite3.IntegrityError):
            _ins(con, "daily_summaries", dict(ok, summary_date="2026-11-10", **bad))


def test_analysis_tables_trace_columns(con):
    """DECISIONS 2026-11-10 03:45 ③：三張分析表都有 computed_at、analysis_version、input_max_fetched_at；
    ② news_events 不再有 related_symbols_json。"""
    for t in ("screen_results", "news_events", "daily_summaries"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        assert {"computed_at", "analysis_version", "input_max_fetched_at", "generated_at"} <= cols, t
    assert "related_symbols_json" not in {r[1] for r in con.execute("PRAGMA table_info(news_events)")}
