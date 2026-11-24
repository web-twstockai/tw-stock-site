"""研究用：由 v3_stats.csv 產生勝率門檻判定表 v3_qualification.csv（H AO 2026-11-24 18:20 規則）。
主口徑：新變體＝成交金額≥1億（tv1e8）；參照模式＝無額外流動性門檻（none，與網站統計同口徑）。
判定只看樣本外（OOS）5 日、扣成本、N≥100 且 D≥30：勝率≥70% → 可列進階；≥80% → 可列高級。10／20／60 日只報告、不判定。"""
import sys
from pathlib import Path
import pandas as pd

d = Path(sys.argv[1])
s = pd.read_csv(d / "v3_stats.csv")
s = s[((s["variant"].str.startswith("REF_")) & (s["liq"] == "none")) | ((~s["variant"].str.startswith("REF_")) & (s["liq"] == "tv1e8"))]
rows = []
for (v, H), g in s.groupby(["variant", "H"], sort=False):
    r = dict(variant=v, family=g["family"].iloc[0], name=g["name"].iloc[0], H=H, liq=g["liq"].iloc[0])
    for samp in ("ALL", "IS", "OOS"):
        x = g[g["sample"] == samp]
        if len(x) and x["N"].iloc[0] > 0:
            x = x.iloc[0]
            r.update({f"{samp}_N": x["N"], f"{samp}_D": x["D"], f"{samp}_win": x["win"], f"{samp}_base_win": x["base_win"],
                      f"{samp}_median": x["median"], f"{samp}_excess": x["excess"], f"{samp}_displayable": bool(x["displayable"])})
        else:
            r.update({f"{samp}_N": 0, f"{samp}_D": 0, f"{samp}_displayable": False})
    disp = r.get("OOS_displayable", False)
    w = r.get("OOS_win")
    if H == 5:
        r["判定"] = ("樣本外資料不足" if not disp else "高級" if w >= 0.80 else "進階" if w >= 0.70 else "未達門檻")
    else:
        r["判定"] = "只報告（門檻以 5 日為準）"
    r["only_oos"] = r.get("IS_N", 0) == 0
    rows.append(r)
q = pd.DataFrame(rows)
q.to_csv(d / "v3_qualification.csv", index=False, float_format="%.6g")
print(q[q.H == 5]["判定"].value_counts().to_string())
print("max OOS 5d win:", q[(q.H == 5) & (q["OOS_displayable"])].sort_values("OOS_win", ascending=False)[["variant", "OOS_N", "OOS_D", "OOS_win", "OOS_base_win"]].head(5).to_string(index=False))
