"""研究用（非正式程式）：籌碼候選訊號 CH1～CH7（事先登記，2026-11-24）。
目前 institutional_trades、margin_balances、shareholding_distribution 皆 0 列 → 一律「資料不足」，不產生任何訊號、不算任何統計。
等 chips_backfill 補到 2023-11 後，依 SCREEN_MODES_v3 §8 的規則原樣重跑（規則與門檻不得因結果修改）。

單位：法人買賣超＝股；融資餘額 TWSE＝交易單位、TPEx＝張（只用變化率，不跨市場比絕對值）；集保分級 tdcc_17（第 15 級＝1,000,001 股以上，即 >1000 張）。
外資口徑：TPEx 用 foreign_total_net（外資及陸資合計）；TWSE 用 foreign_ex_dealer_net＋foreign_dealer_net（兩者之和即官方「外資及陸資」合計）。
任何一個需要的欄位為 NULL → 該股該日不產生訊號（不補值、不當 0）。
資料可得時點：法人、融資為當日盤後公布，訊號日 S＝該交易日，T1 開盤進場（同 §11）；集保為週資料，資料日之後的下一個交易日才可用。"""
import sqlite3
import numpy as np
import pandas as pd

PARAMS = dict(
    ch2_trust_days=3,        # 投信連買天數
    ch3_foreign_days=5,      # 外資連買天數
    ch4_top_pct=0.9,         # 三大法人買超÷成交量 當日排名前 10%
    ch5_margin_days=5,       # 融資餘額 5 日變化
    ch6_usage_max=10.0,      # 融資使用率 <10%（僅 TPEx 有此欄）
    ch7_level=15,            # 集保 >1000 張
    min_turnover=1e8,        # 與價格類變體同一流動性門檻
)
CH_NAMES = {
    "CH1": "外資、投信同日買超",
    "CH2": "投信連 3 日買超",
    "CH3": "外資連 5 日買超",
    "CH4": "三大法人買超占成交量前 10%",
    "CH5": "融資 5 日減少而股價 5 日上漲",
    "CH6": "融資使用率低（<10%）且站上季線",
    "CH7": "千張大戶持股比例週增加且站上季線",
}
SOURCES = {  # 研究依據（見 SCREEN_MODES_v3 §2）
    "CH1": "R15、R16", "CH2": "R16、R17、R20", "CH3": "R15", "CH4": "R16、R17、R20", "CH5": "R18", "CH6": "R18、R20", "CH7": "R19、R21",
}


def load_chips(db):
    """回傳 (inst, margin, tdcc, status)；status＝每表列數，0 列者標「資料不足」。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s: pd.read_sql_query(s, con)
    inst = q("select symbol, market, trade_date, foreign_ex_dealer_net, foreign_dealer_net, foreign_total_net, trust_net, "
             "dealer_net, total_net from institutional_trades where is_current=1")
    mg = q("select symbol, market, trade_date, margin_balance, margin_prev_balance, margin_usage_pct "
           "from margin_balances where is_current=1")
    td = q(f"select symbol, data_date, pct_{PARAMS['ch7_level']} as big_pct from shareholding_distribution where is_current=1")
    con.close()
    status = {"institutional_trades": len(inst), "margin_balances": len(mg), "shareholding_distribution": len(td)}
    status = {k: (n if n else "資料不足") for k, n in status.items()}
    return inst, mg, td, status


def foreign_net(inst):
    tw = inst["foreign_ex_dealer_net"] + inst["foreign_dealer_net"]      # NULL 相加 → NaN
    return np.where(inst["market"] == "TPEx", inst["foreign_total_net"], tw).astype(float)


def _streak(pos, sym):
    """連續為 True 的天數（以股票分組、資料列需已依日期排序；缺列不視為連續中斷以外的任何值）。"""
    grp = (~pos).groupby(sym).cumsum()
    return pos.groupby([sym, grp]).cumsum().where(pos, 0)


def chip_signals(px, inst, mg, td, params=PARAMS):
    """px：date, symbol, close, ma60, ret5, volume_shares, amount_twd（只含可評估股-日）。
    回傳 px 加上 CH1..CH7 布林欄。籌碼表沒有該股該日資料 → False（並在 n_missing 欄計數）。"""
    p = dict(PARAMS, **(params or {}))
    out = px.copy()
    out["date"] = pd.to_datetime(out["date"])
    liq = out["amount_twd"] >= p["min_turnover"]
    for k in CH_NAMES:
        out[k] = False
    if len(inst):
        i = inst.copy()
        i["date"] = pd.to_datetime(i["trade_date"])
        i["fnet"] = foreign_net(i)
        i = i.sort_values(["symbol", "date"]).reset_index(drop=True)
        f_pos = i["fnet"] > 0
        t_pos = i["trust_net"].astype(float) > 0
        i["t_streak"] = _streak(t_pos, i["symbol"])
        i["f_streak"] = _streak(f_pos, i["symbol"])
        i["ch1"] = f_pos & t_pos
        m = out.merge(i[["symbol", "date", "ch1", "t_streak", "f_streak", "total_net"]], on=["symbol", "date"], how="left")
        out["CH1"] = m["ch1"].fillna(False).astype(bool).to_numpy() & liq
        out["CH2"] = (m["t_streak"] >= p["ch2_trust_days"]).to_numpy() & liq
        out["CH3"] = (m["f_streak"] >= p["ch3_foreign_days"]).to_numpy() & liq
        ratio = m["total_net"].astype(float) / m["volume_shares"].where(m["volume_shares"] > 0)
        rk = ratio.groupby(m["date"]).rank(pct=True)
        out["CH4"] = ((rk >= p["ch4_top_pct"]) & (ratio > 0)).to_numpy() & liq
    if len(mg):
        g = mg.copy()
        g["date"] = pd.to_datetime(g["trade_date"])
        g = g.sort_values(["symbol", "date"]).reset_index(drop=True)
        g["mb"] = g["margin_balance"].astype(float)
        g["mb_lag"] = g.groupby("symbol")["mb"].shift(p["ch5_margin_days"])
        g["mchg"] = g["mb"] / g["mb_lag"].where(g["mb_lag"] > 0) - 1
        m = out.merge(g[["symbol", "date", "mchg", "margin_usage_pct"]], on=["symbol", "date"], how="left")
        out["CH5"] = ((m["mchg"] < 0) & (m["ret5"] > 0)).to_numpy() & liq
        out["CH6"] = ((m["margin_usage_pct"] < p["ch6_usage_max"]) & (m["close"] > m["ma60"])).to_numpy() & liq
    if len(td):
        t = td.copy()
        t["data_date"] = pd.to_datetime(t["data_date"])
        t = t.sort_values(["symbol", "data_date"]).reset_index(drop=True)
        t["d_big"] = t.groupby("symbol")["big_pct"].diff()
        days = np.sort(out["date"].unique())
        idx = np.searchsorted(days, t["data_date"].to_numpy(), side="right")   # 資料日之後下一個交易日起可用
        t = t[idx < len(days)].copy()
        t["avail"] = days[idx[idx < len(days)]]
        t = t.sort_values("avail")
        m = pd.merge_asof(out[["symbol", "date"]].reset_index().sort_values("date"), t[["symbol", "avail", "d_big"]],
                          left_on="date", right_on="avail", by="symbol", direction="backward").set_index("index").sort_index()
        out["CH7"] = ((m["d_big"] > 0).to_numpy() & (out["close"] > out["ma60"]).to_numpy()) & liq
    return out
