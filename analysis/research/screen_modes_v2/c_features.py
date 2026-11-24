"""研究用（非正式程式）：由還原 OHLC 面板（b_adjusted_panel 輸出）＋ screen 面板（a_screen_panel 輸出）
＋ 月營收（analysis/revenue.evaluate_revenue）＋ 財報（單季 EPS 推算）建立每檔每日特徵表。

視窗一律以「有效交易日」計（只用 status=ok 的列，跳過停牌／無成交日），與 indicators.md 第 0 節相同。
只用 ≤ t 的資料（rolling 都是往回看；「前 N 日」用 shift(1) 不含當日）。
月營收可得日＝次月 10 日（法定期限，§11.0）；財報可得日＝5/15、8/14、11/14、次年 3/31。
"""
import sys, sqlite3, glob
from pathlib import Path
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "analysis"))
import revenue as RV


def load_grid(adj_path, panel_dir):
    a = pd.read_csv(adj_path, dtype={"symbol": str})
    a["date"] = pd.to_datetime(a["price_date"])
    files = sorted(glob.glob(f"{panel_dir}/screen_*.csv.gz"))
    sc = pd.concat([pd.read_csv(f, dtype={"symbol": str}, usecols=["symbol", "market", "price_date", "status", "exclusion_code"])
                    for f in files], ignore_index=True)
    sc["date"] = pd.to_datetime(sc["price_date"])
    sc = sc.rename(columns={"status": "scr_status"})[["symbol", "date", "market", "scr_status", "exclusion_code"]]
    g = a.merge(sc, on=["symbol", "date"], how="left")
    g = g.sort_values(["symbol", "date"]).reset_index(drop=True)
    return g


def _kd(rsv):
    k = np.empty(len(rsv)); d = np.empty(len(rsv)); pk = pdd = 50.0
    for i, r in enumerate(rsv):
        if np.isnan(r):
            k[i] = d[i] = np.nan; continue
        pk = pk * 2 / 3 + r / 3; pdd = pdd * 2 / 3 + pk / 3
        k[i], d[i] = pk, pdd
    return k, d


def tech_features(g):
    """g：全部格（含無效日）。回傳只含有效日的特徵表（symbol, date ＋ 特徵）。"""
    v = g[g["status"] == "ok"].copy()
    v = v.sort_values(["symbol", "date"]).reset_index(drop=True)
    C, H, L, O, V = v["adj_close_twd"], v["adj_high_twd"], v["adj_low_twd"], v["adj_open_twd"], v["adj_volume_shares"]
    gb = lambda s: s.groupby(v["symbol"], sort=False)
    v["n_valid"] = gb(C).cumcount() + 1
    for n in (5, 10, 20, 60, 120):
        v[f"ma{n}"] = gb(C).transform(lambda x: x.rolling(n, min_periods=n).mean())
    v["ma20_prev5"] = gb(v["ma20"]).shift(5)
    v["ma60_prev10"] = gb(v["ma60"]).shift(10)
    v["ma120_prev20"] = gb(v["ma120"]).shift(20)
    v["ma20_prev"] = gb(v["ma20"]).shift(1); v["ma60_prev"] = gb(v["ma60"]).shift(1)
    v["ma5_prev"] = gb(v["ma5"]).shift(1)
    v["c_prev"] = gb(C).shift(1); v["o_prev"] = gb(O).shift(1); v["h_prev"] = gb(H).shift(1); v["l_prev"] = gb(L).shift(1)
    v["v_prev"] = gb(V).shift(1)
    for n in (20, 60, 120):
        v[f"hh{n}_prior"] = gb(H).transform(lambda x: x.shift(1).rolling(n, min_periods=n).max())
        v[f"ll{n}_prior"] = gb(L).transform(lambda x: x.shift(1).rolling(n, min_periods=n).min())
    v["hh120_incl"] = gb(H).transform(lambda x: x.rolling(120, min_periods=120).max())
    v["ll120_incl"] = gb(L).transform(lambda x: x.rolling(120, min_periods=120).min())
    v["avgv20_prior"] = gb(V).transform(lambda x: x.shift(1).rolling(20, min_periods=20).mean())
    v["avgv50_prior"] = gb(V).transform(lambda x: x.shift(1).rolling(50, min_periods=50).mean())
    v["avgv5_prior"] = gb(V).transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())
    v["ret1"] = C / v["c_prev"] - 1
    v["ret5"] = C / gb(C).shift(5) - 1
    v["ret60"] = C / gb(C).shift(60) - 1
    v["vol60"] = gb(v["ret1"]).transform(lambda x: x.rolling(60, min_periods=60).std())
    # 布林 20,2：頻寬＝(上−下)/中；前一日頻寬在前 60 日的百分位
    sd20 = gb(C).transform(lambda x: x.rolling(20, min_periods=20).std(ddof=0))
    v["bb_up"] = v["ma20"] + 2 * sd20
    v["bbw"] = 4 * sd20 / v["ma20"]
    v["bbw_prev"] = gb(v["bbw"]).shift(1)
    v["bbw_prev_min60"] = gb(v["bbw"]).transform(lambda x: x.shift(1).rolling(60, min_periods=60).min())
    v["bbw_prev_max60"] = gb(v["bbw"]).transform(lambda x: x.shift(1).rolling(60, min_periods=60).max())
    # 均線糾結（5/10/20）
    mx = v[["ma5", "ma10", "ma20"]].max(axis=1); mn = v[["ma5", "ma10", "ma20"]].min(axis=1)
    v["ma_max3"] = mx; v["ma_spread"] = mx / mn - 1
    v["ma_spread_prev"] = gb(v["ma_spread"]).shift(1)
    v["ma_spread_prev_max5"] = gb(v["ma_spread"]).transform(lambda x: x.shift(1).rolling(5, min_periods=5).max())
    # 區間（量縮整理）
    v["range20_prior"] = v["hh20_prior"] / v["ll20_prior"] - 1
    # VCP 三段區間（t-60..t-41, t-40..t-21, t-20..t-1）
    for k, sh in ((1, 41), (2, 21), (3, 1)):
        hh = gb(H).transform(lambda x, sh=sh: x.shift(sh).rolling(20, min_periods=20).max())
        ll = gb(L).transform(lambda x, sh=sh: x.shift(sh).rolling(20, min_periods=20).min())
        v[f"seg{k}_range"] = hh / ll - 1
    v["avgv10_prior"] = gb(V).transform(lambda x: x.shift(1).rolling(10, min_periods=10).mean())
    # RSI（Wilder）
    def rsi(x, n):
        d = x.diff(); up = d.clip(lower=0); dn = (-d).clip(lower=0)
        au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(); ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        return 100 - 100 / (1 + au / ad.replace(0, np.nan))
    v["rsi2"] = gb(C).transform(lambda x: rsi(x, 2))
    v["rsi14"] = gb(C).transform(lambda x: rsi(x, 14))
    v["rsi14_prev"] = gb(v["rsi14"]).shift(1)
    # MACD 12/26/9（需 ≥ 35 個有效日才採用）
    e12 = gb(C).transform(lambda x: x.ewm(span=12, adjust=False).mean())
    e26 = gb(C).transform(lambda x: x.ewm(span=26, adjust=False).mean())
    v["dif"] = e12 - e26
    v["dem"] = gb(v["dif"]).transform(lambda x: x.ewm(span=9, adjust=False).mean())
    v.loc[v["n_valid"] < 35, ["dif", "dem"]] = np.nan
    v["dif_prev"] = gb(v["dif"]).shift(1); v["dem_prev"] = gb(v["dem"]).shift(1)
    # KD(9,3,3) 台灣常用：K＝2/3 前K＋1/3 RSV，D 同理，起始 50
    hh9 = gb(H).transform(lambda x: x.rolling(9, min_periods=9).max()); ll9 = gb(L).transform(lambda x: x.rolling(9, min_periods=9).min())
    rsv = ((C - ll9) / (hh9 - ll9).replace(0, np.nan) * 100)
    ks, ds = [], []
    for _, idx in v.groupby("symbol", sort=False).indices.items():
        k, d = _kd(rsv.to_numpy()[idx]); ks.append(pd.Series(k, index=v.index[idx])); ds.append(pd.Series(d, index=v.index[idx]))
    v["k9"] = pd.concat(ks); v["d9"] = pd.concat(ds)
    v["k9_prev"] = gb(v["k9"]).shift(1); v["d9_prev"] = gb(v["d9"]).shift(1)
    # ADX(14) Wilder
    upm = H - v["h_prev"]; dnm = v["l_prev"] - L
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0); mdm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    tr = pd.concat([H - L, (H - v["c_prev"]).abs(), (L - v["c_prev"]).abs()], axis=1).max(axis=1)
    w = lambda s: gb(pd.Series(s, index=v.index)).transform(lambda x: x.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    atr = w(tr); pdi = 100 * w(pdm) / atr; mdi = 100 * w(mdm) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    v["adx"] = gb(dx).transform(lambda x: x.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    v.loc[v["n_valid"] < 30, "adx"] = np.nan
    v["pdi"], v["mdi"] = pdi, mdi
    v["pdi_prev"] = gb(v["pdi"]).shift(1); v["mdi_prev"] = gb(v["mdi"]).shift(1)
    # OBV
    sgn = np.sign(v["ret1"]).fillna(0)
    v["obv"] = gb(sgn * V).cumsum()
    v["obv_hh60_prior"] = gb(v["obv"]).transform(lambda x: x.shift(1).rolling(60, min_periods=60).max())
    # NR7（前一日）：前一日振幅小於再前 6 日每一日
    rng = H - L
    v["rng"] = rng
    v["rng_prev"] = gb(rng).shift(1)
    v["rng_prev6_min"] = gb(rng).transform(lambda x: x.shift(2).rolling(6, min_periods=6).min())
    # Pocket pivot：前 10 日下跌日的最大量
    downv = V.where(v["ret1"] < 0, 0.0)
    v["max_downv10_prior"] = gb(downv).transform(lambda x: x.shift(1).rolling(10, min_periods=10).max())
    # 連三紅量增
    up = (v["ret1"] > 0) & (C > O)
    v["up3"] = up & gb(up).shift(1).fillna(False).astype(bool) & gb(up).shift(2).fillna(False).astype(bool)
    vinc = V > v["v_prev"]
    v["vinc2"] = vinc & gb(vinc).shift(1).fillna(False).astype(bool)
    # Darvas：前 20 日最高價出現的位置距今 ≥ 3 日，且其後的最低價也已 3 日未破
    def darvas(h, l):
        h = h.to_numpy(); l = l.to_numpy(); n = len(h)
        top = np.full(n, np.nan); bot = np.full(n, np.nan)
        for i in range(21, n):
            wh = h[i - 20:i]
            if np.isnan(wh).any():
                continue
            j = i - 20 + int(np.argmax(wh))          # 最高價日（最早者）
            if i - j < 4:                            # 最高日之後至少 3 日未再創高（j+1..j+3 都在 i 之前）
                continue
            wl = l[j:i]
            m = j + int(np.argmin(wl))
            if i - m < 4:                            # 低點之後至少 3 日未再破
                continue
            top[i] = h[j]; bot[i] = l[m]
        return pd.Series(top), pd.Series(bot)
    tops, bots = [], []
    for _, idx in v.groupby("symbol", sort=False).indices.items():
        t_, b_ = darvas(H.iloc[idx].reset_index(drop=True), L.iloc[idx].reset_index(drop=True))
        t_.index = v.index[idx]; b_.index = v.index[idx]; tops.append(t_); bots.append(b_)
    v["dv_top"] = pd.concat(tops); v["dv_bot"] = pd.concat(bots)
    return v


def cross_section(v):
    """同日橫斷面排名（只在 screen 可評估的股票之間排名）。"""
    ev = v["scr_status"].isin(["PASS", "FAIL"])
    v["rs60_pct"] = np.nan; v["vol60_pct"] = np.nan; v["ret5_pct"] = np.nan
    sub = v[ev]
    v.loc[ev, "rs60_pct"] = sub.groupby("date")["ret60"].rank(pct=True)
    v.loc[ev, "vol60_pct"] = sub.groupby("date")["vol60"].rank(pct=True)
    v.loc[ev, "ret5_pct"] = sub.groupby("date")["ret5"].rank(pct=True)
    return v


def _next_trade_on_or_after(trade_days, d):
    i = np.searchsorted(trade_days, np.datetime64(d))
    return trade_days[i] if i < len(trade_days) else None


def revenue_features(db, trade_days):
    """每檔每個可得日 → 最新可得月份的營收狀態。回傳 (symbol, avail_date, 欄位…) 供 asof 合併；
    以及事件版（S＝期限當日或之後第一個交易日）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rv = pd.read_sql_query("select symbol, market, revenue_month, revenue_twd, revenue_prev_year_twd, yoy_pct, "
                           "cumulative_twd, cumulative_prev_year_twd, source, fetched_at, is_current from monthly_revenues where is_current=1", con)
    con.close()
    ev = RV.evaluate_revenue(rv)
    ev = ev[["symbol", "revenue_month", "rev_12m_high", "yoy_pct", "yoy_status"]].copy()
    ev = ev.merge(rv[["symbol", "revenue_month", "cumulative_twd", "cumulative_prev_year_twd"]], on=["symbol", "revenue_month"], how="left")
    ev = ev.sort_values(["symbol", "revenue_month"]).reset_index(drop=True)
    g = ev.groupby("symbol", sort=False)
    ym = pd.PeriodIndex(ev["revenue_month"], freq="M")
    ev["_p"] = ym.astype("int64")
    contig1 = g["_p"].diff() == 1
    y = pd.to_numeric(ev["yoy_pct"], errors="coerce").where(ev["yoy_status"] != "not_applicable")
    y1 = g["yoy_pct"].shift(1); y2 = g["yoy_pct"].shift(2)
    y1 = pd.to_numeric(y1, errors="coerce"); y2 = pd.to_numeric(y2, errors="coerce")
    c2 = contig1 & g["_p"].diff(2).eq(2)
    ev["yoy3_pos"] = (y > 0) & (y1 > 0) & (y2 > 0) & c2
    ev["yoy3_ge20"] = (y >= 20) & (y1 >= 20) & (y2 >= 20) & c2
    ev["yoy_accel"] = (y > y1) & (y1 > y2) & (y > 0) & c2
    cum, cump = ev["cumulative_twd"], ev["cumulative_prev_year_twd"]
    ev["cum_yoy"] = np.where((cump > 0) & cum.notna(), (cum / cump - 1) * 100, np.nan)
    ev["rev_high"] = ev["rev_12m_high"].astype(object).map(lambda x: x is True or x == 1 or x == "True")
    ev["yoy"] = y
    # 可得日＝次月 10 日
    nxt = (ym + 1).to_timestamp()
    ev["deadline"] = nxt + pd.Timedelta(days=9)
    ev["avail_date"] = [_next_trade_on_or_after(trade_days, d) for d in ev["deadline"]]
    ev = ev.dropna(subset=["avail_date"])
    ev["avail_date"] = pd.to_datetime(ev["avail_date"])
    return ev[["symbol", "revenue_month", "avail_date", "rev_high", "yoy", "yoy3_pos", "yoy3_ge20", "yoy_accel", "cum_yoy"]]


def eps_features(db, trade_days):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    f = pd.read_sql_query("select symbol, fiscal_year y, fiscal_quarter q, eps_twd eps, revenue_twd rev, gross_profit_twd gp "
                          "from income_statements where is_current=1 and period_type='累計' and report_basis='合併'", con)
    con.close()
    f = f.sort_values(["symbol", "y", "q"]).reset_index(drop=True)
    key = f.set_index(["symbol", "y", "q"])
    prev = key.reindex(pd.MultiIndex.from_arrays([f["symbol"], f["y"], f["q"] - 1]))
    for c in ("eps", "rev", "gp"):
        pv = prev[c].to_numpy()
        f[f"{c}_q"] = np.where(f["q"] == 1, f[c], f[c].to_numpy() - pv)   # 缺上季累計 → NaN
    k2 = f.set_index(["symbol", "y", "q"])
    ly = k2.reindex(pd.MultiIndex.from_arrays([f["symbol"], f["y"] - 1, f["q"]]))
    f["eps_q_ly"] = ly["eps_q"].to_numpy()
    f["gm_q"] = f["gp_q"] / f["rev_q"]; f["gm_q_ly"] = ly["gp_q"].to_numpy() / ly["rev_q"].to_numpy()
    f["eps_yoy_pos"] = (f["eps_q"] > 0) & (f["eps_q"] > f["eps_q_ly"])
    f["eps_yoy_ge25"] = (f["eps_q"] > 0) & (f["eps_q_ly"] > 0) & (f["eps_q"] >= 1.25 * f["eps_q_ly"])
    f["gm_up"] = (f["gm_q"] > f["gm_q_ly"]) & (f["rev_q"] > 0)
    f["eps_ok"] = f["eps_q"].notna() & f["eps_q_ly"].notna()
    dl = {1: (5, 15), 2: (8, 14), 3: (11, 14)}
    f["deadline"] = [pd.Timestamp(y + 1, 3, 31) if q == 4 else pd.Timestamp(y, *dl[q]) for y, q in zip(f["y"], f["q"])]
    f["avail_date"] = pd.to_datetime([_next_trade_on_or_after(trade_days, d) for d in f["deadline"]])
    f = f.dropna(subset=["avail_date"])
    return f[["symbol", "y", "q", "avail_date", "eps_q", "eps_q_ly", "eps_ok", "eps_yoy_pos", "eps_yoy_ge25", "gm_up"]]


def asof_join(v, fx, cols, prefix):
    """v（每檔每日）以 avail_date ≤ date 取最新一筆；另加「今天是否為該筆的可得日」旗標（事件版）。"""
    fx = fx.copy(); fx["avail_date"] = pd.to_datetime(fx["avail_date"]).astype("datetime64[ns]")
    fx = fx.sort_values("avail_date")
    left = v[["symbol", "date"]].reset_index()
    left["date"] = left["date"].astype("datetime64[ns]")
    left = left.sort_values("date")
    m = pd.merge_asof(left, fx[["symbol", "avail_date", *cols]], left_on="date", right_on="avail_date", by="symbol",
                      direction="backward")
    m = m.set_index("index").sort_index()
    for c in cols:
        v[f"{prefix}{c}"] = m[c]
    v[f"{prefix}event_day"] = (m["avail_date"] == m["date"]).fillna(False)
    return v
