"""研究用（非正式程式）：篩選模式 v4——診斷勝率偏低的原因，並測試市場環境濾網、出場規則、持有期、流動性與訊號組合。
沿用 v3 的訊號（signals.pkl）、§11 成本與進場規則（T1 開盤進場、各邊手續費 0.1425%、賣出稅 0.3%、還原價、開盤漲停不進場）。
**注意：只有出場規則 FIX（固定持有 H 日收盤出場）是 docs/indicators.md §11 的正式勝率口徑；其他出場規則的勝率不是 §11 勝率。**
所有參數在看結果前寫死（2026-11-24 19:4x）。樣本切分同 v3：訊號日 ≤ 2026-08-19 為樣本內（IS）。
用法：python a_optimize.py DB ADJ_PANEL SIGNALS_PKL OUTDIR"""
import sys, json, sqlite3, time
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "screen_modes_v3"))
import b_backtest as BB   # noqa: E402  （tick、dedup、block_boot 沿用 v3）

FEE, TAX, MAX_DELAY = 0.001425, 0.003, 20
SPLIT = pd.Timestamp("2026-08-19")
HOLDS = (3, 5, 10, 20)
CANDS = ("BASE", "REF_M1", "REF_M9", "REF_M13", "REF_M17", "REF_M18", "F01c", "F03b", "F04a", "F04b", "F10b", "COMBO2", "COMBO3")
REGIMES = ("none", "taiex_ma20", "taiex_ma60", "tpex_ma20", "breadth50")
RULES = {  # 名稱: (類型, 參數)
    "FIX": ("fix", None),              # §11 正式口徑：第 H 日收盤出場
    "TP5_SL5": ("tpsl", (0.05, 0.05)),
    "TP3_SL3": ("tpsl", (0.03, 0.03)),
    "TP3_SL10": ("tpsl", (0.03, 0.10)),  # 窄停利＋寬停損：預期高勝率、負期望值的反例
    "TP10_SL5": ("tpsl", (0.10, 0.05)),
    "TRAIL5": ("trail", 0.05),         # 收盤較持有期最高收盤回落 5% 出場
    "TRAIL8": ("trail", 0.08),
    "FPC": ("fpc", None),              # 第一個「扣成本後為正」的收盤就出場，否則第 H 日收盤
}
LIQ = {"tv1e8": 1e8, "tv5e8": 5e8, "tv1e9": 1e9}
B, SEED = 2000, 20261124
WIN_ADV, WIN_TOP = 0.70, 0.80


def load(adj, trade_days):
    a = pd.read_csv(adj, dtype={"symbol": str}, usecols=["symbol", "price_date", "status", "open_twd", "adj_open_twd",
                                                         "adj_high_twd", "adj_low_twd", "adj_close_twd"])
    a["date"] = pd.to_datetime(a["price_date"])
    syms = sorted(a["symbol"].unique())
    di = {d: i for i, d in enumerate(trade_days)}; si = {s: j for j, s in enumerate(syms)}
    a = a[a["date"].isin(di)]
    T, S = len(trade_days), len(syms)
    r, c = a["date"].map(di).to_numpy(), a["symbol"].map(si).to_numpy()
    M = {k: np.full((T, S), np.nan) for k in ("ao", "ah", "al", "ac", "ro")}
    for k, col in (("ao", "adj_open_twd"), ("ah", "adj_high_twd"), ("al", "adj_low_twd"), ("ac", "adj_close_twd"), ("ro", "open_twd")):
        M[k][r, c] = a[col].to_numpy()
    valid = np.zeros((T, S), bool)
    ok = (a["status"] == "ok").to_numpy() & a["adj_open_twd"].notna().to_numpy()
    valid[r[ok], c[ok]] = True
    return syms, si, valid, M


def outcomes(valid, M, H, rule):
    """每個 (訊號日, 股票)：扣成本報酬 ret、零成本報酬 gross、旗標 flag（0 ok、1 未進場、2 開盤漲停、3 無法結算）、持有日數 hd。"""
    ao, ah, al, ac, ro = M["ao"], M["ah"], M["al"], M["ac"], M["ro"]
    T, S = valid.shape
    kind, par = RULES[rule]
    nxt = np.full((T, S), T, dtype=np.int64); cur = np.full(S, T, dtype=np.int64)
    for i in range(T - 1, -1, -1):
        cur = np.where(valid[i], i, cur); nxt[i] = cur
    ret = np.full((T, S), np.nan); gross = np.full((T, S), np.nan); flag = np.full((T, S), 3, np.int8); hd = np.full((T, S), np.nan)
    cols = np.arange(S)
    for i in range(T - 1):
        j = i + 1
        e_ok = valid[j]
        with np.errstate(invalid="ignore", divide="ignore"):
            ref = ac[i] * ro[j] / ao[j]
            lim = np.floor(ref * 1.1 / BB.tick(ref * 1.1) + 1e-9) * BB.tick(ref * 1.1)
        lu = e_ok & (ro[j] >= lim - 1e-6)
        k0 = j + H - 1
        k = nxt[k0] if k0 < T else np.full(S, T)
        settle = (k < T) & (k <= k0 + MAX_DELAY)
        kk = np.minimum(k, T - 1)
        entry = ao[j]
        px = ac[kk, cols].copy(); xday = kk.astype(float)
        done = np.zeros(S, bool)
        if kind != "fix":
            peak = entry.copy()
            for d in range(j, min(k0, T - 1) + 1):
                v = valid[d] & ~done
                with np.errstate(invalid="ignore"):
                    if kind == "tpsl":
                        tp, sl = par
                        up, dn = entry * (1 + tp), entry * (1 - sl)
                        o = entry if d == j else ao[d]
                        hs, ht = v & (al[d] <= dn), v & (ah[d] >= up)
                        # 跳空：開盤已越過門檻 → 以開盤價成交；同日兩者都碰到 → 保守假設先停損
                        p = np.where(o <= dn, o, np.where(o >= up, o, np.where(hs, dn, up)))
                        hit = hs | ht
                    elif kind == "trail":
                        peak = np.where(v, np.fmax(peak, ac[d]), peak)
                        hit = v & (ac[d] <= peak * (1 - par)); p = ac[d]
                    else:  # fpc
                        hit = v & (ac[d] * (1 - FEE - TAX) > entry * (1 + FEE)); p = ac[d]
                newly = hit & ~done
                px = np.where(newly, p, px); xday = np.where(newly, d, xday); done |= newly
        ok = e_ok & ~lu & (settle | done)
        with np.errstate(invalid="ignore", divide="ignore"):
            n = px * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
            g = px / entry - 1
        f = np.where(~e_ok, 1, np.where(lu, 2, np.where(ok, 0, 3)))
        flag[i] = f; ret[i] = np.where(f == 0, n, np.nan); gross[i] = np.where(f == 0, g, np.nan)
        hd[i] = np.where(f == 0, xday - j + 1, np.nan)
    return ret, gross, flag, hd


def regimes(db, trade_days, valid, ac):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ix = pd.read_sql_query("select index_id, price_date, close from index_daily where is_current=1 and index_id in ('TAIEX','TPEX')", con)
    con.close()
    ix["date"] = pd.to_datetime(ix["price_date"])
    p = ix.pivot(index="date", columns="index_id", values="close").reindex(trade_days)
    out = pd.DataFrame(index=trade_days)
    for name, col, w in (("taiex_ma20", "TAIEX", 20), ("taiex_ma60", "TAIEX", 60), ("tpex_ma20", "TPEX", 20)):
        ma = p[col].rolling(w, min_periods=w).mean()
        out[name] = (p[col] > ma).where(ma.notna(), False).astype(bool)
    c = np.where(valid, ac, np.nan)
    ma20 = pd.DataFrame(c).rolling(20, min_periods=15).mean().to_numpy()
    with np.errstate(invalid="ignore"):
        above = (c > ma20) & valid & ~np.isnan(ma20)
        has = valid & ~np.isnan(ma20)
    br = above.sum(1) / np.maximum(has.sum(1), 1)
    out["breadth"] = br
    out["breadth50"] = br > 0.5
    out["none"] = True
    out["taiex_close"] = p["TAIEX"].to_numpy(); out["tpex_close"] = p["TPEX"].to_numpy()
    return out


def stats_block(x, H, test):
    r = x["r"].to_numpy(); N, D = len(r), x["ri"].nunique()
    row = dict(N=N, D=D)
    if not N:
        return row
    w = r[r > 0]; l = r[r <= 0]
    row.update(win=(r > 0).mean(), gross_win=(x["g"] > 0).mean(), mean=r.mean(), median=np.median(r),
               avg_win=w.mean() if len(w) else np.nan, avg_loss=l.mean() if len(l) else np.nan,
               excess=(x["r"] - x["bm"]).mean(), base_win=x["bw"].mean(), hold_days=x["hd"].mean(),
               displayable=(N >= 100 and D >= 30))
    row["payoff"] = row["avg_win"] / -row["avg_loss"] if len(w) and len(l) and row["avg_loss"] < 0 else np.nan
    ok = test and N >= 100 and D >= 30 and D >= 3 * max(5, H)
    row["test_ok"] = ok
    if ok:
        dd = x.groupby("ri").agg(s=("r", "sum"), e=("ex", "sum"), w=("win", "sum"), n=("r", "size"))
        lo, hi, p = BB.block_boot(dd["s"].to_numpy(), dd["n"].to_numpy().astype(float), H)
        elo, ehi, ep = BB.block_boot(dd["e"].to_numpy(), dd["n"].to_numpy().astype(float), H)
        # 勝率區間：同一套區塊重抽（勝＝1）
        wlo, whi, _ = BB.block_boot(dd["w"].to_numpy().astype(float) - 0.7 * dd["n"].to_numpy(), dd["n"].to_numpy().astype(float), H)
        row.update(mean_lo=lo, mean_hi=hi, p_mean=p, excess_lo=elo, excess_hi=ehi, p_excess=ep,
                   win_lo=wlo + 0.7, win_hi=whi + 0.7)
    return row


def main():
    db, adj, sig_pkl, out = sys.argv[1:5]
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    first, last = [pd.Timestamp(x) for x in con.execute("select min(price_date), max(price_date) from daily_prices where is_current=1").fetchone()]
    cal = pd.to_datetime([r[0] for r in con.execute("select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    con.close()
    td = [d for d in cal if first <= d <= last]; T = len(td)
    syms, si, valid, M = load(adj, td)
    RG = regimes(db, pd.DatetimeIndex(td), valid, M["ac"])
    P = pd.read_pickle(sig_pkl); sig = P["df"]
    di = {d: i for i, d in enumerate(td)}
    sig["ri"] = sig["date"].map(di).astype(int); sig["ci"] = sig["symbol"].map(si).astype(int)
    sig = sig[sig["ev"]].reset_index(drop=True)
    refs = [c for c in sig.columns if c.startswith("REF_")]
    nm = sig[refs].sum(1)
    sig["BASE"] = True; sig["COMBO2"] = nm >= 2; sig["COMBO3"] = nm >= 3
    win_start = int(sig["ri"].min())
    reg_on = {k: RG[k].to_numpy()[sig["ri"].to_numpy()] for k in REGIMES}
    dates = np.array(td, dtype="datetime64[ns]")
    RG.assign(date=[d.date() for d in td]).to_csv(out / "v4_regime_daily.csv", index=False, float_format="%.6g")
    print("prep", f"{time.time()-t0:.0f}s", flush=True)
    rows, diag_reg, diag_dist = [], [], []
    for H in HOLDS:
        win_end = T - 1 - H
        inwin = ((sig["ri"] >= win_start) & (sig["ri"] <= win_end)).to_numpy()
        OUT = {r: outcomes(valid, M, H, r) for r in RULES}
        print("H", H, "outcomes", f"{time.time()-t0:.0f}s", flush=True)
        ri, ci, tv = sig["ri"].to_numpy(), sig["ci"].to_numpy(), sig["tv"].to_numpy()
        # 流動性基準（每條出場規則各自）：可評估、成交金額≥1 億、窗內、可結算，不去重
        base = {}
        bmask = inwin & (tv >= 1e8)
        for r in RULES:
            R = OUT[r][0][ri, ci]
            b = pd.DataFrame({"ri": ri[bmask], "r": R[bmask]}).dropna()
            base[r] = b.groupby("ri")["r"].agg(bm="mean", bw=lambda s: (s > 0).mean())
        F = OUT["FIX"][2][ri, ci]
        for cand in CANDS:
            cm = sig[cand].to_numpy() & inwin
            combos = [(rg, "tv1e8") for rg in REGIMES] + [("none", "tv5e8"), ("none", "tv1e9")]
            for rg, liq in combos:
                m = cm & reg_on[rg] & (tv >= LIQ[liq])
                rr, cc = ri[m], ci[m]
                keep = BB.dedup(rr, cc, H) & (F[m] == 0)
                er, ec = rr[keep], cc[keep]
                for rule in RULES:
                    ret, gross, flag, hd = OUT[rule]
                    e = pd.DataFrame({"ri": er, "r": ret[er, ec], "g": gross[er, ec], "hd": hd[er, ec]})
                    e = e[np.isfinite(e["r"])]
                    e = e.join(base[rule], on="ri")
                    e["ex"] = e["r"] - e["bm"]; e["win"] = (e["r"] > 0).astype(int)
                    isd = dates[e["ri"].to_numpy()] <= np.datetime64(SPLIT) if len(e) else np.zeros(0, bool)
                    for samp, sel in (("ALL", np.ones(len(e), bool)), ("IS", isd), ("OOS", ~isd)):
                        x = e[sel]
                        row = dict(cand=cand, regime=rg, liq=liq, rule=rule, H=H, sample=samp,
                                   official=(rule == "FIX"), days_on=int(np.unique(sig["ri"].to_numpy()[inwin & reg_on[rg]]).size))
                        row.update(stats_block(x, H, test=(samp == "OOS")))
                        rows.append(row)
                    if rule == "FIX" and rg == "none" and liq == "tv1e8" and H in (5,):
                        allr = e.copy()
                        for k in ("taiex_ma20", "taiex_ma60", "tpex_ma20", "breadth50"):
                            on = RG[k].to_numpy()[allr["ri"].to_numpy()]
                            for state, s_ in (("上方", on), ("下方", ~on)):
                                y = allr[s_]
                                diag_reg.append(dict(cand=cand, H=H, regime=k, state=state, N=len(y), D=y["ri"].nunique(),
                                                     win=(y["r"] > 0).mean() if len(y) else np.nan,
                                                     gross_win=(y["g"] > 0).mean() if len(y) else np.nan,
                                                     mean=y["r"].mean() if len(y) else np.nan, base_win=y["bw"].mean() if len(y) else np.nan))
                    if rule == "FIX" and rg == "none" and liq == "tv1e8":
                        r_ = e["r"].to_numpy()
                        if len(r_):
                            pos = np.sort(r_[r_ > 0])[::-1]
                            top = pos[:max(1, int(np.ceil(0.1 * len(r_))))] if len(pos) else np.array([])
                            diag_dist.append(dict(cand=cand, H=H, N=len(r_), win=(r_ > 0).mean(), gross_win=(e["g"] > 0).mean(),
                                                  win_if_zero_cost=(e["g"] > 0).mean(), cost_drag_pp=((e["g"] > 0).mean() - (r_ > 0).mean()) * 100,
                                                  share_in_cost_band=((e["g"] > 0) & (r_ <= 0)).mean(),
                                                  mean=r_.mean(), median=np.median(r_), std=r_.std(), skew=pd.Series(r_).skew(),
                                                  avg_win=r_[r_ > 0].mean() if (r_ > 0).any() else np.nan,
                                                  avg_loss=r_[r_ <= 0].mean() if (r_ <= 0).any() else np.nan,
                                                  p10=np.percentile(r_, 10), p90=np.percentile(r_, 90),
                                                  top10pct_trades_share_of_gains=top.sum() / pos.sum() if len(pos) else np.nan,
                                                  is_win=(e.loc[dates[e["ri"].to_numpy()] <= np.datetime64(SPLIT), "r"] > 0).mean(),
                                                  oos_win=(e.loc[dates[e["ri"].to_numpy()] > np.datetime64(SPLIT), "r"] > 0).mean()))
            print(" ", H, cand, f"{time.time()-t0:.0f}s", flush=True)
        del OUT
    st = pd.DataFrame(rows)
    # 多重比較：所有可檢定的樣本外結果一起做 BH（期望值>0、超額>0 各一個家族）
    for col, qn in (("p_mean", "q_mean"), ("p_excess", "q_excess")):
        o = st[(st["sample"] == "OOS") & st[col].notna()]
        m = len(o)
        srt = o[col].sort_values()
        q = (srt * m / np.arange(1, m + 1))[::-1].cummin()[::-1].clip(upper=1)
        st.loc[q.index, qn] = q.to_numpy()
        st[f"n_tests_{qn}"] = m
    st["qualifies_adv"] = (st["sample"] == "OOS") & (st["displayable"] == True) & (st["win"] >= WIN_ADV) & (st["mean"] > 0)
    st["high_win_neg_exp"] = (st["displayable"] == True) & (st["win"] >= 0.6) & (st["mean"] <= 0)
    st.to_csv(out / "v4_stats.csv", index=False, float_format="%.6g")
    pd.DataFrame(diag_reg).to_csv(out / "v4_diag_regime.csv", index=False, float_format="%.6g")
    pd.DataFrame(diag_dist).to_csv(out / "v4_diag_dist.csv", index=False, float_format="%.6g")
    # 前推：每個候選，只用樣本內（可顯示）選設定，再看樣本外
    wf = []
    key = ["cand", "regime", "liq", "rule", "H"]
    isd = st[(st["sample"] == "IS") & (st["displayable"] == True)]
    oos = st[st["sample"] == "OOS"].set_index(key)
    for cand, g in isd.groupby("cand"):
        for crit, gg in (("最高期望值", g.sort_values("mean", ascending=False)),
                         ("最高勝率（期望值>0）", g[g["mean"] > 0].sort_values("win", ascending=False)),
                         ("最高勝率（§11 FIX）", g[g["rule"] == "FIX"].sort_values("win", ascending=False))):
            if not len(gg):
                wf.append(dict(cand=cand, criterion=crit, note="樣本內無符合者")); continue
            b = gg.iloc[0]; k = tuple(b[c] for c in key)
            o = oos.loc[k] if k in oos.index else None
            d = dict(criterion=crit, **{c: b[c] for c in key}, is_N=b["N"], is_D=b["D"], is_win=b["win"], is_mean=b["mean"])
            if o is not None:
                d.update(oos_N=o["N"], oos_D=o["D"], oos_win=o.get("win"), oos_mean=o.get("mean"), oos_mean_lo=o.get("mean_lo"),
                         oos_mean_hi=o.get("mean_hi"), oos_q_mean=o.get("q_mean"), oos_excess=o.get("excess"),
                         oos_q_excess=o.get("q_excess"), oos_displayable=o.get("displayable"))
            wf.append(d)
    pd.DataFrame(wf).to_csv(out / "v4_walkforward.csv", index=False, float_format="%.6g")
    meta = dict(trade_days=[str(td[0].date()), str(td[-1].date())], signal_start=str(td[win_start].date()), split=str(SPLIT.date()),
                holds=HOLDS, rules={k: [v[0], v[1]] for k, v in RULES.items()}, regimes=REGIMES, cands=CANDS, liq=LIQ,
                n_variants=int(st[st["sample"] == "OOS"].shape[0]),
                n_tests_mean=int(st["n_tests_q_mean"].iloc[0]), n_tests_excess=int(st["n_tests_q_excess"].iloc[0]),
                regime_days={k: dict(IS=int(RG.loc[(RG.index >= td[win_start]) & (RG.index <= SPLIT), k].sum()),
                                     OOS=int(RG.loc[RG.index > SPLIT, k].sum()),
                                     IS_total=int(((RG.index >= td[win_start]) & (RG.index <= SPLIT)).sum()),
                                     OOS_total=int((RG.index > SPLIT).sum())) for k in REGIMES})
    (out / "v4_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", len(st), f"{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
