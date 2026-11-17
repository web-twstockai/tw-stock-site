"""月營收近 12 個月新高／年增率（revenue.py）測試。只用【測試用合成資料】（tests/revenue_synthetic.py，代號 TESTR*）。"""
import math

import pandas as pd
import pytest

import revenue as R
import revenue_synthetic as RS
import verify_revenue as VR


@pytest.fixture(scope="module")
def fx():
    df = pd.read_csv(RS.FIXTURE, dtype={"symbol": str, "revenue_month": str})
    return df


def test_fixture_labelled_and_up_to_date(fx):
    assert fx["symbol"].str.startswith("TESTR").all() and (fx["source"] == RS.SRC).all()
    assert (fx["remark"].dropna() == "測試用合成資料").all()
    assert len(fx) == len(RS.rows())


@pytest.mark.parametrize("sym,exp", sorted(RS.EXPECTED.items()))
def test_expected(fx, sym, exp):
    out = R.evaluate_revenue(fx, month=RS.EVAL_MONTH).set_index("symbol")
    r = out.loc[sym]
    assert (r["rev_12m_high_status"], r["rev_12m_high_reason"], r["yoy_status"], r["yoy_reason"]) == exp
    assert r["rev_12m_high"] is {"true": True, "false": False, "insufficient": None}[exp[0]]
    assert r["rev_12m_high_version"] == "rev_12m_high-v1" and r["yoy_version"] == R.REV_YOY_VERSION
    assert exp[0] != "true" or r["revenue_twd"] > r["prior_12m_max_twd"]


def test_details(fx):
    out = R.evaluate_revenue(fx, month=RS.EVAL_MONTH).set_index("symbol")
    assert out.loc["TESTR03", "missing_months"] == "2026-05"            # 斷月
    assert out.loc["TESTR04", "null_months"] == "2026-03"               # NULL
    assert out.loc["TESTR05", "missing_months"] == "2025-10"            # 不足 13 個月
    assert out.loc["TESTR02", "revenue_twd"] == out.loc["TESTR02", "prior_12m_max_twd"]   # 相等不算
    assert out.loc["TESTR13", "prior_12m_max_months"] == "2025-10"      # 視窗邊界 m−12 有算進去
    assert out.loc["TESTR10", "prior_12m_max_twd"] == 104_000_000       # m−13 不算進去
    assert (out.loc["TESTR01", "prior_window_start"], out.loc["TESTR01", "prior_window_end"]) == ("2025-10", "2026-09")
    # 年增率不產生 inf／NaN 以外的怪值；不適用時為 None
    for v in out["yoy_pct"]:
        assert v is None or (isinstance(v, float) and (math.isnan(v) or math.isfinite(v)))
    assert pd.isna(out.loc["TESTR07", "yoy_pct"]) and out.loc["TESTR07", "revenue_prev_year_twd"] == 0
    assert out.loc["TESTR01", "yoy_pct"] == pytest.approx((150_000_000 / 102_000_000 - 1) * 100)


def test_stale_rows_ignored_and_duplicates_rejected(fx):
    out = R.evaluate_revenue(fx, month=RS.EVAL_MONTH).set_index("symbol")
    assert out.loc["TESTR01", "prior_12m_max_twd"] == 104_000_000       # is_current=0 的 999,000,000 不算
    dup = pd.concat([fx, fx[(fx.symbol == "TESTR01") & (fx.is_current == 1)].head(1)])
    with pytest.raises(ValueError, match="重複"):
        R.evaluate_revenue(dup, month=RS.EVAL_MONTH)


def test_symbols_missing_current_and_all_months(fx):
    out = R.evaluate_revenue(fx, month=RS.EVAL_MONTH, symbols=["TESTR01", "TESTRXX"]).set_index("symbol")
    assert out.loc["TESTRXX", "rev_12m_high_status"] == "insufficient"
    assert out.loc["TESTRXX", "rev_12m_high_reason"] == "REV_CURRENT_MISSING"
    allm = R.evaluate_revenue(fx[fx.symbol == "TESTR01"])
    assert len(allm) == 24 and allm["rev_12m_high_status"].iloc[0] == "insufficient"   # 最早月份沒有前 12 個月


def test_month_add():
    assert R.month_add("2026-01", -1) == "2025-12" and R.month_add("2025-12", 1) == "2026-01"
    assert R.month_add("2026-10", -12) == "2025-10"
    for bad in ("2026-13", "2026/10", "202610"):
        with pytest.raises(ValueError):
            R.month_add(bad, 0)


def test_reason_codes_documented():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[2] / "docs" / "indicators.md").read_text(encoding="utf-8")
    for code in R.REASON_CODES:
        assert code in doc, code
    assert R.REV_12M_HIGH_VERSION in doc and R.REV_YOY_VERSION in doc


def test_independent_matches(tmp_path):
    db = RS.build_sqlite(tmp_path / "rev.db")
    st = VR.compare(db, RS.EVAL_MONTH)
    assert st["n_symbols"] == len(RS.EXPECTED) and st["mismatches"] == []
    got = {s: b["status"] for s, b in st["rows"]}
    assert got == {s: e[0] for s, e in RS.EXPECTED.items()}


def test_independent_is_sensitive(tmp_path):
    db = RS.build_sqlite(tmp_path / "rev.db")
    data = VR.load(db)
    assert VR.ind_eval(data, "TESTR02", RS.EVAL_MONTH)["status"] == "false"
    data["TESTR02"]["m"][VR.ym_index(RS.EVAL_MONTH)] = (104_000_001, None)
    assert VR.ind_eval(data, "TESTR02", RS.EVAL_MONTH)["status"] == "true"
