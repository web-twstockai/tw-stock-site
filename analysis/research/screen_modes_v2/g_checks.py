"""研究用（非正式程式）：兩項檢查。
(1) 指標交叉核對：本研究自算的 MA20／MA60／前20日最高／量比，與 analysis screen() 同日輸出比對
    （比較比例：收盤/MA20、收盤/MA60、收盤>前20日最高、量比，前復權基準不同不影響比例）。
(2) 多重比較：以訊號日為叢集的 bootstrap（B=20000、固定種子），給每個模式的
    「勝率－同日基準」與「勝率－同日流動性基準」在 Bonferroni 校正（α=0.05／模式數，雙尾）下的區間。
用法：python g_checks.py DB ADJ_PANEL PANEL_DIR OUTDIR
"""
import sys, glob
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_features as CF


def main():
    db, adj, pdir, out = sys.argv[1:5]; out = Path(out)
    g = CF.load_grid(adj, pdir)
    v = CF.tech_features(g)
    files = sorted(glob.glob(f"{pdir}/screen_*.csv.gz"))
    sc = pd.concat([pd.read_csv(f, dtype={"symbol": str}) for f in files], ignore_index=True)
    sc["date"] = pd.to_datetime(sc["price_date"])
    sc = sc[sc["status"].isin(["PASS", "FAIL"])]
    m = sc.merge(v[["symbol", "date", "adj_close_twd", "ma20", "ma60", "hh20_prior", "avgv20_prior", "adj_volume_shares"]],
                 on=["symbol", "date"], how="left")
    r1 = (m["adj_close_twd"] / m["ma20"]) / (m["close_twd"] / m["ma_short"]) - 1
    r2 = (m["adj_close_twd"] / m["ma60"]) / (m["close_twd"] / m["ma_long"]) - 1
    vr = m["adj_volume_shares"] / m["avgv20_prior"]
    r3 = vr / m["volume_ratio"] - 1
    bo = (m["adj_close_twd"] > m["hh20_prior"]) == m["pass_breakout"].astype(bool)
    lines = [f"rows={len(m)} missing_feature={(m['ma20'].isna()).sum()}",
             f"close/MA20 rel diff: max={r1.abs().max():.2e} >1e-6: {(r1.abs() > 1e-6).sum()}",
             f"close/MA60 rel diff: max={r2.abs().max():.2e} >1e-6: {(r2.abs() > 1e-6).sum()}",
             f"volume ratio rel diff: max={r3.abs().max():.2e} >1e-6: {(r3.abs() > 1e-6).sum()}",
             f"breakout agree: {bo.mean():.6f} disagree={(~bo).sum()}"]
    # (2) Bonferroni
    st = pd.read_csv(out / "mode_stats.csv")
    ev = pd.read_csv(out / "mode_events.csv.gz", dtype={"symbol": str}, parse_dates=["date"])
    ev = ev[ev["fflag"] == "ok"]
    bday = pd.read_csv(out / "baseline_daily.csv", parse_dates=["date"]).set_index("date")
    # 流動性基準逐日（重算：可評估且成交金額≥1億）
    k = len([x for x in st["mode"] if not str(x).startswith("BASE")])
    alpha = 0.05 / k
    rng = np.random.default_rng(20261124)
    res = []
    g2 = g.copy()
    for mid, e in ev.groupby("mode"):
        dd = e.groupby("date").agg(w=("fret", lambda s: (s > 0).sum()), n=("fret", "size"))
        if dd["n"].sum() < 100 or len(dd) < 30:
            continue
        bb = bday.reindex(dd.index)
        D = len(dd); idx = rng.integers(0, D, size=(20000, D))
        w = dd["w"].to_numpy(float)[idx].sum(1) / dd["n"].to_numpy(float)[idx].sum(1)
        bw = bb["wins"].to_numpy(float)[idx].sum(1) / bb["n_ok"].to_numpy(float)[idx].sum(1)
        lo, hi = np.percentile(w - bw, [100 * alpha / 2, 100 * (1 - alpha / 2)])
        res.append(dict(mode=mid, bonf_alpha=alpha, edge_bonf_lo=lo, edge_bonf_hi=hi))
    pd.DataFrame(res).to_csv(out / "mode_bonferroni.csv", index=False)
    lines.append(f"bonferroni k={k} alpha={alpha:.5f}; modes with lower>0: "
                 + ", ".join(r["mode"] for r in res if r["edge_bonf_lo"] > 0))
    (out / "checks.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
