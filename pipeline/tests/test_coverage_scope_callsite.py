"""呼叫端修正（2026-11-18）：3b 遷移後除權息覆蓋表沒有 scope=all 的列，
sample30（傳代號清單）必須傳 coverage_scope="tracked" 與 scope_label="追蹤範圍"；全市場（None）維持 "all"／「全市場」。"""
from __future__ import annotations

import sqlite3
import sys
import types

import pandas as pd
import pytest

from pipeline import analysis_bridge


def _fake_modules(monkeypatch, captured):
    io = types.ModuleType("io_sqlite")

    def load_screen_inputs(db, **kw):
        captured["load"] = kw
        return types.SimpleNamespace(as_of="2026-11-17", screen_kwargs=lambda: {}, input_max_fetched_at=None)

    io.load_screen_inputs = load_screen_inputs
    sc = types.ModuleType("screener")
    sc.ScreenParams = lambda: None
    sc.screen = lambda **k: types.SimpleNamespace(summary=pd.DataFrame({"status": ["FAIL"]}))
    na = types.ModuleType("narrative")

    def summary_narrative(result, **kw):
        captured["narr"] = kw
        return object()

    na.summary_narrative = summary_narrative
    wr = types.ModuleType("writer")
    res = types.SimpleNamespace(inserted=1, versioned=0, skipped=0, dropped_columns=[])
    wr.connect_writable = lambda *a, **k: types.SimpleNamespace(close=lambda: None)
    wr.write_screen_results = lambda *a, **k: res
    wr.write_daily_summary = lambda *a, **k: res
    for name, m in (("io_sqlite", io), ("screener", sc), ("narrative", na), ("writer", wr)):
        monkeypatch.setitem(sys.modules, name, m)


@pytest.mark.parametrize("symbols,scope,label", [(["2330", "2317"], "tracked", "追蹤範圍"), (None, "all", "全市場")])
def test_bridge_passes_coverage_scope(monkeypatch, tmp_path, symbols, scope, label):
    captured: dict = {}
    _fake_modules(monkeypatch, captured)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trading_calendar (calendar_date TEXT, is_trade_day INTEGER)")
    out = analysis_bridge.run_screen_and_write(conn, 1, symbols)
    assert out["ok"], out
    assert captured["load"]["coverage_scope"] == scope
    assert captured["load"]["symbols"] == symbols
    assert captured["narr"]["scope_label"] == label
