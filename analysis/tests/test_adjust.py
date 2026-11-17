"""adjust.py 共用函式單元測試（測試用合成數值）。"""
import pandas as pd
import pytest

import adjust as A


def test_daily_move_limit_segmented():
    assert A.daily_move_limit(pd.Timestamp("2015-05-29"), 0.11, 0.07) == 0.07
    assert A.daily_move_limit(pd.Timestamp("2015-06-01"), 0.11, 0.07) == 0.11


def test_unexplained_gaps_legacy_threshold():
    d = [pd.Timestamp(x) for x in ("2015-05-28", "2015-05-29", "2015-06-01", "2015-06-02")]
    tpos = {x: i for i, x in enumerate(d)}
    close = [100.0, 108.0, 118.8, 118.8]  # +8%（舊制超過 7%）、+10%（新制未超過 11%）
    g = A.unexplained_gaps(d, close, 0.11, 0.07, None, tpos, 5)
    assert [i for i, _, _ in g] == [1]
    assert g[0][2] == 0.07
    # 上市初期免檢
    assert A.unexplained_gaps(d, close, 0.11, 0.07, 0, tpos, 5) == []


@pytest.mark.parametrize("p,t", [(9.99, 0.01), (10, 0.05), (49.95, 0.05), (50, 0.1), (100, 0.5), (500, 1.0),
                                 (1000, 5.0)])
def test_tick_size(p, t):
    assert A.tick_size(p) == t


@pytest.mark.parametrize("chg,sign,state", [
    (1.0, "+", "ok"), (1.0, None, "ok"), (1.0, "-", "mismatch"), (-1.0, "-", "mismatch"),
    (None, None, "no_source"), (float("nan"), None, "no_source"), (0.0, "X", "source_not_comparable"),
    (-1.0, "+", "source_sign_conflict"), (1.004, "+", "ok"), (1.006, "+", "mismatch")])
def test_check_change_states(chg, sign, state):
    st, src, diff, tol = A.check_change(101.0, 100.0, False, chg, sign)
    assert st == state and tol == 0.005


def test_event_day_tolerance_half_tick():
    # 參考價 97.3 → 競價基準進位到 97.3（檔位 0.1）；來源 change 以 97.3 計，容許半檔
    assert A.change_tolerance(97.3, True) == pytest.approx(0.055)
    st, *_ = A.check_change(98.0, 97.27, True, 0.7, "+")
    assert st == "ok"


def test_change_base_multiplies_events_in_interval():
    ev = [A.EventResolution(date=pd.Timestamp("2026-07-02"), action_type="ex_dividend", code=None, detail="",
                            f=0.97, s=1.0, desc="")]
    b, is_ev = A.change_base(100.0, pd.Timestamp("2026-07-01"), pd.Timestamp("2026-07-02"), ev)
    assert b == pytest.approx(97.0) and is_ev
    b, is_ev = A.change_base(100.0, pd.Timestamp("2026-07-02"), pd.Timestamp("2026-07-03"), ev)
    assert b == 100.0 and not is_ev


def test_normalize_coverage_states():
    assert A.normalize_coverage(None).status == "coverage_table_missing"
    empty = pd.DataFrame(columns=list(A.COVERAGE_COLUMNS))
    assert A.normalize_coverage(empty).status == "coverage_table_empty"
    odd = pd.DataFrame([{"market": "TWSE", "action_kind": "split", "coverage_start": "2020-01-01"}])
    c = A.normalize_coverage(odd)
    assert c.status == "ok" and c.segments == {} and c.ignored_kinds == ("split",)
    assert A.coverage_problems(c, "TWSE", pd.Timestamp("2026-01-01"))[0] == "coverage_missing:TWSE:cash_dividend"


def test_coverage_segments_merge():
    rows = [("TPEx", "dividend", "2020-01-01", "2020-12-31"), ("TPEx", "dividend", "2021-01-01", None)]
    rows += [("TPEx", k, "2019-01-01", None) for k in ("capital_reduction", "par_value_change", "stock_split")]
    df = pd.DataFrame(rows, columns=["market", "action_kind", "coverage_start", "coverage_end"])
    c = A.normalize_coverage(df)
    for k in ("cash_dividend", "stock_dividend", "ex_both"):   # 相容值 dividend 展開為三類
        assert len(c.segments[("TPEx", k)]) == 1
    assert A.coverage_problems(c, "TPEx", pd.Timestamp("2020-06-01"), pd.Timestamp("2026-01-01")) == []
    d, probs, note = A.coverage_boundary(c, "TPEx", pd.Timestamp("2026-01-01"))
    assert d == pd.Timestamp("2020-01-01") and probs == [] and note.endswith(":2020-01-01")


def test_action_type_aliases():
    assert A.canonical_action_type("ex_dividend") == "cash_dividend"
    assert A.canonical_action_type("ex_rights") == "stock_dividend"
    assert A.canonical_action_type("ex_both") == "ex_both"
    assert A.canonical_action_type("unknown") == "unknown"


def test_source_change_prefers_change_column():
    """schema §7.3：change（含正負號）優先，缺值退回 change_twd。"""
    df = pd.DataFrame({"symbol": ["A", "A"], "price_date": ["2026-11-06", "2026-11-09"], "open_twd": [1.0, 1.0],
                       "high_twd": [1.0, 1.0], "low_twd": [1.0, 1.0], "close_twd": [1.0, 1.0],
                       "volume_shares": [1, 1], "amount_twd": [1, 1], "change": [-0.5, None],
                       "change_twd": [9.9, 0.3]})
    out = A.normalize_prices(df)
    assert list(out["change_twd"]) == [-0.5, 0.3]


def test_stock_split_covered_by_par_value_change():
    """DECISIONS 2026-11-10 03:45：stock_split 覆蓋＝stock_split 列 ∪ par_value_change 列；反向不成立。"""
    base = [("TWSE", k, "2020-01-01", None) for k in ("cash_dividend", "stock_dividend", "ex_both", "capital_reduction")]
    cols = ["market", "action_kind", "coverage_start", "coverage_end"]
    t0, t1 = pd.Timestamp("2025-01-01"), pd.Timestamp("2026-01-01")
    par_only = A.normalize_coverage(pd.DataFrame(base + [("TWSE", "par_value_change", "2020-01-01", None)], columns=cols))
    assert A.coverage_problems(par_only, "TWSE", t0, t1) == []
    both = A.normalize_coverage(pd.DataFrame(base + [("TWSE", "par_value_change", "2020-01-01", None),
                                                     ("TWSE", "stock_split", "2020-01-01", None)], columns=cols))
    assert A.coverage_problems(both, "TWSE", t0, t1) == []
    # stock_split 列較短時，par_value_change 列補足 → 合併後仍連續
    short = A.normalize_coverage(pd.DataFrame(base + [("TWSE", "par_value_change", "2020-01-01", None),
                                                      ("TWSE", "stock_split", "2025-06-01", None)], columns=cols))
    assert A.coverage_problems(short, "TWSE", t0, t1) == []
    split_only = A.normalize_coverage(pd.DataFrame(base + [("TWSE", "stock_split", "2020-01-01", None)], columns=cols))
    assert A.coverage_problems(split_only, "TWSE", t0, t1) == ["coverage_missing:TWSE:par_value_change"]
    assert A.EXCHANGE_RATIO_COLUMNS == ("share_exchange_ratio",)
