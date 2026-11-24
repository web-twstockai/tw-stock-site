"""研究腳本與正式模組（analysis/modes.py，analysis-0.10.0）在抽樣日的 PASS 名單比對。
用法：
  python j_equality.py research DB ADJ_PANEL PANEL_DIR OUT_CSV DAY [DAY ...]   # 第 1 步：研究流程（c_features＋e_modes）
  python j_equality.py compare  DB RESEARCH_CSV OUT_TXT ADJ_PANEL              # 第 2 步：正式模組逐日重算並比對
兩步分開跑（各自一個行程，省記憶體）。差異逐筆分類：
  邊界／歷史長度：正式模組以評估日為基準復權，研究以 11-23 為基準一次復權（評估日之後才出現的無法解析事件會讓研究
    把該日標成 unavailable、沒有特徵）；或正式模組要求的有效日數較嚴。
  營收／財報回退：研究在缺最新月份／季度時沿用較舊資料，正式模組依規則記資料不足（REV_*／FIN_*）。
  排名母體：橫斷面排名的母體因上述原因相差幾檔，邊緣名次跨過門檻。
  其他：未能解釋（目標 0）。"""
import sys, time
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "analysis"))
PAIRS = [("C28", "M9"), ("C29", "M10"), ("C31", "M11"), ("C24", "M12"), ("C05", "M13"), ("C01", "M14"), ("C09", "M15"),
         ("C13", "M16"), ("C17", "M17"), ("M6s", "M18"), ("C35", "M19"), ("C26", "M20"), ("C06", "M21")]
RANK_COL = {"M12": "ret5_pct", "M14": "rs60_pct", "M19": "rs60_pct"}


def research(db, adj, pdir, out, days):
    import c_features as CF, e_modes as EM, sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trade_all = pd.to_datetime([r[0] for r in con.execute("select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    con.close()
    g = CF.load_grid(adj, pdir)
    v = CF.tech_features(g); del g
    v = CF.cross_section(v)
    rv = CF.revenue_features(db, trade_all.values.astype("datetime64[ns]"))
    v = CF.asof_join(v, rv, ["rev_high", "yoy3_pos", "yoy3_ge20", "yoy_accel", "cum_yoy"], "rv_")
    fn = CF.eps_features(db, trade_all.values.astype("datetime64[ns]"))
    v = CF.asof_join(v, fn, ["eps_ok", "eps_yoy_pos", "eps_yoy_ge25", "gm_up"], "fn_")
    M = EM.modes(v)
    ev = v["scr_status"].isin(["PASS", "FAIL"])
    sel = v["date"].isin(pd.to_datetime(days))
    rows = []
    for rid, mid in PAIRS:
        c = M[rid]["cond"] & ev & sel
        for i in v.index[c]:
            rows.append(dict(day=v.at[i, "date"].strftime("%Y-%m-%d"), mode_id=mid, research_id=rid, symbol=v.at[i, "symbol"]))
    keep = v.loc[sel & ev, ["date", "symbol", "rs60_pct", "ret5_pct"]].copy()
    keep["day"] = keep["date"].dt.strftime("%Y-%m-%d")
    pd.DataFrame(rows).to_csv(out, index=False)
    keep.drop(columns="date").to_csv(str(out).replace(".csv", "_ranks.csv"), index=False)
    print("research pass rows", len(rows))


def compare(db, rcsv, out_txt, adj):
    import io_sqlite as IO, modes as MD
    rp = pd.read_csv(rcsv, dtype={"symbol": str})
    rk = pd.read_csv(str(rcsv).replace(".csv", "_ranks.csv"), dtype={"symbol": str}).set_index(["day", "symbol"])
    days = sorted(rp["day"].unique()) if len(rp) else []
    days = sorted(set(days) | set(rk.index.get_level_values(0)))
    ad = pd.read_csv(adj, dtype={"symbol": str}, usecols=["symbol", "price_date", "status"])
    ad = ad[ad["price_date"].isin(days)].set_index(["price_date", "symbol"])["status"].to_dict()
    lines = [f"# 研究腳本 vs analysis/modes.py（{MD.ANALYSIS_VERSION}）PASS 名單比對", f"抽樣日：{', '.join(days)}", ""]
    tot = dict(same=0, research_only=0, prod_only=0, unexplained=0)
    lines.append("| 日期 | 模式 | 研究 | 正式 | 相同 | 只在研究 | 只在正式 | 差異分類 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    detail = []
    for d in days:
        t = time.time()
        mi = IO.load_mode_inputs(db, as_of=d)
        r = MD.evaluate_modes(**mi.mode_kwargs())
        S = r.summary.set_index(["mode_id", "symbol"])
        for rid, mid in PAIRS:
            a = set(rp.loc[(rp["day"] == d) & (rp["mode_id"] == mid), "symbol"])
            b = set(r.selected(mid)["symbol"])
            cats = {}
            for s in sorted(a ^ b):
                st, code = (S.loc[(mid, s), "status"], S.loc[(mid, s), "exclusion_code"]) if (mid, s) in S.index else ("NA", None)
                if st == "EXCLUDED" and code and (code.startswith("REV_") or code.startswith("FIN_")):
                    cat = "營收／財報回退"
                elif st == "EXCLUDED" and code in ("EXCL_ADJ_FACTOR_MISSING", "EXCL_UNEXPLAINED_GAP", "EXCL_INSUFFICIENT_HISTORY",
                                                    "EXCL_NEW_LISTING", "EXCL_ADJ_COVERAGE_GAP", "EXCL_ADJ_EVENT_UNRESOLVED"):
                    cat = "邊界／歷史長度"
                elif mid in RANK_COL:
                    cat = "排名母體"
                else:
                    cat = "其他"
                if ad.get((d, s)) != "ok":   # 研究端該股-日在 11-23 基準復權的可用範圍外（沒有特徵）
                    cat = "邊界／歷史長度"
                if cat == "其他":
                    tot["unexplained"] += 1
                cats[cat] = cats.get(cat, 0) + 1
                detail.append(f"{d} {mid} {s} {'只在研究' if s in a else '只在正式'} 正式={st}/{code} → {cat}")
            tot["same"] += len(a & b); tot["research_only"] += len(a - b); tot["prod_only"] += len(b - a)
            lines.append(f"| {d} | {mid}（{rid}） | {len(a)} | {len(b)} | {len(a & b)} | {len(a - b)} | {len(b - a)} | "
                         f"{'、'.join(f'{k} {v}' for k, v in cats.items()) or '—'} |")
        print(d, f"{time.time() - t:.0f}s", flush=True)
        del r, mi
    lines += ["", "分類說明：邊界／歷史長度＝研究以 2026-11-23 為基準一次復權，評估日之後才出現的無法解析事件讓研究端該股-日沒有特徵"
              "（docs/rd/SCREEN_MODES_v2.md 第 6 節第 6 點），正式模組以評估日為基準復權所以可評估；排名母體＝因前者母體相差幾檔，"
              "橫斷面排名邊緣跨過門檻；營收／財報回退＝研究沿用舊月份／季度、正式模組記資料不足。", f"合計：相同 {tot['same']}、只在研究 {tot['research_only']}、只在正式 {tot['prod_only']}、未能解釋 {tot['unexplained']}", "",
              "## 差異明細", ""] + [f"- {x}" for x in detail]
    Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:4 + 13 * len(days) + 3]))


if __name__ == "__main__":
    if sys.argv[1] == "research":
        research(*sys.argv[2:6], sys.argv[6:])
    else:
        compare(*sys.argv[2:6])
