"""獨立重算核對測試（測試用合成資料）。"""
import screener
import verify_independent as V


def test_independent_recompute_matches_screener(synth_db):
    stats = V.run_comparison(synth_db, n_dates=5)
    assert stats["rows"] > 0
    assert stats["mismatches"] == []


def test_independent_is_sensitive(synth_db):
    data = V.load_db(synth_db)
    uni = sorted(data["secs"])
    a = V.recompute(data, screener.ScreenParams().to_dict(), uni)
    b = V.recompute(data, screener.ScreenParams(volume_multiplier=1.2).to_dict(), uni)
    assert a["TEST02"]["status"] == "FAIL" and b["TEST02"]["status"] == "PASS"


def test_independent_series_matches(synth_db):
    st = V.run_series_comparison(synth_db, n_dates=1)
    assert st["rows"] > 0 and st["values"] > 0
    assert st["mismatches"] == []


def test_independent_series_sensitive(synth_db):
    """獨立實作對可信邊界有反應：TEST10 跳空日前均線為 None。"""
    data = V.load_db(synth_db)
    p = screener.ScreenParams().to_dict()
    m = V.ind_ma_series(data, "TEST10", (20,), p)
    sts = [r["status"] for r in m.values()]
    assert "unavailable" in sts and sts[-1] == "ok"


def test_independent_with_segmented_coverage_and_aliases(tmp_path):
    """分段覆蓋（含缺口與迄日）＋ schema §10 別名：兩邊仍逐值一致，且確實有覆蓋排除。"""
    import synthetic_data as SD
    rows = [dict(market="TWSE", action_kind="ex_right_dividend", coverage_start="2020-01-01", coverage_end="2026-07-15"),
            dict(market="TWSE", action_kind="ex_right_dividend", coverage_start="2026-07-16", coverage_end=None),
            dict(market="TWSE", action_kind="capital_reduction_restore", coverage_start="2020-01-01",
                 coverage_end="2026-08-10"),
            dict(market="TWSE", action_kind="capital_reduction_restore", coverage_start="2026-08-20", coverage_end=None),
            dict(market="TPEx", action_kind="dividend", coverage_start="2020-01-01", coverage_end="2026-09-30"),
            dict(market="TPEx", action_kind="capital_reduction", coverage_start="2020-01-01", coverage_end=None)]
    rows += [dict(market=m, action_kind=k, coverage_start="2020-01-01", coverage_end=None)
             for m in ("TWSE", "TPEx") for k in ("par_value_change", "stock_split")]
    db = SD.build_sqlite(tmp_path / "seg.db", coverage_rows=rows)
    st = V.run_comparison(db, n_dates=3)
    assert st["mismatches"] == []
    sc = st["status_counts"]
    assert ("預設參數", "PASS") not in sc and sc[("預設參數", "EXCLUDED")] > 0  # 視窗跨缺口 → strict 全排除
    assert any(k[1] == "PASS" for k in sc)  # 短視窗參數組落在最後一段內，仍可評估
    ss = V.run_series_comparison(db, n_dates=1)
    assert ss["mismatches"] == []


def test_independent_without_halts_and_migrated_columns(tmp_path):
    """trading_halts 不存在、is_halted／cash_subscription_ratio 未遷移時，兩邊仍一致，且停牌改判為「停牌或資料缺漏」。"""
    import synthetic_data as SD
    db = SD.build_sqlite(tmp_path / "old.db", with_halts_table=False, with_extra_columns=False)
    st = V.run_comparison(db, n_dates=2)
    assert st["mismatches"] == []
    data = V.load_db(db)
    r = V.recompute(data, screener.ScreenParams().to_dict(), ["TEST26", "TEST27"])
    assert r["TEST26"]["exclusion_code"] == "EXCL_NO_ROW_SUSPENDED_OR_MISSING"
    assert r["TEST27"]["exclusion_code"] == "EXCL_NO_TRADE"


def test_independent_detects_suspended(synth_db):
    data = V.load_db(synth_db)
    r = V.recompute(data, screener.ScreenParams().to_dict(), ["TEST07", "TEST26", "TEST27"])
    assert r["TEST26"]["exclusion_code"] == "EXCL_SUSPENDED" and r["TEST27"]["exclusion_code"] == "EXCL_SUSPENDED"
    assert r["TEST07"]["window_halt_days"] == 2


def test_independent_stock_split_covered_by_par_value_change(tmp_path):
    """只有 par_value_change 覆蓋列（無 stock_split 列）：兩邊一致且 strict 可過關。"""
    import synthetic_data as SD
    rows = [r for r in SD.tables()["actions_coverage"] if r["action_kind"] != "stock_split"]
    db = SD.build_sqlite(tmp_path / "par.db", coverage_rows=rows)
    st = V.run_comparison(db, n_dates=2)
    assert st["mismatches"] == []
    data = V.load_db(db)
    r = V.recompute(data, screener.ScreenParams().to_dict(), ["TEST01", "TEST32", "TEST33"])
    assert all(r[s]["status"] == "PASS" for s in ("TEST01", "TEST32", "TEST33"))
