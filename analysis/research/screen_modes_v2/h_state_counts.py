"""研究用（非正式程式）：現有月營收／財報模式在網站上是「每天列出最新可得資料符合者」（狀態版），
這裡只算狀態版的每日檔數（供分級參考），不算報酬（勝率仍依 §11.1 的事件版）。
用法：python h_state_counts.py DB ADJ_PANEL PANEL_DIR OUTDIR"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_features as CF


def main():
    db, adj, pdir, out = sys.argv[1:5]
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trade_all = pd.to_datetime([r[0] for r in con.execute("select calendar_date from trading_calendar where is_trade_day=1 order by 1")])
    con.close()
    g = CF.load_grid(adj, pdir)
    v = CF.tech_features(g)
    v = CF.asof_join(v, CF.revenue_features(db, trade_all.values.astype("datetime64[ns]")), ["rev_high", "yoy3_pos"], "rv_")
    v = CF.asof_join(v, CF.eps_features(db, trade_all.values.astype("datetime64[ns]")), ["eps_yoy_pos"], "fn_")
    v = v[v["scr_status"].isin(["PASS", "FAIL"]) & (v["date"] >= "2026-05-27") & (v["date"] <= "2026-11-16")]
    C = v["adj_close_twd"]; tr = (C > v["ma20"]) & (v["ma20"] > v["ma60"])
    st = {"M3_state": v["rv_rev_high"] == True, "M4_state": v["rv_yoy3_pos"] == True, "M5_state": v["fn_eps_yoy_pos"] == True,
          "M7_state": (v["rv_rev_high"] == True) & tr,
          "M8_state": (v["fn_eps_yoy_pos"] == True) & (v["rv_yoy3_pos"] == True) & tr}
    rows = []
    for k, m in st.items():
        c = m.groupby(v["date"]).sum()
        rows.append(dict(mode=k, avg=c.mean(), median=c.median(), min=c.min(), max=c.max(), days=len(c)))
    df = pd.DataFrame(rows); df.to_csv(Path(out) / "state_counts.csv", index=False); print(df.round(1).to_string())


if __name__ == "__main__":
    main()
