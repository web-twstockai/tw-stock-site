"""研究用（非正式程式）：執行全部候選模式的 5 日歷史統計，輸出 CSV。
用法：python e_run.py DB ADJ_PANEL PANEL_DIR OUTDIR
"""
import sys, json, sqlite3, time
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_features as CF
import d_backtest as BT
import e_modes as EM

MIN_N, MIN_D = 100, 30


def main():
    db, adj, pdir, out = sys.argv[1:5]
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trade_all = pd.to_datetime([r[0] for r in con.execute(
        "select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    first, last = [pd.Timestamp(x) for x in con.execute("select min(price_date), max(price_date) from daily_prices where is_current=1").fetchone()]
    con.close()
    trade_days = np.array(trade_all[(trade_all >= first) & (trade_all <= last)], dtype="datetime64[ns]")
    g = CF.load_grid(adj, pdir)
    print("grid", len(g), f"{time.time()-t0:.0f}s", flush=True)
    syms, ret, gross, flag = BT.forward_returns(g, list(pd.to_datetime(trade_days)))
    print("fwd done", f"{time.time()-t0:.0f}s", flush=True)
    v = CF.tech_features(g)
    del g
    v = CF.cross_section(v)
    rv = CF.revenue_features(db, trade_all.values.astype("datetime64[ns]"))
    v = CF.asof_join(v, rv, ["rev_high", "yoy3_pos", "yoy3_ge20", "yoy_accel", "cum_yoy"], "rv_")
    fn = CF.eps_features(db, trade_all.values.astype("datetime64[ns]"))
    v = CF.asof_join(v, fn, ["eps_ok", "eps_yoy_pos", "eps_yoy_ge25", "gm_up"], "fn_")
    print("features", v.shape, f"{time.time()-t0:.0f}s", flush=True)

    tdl = list(pd.to_datetime(trade_days))
    di = {d: i for i, d in enumerate(tdl)}; si = {s: j for j, s in enumerate(syms)}
    v["ri"] = v["date"].map(di).astype(int); v["ci"] = v["symbol"].map(si).astype(int)
    ev = v["scr_status"].isin(["PASS", "FAIL"]).to_numpy()
    # 統計窗口：screen 有結果的第一天 ～ 能在資料內結算的最後訊號日（T5＝最後一日 → S＝倒數第 6 個交易日）
    scr_days = sorted(v.loc[v["scr_status"].notna(), "date"].unique())
    win_start = pd.Timestamp(scr_days[0]); win_end = tdl[len(tdl) - 1 - BT.HOLD]
    in_win = ((v["date"] >= win_start) & (v["date"] <= win_end)).to_numpy()
    R = ret[v["ri"], v["ci"]]; G = gross[v["ri"], v["ci"]]; F = flag[v["ri"], v["ci"]]
    v["fret"], v["fgross"], v["fflag"] = R, G, F
    tv1 = (v["amount_twd"] >= 1e8).to_numpy()

    # ---- 基準 ----
    base = v[ev & in_win]
    bday = base.groupby("date").agg(n_eval=("symbol", "size"),
                                    n_ok=("fflag", lambda s: (s == "ok").sum()),
                                    wins=("fret", lambda s: (s > 0).sum()),
                                    sum_ret=("fret", "sum"))
    liq = v[ev & in_win & tv1]
    lday = liq.groupby("date").agg(n_ok=("fflag", lambda s: (s == "ok").sum()), wins=("fret", lambda s: (s > 0).sum()),
                                   sum_ret=("fret", "sum"))
    bday.to_csv(out / "baseline_daily.csv")
    bok = base[base["fflag"] == "ok"]
    lok = liq[liq["fflag"] == "ok"]
    b_stats = dict(mode="BASE", name="基準：所有可評估股票每天", N=len(bok), D=bok["date"].nunique(),
                   win=(bok["fret"] > 0).mean(), mean=bok["fret"].mean(), median=bok["fret"].median(), std=bok["fret"].std(),
                   gross_win=(bok["fgross"] > 0).mean())
    l_stats = dict(mode="BASE_LIQ", name="流動性基準：可評估且成交金額≥1億", N=len(lok), D=lok["date"].nunique(),
                   win=(lok["fret"] > 0).mean(), mean=lok["fret"].mean(), median=lok["fret"].median(), std=lok["fret"].std(),
                   gross_win=(lok["fgross"] > 0).mean())
    for st, ok in ((b_stats, bok), (l_stats, lok)):
        m = ok.groupby(ok["date"].dt.to_period("M"))["fret"].mean()
        st.update(months=len(m), pos_month_pct=(m > 0).mean(), worst_month=m.min(), worst_month_id=str(m.idxmin()),
                  avg_matches=ok.groupby("date").size().mean())
    print("baseline", b_stats, l_stats, flush=True)

    mid_split = tdl[(tdl.index(win_start) + tdl.index(win_end)) // 2]
    M = EM.modes(v)
    rows, monthly_rows, daily_counts = [], [], {}
    all_events = []
    for mid, md in M.items():
        ws = max(win_start, tdl[min(md["warm"] - 1, len(tdl) - 1)])
        mask = md["cond"].to_numpy() & ev & in_win & (v["date"] >= ws).to_numpy()
        sig = v.loc[mask, ["symbol", "date", "ri", "ci", "fret", "fgross", "fflag", "amount_twd"]].sort_values(["ci", "ri"])
        days_in = [d for d in tdl if ws <= d <= win_end]
        cnt = sig.groupby("date").size().reindex(days_in, fill_value=0)
        daily_counts[mid] = cnt
        keep = BT.dedup(sig["ri"].to_numpy(), sig["ci"].to_numpy())
        e = sig[keep]
        okk = e[e["fflag"] == "ok"]
        N = len(okk); D = okk["date"].nunique()
        r = dict(mode=mid, name=md["name"], src=md["src"], window=f"{ws.date()}～{win_end.date()}", window_days=len(days_in),
                 signals_raw=len(sig), events_dedup=len(e), N=N, D=D,
                 no_entry=(e["fflag"] == "no_entry").sum(), limit_up=(e["fflag"] == "limit_up").sum(),
                 unsettled=(e["fflag"] == "unsettled").sum(),
                 avg_matches=cnt.mean(), median_matches=cnt.median(), zero_day_pct=(cnt == 0).mean(), max_matches=cnt.max())
        if N:
            r.update(win=(okk["fret"] > 0).mean(), gross_win=(okk["fgross"] > 0).mean(), mean=okk["fret"].mean(),
                     median=okk["fret"].median(), std=okk["fret"].std())
            m = okk.groupby(okk["date"].dt.to_period("M"))["fret"].agg(["mean", "size"])
            for p, row in m.iterrows():
                monthly_rows.append(dict(mode=mid, month=str(p), n=int(row["size"]), mean_ret=row["mean"]))
            r.update(months=len(m), pos_month_pct=(m["mean"] > 0).mean(), worst_month=m["mean"].min(), worst_month_id=str(m["mean"].idxmin()))
            wk = okk.groupby(okk["date"].dt.to_period("W"))["fret"].mean()
            r.update(weeks=len(wk), pos_week_pct=(wk > 0).mean())
            # 同日基準
            Sd = okk["date"].unique()
            bsub = bday.loc[bday.index.isin(Sd)]
            r.update(sameday_base_win=bsub["wins"].sum() / bsub["n_ok"].sum(),
                     sameday_base_mean=bsub["sum_ret"].sum() / bsub["n_ok"].sum())
            lsub = lday.loc[lday.index.isin(Sd)]
            r.update(sameday_liq_win=lsub["wins"].sum() / lsub["n_ok"].sum(), sameday_liq_mean=lsub["sum_ret"].sum() / lsub["n_ok"].sum())
            r["edge_win"] = r["win"] - r["sameday_base_win"]; r["edge_mean"] = r["mean"] - r["sameday_base_mean"]
            r["edge_win_liq"] = r["win"] - r["sameday_liq_win"]; r["edge_mean_liq"] = r["mean"] - r["sameday_liq_mean"]
            dd = okk.groupby("date").agg(w=("fret", lambda s: (s > 0).sum()), n=("fret", "size"))
            bb = bday.reindex(dd.index); ll = lday.reindex(dd.index)
            lo, hi, elo, ehi = BT.boot(dd["w"].to_numpy(float), dd["n"].to_numpy(float), bb["wins"].to_numpy(float), bb["n_ok"].to_numpy(float))
            _, _, llo, lhi = BT.boot(dd["w"].to_numpy(float), dd["n"].to_numpy(float), ll["wins"].to_numpy(float), ll["n_ok"].to_numpy(float))
            r.update(win_ci_lo=lo, win_ci_hi=hi, edge_ci_lo=elo, edge_ci_hi=ehi, edge_liq_ci_lo=llo, edge_liq_ci_hi=lhi)
            for h, part in (("h1", okk[okk["date"] <= mid_split]), ("h2", okk[okk["date"] > mid_split])):
                if len(part):
                    bs = bday.loc[bday.index.isin(part["date"].unique())]
                    r[f"{h}_N"] = len(part); r[f"{h}_D"] = part["date"].nunique()
                    r[f"{h}_win"] = (part["fret"] > 0).mean(); r[f"{h}_edge"] = r[f"{h}_win"] - bs["wins"].sum() / bs["n_ok"].sum()
        r["show"] = "顯示" if (N >= MIN_N and D >= MIN_D) else "樣本不足"
        r["desc"] = md["desc"]
        rows.append(r)
        e2 = e.assign(mode=mid)[["mode", "symbol", "date", "fflag", "fret", "fgross"]]
        all_events.append(e2)
        print(mid, md["name"], r["show"], N, D, round(r.get("win", float("nan")), 4), round(r.get("edge_win", float("nan")), 4),
              round(r["avg_matches"], 1), flush=True)
    st = pd.DataFrame([b_stats, l_stats] + rows)
    st.to_csv(out / "mode_stats.csv", index=False)
    pd.DataFrame(monthly_rows).to_csv(out / "mode_monthly.csv", index=False)
    pd.DataFrame(daily_counts).to_csv(out / "mode_daily_counts.csv")
    ev_df = pd.concat(all_events, ignore_index=True)
    ev_df["date"] = ev_df["date"].dt.strftime("%Y-%m-%d")
    ev_df.to_csv(out / "mode_events.csv.gz", index=False, compression="gzip")
    meta = dict(db_snapshot=db, win_start=str(win_start.date()), win_end=str(win_end.date()), mid_split=str(mid_split.date()),
                price_first=str(first.date()), price_last=str(last.date()), bootstrap_B=BT.B, seed=BT.SEED,
                fee=BT.FEE, tax=BT.TAX, hold=BT.HOLD, max_delay=BT.MAX_DELAY, min_N=MIN_N, min_D=MIN_D,
                runtime_s=round(time.time() - t0))
    (out / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print("done", meta, flush=True)


if __name__ == "__main__":
    main()
