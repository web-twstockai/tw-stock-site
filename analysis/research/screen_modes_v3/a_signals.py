"""研究用（非正式程式）：篩選模式 v3 候選訊號。沿用 v2 的特徵（c_features）與可評估遮罩（screen panel），
另加 v3 特徵：60 日高點距離、20 日反轉、低波動、MAX（樂透股）、中期動能、營收未預期（SUS）、近 3 月營收年增、
EPS 未預期（以股價標準化）、現金流品質（應計）、盈餘殖利率、品質＋價值綜合、除息事件。
所有參數在看結果前寫死於本檔（2026-11-24 18:3x），之後不依結果修改；變體選擇只用前半段（IS）。
用法：python a_signals.py DB ADJ_PANEL PANEL_DIR OUT_PKL
輸出：每個股-日一列（只含有效日）：date, symbol, ev（screen 可評估）, tv（成交金額）, 以及每個變體的布林欄。"""
import sys, sqlite3, time
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
V2 = HERE.parent / "screen_modes_v2"
sys.path.insert(0, str(V2))
import c_features as CF   # noqa: E402
import e_modes as EM      # noqa: E402

FAMILY = {}   # 變體 → (家族, 中文名, 規則說明, 研究來源 R 編號)


def _q(con, sql):
    return pd.read_sql_query(sql, con)


def _next_trade(trade_days, d):
    i = np.searchsorted(trade_days, np.datetime64(d))
    return trade_days[i] if i < len(trade_days) else None


def revenue_v3(db, trade_days):
    """每檔每月：yoy（單月年增%）、yoy3（近 3 月合計年增%，M03）、sus（標準化未預期營收：
    (S_t−S_{t−12} − 過去 12 個月變動值平均) ÷ 標準差，至少 12 個變動值；原文用 24 個月，本庫歷史不足故縮短）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rv = _q(con, "select symbol, revenue_month, revenue_twd, revenue_prev_year_twd from monthly_revenues where is_current=1")
    con.close()
    rv = rv.sort_values(["symbol", "revenue_month"]).reset_index(drop=True)
    p = pd.PeriodIndex(rv["revenue_month"], freq="M")
    rv["_p"] = p.astype("int64")
    g = rv.groupby("symbol", sort=False)
    contig = (g["_p"].diff() == 1) & (g["_p"].diff(2) == 2)
    s, sl = rv["revenue_twd"].astype(float), rv["revenue_prev_year_twd"].astype(float)
    rv["yoy"] = np.where(sl > 0, (s / sl - 1) * 100, np.nan)
    s3 = g["revenue_twd"].transform(lambda x: x.astype(float).rolling(3).sum())
    sl3 = g["revenue_prev_year_twd"].transform(lambda x: x.astype(float).rolling(3).sum())
    rv["yoy3"] = np.where(contig & (sl3 > 0), (s3 / sl3 - 1) * 100, np.nan)
    chg = s - sl
    rv["_chg"] = chg
    mu = g["_chg"].transform(lambda x: x.shift(1).rolling(24, min_periods=12).mean())
    sd = g["_chg"].transform(lambda x: x.shift(1).rolling(24, min_periods=12).std())
    rv["sus"] = np.where(sd > 0, (chg - mu) / sd, np.nan)
    nxt = (p + 1).to_timestamp() + pd.Timedelta(days=9)
    rv["avail_date"] = [_next_trade(trade_days, d) for d in nxt]
    return rv.dropna(subset=["avail_date"])[["symbol", "avail_date", "yoy3", "sus"]]


def fin_v3(db, trade_days):
    """每檔每季（合併、累計列推單季）：eps_q、eps_q_ly、ttm_eps（近 4 季單季合計）、accrual＝(累計淨利−累計營業現金流)÷累計營收
    （同一期間的年初至今數字；無資產負債表，故以營收標準化，與 Sloan 用總資產不同）、cfo_ni＝累計營業現金流÷累計淨利。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    f = _q(con, "select symbol, fiscal_year y, fiscal_quarter q, eps_twd eps, revenue_twd rev, net_income_twd ni "
                "from income_statements where is_current=1 and period_type='累計' and report_basis='合併'")
    cf = _q(con, "select symbol, fiscal_year y, fiscal_quarter q, operating_cf_twd cfo from cash_flow_statements "
                 "where is_current=1 and period_type='累計' and report_basis='合併'")
    con.close()
    for c in ("eps", "rev", "ni"):
        f[c] = pd.to_numeric(f[c], errors="coerce")
    cf["cfo"] = pd.to_numeric(cf["cfo"], errors="coerce")
    f = f.merge(cf, on=["symbol", "y", "q"], how="left").sort_values(["symbol", "y", "q"]).reset_index(drop=True)
    key = f.set_index(["symbol", "y", "q"])
    prev = key.reindex(pd.MultiIndex.from_arrays([f["symbol"], f["y"], f["q"] - 1]))
    f["eps_q"] = np.where(f["q"] == 1, f["eps"], f["eps"].to_numpy() - prev["eps"].to_numpy())
    k2 = f.set_index(["symbol", "y", "q"])
    ly = k2.reindex(pd.MultiIndex.from_arrays([f["symbol"], f["y"] - 1, f["q"]]))
    f["eps_q_ly"] = ly["eps_q"].to_numpy()
    # TTM：本季與前 3 季單季
    qs = []
    for k in range(4):
        yy = f["y"] - ((f["q"] - 1 - k) < 0).astype(int) * ((k - f["q"]) // 4 + 1)
        qq = ((f["q"] - 1 - k) % 4) + 1
        qs.append(k2.reindex(pd.MultiIndex.from_arrays([f["symbol"], yy, qq]))["eps_q"].to_numpy())
    f["ttm_eps"] = np.sum(qs, axis=0)   # 任一季缺 → NaN
    f["accrual"] = np.where(f["rev"] > 0, (f["ni"] - f["cfo"]) / f["rev"], np.nan)
    f["cfo_ni"] = np.where(f["ni"] > 0, f["cfo"] / f["ni"], np.nan)
    f["ni_pos"] = f["ni"] > 0
    dl = {1: (5, 15), 2: (8, 14), 3: (11, 14)}
    f["deadline"] = [pd.Timestamp(y + 1, 3, 31) if q == 4 else pd.Timestamp(y, *dl[q]) for y, q in zip(f["y"], f["q"])]
    f["avail_date"] = [_next_trade(trade_days, d) for d in f["deadline"]]
    return f.dropna(subset=["avail_date"])[["symbol", "avail_date", "eps_q", "eps_q_ly", "ttm_eps", "accrual", "cfo_ni", "ni_pos"]]


def dividends(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    d = _q(con, "select symbol, action_date, action_type, cash_dividend_twd from corporate_actions "
                "where is_current=1 and action_type in ('ex_dividend','ex_both')")
    con.close()
    d["cash_dividend_twd"] = pd.to_numeric(d["cash_dividend_twd"], errors="coerce")
    d = d[d["cash_dividend_twd"] > 0]
    d["date"] = pd.to_datetime(d["action_date"])
    return d[["symbol", "date"]].drop_duplicates()


def main():
    db, adj, pdir, out = sys.argv[1:5]
    t0 = time.time()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trade_all = pd.to_datetime([r[0] for r in con.execute(
        "select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    con.close()
    tda = trade_all.values.astype("datetime64[ns]")
    g = CF.load_grid(adj, pdir)
    v = CF.tech_features(g); del g
    v = CF.cross_section(v)
    rv = CF.revenue_features(db, tda)
    v = CF.asof_join(v, rv, ["rev_high", "yoy3_pos", "yoy3_ge20", "yoy_accel", "cum_yoy"], "rv_")
    fn = CF.eps_features(db, tda)
    v = CF.asof_join(v, fn, ["eps_ok", "eps_yoy_pos", "eps_yoy_ge25", "gm_up"], "fn_")
    M2 = EM.modes(v)   # v2 模式（參照組）
    print("v2 features", v.shape, f"{time.time()-t0:.0f}s", flush=True)

    C, O, H = v["adj_close_twd"], v["adj_open_twd"], v["adj_high_twd"]
    gb = lambda s: s.groupby(v["symbol"], sort=False)
    v["ret20"] = C / gb(C).shift(20) - 1
    v["mom60_5"] = gb(C).shift(5) / gb(C).shift(60) - 1           # 中期動能（跳過最近 5 日）
    v["hh60_incl"] = gb(H).transform(lambda x: x.rolling(60, min_periods=60).max())
    v["near60"] = C / v["hh60_incl"]
    v["near120"] = C / v["hh120_incl"]
    v["max20"] = gb(v["ret1"]).transform(lambda x: x.rolling(20, min_periods=20).max())
    v = CF.asof_join(v, revenue_v3(db, tda), ["yoy3", "sus"], "r3_")
    v = CF.asof_join(v, fin_v3(db, tda), ["eps_q", "eps_q_ly", "ttm_eps", "accrual", "cfo_ni", "ni_pos"], "f3_")
    v["ey"] = v["f3_ttm_eps"] / v["close_twd"]                    # 盈餘殖利率（原始收盤價，非還原）
    v["esurp"] = (v["f3_eps_q"] - v["f3_eps_q_ly"]) / v["close_twd"]
    ev = v["scr_status"].isin(["PASS", "FAIL"])
    sub = v[ev]
    for c in ("near60", "near120", "ret20", "vol60", "max20", "mom60_5", "r3_yoy3", "r3_sus", "ey", "esurp", "f3_accrual"):
        v[f"{c}_pct"] = np.nan
        v.loc[ev, f"{c}_pct"] = sub.groupby("date")[c].rank(pct=True)
    # 品質＋價值綜合：盈餘殖利率高、應計低、波動低三者百分位平均
    v["qv"] = np.nan
    v.loc[ev, "qv"] = (v.loc[ev, "ey_pct"] + (1 - v.loc[ev, "f3_accrual_pct"]) + (1 - v.loc[ev, "vol60_pct"])) / 3
    v["qv_pct"] = np.nan
    v.loc[ev, "qv_pct"] = v.loc[ev].groupby("date")["qv"].rank(pct=True)
    dv = dividends(db)
    dv["exd"] = True
    v = v.merge(dv.rename(columns={"date": "date"}), on=["symbol", "date"], how="left")
    v["exd"] = v["exd"].fillna(False).astype(bool)
    # 除息日前 7 個有效交易日（假設除息日事先已知：有前視風險，只作參考，不提案）
    v["exd_in7"] = gb(v["exd"]).shift(-7).fillna(False).astype(bool)
    print("v3 features", f"{time.time()-t0:.0f}s", flush=True)

    up = (C > v["ma60"])
    S = {}

    def add(fam, vid, name, rule, cond, src):
        S[vid] = cond.fillna(False).astype(bool).to_numpy()
        FAMILY[vid] = (fam, name, rule, src)

    # ---- 參照：v2 已上線／已研究模式（規則同 analysis/modes.py；M1＝screen PASS）
    for rid, mid in (("M1", "M1"), ("C28", "M9"), ("C29", "M10"), ("C31", "M11"), ("C24", "M12"), ("C05", "M13"),
                     ("C01", "M14"), ("C09", "M15"), ("C13", "M16"), ("C17", "M17"), ("M6s", "M18"), ("C35", "M19"),
                     ("C26", "M20"), ("C06", "M21")):
        tdl = sorted(v["date"].unique())
        ws = tdl[min(M2[rid]["warm"] - 1, len(tdl) - 1)]          # 與 v2 e_run 相同的暖機起點
        S[f"REF_{mid}"] = (M2[rid]["cond"] & (v["date"] >= ws)).to_numpy()
        FAMILY[f"REF_{mid}"] = ("參照（已上線模式）", f"{mid}（{rid}）{M2[rid]['name']}", M2[rid]["desc"], "v2")
    tv = v["amount_twd"] >= 1e8   # 所有新變體都要求成交金額 ≥ 1 億（流動性主口徑）；另以 ≥ 5 億做敏感度
    # F01 接近高點（George & Hwang 2004，52 週高的縮短版）
    add("F01 接近高點", "F01a", "接近 60 日高點（前 10%）", "收盤÷近 60 日最高（含當日）排名前 10%", (v["near60_pct"] >= 0.9) & tv, "R01")
    add("F01 接近高點", "F01b", "接近 60 日高點（前 20%）＋站上季線", "收盤÷近 60 日最高排名前 20%、收盤>MA60", (v["near60_pct"] >= 0.8) & up & tv, "R01")
    add("F01 接近高點", "F01c", "接近 120 日高點（前 10%）", "收盤÷近 120 日最高排名前 10%（2026-08 後才可評估）", (v["near120_pct"] >= 0.9) & tv, "R01")
    # F02 短期反轉（Jegadeesh 1990）
    add("F02 短期反轉", "F02a", "近 20 日跌幅最深 10%", "近 20 日報酬排名最低 10%", (v["ret20_pct"] <= 0.1) & tv, "R03")
    add("F02 短期反轉", "F02b", "季線上的近 5 日急跌", "收盤>MA60 且近 5 日報酬 ≤ −5%", up & (v["ret5"] <= -0.05) & tv, "R03")
    add("F02 短期反轉", "F02c", "近 5 日跌幅最深 5%", "近 5 日報酬排名最低 5%", (v["ret5_pct"] <= 0.05) & tv, "R03")
    # F03 低波動（Blitz & van Vliet 2007）
    add("F03 低波動", "F03a", "低波動 10%", "近 60 日日報酬標準差排名最低 10%", (v["vol60_pct"] <= 0.1) & tv, "R04")
    add("F03 低波動", "F03b", "低波動 20%＋站上季線", "波動最低 20%、收盤>MA60", (v["vol60_pct"] <= 0.2) & up & tv, "R04")
    # F04 MAX／樂透股（Bali, Cakici & Whitelaw 2011）：避開近 20 日最大單日漲幅最高者
    add("F04 低 MAX", "F04a", "低 MAX 20%", "近 20 日最大單日報酬排名最低 20%", (v["max20_pct"] <= 0.2) & tv, "R05")
    add("F04 低 MAX", "F04b", "低 MAX 20%＋站上季線", "MAX 最低 20%、收盤>MA60", (v["max20_pct"] <= 0.2) & up & tv, "R05")
    # F05 中期動能（Jegadeesh & Titman 1993 的縮短版：t−60～t−5）
    add("F05 中期動能", "F05a", "60 日動能前 10%（跳過最近 5 日）", "C(t−5)÷C(t−60)−1 排名前 10%", (v["mom60_5_pct"] >= 0.9) & tv, "R06")
    add("F05 中期動能", "F05b", "60 日動能前 20%＋近 5 日回檔", "動能前 20%、近 5 日報酬 < 0", (v["mom60_5_pct"] >= 0.8) & (v["ret5"] < 0) & tv, "R06")
    # F06 多頭中的回檔
    add("F06 多頭回檔", "F06a", "多頭排列中 RSI(2)≤5", "MA20>MA60、收盤>MA60、RSI(2)≤5", (v["ma20"] > v["ma60"]) & up & (v["rsi2"] <= 5) & tv, "R07")
    add("F06 多頭回檔", "F06b", "多頭中回到月線", "MA20>MA60、MA20 上升、收盤在 MA20 的 0～2% 之間、近 5 日報酬<0",
        (v["ma20"] > v["ma60"]) & (v["ma20"] > v["ma20_prev5"]) & (C >= v["ma20"]) & (C <= v["ma20"] * 1.02) & (v["ret5"] < 0) & tv, "R07")
    # F07 營收動能（顧廣平 2010；黃翊閎 2021；TEJ）
    add("F07 營收動能", "F07a", "近 3 月營收年增前 20%", "近 3 個月合計營收年增率排名前 20%", (v["r3_yoy3_pct"] >= 0.8) & tv, "R08")
    add("F07 營收動能", "F07b", "標準化未預期營收前 20%", "SUS 排名前 20%", (v["r3_sus_pct"] >= 0.8) & tv, "R08")
    add("F07 營收動能", "F07c", "營收動能＋價格趨勢", "近 3 月營收年增前 20%、收盤>MA20>MA60", (v["r3_yoy3_pct"] >= 0.8) & (C > v["ma20"]) & (v["ma20"] > v["ma60"]) & tv, "R09")
    # F08 盈餘未預期（Bernard & Thomas 1989；以股價標準化）
    add("F08 盈餘未預期", "F08a", "EPS 年增÷股價 前 20%", "（單季 EPS−去年同季）÷收盤 排名前 20%", (v["esurp_pct"] >= 0.8) & tv, "R10")
    add("F08 盈餘未預期", "F08b", "EPS 未預期前 20%（期限日事件）", "財報期限後第一個交易日、未預期排名前 20%", (v["esurp_pct"] >= 0.8) & v["f3_event_day"] & tv, "R10")
    # F09 盈餘品質（Sloan 1996）
    add("F09 盈餘品質", "F09a", "低應計 20%（淨利>0）", "(淨利−營業現金流)÷營收 排名最低 20%、淨利>0", (v["f3_accrual_pct"] <= 0.2) & (v["f3_ni_pos"] == True) & tv, "R11")
    add("F09 盈餘品質", "F09b", "現金流≥淨利＋站上季線", "營業現金流÷淨利 ≥ 1、淨利>0、收盤>MA60", (v["f3_cfo_ni"] >= 1) & up & tv, "R11")
    # F10 價值與品質＋價值（Asness et al. QMJ 精神；本庫無淨值，只用盈餘殖利率）
    add("F10 價值／品質", "F10a", "盈餘殖利率前 20%", "近 4 季 EPS÷收盤 排名前 20%（EPS>0）", (v["ey_pct"] >= 0.8) & (v["ey"] > 0) & tv, "R12")
    add("F10 價值／品質", "F10b", "品質＋價值綜合前 20%", "盈餘殖利率、低應計、低波動三者百分位平均前 20%", (v["qv_pct"] >= 0.8) & (v["ey"] > 0) & tv, "R13")
    # F11 除權息
    add("F11 除息", "F11a", "除息日當天收盤後買（填息）", "現金股利>0 的除息日（含權息）", v["exd"] & tv, "R14")
    add("F11 除息", "F11b", "除息前 7 日買（前視風險，只作參考）", "除息日前 7 個有效交易日（假設除息日已知）", v["exd_in7"] & tv, "R14")
    out_df = pd.DataFrame({"date": v["date"].to_numpy(), "symbol": v["symbol"].to_numpy(), "ev": ev.to_numpy(),
                           "tv": v["amount_twd"].to_numpy()})
    for k, a in S.items():
        out_df[k] = a
    out_df.attrs["family"] = FAMILY
    pd.to_pickle(dict(df=out_df, family=FAMILY), out)
    print("signals", out_df.shape, {k: int(out_df[k].sum()) for k in S}, f"{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
