"""io_sqlite.py 測試：以 docs/schema.md DDL 建立的合成 SQLite（測試用合成資料）。"""
import sqlite3

import pandas as pd
import pytest

import io_sqlite as IO
import screener as S
import synthetic_data as SD


def test_load_and_screen_matches_csv_path(synth_db, T):
    inp = IO.load_screen_inputs(synth_db)
    assert inp.as_of == T["trading_calendar"].query("is_trade_day == 1")["calendar_date"].max()
    assert (inp.prices["is_current"] == 1).all()
    a = S.screen(**inp.screen_kwargs()).summary
    b = S.screen(prices=T["daily_prices"], calendar=T["trading_calendar"], actions=T["corporate_actions"],
                 securities=T["securities"], actions_coverage=T["actions_coverage"], halts=T["trading_halts"]).summary
    cols = ["symbol", "status", "exclusion_code", "close_twd", "ma_short", "ma_long", "prior_high_twd", "volume_ratio"]
    pd.testing.assert_frame_equal(a[cols], b[cols])


def test_symbols_filter_keeps_market_days(synth_db, T):
    # analysis-0.8.0：未傳 as_of 時改取範圍內（TEST11）最新行情日，故此處明確傳入市場最新交易日
    market_last = T["trading_calendar"].query("is_trade_day == 1")["calendar_date"].max()
    assert pd.Timestamp(IO.load_screen_inputs(synth_db, symbols=["TEST11"]).as_of) < pd.Timestamp(market_last)
    inp = IO.load_screen_inputs(synth_db, as_of=market_last, symbols=["TEST11"])
    assert set(inp.prices["symbol"]) <= {"TEST11"}
    s = S.screen(**inp.screen_kwargs()).summary.set_index("symbol")
    assert s.loc["TEST11", "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"   # 市場層級資料不受 symbols 限制


def test_lookback_and_as_of(synth_db):
    inp = IO.load_screen_inputs(synth_db, as_of="2026-09-30", lookback_trade_days=70)
    trade = inp.calendar.query("is_trade_day == 1")
    assert len(trade) == 70 and inp.calendar["calendar_date"].max() == "2026-09-30"
    with pytest.raises(ValueError):
        IO.load_screen_inputs(synth_db, as_of="2026-06-19")


@pytest.mark.parametrize("kind,status,note", [
    ("missing", "coverage_table_missing", "coverage_table_missing"),
    ("empty", "coverage_table_empty", "coverage_table_empty"),
    ("no_twse_cr", "ok", "coverage_missing:TWSE:capital_reduction"),
    ("late", "ok", "window_before_coverage_start:TWSE:cash_dividend:2026-08-03;"
                   "window_before_coverage_start:TWSE:stock_dividend:2026-08-03;"
                   "window_before_coverage_start:TWSE:ex_both:2026-08-03"),
])
def test_coverage_table_states(tmp_path, kind, status, note):
    rows = {"empty": [], "no_twse_cr": [r for r in SD.tables()["actions_coverage"]
                                         if not (r["market"] == "TWSE" and r["action_kind"] == "capital_reduction_restore")],
            "late": [dict(r, coverage_start="2026-08-03") if r["action_kind"] == "ex_right_dividend" else r
                     for r in SD.tables()["actions_coverage"]]}.get(kind)
    db = SD.build_sqlite(tmp_path / f"{kind}.db", with_coverage_table=(kind != "missing"), coverage_rows=rows)
    inp = IO.load_screen_inputs(db)
    assert inp.coverage_status == status
    assert (inp.actions_coverage is None) == (kind == "missing")
    s = S.screen(**inp.screen_kwargs()).summary.set_index("symbol")
    assert (s.status != "PASS").all()
    assert s.loc["TEST01", "exclusion_code"] == "EXCL_ADJ_COVERAGE_GAP"
    assert s.loc["TEST01", "coverage_note"] == note


def test_coverage_table_wrong_columns_raises(tmp_path):
    db = SD.build_sqlite(tmp_path / "x.db", with_coverage_table=False)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE corporate_actions_coverage (market TEXT, kind TEXT, start TEXT)")
    con.commit(); con.close()
    with pytest.raises(ValueError):
        IO.load_screen_inputs(db)


def test_readonly(synth_db):
    con = IO.connect_readonly(synth_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM daily_prices")
    con.close()


def test_halts_and_migrated_columns_optional(tmp_path):
    db = SD.build_sqlite(tmp_path / "h.db")
    inp = IO.load_screen_inputs(db)
    assert inp.halts_status == "ok" and set(inp.halts["symbol"]) == {"TEST07", "TEST26"}
    s = S.screen(**inp.screen_kwargs()).summary.set_index("symbol")
    assert s.loc["TEST26", "exclusion_code"] == "EXCL_SUSPENDED"
    assert s.loc["TEST27", "exclusion_code"] == "EXCL_SUSPENDED"
    assert "現增配股率" in s.loc["TEST31", "adj_events"]
    db2 = SD.build_sqlite(tmp_path / "h2.db", with_halts_table=False, with_extra_columns=False)
    inp2 = IO.load_screen_inputs(db2)
    assert inp2.halts is None and inp2.halts_status == "halts_table_missing"
    s2 = S.screen(**inp2.screen_kwargs()).summary.set_index("symbol")
    assert s2.loc["TEST26", "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"
    assert s2.loc["TEST27", "exclusion_code"] == "EXCL_NO_TRADE"
    assert s2.loc["TEST30", "status"] != "EXCLUDED"   # 無 cash_subscription_ratio 欄：依核可假設推算股數因子
