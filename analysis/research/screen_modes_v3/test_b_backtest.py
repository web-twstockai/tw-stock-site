"""b_backtest 的合成資料測試：§11 成本公式、開盤漲停、順延、去重、MDD。"""
import numpy as np
import b_backtest as BB


def mats(T=12, price=10.0):
    valid = np.ones((T, 1), bool)
    ao = np.full((T, 1), price); ac = np.full((T, 1), price); ro = ao.copy()
    return valid, ao, ac, ro


def test_cost_formula_h5():
    valid, ao, ac, ro = mats()
    ac[5, 0] = 11.0                       # 訊號日 0 → T1＝1 → 第 5 個交易日＝5
    ret, kx, flag = BB.forward(valid, ao, ac, ro, 5)
    exp = 11.0 * (1 - 0.001425 - 0.003) / (10.0 * 1.001425) - 1
    assert flag[0, 0] == 0 and kx[0, 0] == 5 and abs(ret[0, 0] - exp) < 1e-12


def test_flat_price_is_a_loss_after_costs():
    valid, ao, ac, ro = mats()
    ret, _, _ = BB.forward(valid, ao, ac, ro, 5)
    assert ret[0, 0] < 0


def test_limit_up_open_not_entered_and_delay():
    valid, ao, ac, ro = mats(T=40)
    ro[1, 0] = ao[1, 0] = 11.0            # 前收 10 → 漲停 11.0
    ret, _, flag = BB.forward(valid, ao, ac, ro, 5)
    assert flag[0, 0] == 2 and np.isnan(ret[0, 0])
    valid[6, 0] = False; valid[7, 0] = False       # 訊號日 1：T1＝2，T5＝6 停牌 → 順延到 8
    _, kx, flag = BB.forward(valid, ao, ac, ro, 5)
    assert flag[1, 0] == 0 and kx[1, 0] == 8


def test_unsettled_tail():
    valid, ao, ac, ro = mats(T=8)
    _, _, flag = BB.forward(valid, ao, ac, ro, 5)
    assert flag[2, 0] == 0 and flag[3, 0] == 3


def test_dedup_within_hold():
    rows = np.array([0, 1, 4, 5, 9, 0]); cols = np.array([0, 0, 0, 0, 0, 1])
    k = BB.dedup(rows, cols, 5)
    assert k.tolist() == [True, False, False, True, False, True]
    assert BB.dedup(rows, cols, 10).tolist() == [True, False, False, False, False, True]


def test_mdd():
    assert abs(BB.mdd(np.array([1.0, 1.2, 0.9, 1.3])) - (0.9 / 1.2 - 1)) < 1e-12
