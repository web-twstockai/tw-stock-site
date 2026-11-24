"""c_chips 的合成資料測試（籌碼表目前 0 列，只能用合成資料驗證程式邏輯，不代表任何績效）。"""
import sqlite3
import numpy as np
import pandas as pd
import pytest
import c_chips as CC

DAYS = pd.bdate_range("2026-01-05", periods=12)


def px(symbols=("1111", "2222"), amount=2e8):
    rows = []
    for s in symbols:
        for d in DAYS:
            rows.append(dict(date=d, symbol=s, close=10.0, ma60=9.0, ret5=0.02, volume_shares=1_000_000, amount_twd=amount))
    return pd.DataFrame(rows)


def inst_rows(sym, market, f, t, tot=None):
    out = []
    for d, fv, tv in zip(DAYS, f, t):
        r = dict(symbol=sym, market=market, trade_date=str(d.date()), trust_net=tv, dealer_net=0,
                 total_net=tot if tot is not None else (fv or 0) + (tv or 0))
        if market == "TPEx":
            r.update(foreign_ex_dealer_net=None, foreign_dealer_net=None, foreign_total_net=fv)
        else:
            r.update(foreign_ex_dealer_net=fv, foreign_dealer_net=0, foreign_total_net=None)
        out.append(r)
    return out


def empty():
    return pd.DataFrame()


def test_ch1_ch2_ch3_streaks():
    f = [1, 1, 1, 1, 1, -1, 1, 1, 1, 1, 1, 1]
    t = [1, 1, 1, -1, 1, 1, 1, 1, 0, 1, 1, 1]
    inst = pd.DataFrame(inst_rows("1111", "TWSE", f, t))
    o = CC.chip_signals(px(("1111",)), inst, empty(), empty())
    assert o["CH1"].tolist() == [a > 0 and b > 0 for a, b in zip(f, t)]
    assert o["CH2"].tolist() == [False, False, True, False, False, False, True, True, False, False, False, True]
    assert o["CH3"].tolist() == [False] * 4 + [True] + [False] * 5 + [True, True]


def test_tpex_uses_foreign_total_and_null_gives_no_signal():
    inst = pd.DataFrame(inst_rows("2222", "TPEx", [5] * 12, [5] * 12))
    o = CC.chip_signals(px(("2222",)), inst, empty(), empty())
    assert o["CH1"].all()
    inst2 = pd.DataFrame(inst_rows("2222", "TWSE", [None] * 12, [5] * 12))   # TWSE 外資 NULL → 不推算
    o2 = CC.chip_signals(px(("2222",)), inst2, empty(), empty())
    assert not o2["CH1"].any() and not o2["CH3"].any()


def test_liquidity_filter():
    inst = pd.DataFrame(inst_rows("1111", "TWSE", [1] * 12, [1] * 12))
    o = CC.chip_signals(px(("1111",), amount=5e7), inst, empty(), empty())
    assert not o[list(CC.CH_NAMES)].any().any()


def test_ch4_top_decile_and_positive():
    syms = [f"{i:04d}" for i in range(1000, 1020)]
    rows = []
    for k, s in enumerate(syms):
        rows += inst_rows(s, "TWSE", [0] * 12, [0] * 12, tot=(k - 5) * 1000)
    o = CC.chip_signals(px(syms), pd.DataFrame(rows), empty(), empty())
    top = o[o["CH4"]]["symbol"].unique().tolist()
    assert sorted(top) == ["1017", "1018", "1019"]   # 百分位 ≥0.9（含邊界，與價格類變體一致）：20 檔中第 18～20 名


def test_ch5_ch6_margin():
    mg = pd.DataFrame([dict(symbol="1111", market="TPEx", trade_date=str(d.date()), margin_balance=100 - i,
                            margin_prev_balance=101 - i, margin_usage_pct=5.0) for i, d in enumerate(DAYS)])
    o = CC.chip_signals(px(("1111",)), empty(), mg, empty())
    assert o["CH5"].tolist() == [False] * 5 + [True] * 7
    assert o["CH6"].all()


def test_ch7_weekly_available_next_trade_day():
    td = pd.DataFrame([dict(symbol="1111", data_date="2026-01-09", big_pct=50.0),
                       dict(symbol="1111", data_date="2026-01-16", big_pct=51.0)])
    o = CC.chip_signals(px(("1111",)), empty(), empty(), td)
    got = o.set_index("date")["CH7"]
    assert not got[:"2026-01-16"].any()          # 01-16 資料日當天不可用
    assert got["2026-01-19":].all()


def test_empty_db_reports_insufficient(tmp_path):
    db = tmp_path / "e.db"
    con = sqlite3.connect(db)
    con.executescript(open(__import__("pathlib").Path(__file__).resolve().parents[3] / "pipeline" / "chips_schema.sql").read())
    con.close()
    inst, mg, td, st = CC.load_chips(str(db))
    assert st == {"institutional_trades": "資料不足", "margin_balances": "資料不足", "shareholding_distribution": "資料不足"}
    o = CC.chip_signals(px(), inst, mg, td)
    assert not o[list(CC.CH_NAMES)].any().any()
