"""series.py 單元測試（測試用合成資料）。"""
import math

import pandas as pd
import pytest

import screener as S
import series as SR
import synthetic_data as SD


def kw(T, sym, **o):
    a = dict(prices=T["daily_prices"], actions=T["corporate_actions"], calendar=T["trading_calendar"], symbol=sym,
             securities=T["securities"], actions_coverage=T["actions_coverage"])
    a.update(o)
    return a


def _scr(T):
    return S.screen(prices=T["daily_prices"], calendar=T["trading_calendar"], actions=T["corporate_actions"],
                    securities=T["securities"], actions_coverage=T["actions_coverage"], halts=T["trading_halts"]).summary.set_index("symbol")


@pytest.mark.parametrize("sym", ["TEST01", "TEST05", "TEST06", "TEST07", "TEST02", "TEST25"])
def test_last_row_equals_screener(T, sym):
    s = _scr(T).loc[sym]
    m = SR.ma_series(**kw(T, sym)).iloc[-1]
    assert math.isclose(m["ma_20"], s["ma_short"], rel_tol=1e-12)
    assert math.isclose(m["ma_60"], s["ma_long"], rel_tol=1e-12)


def test_insufficient_history_notes(T):
    m = SR.ma_series(**kw(T, "TEST01"))
    assert m["ma_20"].iloc[:19].isna().all() and (m["ma_20_note"].iloc[:19] == "INSUFFICIENT_HISTORY").all()
    assert m["ma_20"].iloc[19:].notna().all() and m["ma_20_note"].iloc[19:].isna().all()
    assert m["ma_60"].iloc[:59].isna().all()


@pytest.mark.parametrize("sym,code", [("TEST08", "EXCL_ADJ_FACTOR_MISSING"), ("TEST10", "EXCL_UNEXPLAINED_GAP"),
                                      ("TEST24", "EXCL_ADJ_COVERAGE_GAP")])
def test_unavailable_segment(T, sym, code):
    m = SR.ma_series(**kw(T, sym))
    un = m[m["status"] == "unavailable"]
    assert len(un) > 0 and (un["unavailable_code"] == code).all()
    assert un["adj_close_twd"].isna().all()
    # 不可用段之後才重新起算均線
    first_ok = m.index[m["status"] == "ok"]
    if len(first_ok):
        i = first_ok[0]
        assert m.loc[i:i + 18, "ma_20"].isna().all()


@pytest.mark.parametrize("cov,reason", [(None, "coverage_table_missing"), ("empty", "coverage_table_empty")])
def test_no_coverage_whole_series_unavailable(T, cov, reason):
    c = None if cov is None else T["actions_coverage"].iloc[0:0]
    m = SR.ma_series(**kw(T, "TEST05", actions_coverage=c))
    assert (m["status"] == "unavailable").all() and m["ma_20"].isna().all()
    assert reason in m["unavailable_reason"].iloc[-1]


def test_change_series_on_resumption_and_event(T):
    c = SR.change_series(**kw(T, "TEST07")).set_index("price_date")
    s = _scr(T).loc["TEST07"]
    last = c.iloc[-1]
    assert math.isclose(last["change_pct"], s["change_pct"], rel_tol=1e-12)
    ev = c[c["is_event_day"] == True]  # noqa: E712
    assert len(ev) == 1
    e = ev.iloc[0]
    # 減資恢復日：基準＝前一有效收盤 × f，前一有效日早於日曆前一日（停止買賣期間）
    assert math.isclose(e["change_base_twd"], e["prev_close_twd"] * 2.0, rel_tol=1e-9)
    assert e["change_check"] == "ok"


def test_change_mismatch_flagged_not_overwritten(T):
    c = SR.change_series(**kw(T, "TEST04")).iloc[-1]
    assert c["change_check"] == "mismatch"
    assert math.isclose(c["change_twd_calc"], c["close_twd"] - c["change_base_twd"], abs_tol=1e-12)
    assert not math.isclose(c["change_twd_calc"], c["source_change_twd"], abs_tol=0.01)


def test_change_column_optional(T):
    p = T["daily_prices"].drop(columns=["change_twd", "change_sign"])
    c = SR.change_series(**kw(T, "TEST01", prices=p))
    assert (c["change_check"].dropna() == "no_source").all()
    assert c["change_pct"].iloc[1:].notna().all()


def test_change_unavailable_on_failed_event(T):
    c = SR.change_series(**kw(T, "TEST08"))
    assert (c["status"] == "unavailable").sum() == 1
    assert c.loc[c["status"] == "unavailable", "change_pct"].isna().all()


def test_windows_validation(T):
    with pytest.raises(ValueError):
        SR.ma_series(**kw(T, "TEST01"), windows=(0,))
