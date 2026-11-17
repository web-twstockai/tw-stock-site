"""series.ohlc_series 與 explain.condition_explanations 單元測試（測試用合成資料）。"""
import math

import numpy as np
import pytest

import explain as E
import screener as S
import series as SR

from test_series import _scr, kw


@pytest.mark.parametrize("sym", ["TEST01", "TEST05", "TEST06", "TEST07", "TEST02", "TEST25"])
def test_ohlc_matches_screener_and_ma_series(T, sym):
    o = SR.ohlc_series(**kw(T, sym))
    m = SR.ma_series(**kw(T, sym))
    b = o.bars
    assert list(b["price_date"]) == list(m["price_date"])
    assert np.allclose(b["adj_close_twd"].to_numpy(float), m["adj_close_twd"].to_numpy(float), equal_nan=True)
    assert (b["status"] == m["status"]).all()
    s = _scr(T).loc[sym]
    if s["status"] == "EXCLUDED":
        return
    ok = b[b["status"] == "ok"]
    assert math.isclose(ok["adj_close_twd"].iloc[-1], s["close_twd"], rel_tol=1e-12)
    p = S.ScreenParams()
    prior = ok.iloc[-p.breakout_lookback - 1:-1]
    assert math.isclose(prior["adj_high_twd"].max(), s["prior_high_twd"], rel_tol=1e-12)
    pv = ok.iloc[-p.volume_lookback - 1:-1]
    assert math.isclose(pv["adj_volume_shares"].mean(), s["prior_avg_volume_shares"], rel_tol=1e-12)


def test_ohlc_adjusted_equals_raw_times_factor(T):
    b = SR.ohlc_series(**kw(T, "TEST05")).bars
    ok = b[b["status"] == "ok"]
    for c in ("open", "high", "low", "close"):
        assert np.allclose(ok[f"adj_{c}_twd"], ok[f"{c}_twd"] * ok["adj_factor"])
    assert np.allclose(ok["adj_volume_shares"], ok["volume_shares"] * ok["share_factor"])
    assert (ok["adj_factor"].iloc[-1], ok["share_factor"].iloc[-1]) == (1.0, 1.0)   # 前復權：評估日因子為 1


def test_ohlc_events_listed(T):
    o = SR.ohlc_series(**kw(T, "TEST05"))
    ev = o.events
    assert len(ev) >= 1 and set(ev.columns) == set(SR.EVENT_COLUMNS)
    ap = ev[ev["status"] == "applied"]
    assert len(ap) >= 1 and ap["price_factor"].notna().all() and ap["code"].isna().all()
    # 事件日前一日的價格因子包含該事件
    d = ap["action_date"].iloc[-1]
    before = o.bars[(o.bars["price_date"] < d) & (o.bars["status"] == "ok")]
    assert before["adj_factor"].iloc[-1] < 1.0 or ap["price_factor"].iloc[-1] == 1.0


@pytest.mark.parametrize("sym,code", [("TEST08", "EXCL_ADJ_FACTOR_MISSING"), ("TEST10", "EXCL_UNEXPLAINED_GAP"),
                                      ("TEST24", "EXCL_ADJ_COVERAGE_GAP")])
def test_ohlc_unavailable_segment_same_as_ma_series(T, sym, code):
    o = SR.ohlc_series(**kw(T, sym))
    m = SR.ma_series(**kw(T, sym))
    un = o.bars[o.bars["status"] == "unavailable"]
    assert len(un) > 0 and (un["unavailable_code"] == code).all()
    assert un["adj_close_twd"].isna().all() and un["close_twd"].notna().all()   # 原始值照列
    assert list(o.bars["status"]) == list(m["status"])
    traded = o.bars[o.bars["status"] != "no_trade"]
    if (traded["status"] == "unavailable").all():
        assert o.all_unavailable and o.boundary_date is None
    else:
        assert o.boundary_date is not None and not o.all_unavailable
        assert (o.bars.loc[o.bars["price_date"] < o.boundary_date, "status"] != "ok").all()
    if code == "EXCL_ADJ_FACTOR_MISSING":
        assert (o.events["status"] == "failed").any()


def test_ohlc_no_coverage_all_unavailable(T):
    o = SR.ohlc_series(**kw(T, "TEST05", actions_coverage=T["actions_coverage"].iloc[0:0]))
    assert o.all_unavailable and (o.bars["status"] == "unavailable").all()
    assert o.bars["adj_close_twd"].isna().all()


def test_explanations_use_params():
    p = S.ScreenParams(ma_short=10, ma_long=30, breakout_lookback=15, volume_lookback=10, volume_multiplier=2.5,
                       min_turnover=50_000_000, max_gap_days=4, enable_volume=False)
    ex = E.condition_explanations(p)
    assert [x["condition_id"] for x in ex] == list(S.CONDITION_NAMES)
    t = {x["condition_id"]: x for x in ex}
    assert "MA10" in t["C1_TREND"]["text"] and "MA30" in t["C1_TREND"]["text"]
    assert "前 15 個有效交易日（不含今天）" in t["C2_BREAKOUT"]["text"]
    assert "前 10 個" in t["C3_VOLUME"]["text"] and "2.5 倍" in t["C3_VOLUME"]["text"]
    assert t["C3_VOLUME"]["enabled"] is False and t["C3_VOLUME"]["text"].startswith("未啟用，不檢查。")
    assert "50,000,000 元" in t["C4_TURNOVER"]["text"]
    c = E.common_explanation(p)
    assert f"不足 {p.required_valid_days} 天" in c and "超過 4 天" in c


def test_explanations_default_and_neutral():
    t = {x["condition_id"]: x["text"] for x in E.condition_explanations()}
    assert "1.5 倍" in t["C3_VOLUME"] and "1 億元" in t["C4_TURNOVER"] and "MA20" in t["C1_TREND"]
    text = "".join(t.values()) + E.common_explanation()
    for w in ("勝率", "報酬", "獲利", "建議", "保證", "看漲", "買進", "賣出"):
        assert w not in text, w
    assert "gap_check" in E.common_explanation(S.ScreenParams(coverage_policy="gap_check"))
    with pytest.raises(TypeError):
        E.condition_explanations({"ma_short": 20})


def test_format_helpers():
    assert E.format_number(2.0) == "2" and E.format_number(1.5) == "1.5"
    assert E.format_amount_twd(100_000_000) == "1 億元" and E.format_amount_twd(250_000_000) == "2.5 億元"
    assert E.format_amount_twd(30_000_000) == "30,000,000 元"
