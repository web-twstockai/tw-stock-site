"""研究用（非正式程式）：v3 多持有期歷史統計、樣本內／樣本外、多重比較、等權投組最大回撤。
成本模型同 docs/indicators.md §11（T1 開盤進場、各邊手續費 0.1425%、賣出證交稅 0.3%、還原價、開盤漲停不進場、
出場日無成交最多順延 20 日）。持有期 H＝5、10、20、60 個交易日（T1 起第 H 個交易日收盤出場）。
同一檔在持有期 H 內的重複訊號只算第一個。基準＝同一訊號日所有可評估股票（等權，不去重）；另列流動性基準（成交金額≥1億）。
樣本切分（事先決定）：訊號日 ≤ 2026-08-19 為樣本內（IS，選變體用），之後為樣本外（OOS，不動）。
區間：以訊號日為單位的移動區塊 bootstrap（區塊長度＝max(5,H) 個訊號日，B＝2000，種子 20261124），
p 值＝單尾 P(超額≤0)。多重比較：所有 (變體×H×流動性) 的 OOS 檢定一起做 Bonferroni 與 Benjamini–Hochberg（FDR 5%）。
用法：python b_backtest.py DB ADJ_PANEL SIGNALS_PKL OUTDIR"""
import sys, json, sqlite3, time
from pathlib import Path
import numpy as np
import pandas as pd

FEE, TAX, MAX_DELAY = 0.001425, 0.003, 20
HOLDS = (5, 10, 20, 60)
SPLIT = pd.Timestamp("2026-08-19")
B, SEED = 2000, 20261124
LIQ = {"none": 0.0, "tv1e8": 1e8, "tv5e8": 5e8}   # "none" 只給參照模式做與 v2 的一致性核對，不列入多重比較
WIN_ADV, WIN_TOP = 0.70, 0.80   # H AO 2026-11-24 18:20：進階 ≥70%、高級 ≥80%（5 日勝率，§11）


def tick(p):
    return np.select([p < 10, p < 50, p < 100, p < 500, p < 1000], [0.01, 0.05, 0.1, 0.5, 1.0], 5.0)


def load_mats(adj, trade_days):
    a = pd.read_csv(adj, dtype={"symbol": str}, usecols=["symbol", "price_date", "status", "open_twd", "adj_open_twd",
                                                         "adj_close_twd"])
    a["date"] = pd.to_datetime(a["price_date"])
    syms = sorted(a["symbol"].unique())
    di = {d: i for i, d in enumerate(trade_days)}; si = {s: j for j, s in enumerate(syms)}
    a = a[a["date"].isin(di)]
    T, S = len(trade_days), len(syms)
    r, c = a["date"].map(di).to_numpy(), a["symbol"].map(si).to_numpy()
    valid = np.zeros((T, S), bool); ao = np.full((T, S), np.nan); ac = np.full((T, S), np.nan); ro = np.full((T, S), np.nan)
    ok = (a["status"] == "ok").to_numpy() & a["adj_open_twd"].notna().to_numpy()
    valid[r[ok], c[ok]] = True
    ao[r, c] = a["adj_open_twd"].to_numpy(); ac[r, c] = a["adj_close_twd"].to_numpy(); ro[r, c] = a["open_twd"].to_numpy()
    return syms, si, valid, ao, ac, ro


def forward(valid, ao, ac, ro, H):
    """每個 (訊號日 i, 股票) 的扣成本報酬、出場日索引、旗標（ok／no_entry／limit_up／unsettled）。"""
    T, S = valid.shape
    nxt = np.full((T, S), T, dtype=np.int64); cur = np.full(S, T, dtype=np.int64)
    for i in range(T - 1, -1, -1):
        cur = np.where(valid[i], i, cur); nxt[i] = cur
    ret = np.full((T, S), np.nan); kx = np.full((T, S), -1, dtype=np.int64); flag = np.zeros((T, S), np.int8)  # 0 ok
    cols = np.arange(S)
    for i in range(T):
        j = i + 1
        if j >= T:
            flag[i] = 3; continue
        e_ok = valid[j]
        with np.errstate(invalid="ignore", divide="ignore"):
            ref = ac[i] * ro[j] / ao[j]
            lim = np.floor(ref * 1.1 / tick(ref * 1.1) + 1e-9) * tick(ref * 1.1)
        lu = e_ok & (ro[j] >= lim - 1e-6)
        k0 = j + H - 1
        k = nxt[k0] if k0 < T else np.full(S, T)
        settle = (k < T) & (k <= k0 + MAX_DELAY)
        kk = np.minimum(k, T - 1)
        n = ac[kk, cols] * (1 - FEE - TAX) / (ao[j] * (1 + FEE)) - 1
        f = np.where(~e_ok, 1, np.where(lu, 2, np.where(~settle, 3, 0)))
        flag[i] = f; ret[i] = np.where(f == 0, n, np.nan); kx[i] = np.where(f == 0, kk, -1)
    return ret, kx, flag


def dedup(rows, cols, H):
    order = np.lexsort((rows, cols)); keep = np.zeros(len(rows), bool); last = {}
    for n in order:
        r, c = rows[n], cols[n]; lr = last.get(c)
        if lr is None or r >= lr + H:
            keep[n] = True; last[c] = r
    return keep


def block_boot(day_ex, day_n, H, b=B, seed=SEED):
    """day_ex：每訊號日「事件超額報酬合計」；day_n：事件數。回傳 (lo, hi, p_one_sided)。"""
    D = len(day_ex)
    if D < 2:
        return np.nan, np.nan, np.nan
    L = max(5, H) if D > max(5, H) else max(1, D // 2)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(D / L))
    starts = rng.integers(0, D - L + 1, size=(b, nb))
    idx = (starts[:, :, None] + np.arange(L)[None, None, :]).reshape(b, -1)[:, :D]
    est = day_ex[idx].sum(1) / np.maximum(day_n[idx].sum(1), 1)
    lo, hi = np.percentile(est, [2.5, 97.5])
    p = float((est <= 0).mean())
    return lo, hi, p


def mdd(curve):
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1).min()) if len(curve) else np.nan


def portfolio(rows, cols, kx_sel, ao, ac, valid, T):
    """等權投組（每日在持有中的部位間等權、每日再平衡）：T1 開盤買（含手續費）、持有期間收盤對收盤、出場日收盤扣手續費＋稅。
    回傳每日報酬（無部位的日子＝0）。"""
    S = ao.shape[1]
    acf = pd.DataFrame(ac).ffill().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        cc = acf / np.vstack([np.full((1, S), np.nan), acf[:-1]]) - 1
    cc = np.nan_to_num(cc)
    num = np.zeros(T); den = np.zeros(T)
    for r, c, k in zip(rows, cols, kx_sel):
        j = r + 1
        seg = np.empty(k - j + 1)
        seg[0] = ac[j, c] / (ao[j, c] * (1 + FEE)) - 1 if valid[j, c] else 0.0
        if k > j:
            seg[1:] = cc[j + 1:k + 1, c]
        seg[-1] = (1 + seg[-1]) * (1 - FEE - TAX) - 1
        num[j:k + 1] += seg; den[j:k + 1] += 1
    return np.where(den > 0, num / np.maximum(den, 1), 0.0)


def main():
    db, adj, sig_pkl, out = sys.argv[1:5]
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    first, last = [pd.Timestamp(x) for x in con.execute("select min(price_date), max(price_date) from daily_prices where is_current=1").fetchone()]
    cal = pd.to_datetime([r[0] for r in con.execute("select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    con.close()
    trade_days = [d for d in cal if first <= d <= last]
    T = len(trade_days)
    syms, si, valid, ao, ac, ro = load_mats(adj, trade_days)
    P = pd.read_pickle(sig_pkl); sig, fam = P["df"], P["family"]
    di = {d: i for i, d in enumerate(trade_days)}
    sig["ri"] = sig["date"].map(di).astype(int); sig["ci"] = sig["symbol"].map(si).astype(int)
    sig = sig[sig["ev"]].reset_index(drop=True)      # 只有可評估股-日能發訊號
    scr_days = sorted(sig["date"].unique()); win_start = di[pd.Timestamp(scr_days[0])]
    vids = list(fam)
    # 市場等權日報酬（所有有效成交股）與其最大回撤
    acf = pd.DataFrame(ac).ffill().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        cc = np.where(valid & np.vstack([np.zeros((1, ac.shape[1]), bool), valid[:-1]]), ac / np.vstack([np.full((1, ac.shape[1]), np.nan), ac[:-1]]) - 1, np.nan)
    mkt = np.nanmean(cc, axis=1)
    rows_out, port_rows, monthly_rows = [], [], []
    for H in HOLDS:
        ret, kx, flag = forward(valid, ao, ac, ro, H)
        win_end = T - 1 - H        # 最後一個能在資料內結算（不含順延）的訊號日
        R = ret[sig["ri"], sig["ci"]]; K = kx[sig["ri"], sig["ci"]]; F = flag[sig["ri"], sig["ci"]]
        inwin = (sig["ri"] >= win_start) & (sig["ri"] <= win_end)
        okm = (F == 0) & inwin.to_numpy()
        # 同日基準（全部可評估、流動性）
        bdf = pd.DataFrame({"ri": sig["ri"], "r": R, "ok": okm, "liq": sig["tv"] >= 1e8})
        b_all = bdf[bdf["ok"]].groupby("ri")["r"].agg(["mean", lambda s: (s > 0).mean()]); b_all.columns = ["bm", "bw"]
        b_liq = bdf[bdf["ok"] & bdf["liq"]].groupby("ri")["r"].agg(["mean", lambda s: (s > 0).mean()]); b_liq.columns = ["lm", "lw"]
        mkt_curve = np.cumprod(1 + np.nan_to_num(mkt[win_start:win_end + H + 1]))
        for liq, thr in LIQ.items():
            for vid in vids:
                if liq == "none" and not vid.startswith("REF_"):
                    continue
                # §11：先對「所有訊號」去重（未進場／漲停／未結算的訊號也佔住持有期），再只留可結算者
                m = sig[vid].to_numpy() & inwin.to_numpy() & (sig["tv"].to_numpy() >= thr)
                rr, cc_ = sig["ri"].to_numpy()[m], sig["ci"].to_numpy()[m]
                keep = dedup(rr, cc_, H) & (F[m] == 0)
                e = pd.DataFrame({"ri": rr[keep], "ci": cc_[keep], "r": R[m][keep], "k": K[m][keep]})
                e = e.join(b_all, on="ri").join(b_liq, on="ri")
                e["date"] = pd.to_datetime(pd.Series([trade_days[i] for i in e["ri"]], index=e.index, dtype="datetime64[ns]"))
                e["ex"] = e["r"] - e["bm"]; e["exl"] = e["r"] - e["lm"]
                for samp, sel in (("ALL", np.ones(len(e), bool)), ("IS", (e["date"] <= SPLIT).to_numpy()),
                                  ("OOS", (e["date"] > SPLIT).to_numpy())):
                    x = e[sel]
                    N, D = len(x), x["ri"].nunique()
                    row = dict(variant=vid, family=fam[vid][0], name=fam[vid][1], H=H, liq=liq, sample=samp, N=N, D=D)
                    if N:
                        dd = x.groupby("ri").agg(s=("ex", "sum"), n=("ex", "size"))
                        # 只有「可顯示（N≥100、D≥30）且訊號日數 ≥ 3 個區塊（D ≥ 3×max(5,H)）」才做推論；
                        # 2026-11-24 第一次執行發現小樣本（D<區塊長度）的 bootstrap 退化成 p＝0，故加此條件（見 v3 §5.4 揭露）。
                        test_ok = (N >= 100) and (D >= 30) and (D >= 3 * max(5, H))
                        lo, hi, p = block_boot(dd["s"].to_numpy(), dd["n"].to_numpy().astype(float), H) if test_ok else (np.nan,) * 3
                        mon = x.groupby(x["date"].dt.to_period("M")).agg(r=("r", "mean"), ex=("ex", "mean"))
                        row.update(test_ok=test_ok, win=(x["r"] > 0).mean(), base_win=x["bw"].mean(), mean=x["r"].mean(), median=x["r"].median(),
                                   base_mean=x["bm"].mean(), excess=x["ex"].mean(), excess_liq=x["exl"].mean(),
                                   excess_lo=lo, excess_hi=hi, p_excess=p, months=len(mon),
                                   pos_month=(mon["r"] > 0).mean(), pos_ex_month=(mon["ex"] > 0).mean(),
                                   avg_per_day=N / max(D, 1), displayable=(N >= 100 and D >= 30))
                        if samp == "ALL":
                            for per, a in mon.iterrows():
                                monthly_rows.append(dict(variant=vid, H=H, liq=liq, month=str(per), mean=a["r"], excess=a["ex"]))
                    rows_out.append(row)
                if liq == "tv1e8" and len(e):
                    pr = portfolio(e["ri"].to_numpy(), e["ci"].to_numpy(), e["k"].to_numpy(), ao, ac, valid, T)
                    seg = pr[win_start:win_end + H + 1]
                    curve = np.cumprod(1 + seg)
                    port_rows.append(dict(variant=vid, H=H, port_total=curve[-1] - 1, port_mdd=mdd(curve),
                                          mkt_total=mkt_curve[-1] - 1, mkt_mdd=mdd(mkt_curve),
                                          days=len(seg), active_days=int((seg != 0).sum())))
        print("H", H, f"{time.time()-t0:.0f}s", flush=True)
    st = pd.DataFrame(rows_out)
    po = pd.DataFrame(port_rows)
    st = st.merge(po, on=["variant", "H"], how="left")
    st.loc[st["liq"] != "tv1e8", ["port_total", "port_mdd", "mkt_total", "mkt_mdd"]] = np.nan
    # 多重比較：OOS 全部檢定
    o = st[(st["sample"] == "OOS") & st["p_excess"].notna() & (st["liq"] != "none")].copy()
    m = len(o)
    o["p_bonf"] = np.minimum(o["p_excess"] * m, 1)
    rk = o["p_excess"].rank(method="first")
    o_sorted = o.sort_values("p_excess")
    q = (o_sorted["p_excess"] * m / np.arange(1, m + 1))[::-1].cummin()[::-1]
    o.loc[o_sorted.index, "q_bh"] = np.minimum(q.to_numpy(), 1)
    st = st.merge(o[["variant", "H", "liq", "sample", "p_bonf", "q_bh"]], on=["variant", "H", "liq", "sample"], how="left")
    st["n_tests_oos"] = m
    st.to_csv(out / "v3_stats.csv", index=False, float_format="%.6g")
    pd.DataFrame(monthly_rows).to_csv(out / "v3_monthly.csv", index=False, float_format="%.6g")
    # 變體選擇（只用 IS）：每個家族、每個 H，IS 超額最高者（需 IS 可顯示）→ 報告其 OOS
    sel = []
    for (f_, H), g in st[(st["sample"] == "IS") & (st["liq"] == "tv1e8") & (~st["family"].str.startswith("參照"))].groupby(["family", "H"]):
        g = g[g["displayable"] == True]
        if not len(g):
            sel.append(dict(family=f_, H=H, chosen=None, reason="樣本內無可顯示變體（N<100 或 D<30）")); continue
        best = g.sort_values("excess", ascending=False).iloc[0]
        oos = st[(st["variant"] == best["variant"]) & (st["H"] == H) & (st["liq"] == "tv1e8") & (st["sample"] == "OOS")].iloc[0]
        sel.append(dict(family=f_, H=H, chosen=best["variant"], is_excess=best["excess"], is_win=best["win"],
                        oos_N=oos["N"], oos_D=oos["D"], oos_win=oos.get("win"), oos_base_win=oos.get("base_win"),
                        oos_excess=oos.get("excess"), oos_lo=oos.get("excess_lo"), oos_hi=oos.get("excess_hi"),
                        oos_p=oos.get("p_excess"), oos_q_bh=oos.get("q_bh"), oos_displayable=oos.get("displayable")))
    pd.DataFrame(sel).to_csv(out / "v3_walkforward.csv", index=False, float_format="%.6g")
    meta = dict(trade_days=[str(trade_days[0].date()), str(trade_days[-1].date())], n_trade_days=T,
                signal_window_start=str(trade_days[win_start].date()), split=str(SPLIT.date()),
                holds=HOLDS, fee=FEE, tax=TAX, B=B, seed=SEED, n_variants=len(vids), n_tests_oos=m,
                win_thresholds=dict(進階=WIN_ADV, 高級=WIN_TOP),
                window_end={H: str(trade_days[T - 1 - H].date()) for H in HOLDS})
    (out / "v3_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", f"{time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
