"""screener.py 單元測試（全部使用測試用合成資料，非真實行情）。"""
import math

import pandas as pd
import pytest

import screener as S
import synthetic_data as SD


def run(T, universe=None, drop=None, **kw):
    args = dict(prices=T["daily_prices"], calendar=T["trading_calendar"], actions=T["corporate_actions"],
                securities=T["securities"], actions_coverage=T["actions_coverage"], universe=universe,
                halts=T["trading_halts"])
    args.update(drop or {})
    args.update(kw)
    return S.screen(**args)


def summ(T, universe=None, **kw):
    return run(T, universe=universe, **kw).summary.set_index("symbol")


def test_fixtures_labelled_and_up_to_date(T):
    assert (T["daily_prices"]["source"] == SD.SOURCE).all()
    assert T["daily_prices"]["symbol"].str.startswith("TEST").all()
    assert (T["securities"]["name"].str.startswith("測試用合成-")).all()
    fresh = SD.tables()
    for name in ("daily_prices", "corporate_actions", "trading_calendar", "securities"):
        assert len(fresh[name]) == len(T[name]), name
    rebuilt = pd.DataFrame(fresh["daily_prices"])["close_twd"].astype(float).fillna(-1).tolist()
    assert rebuilt == T["daily_prices"]["close_twd"].fillna(-1).tolist()


def test_fixture_columns_match_schema_ddl():
    ddl = SD.schema_ddl()
    for table, cols in (("daily_prices", SD.PRICE_COLS), ("corporate_actions", SD.ACTION_COLS),
                        ("trading_calendar", SD.CAL_COLS), ("securities", SD.SEC_COLS)):
        block = ddl.split(f"CREATE TABLE IF NOT EXISTS {table} (")[1].split(");")[0]
        ddl_cols = [ln.strip().split()[0] for ln in block.splitlines()
                    if ln.strip() and not ln.strip().startswith(("PRIMARY", "UNIQUE"))]
        # DDL 可能來自 schema.md §4 或 pipeline/schema.sql（後者已內含遷移欄位，如 change、is_halted）
        # change_note、is_no_trade、next_ref_price_twd：pipeline 2026-11-10 新增，analysis 不讀（無成交以價量判定，見 indicators.md §8）
        extra = set(SD.PRICE_EXTRA) | set(SD.ACTION_EXTRA) | {"change", "change_note", "is_no_trade", "next_ref_price_twd"}
        assert set(cols) <= set(ddl_cols) | extra, (table, sorted(set(cols) - set(ddl_cols)))
        assert set(ddl_cols) - set(cols) <= extra, (table, sorted(set(ddl_cols) - set(cols)))


@pytest.mark.parametrize("sid,expected", sorted(SD.EXPECTED_DEFAULT.items()))
def test_expected_status_default(T, all_symbols, sid, expected):
    row = summ(T, universe=all_symbols).loc[sid]
    assert row["status"] == expected[0], row["exclusion_reason"]
    if expected[1]:
        assert row["exclusion_code"] == expected[1]


def test_default_universe_is_securities(T):
    s = summ(T)
    assert "TEST18" not in s.index          # ETF 不在 securities，預設不評估
    assert set(s.index) == set(T["securities"]["symbol"])


def test_is_current_filter(T):
    s = summ(T).loc["TEST01"]
    assert s["close_twd"] != 1.0 and s["status"] == "PASS"   # 舊版本列 close=1.0 被忽略


def test_failing_conditions_are_the_intended_ones(T):
    s = summ(T)
    assert s.loc["TEST02", "pass_volume"] is False and s.loc["TEST02", "pass_breakout"] is True
    assert s.loc["TEST03", "pass_turnover"] is False and s.loc["TEST03", "pass_volume"] is True
    assert s.loc["TEST04", "pass_trend"] is False
    assert s.loc["TEST20", "pass_breakout"] is False


def test_boundaries_ge_pass(T):
    s = summ(T).loc["TEST19"]
    assert s["prior_avg_volume_shares"] == 1_000_000 and s["volume_shares"] == 1_500_000
    assert s["volume_ratio"] == 1.5 and s["pass_volume"] is True
    assert s["amount_twd"] == 100_000_000 and s["pass_turnover"] is True


def test_exdividend_in_window_uses_adjusted_prices(T):
    s = summ(T).loc["TEST05"]
    g = T["daily_prices"][T["daily_prices"].symbol == "TEST05"].reset_index(drop=True)
    a = T["corporate_actions"][T["corporate_actions"].symbol == "TEST05"].iloc[0]
    assert g["high_twd"].iloc[-21:-1].max() > s["close_twd"]   # 未還原會誤判未突破
    assert s["prior_high_twd"] < s["close_twd"] and s["n_adj_events"] == 1
    f = a["reference_twd"] / a["prev_close_twd"]
    k = g.index[g.price_date == a["action_date"]][0]
    exp = max(g["high_twd"].iloc[69:k].max() * f, g["high_twd"].iloc[k:89].max())
    assert math.isclose(s["prior_high_twd"], exp, rel_tol=1e-12)


def test_exdividend_today_change_pct_vs_reference(T):
    s = summ(T).loc["TEST06"]
    a = T["corporate_actions"][T["corporate_actions"].symbol == "TEST06"].iloc[0]
    assert math.isclose(s["change_pct"], (s["close_twd"] / a["reference_twd"] - 1) * 100, rel_tol=1e-9)
    assert (s["close_twd"] / a["prev_close_twd"] - 1) < 0 < s["change_pct"]


def test_capital_reduction_preannounce_only(T):
    s = summ(T).loc["TEST07"]
    assert s["status"] == "PASS" and s["window_gap_days"] == 2 and "參考價依公式推算" in s["adj_events"]
    g = T["daily_prices"][T["daily_prices"].symbol == "TEST07"]
    ev = T["corporate_actions"][T["corporate_actions"].symbol == "TEST07"].iloc[0]
    prior = g[g.volume_shares > 0].iloc[-21:-1]
    vols = [v * (0.5 if d < ev["action_date"] else 1.0) for d, v in zip(prior.price_date, prior.volume_shares)]
    assert math.isclose(s["prior_avg_volume_shares"], sum(vols) / 20, rel_tol=1e-12)
    # 沒有 corporate_actions 時，恢復買賣日會被偵測為無法解釋跳空
    no_act = summ(T, actions=T["corporate_actions"][T["corporate_actions"].symbol != "TEST07"])
    assert no_act.loc["TEST07", "exclusion_code"] == "EXCL_UNEXPLAINED_GAP"


def test_capital_reduction_with_refund_and_reference(T):
    s = summ(T).loc["TEST25"]
    a = T["corporate_actions"][T["corporate_actions"].symbol == "TEST25"].iloc[0]
    assert s["status"] == "PASS" and s["window_gap_days"] == 3
    assert f"f={a['reference_twd'] / a['prev_close_twd']:.6f}" in s["adj_events"]


def test_derived_share_factor_and_switch(T):
    s = summ(T).loc["TEST16"]
    assert "s=1.100000" in s["adj_events"] and s["status"] == "PASS"
    p = S.ScreenParams(derive_share_factor=False)
    assert summ(T, params=p).loc["TEST16", "exclusion_code"] == "EXCL_ADJ_FACTOR_MISSING"


def test_coverage_policies(T):
    assert summ(T).loc["TEST24", "exclusion_code"] == "EXCL_ADJ_COVERAGE_GAP"
    p = S.ScreenParams(coverage_policy="gap_check")
    r = summ(T, params=p).loc["TEST24"]
    assert r["status"] == "PASS" and r["coverage_note"] == "coverage_missing:TPEx:capital_reduction"
    assert summ(T).loc["TEST24", "coverage_note"] == "coverage_missing:TPEx:capital_reduction"


_L = "window_before_coverage_start:TWSE:{}:2026-09-01"
_ALL6 = ("cash_dividend", "stock_dividend", "ex_both", "capital_reduction", "par_value_change", "stock_split")


@pytest.mark.parametrize("cov,note", [
    (None, "coverage_table_missing"),
    ("empty", "coverage_table_empty"),
    ("no_twse_div", "coverage_missing:TWSE:cash_dividend;coverage_missing:TWSE:stock_dividend;"
                    "coverage_missing:TWSE:ex_both"),
    ("no_twse_par_split", "coverage_missing:TWSE:par_value_change;coverage_missing:TWSE:stock_split"),
    ("no_twse_par", "coverage_missing:TWSE:par_value_change"),
    ("late", ";".join(_L.format(k) for k in _ALL6)),
])
def test_coverage_notes_distinguishable(T, cov, note):
    c = T["actions_coverage"]
    arg = {None: None, "empty": c.iloc[0:0],
           "no_twse_div": c[~((c.market == "TWSE") & (c.action_kind == "ex_right_dividend"))],
           "no_twse_par_split": c[~((c.market == "TWSE") & c.action_kind.isin(["par_value_change", "stock_split"]))],
           "no_twse_par": c[~((c.market == "TWSE") & (c.action_kind == "par_value_change"))],
           "late": c.assign(coverage_start="2026-09-01")}[cov]
    s = summ(T, actions_coverage=arg)
    assert (s["status"] != "PASS").all()                  # strict：0 檔就是 0 檔
    r = s.loc["TEST01"]
    assert r["exclusion_code"] == "EXCL_ADJ_COVERAGE_GAP" and r["coverage_note"] == note
    assert note in r["exclusion_reason"]


@pytest.mark.parametrize("variant", ["par_only", "par_and_split_row", "split_row_note"])
def test_stock_split_covered_by_par_value_change(T, variant):
    """DECISIONS 2026-11-10 03:45：stock_split 由 par_value_change 覆蓋。兩種寫法 strict 都要過關：
    一、只有 par_value_change 列；二、另寫一列 stock_split（期間沿用、notes 註 covered_by=par_value_change）。"""
    c = T["actions_coverage"]
    base = c[~((c.market == "TWSE") & (c.action_kind == "stock_split"))]
    if variant == "par_only":
        arg = base
    else:
        row = c[(c.market == "TWSE") & (c.action_kind == "par_value_change")].assign(action_kind="stock_split")
        if variant == "split_row_note":
            row = row.assign(notes="covered_by=par_value_change; source_url=https://www.twse.com.tw/exchangeReport/TWTB8U")
        arg = pd.concat([base, row], ignore_index=True)
    s = summ(T, actions_coverage=arg)
    r = s.loc["TEST01"]
    assert r["status"] == "PASS" and pd.isna(r["coverage_note"])
    assert s.loc["TEST32", "status"] == "PASS"
    # 只有 stock_split 列、沒有 par_value_change 列 → par_value_change 仍缺（反向不成立）
    only_split = c[~((c.market == "TWSE") & (c.action_kind == "par_value_change"))]
    assert summ(T, actions_coverage=only_split).loc["TEST01", "coverage_note"] == "coverage_missing:TWSE:par_value_change"


def test_no_data_vs_suspended(T):
    as_of = T["trading_calendar"].query("is_trade_day == 1")["calendar_date"].max()
    px = T["daily_prices"]
    no_tpex_today = px[~((px.market == "TPEx") & (px.price_date == as_of))]
    s = summ(T, prices=no_tpex_today)
    assert s.loc["TEST24", "exclusion_code"] == "EXCL_NO_DATA"    # 整個市場當日無資料
    assert s.loc["TEST11", "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"  # 同市場有資料、本股無列


def test_holiday_not_counted_as_gap(T):
    # 2026-06-19 為合成假日；所有股票該日無列，但不算停牌缺口
    cal = T["trading_calendar"]
    assert cal.loc[cal.calendar_date == "2026-06-19", "is_trade_day"].iloc[0] == 0
    p = S.ScreenParams(ma_long=80, ma_short=20, max_gap_days=0)
    assert summ(T, params=p).loc["TEST01", "window_gap_days"] == 0


def test_price_on_non_trade_day_is_bad_data(T):
    px = T["daily_prices"]
    extra = px[(px.symbol == "TEST01") & (px.is_current == 1)].tail(1).assign(price_date="2026-06-19")
    s = summ(T, prices=pd.concat([px, extra]))
    assert s.loc["TEST01", "exclusion_code"] == "EXCL_BAD_DATA"


def test_new_listing_no_limit_days_skip_gap(T):
    assert pd.isna(summ(T).loc["TEST15", "exclusion_code"])
    secs = T["securities"].copy()
    secs.loc[secs.symbol == "TEST15", "listed_date"] = None
    assert summ(T, securities=secs).loc["TEST15", "exclusion_code"] == "EXCL_UNEXPLAINED_GAP"


def test_window_gap_tolerance(T):
    assert summ(T).loc["TEST21", "window_gap_days"] == 1
    assert summ(T, params=S.ScreenParams(max_gap_days=0)).loc["TEST21", "exclusion_code"] == "EXCL_WINDOW_GAPS"
    assert summ(T, params=S.ScreenParams(max_gap_days=8)).loc["TEST17", "exclusion_code"] != "EXCL_WINDOW_GAPS"


def test_duplicate_same_day_actions_inconsistent(T):
    a = T["corporate_actions"]
    dup = a[a.symbol == "TEST05"].assign(action_type="ex_both")
    s = summ(T, actions=pd.concat([a, dup]))
    assert s.loc["TEST05", "exclusion_code"] == "EXCL_ADJ_FACTOR_INCONSISTENT"


def test_zero_results_stay_zero(T):
    keep = ["TEST02", "TEST03", "TEST04", "TEST20", "TEST11"]
    r = run(T, universe=keep)
    assert len(r.selected()) == 0 and len(r.summary) == len(keep)
    r2 = run(T, params=S.ScreenParams(volume_multiplier=20.0))
    assert len(r2.selected()) == 0 and r2.params["volume_multiplier"] == 20.0
    assert sum(r2.exclusion_counts().values()) + (r2.summary.status == "FAIL").sum() == len(r2.summary)


def test_no_lookahead(T):
    cal = T["trading_calendar"].query("is_trade_day == 1")["calendar_date"].tolist()
    d = cal[-9]  # TEST05 除息日（第 82 個交易日）前一天
    full = run(T, as_of=d).summary
    cut = run(T, as_of=d, prices=T["daily_prices"][T["daily_prices"].price_date <= d],
              actions=T["corporate_actions"][T["corporate_actions"].action_date <= d]).summary
    pd.testing.assert_frame_equal(full, cut)
    assert full.set_index("symbol").loc["TEST05", "n_adj_events"] == 0


def test_conditions_table_complete(T):
    r = run(T)
    assert len(r.conditions) == 4 * len(r.summary)
    c = r.conditions.set_index(["symbol", "condition_id"])
    assert c.loc[("TEST01", "C3_VOLUME"), "passed"] is True
    assert c.loc[("TEST01", "C3_VOLUME"), "reason"].startswith("成立")
    assert c.loc[("TEST11", "C1_TREND"), "passed"] is None
    assert c.loc[("TEST11", "C1_TREND"), "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"


def test_traceability_columns(T):
    s = summ(T).loc["TEST01"]
    assert s["price_source"] == SD.SOURCE and s["price_fetched_at"] == SD.FETCHED_AT


def test_disabled_condition_not_counted(T):
    p = S.ScreenParams(enable_volume=False)
    assert summ(T, params=p).loc["TEST02", "status"] == "PASS"
    c = run(T, params=p).conditions.set_index(["symbol", "condition_id"])
    assert c.loc[("TEST02", "C3_VOLUME"), "passed"] is None


def test_input_not_mutated(T):
    before = {k: v.copy() for k, v in T.items()}
    run(T)
    for k in T:
        pd.testing.assert_frame_equal(before[k], T[k])


@pytest.mark.parametrize("kw", [
    dict(ma_short=1), dict(ma_long=501), dict(ma_short=60, ma_long=60), dict(volume_multiplier=0.9),
    dict(min_turnover=-1), dict(max_gap_days=21), dict(max_daily_move=0.01), dict(breakout_lookback=0),
    dict(min_turnover=float("nan")), dict(adj_prev_close_tolerance=0.2), dict(coverage_policy="loose"),
    dict(legacy_max_daily_move=0.01),
    dict(enable_trend=False, enable_breakout=False, enable_volume=False, enable_turnover=False),
])
def test_param_out_of_range_raises(kw):
    with pytest.raises(ValueError):
        S.ScreenParams(**kw)


@pytest.mark.parametrize("kw", [dict(ma_short=20.5), dict(ma_short=True), dict(enable_trend=1),
                                dict(volume_multiplier="1.5"), dict(coverage_policy=1)])
def test_param_wrong_type_raises(kw):
    with pytest.raises(TypeError):
        S.ScreenParams(**kw)


def test_bad_inputs_raise(T):
    with pytest.raises(ValueError):
        run(T, prices=T["daily_prices"].drop(columns=["amount_twd"]))
    with pytest.raises(ValueError):
        run(T, as_of="2026-06-19")       # 合成假日
    with pytest.raises(ValueError):  # 覆蓋迄日早於起日
        run(T, actions_coverage={("TWSE", "dividend"): ("2026-06-01", "2026-05-01")})


_PS = [("TWSE", "par_value_change", "2020-01-01", None), ("TWSE", "stock_split", "2020-01-01", None)]


def test_unknown_coverage_kind_ignored_not_counted(T):
    """不認得的 action_kind（例如減資預告）不計入覆蓋 → coverage_missing，而不是當成已覆蓋。"""
    cov = {("TWSE", "dividend"): "2020-01-01", ("TWSE", "capital_reduction_preannounce"): "2020-01-01",
           ("TWSE", "par_value_change"): "2020-01-01", ("TWSE", "stock_split"): "2020-01-01"}
    s = summ(T, universe=["TEST01"], actions_coverage=cov).loc["TEST01"]
    assert s["exclusion_code"] == "EXCL_ADJ_COVERAGE_GAP"
    assert s["coverage_note"] == "coverage_missing:TWSE:capital_reduction"


def test_coverage_kind_aliases_and_segments(T):
    """完整清單＋相容值（ex_right_dividend＝息、權、權息；capital_reduction_restore）；多段區間須涵蓋整個視窗。"""
    cols = ["market", "action_kind", "coverage_start", "coverage_end"]
    base = [("TWSE", "ex_right_dividend", "2020-01-01", None), ("TWSE", "capital_reduction_restore", "2020-01-01", None)]
    df = pd.DataFrame(base + _PS, columns=cols)
    assert summ(T, universe=["TEST01"], actions_coverage=df).loc["TEST01", "status"] == "PASS"
    full = pd.DataFrame([("TWSE", k, "2020-01-01", None) for k in _ALL6], columns=cols)
    assert summ(T, universe=["TEST01"], actions_coverage=full).loc["TEST01", "status"] == "PASS"
    # 中間失敗切段：兩段相鄰（迄日次日即下段起日）視為連續
    seg = pd.DataFrame(base[1:] + _PS + [("TWSE", "ex_right_dividend", "2020-01-01", "2026-07-31"),
                                         ("TWSE", "ex_right_dividend", "2026-08-01", None)], columns=cols)
    assert summ(T, universe=["TEST01"], actions_coverage=seg).loc["TEST01", "status"] == "PASS"
    # 中間缺一段：視窗起日落在缺口前 → window_before_coverage_start（以含 as_of 那段起日）
    hole = pd.DataFrame(base[1:] + _PS + [("TWSE", "ex_right_dividend", "2020-01-01", "2026-07-15"),
                                          ("TWSE", "cash_dividend", "2026-08-01", None),
                                          ("TWSE", "stock_dividend", "2026-08-01", None),
                                          ("TWSE", "ex_both", "2020-01-01", None)], columns=cols)
    s = summ(T, universe=["TEST01"], actions_coverage=hole).loc["TEST01"]
    assert s["coverage_note"] == ("window_before_coverage_start:TWSE:cash_dividend:2026-08-01;"
                                  "window_before_coverage_start:TWSE:stock_dividend:2026-08-01")
    # 覆蓋迄日早於評估日
    old = pd.DataFrame([("TWSE", "ex_dividend", "2020-01-01", "2026-09-30"), ("TWSE", "ex_rights", "2020-01-01", None),
                        ("TWSE", "ex_both", "2020-01-01", None), base[1]] + _PS, columns=cols)
    s = summ(T, universe=["TEST01"], actions_coverage=old).loc["TEST01"]
    assert s["coverage_note"] == "coverage_end_before_as_of:TWSE:cash_dividend:2026-09-30"


def test_capital_reduction_null_refund_not_zero(T):
    """schema §9：refund_per_share_twd 為 NULL 表示未知，不可當作零；缺參考價時即無法計算。"""
    a = T["corporate_actions"].copy()
    a.loc[a.symbol == "TEST07", "refund_per_share_twd"] = None
    s = summ(T, universe=["TEST07"], actions=a).loc["TEST07"]
    assert s["exclusion_code"] == "EXCL_ADJ_FACTOR_MISSING" and "退還股款未知" in s["exclusion_reason"]


def test_duplicate_rows_are_bad_data(T):
    px = T["daily_prices"]
    dup = pd.concat([px, px[(px.symbol == "TEST02")].tail(1)])
    assert summ(T, prices=dup).loc["TEST02", "exclusion_code"] == "EXCL_BAD_DATA"


def test_change_cross_check_flags_not_overwrites(T):
    s = summ(T)
    r = s.loc["TEST04"]
    assert r["change_check"] == "mismatch" and math.isclose(r["change_check_diff_twd"], -0.5, abs_tol=1e-9)
    assert math.isclose(r["change_pct"], (r["close_twd"] / r["change_base_twd"] - 1) * 100)   # 自算值未被覆蓋
    assert s.loc["TEST06", "change_check"] == "ok"          # 除息日：來源以競價基準計，半檔內視為一致
    assert s.loc["TEST01", "change_check"] == "ok"
    no_col = summ(T, prices=T["daily_prices"].drop(columns=["change_twd", "change_sign"]))
    assert no_col.loc["TEST01", "change_check"] == "no_source" and no_col.loc["TEST01", "status"] == "PASS"


def test_params_hash_and_version(T):
    r = run(T)
    assert r.params_hash == S.params_hash(S.ScreenParams()) and len(r.params_hash) == 64
    assert r.params_hash != S.params_hash(S.ScreenParams(volume_multiplier=2.0))
    assert r.analysis_version == S.ANALYSIS_VERSION


def test_suspended_split_codes(T):
    s = summ(T)
    assert "trading_halts" in s.loc["TEST26", "exclusion_reason"]
    assert "is_halted" in s.loc["TEST27", "exclusion_reason"]
    assert s.loc["TEST11", "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"   # 無停牌紀錄
    # 沒有 trading_halts 時無法確認 → 維持「停牌或資料缺漏」
    s2 = summ(T, halts=None)
    assert s2.loc["TEST26", "exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"
    # 沒有 is_halted 欄時，有列無成交 → 無成交
    s3 = summ(T, prices=T["daily_prices"].drop(columns=["is_halted"]))
    assert s3.loc["TEST27", "exclusion_code"] == "EXCL_NO_TRADE"


def test_window_gap_breakdown(T):
    s = summ(T)
    r = s.loc["TEST07"]  # 減資停止買賣 2 日，trading_halts 有紀錄
    assert (r["window_gap_days"], r["window_halt_days"], r["window_missing_days"], r["window_no_trade_days"]) == (2, 2, 0, 0)
    s2 = summ(T, halts=None).loc["TEST07"]
    assert (s2["window_gap_days"], s2["window_halt_days"], s2["window_missing_days"]) == (2, 0, 2)
    for sid in s.index[s["window_gap_days"].notna()]:
        x = s.loc[sid]
        assert x["window_gap_days"] == x["window_halt_days"] + x["window_missing_days"] + x["window_no_trade_days"]


def test_stock_split_and_par_value(T):
    s = summ(T)
    assert "stock_split f=0.5" in s.loc["TEST28", "adj_events"] and "s=2.000000" in s.loc["TEST28", "adj_events"]
    assert "share_exchange_ratio" in s.loc["TEST28", "adj_events"]
    assert "缺換股比例" in s.loc["TEST29", "exclusion_reason"]
    a = T["corporate_actions"].copy()
    m = a.symbol == "TEST28"
    # 參考價與 前收÷換股比例 不符 → 不一致
    bad = a.copy(); bad.loc[m, "share_exchange_ratio"] = 2.5
    assert summ(T, universe=["TEST28"], actions=bad).loc["TEST28", "exclusion_code"] == "EXCL_ADJ_FACTOR_INCONSISTENT"
    # 換股比例 ≤ 0 → 不一致
    neg = a.copy(); neg.loc[m, "share_exchange_ratio"] = 0.0
    assert summ(T, universe=["TEST28"], actions=neg).loc["TEST28", "exclusion_code"] == "EXCL_ADJ_FACTOR_INCONSISTENT"
    # 減資換股率欄不能替代 share_exchange_ratio（schema §3.5）
    alt = a.copy(); alt.loc[m, "share_exchange_ratio"] = None; alt.loc[m, "capital_reduction_ratio"] = 2.0
    assert summ(T, universe=["TEST28"], actions=alt).loc["TEST28", "exclusion_code"] == "EXCL_ADJ_FACTOR_MISSING"
    # share_exchange_ratio 欄不存在（尚未遷移）→ 缺換股比例
    nocol = a.drop(columns=["share_exchange_ratio"])
    assert summ(T, universe=["TEST28"], actions=nocol).loc["TEST28", "exclusion_code"] == "EXCL_ADJ_FACTOR_MISSING"
    # 無參考價：參考價＝前收÷換股比例
    noref = a.copy(); noref.loc[m, "reference_twd"] = None
    assert "參考價＝前收÷換股比例" in summ(T, universe=["TEST28"], actions=noref).loc["TEST28", "adj_events"]


def test_par_value_change_factors(T):
    """變更面額：價格因子 f＝參考價÷前收（無參考價則 1÷換股比例）、股數因子 s＝share_exchange_ratio。"""
    s = summ(T)
    a = T["corporate_actions"].set_index("symbol")
    for sid, ratio in (("TEST32", 4.0), ("TEST33", 2.0)):
        r = s.loc[sid]
        assert r["status"] == "PASS", r["exclusion_reason"]
        assert "par_value_change" in r["adj_events"] and f"s={ratio:.6f}" in r["adj_events"]
    f32 = float(a.loc["TEST32", "reference_twd"]) / float(a.loc["TEST32", "prev_close_twd"])
    assert f"f={f32:.6f}" in s.loc["TEST32", "adj_events"]
    assert "f=0.500000" in s.loc["TEST33", "adj_events"] and "參考價＝前收÷換股比例" in s.loc["TEST33", "adj_events"]
    # 還原：均量以 s 換成新股數口徑，量比不受面額變更影響（情境最後一日為前 20 日均量的 3 倍）
    for sid in ("TEST32", "TEST33"):
        assert 2.9 < s.loc[sid, "volume_ratio"] < 3.1


def test_cash_subscription_ratio(T):
    s = summ(T)
    assert "s=1.150000" in s.loc["TEST31", "adj_events"] and "現增配股率 0.05" in s.loc["TEST31", "adj_events"]
    assert "現金增資" in s.loc["TEST30", "exclusion_reason"]
    a = T["corporate_actions"].copy()
    # 來源現增配股率 0 → 可推算
    z = a.copy(); z.loc[z.symbol == "TEST30", "cash_subscription_ratio"] = 0.0
    assert "來源現增配股率 0" in summ(T, universe=["TEST30"], actions=z).loc["TEST30", "adj_events"]
    # 欄位不存在：相容，假設該次無現金增資
    nocol = a.drop(columns=["cash_subscription_ratio"])
    r = summ(T, universe=["TEST30", "TEST31"], actions=nocol)
    assert "假設該次無現金增資" in r.loc["TEST30", "adj_events"]
    assert "s=1.100000" in r.loc["TEST31", "adj_events"]
    neg = a.copy(); neg.loc[neg.symbol == "TEST31", "cash_subscription_ratio"] = -0.1
    assert summ(T, universe=["TEST31"], actions=neg).loc["TEST31", "exclusion_code"] == "EXCL_ADJ_FACTOR_INCONSISTENT"
