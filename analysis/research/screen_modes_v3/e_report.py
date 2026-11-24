"""研究用：把 v3 CSV 轉成 SCREEN_MODES_v3.md 用的表格（避免手抄數字）。輸出到 stdout。"""
import sys
from pathlib import Path
import pandas as pd

d = Path(sys.argv[1])
s = pd.read_csv(d / "v3_stats.csv")
prim = s[((s.variant.str.startswith("REF_")) & (s.liq == "none")) | ((~s.variant.str.startswith("REF_")) & (s.liq == "tv1e8"))]
pc = lambda x: "—" if pd.isna(x) else f"{x*100:.1f}%"
pp = lambda x: "—" if pd.isna(x) else f"{x*100:+.2f}"
nd = lambda r: "0／0" if r is None else f"{int(r['N'])}／{int(r['D'])}"
port = s[(s.liq == "tv1e8") & (s["sample"] == "ALL")].set_index(["variant", "H"])


def get(g, samp):
    x = g[g["sample"] == samp]
    return None if not len(x) or x["N"].iloc[0] == 0 else x.iloc[0]


def ci(r):
    if r is None or pd.isna(r.get("excess_lo")):
        return "不檢定"
    return f"[{pp(r['excess_lo'])}, {pp(r['excess_hi'])}]"


def q(r):
    return "—" if r is None or pd.isna(r.get("q_bh")) else f"{r['q_bh']:.3f}"


for H in (5, 10, 20, 60):
    print(f"\n#### H＝{H}\n")
    print("| 變體 | 名稱 | 全期 N／D | 全期勝率 | 樣本內 N／D | 樣本內勝率 | 樣本內超額 | 樣本外 N／D | 樣本外勝率（同日基準） | 樣本外中位數 | 樣本外超額［95%］ | q(BH) | 月正報酬比例 | 月正超額比例 | 投組 MDD | 判定 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v, g in prim[prim.H == H].groupby("variant", sort=False):
        a, i, o = get(g, "ALL"), get(g, "IS"), get(g, "OOS")
        name = g["name"].iloc[0]
        try:
            mdd = pc(port.loc[(v, H), "port_mdd"])
        except KeyError:
            mdd = "—"
        if H == 5:
            disp = o is not None and bool(o["displayable"])
            verdict = "樣本外不足" if not disp else ("高級" if o["win"] >= .8 else "進階" if o["win"] >= .7 else "未達")
        else:
            verdict = "只報告"
        flag = "（只有樣本外）" if i is None and o is not None else ""
        print(f"| {v}{flag} | {name} | {nd(a)} | {pc(a['win']) if a is not None else '—'} | {nd(i)} | {pc(i['win']) if i is not None else '—'} | "
              f"{pp(i['excess']) if i is not None else '—'} | {nd(o)} | {pc(o['win']) + '（' + pc(o['base_win']) + '）' if o is not None else '—'} | "
              f"{pp(o['median']) if o is not None else '—'} | {(pp(o['excess']) + ' ' + ci(o)) if o is not None else '—'} | {q(o)} | "
              f"{pc(a['pos_month']) if a is not None else '—'} | {pc(a['pos_ex_month']) if a is not None else '—'} | {mdd} | {verdict} |")
print("\n#### 多重比較後 q(BH) ≤ 0.10 的樣本外檢定（全部流動性口徑）\n")
o = s[(s["sample"] == "OOS") & (s.q_bh <= 0.10)].sort_values(["q_bh", "p_excess"])
print("| 變體 | H | 流動性 | N／D | 勝率（同日基準） | 中位數 | 超額［95%］ | p | q(BH) | Bonferroni p |")
print("|---|---|---|---|---|---|---|---|---|---|")
for _, r in o.iterrows():
    print(f"| {r['variant']} | {r['H']} | {r['liq']} | {int(r['N'])}／{int(r['D'])} | {pc(r['win'])}（{pc(r['base_win'])}） | {pp(r['median'])} | {pp(r['excess'])} {ci(r)} | {r['p_excess']:.4f} | {r['q_bh']:.3f} | {r['p_bonf']:.3f} |")
print("\n#### 前推選擇（每家族只用樣本內選 1 個變體，再看樣本外；tv1e8）\n")
w = pd.read_csv(d / "v3_walkforward.csv")
print("| 家族 | H | 樣本內選中 | 樣本內勝率 | 樣本內超額 | 樣本外 N／D | 樣本外勝率（基準） | 樣本外超額［95%］ | q(BH) |")
print("|---|---|---|---|---|---|---|---|---|")
for _, r in w.iterrows():
    if pd.isna(r["chosen"]):
        print(f"| {r['family']} | {r['H']} | — | — | — | — | — | {r['reason']} | — |"); continue
    c = "不檢定" if pd.isna(r["oos_lo"]) else f"[{pp(r['oos_lo'])}, {pp(r['oos_hi'])}]"
    print(f"| {r['family']} | {r['H']} | {r['chosen']} | {pc(r['is_win'])} | {pp(r['is_excess'])} | {int(r['oos_N'])}／{int(r['oos_D'])} | {pc(r['oos_win'])}（{pc(r['oos_base_win'])}） | {pp(r['oos_excess'])} {c} | {'—' if pd.isna(r['oos_q_bh']) else format(r['oos_q_bh'], '.3f')} |")
