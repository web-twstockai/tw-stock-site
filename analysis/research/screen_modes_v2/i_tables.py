"""研究用：把 mode_stats.csv 等轉成 Markdown 表（數字直接來自 CSV，避免手抄）。
用法：python i_tables.py OUTDIR > tables.md"""
import sys
from pathlib import Path
import pandas as pd

out = Path(sys.argv[1])
s = pd.read_csv(out / "mode_stats.csv").merge(pd.read_csv(out / "mode_bonferroni.csv"), on="mode", how="left")
P = lambda x: "—" if pd.isna(x) else f"{x*100:.1f}%"
PP = lambda x: "—" if pd.isna(x) else f"{x*100:+.1f}"
R = lambda x: "—" if pd.isna(x) else f"{x*100:+.2f}%"
F = lambda x, d=1: "—" if pd.isna(x) else f"{x:.{d}f}"
print("| 代號 | 名稱 | 統計窗口 | N | D | 勝率（扣成本） | 毛勝率 | 平均 | 中位數 | 標準差 | 正報酬月份 | 最差月（平均） | 每日平均／中位檔數 | 零檔日 | 同日基準勝率 | 差（百分點）[95%] | 差 vs 流動性基準 [95%] | 前半／後半差 | 顯示 |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for _, r in s.iterrows():
    if str(r["mode"]).startswith("BASE"):
        print(f"| {r['mode']} | {r['name']} | 2026-05-27～2026-11-16 | {int(r['N'])} | {int(r['D'])} | {P(r['win'])} | {P(r['gross_win'])} | {R(r['mean'])} | {R(r['median'])} | {F(r['std']*100,2)}% | {int(round(r['pos_month_pct']*r['months']))}/{int(r['months'])} | {R(r['worst_month'])}（{r['worst_month_id']}） | {F(r['avg_matches'])} | — | — | — | — | — | — |")
        continue
    win = P(r["win"]) if r["show"] == "顯示" else f"樣本不足（{P(r['win'])}，僅供研究）"
    pm = "—" if pd.isna(r.get("months")) else f"{int(round(r['pos_month_pct']*r['months']))}/{int(r['months'])}"
    wm = "—" if pd.isna(r.get("worst_month")) else f"{R(r['worst_month'])}（{r['worst_month_id']}）"
    print(f"| {r['mode']} | {r['name']} | {r['window']} | {int(r['N'])} | {int(r['D'])} | {win} | {P(r.get('gross_win'))} | {R(r.get('mean'))} | {R(r.get('median'))} | {F(r.get('std', float('nan'))*100,2)}% | {pm} | {wm} | {F(r['avg_matches'])}／{F(r['median_matches'],0)} | {P(r['zero_day_pct'])} | {P(r.get('sameday_base_win'))} | {PP(r.get('edge_win'))} [{PP(r.get('edge_ci_lo'))}, {PP(r.get('edge_ci_hi'))}] | {PP(r.get('edge_win_liq'))} [{PP(r.get('edge_liq_ci_lo'))}, {PP(r.get('edge_liq_ci_hi'))}] | {PP(r.get('h1_edge'))}／{PP(r.get('h2_edge'))} | {r['show']} |")
