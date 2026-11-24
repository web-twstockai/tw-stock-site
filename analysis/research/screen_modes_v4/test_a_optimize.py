"""a_optimize 出場規則的合成資料測試（只驗證邏輯）。"""
import numpy as np
import a_optimize as A

FEE, TAX = A.FEE, A.TAX


def mats(T=12):
    v = np.ones((T, 1), bool)
    M = {k: np.full((T, 1), 100.0) for k in ("ao", "ah", "al", "ac", "ro")}
    return v, M


def net(px, entry=100.0):
    return px * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1


def test_fix_matches_cost_formula():
    v, M = mats(); M["ac"][5, 0] = 104.0
    ret, gross, flag, hd = A.outcomes(v, M, 5, "FIX")
    assert flag[0, 0] == 0 and abs(ret[0, 0] - net(104.0)) < 1e-12 and hd[0, 0] == 5 and abs(gross[0, 0] - 0.04) < 1e-12


def test_tp_hit_intraday_and_stop_first_when_both():
    v, M = mats(); M["ah"][3, 0] = 106.0
    ret, *_ = A.outcomes(v, M, 5, "TP5_SL5")
    assert abs(ret[0, 0] - net(105.0)) < 1e-12
    v, M = mats(); M["ah"][2, 0] = 106.0; M["al"][2, 0] = 94.0
    ret, *_ = A.outcomes(v, M, 5, "TP5_SL5")
    assert abs(ret[0, 0] - net(95.0)) < 1e-12           # 同日都碰到 → 保守先停損


def test_gap_down_through_stop_fills_at_open():
    v, M = mats(); M["ao"][3, 0] = 90.0; M["al"][3, 0] = 89.0
    ret, *_ = A.outcomes(v, M, 5, "TP5_SL5")
    assert abs(ret[0, 0] - net(90.0)) < 1e-12


def test_first_profitable_close_and_trailing():
    v, M = mats(); M["ac"][2, 0] = 100.5; M["ac"][3, 0] = 101.0
    ret, _, _, hd = A.outcomes(v, M, 5, "FPC")
    assert abs(ret[0, 0] - net(101.0)) < 1e-12 and hd[0, 0] == 3   # 100.5 扣成本仍為負，不出場
    v, M = mats(); M["ac"][2, 0] = 110.0; M["ac"][3, 0] = 104.0
    ret, *_ = A.outcomes(v, M, 5, "TRAIL5")
    assert abs(ret[0, 0] - net(104.0)) < 1e-12
