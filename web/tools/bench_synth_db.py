"""效能量測專用：從正式 DB 快照複製出「全市場規模」的合成資料庫（只放 /tmp，絕不發佈、絕不寫回 data/）。

正式 DB 目前只有 30 檔有行情／營收／財報；資料工程的 universe 'all'（約 1,800+ 檔）尚未入庫。
為了量測全市場下的匯出耗時、檔案大小與網站回應時間，把 securities 內其餘普通股各自套用一檔
同市場樣本股的資料（價格、金額、營收、財報乘上每檔固定的比例係數），列數與欄位分布接近真實全市場。

用法：.venv/bin/python tools/bench_synth_db.py --out /tmp/twsite-bench/all_synth.db [--n 1800]
限制：--out 必須在 /tmp 底下；來源 DB 以唯讀方式讀取（SQLite backup API）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRICE_COLS = ("open_twd", "high_twd", "low_twd", "close_twd", "change_twd", "change", "avg_twd")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(ROOT / "data" / "twstock.db"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=0, help="合成後總檔數上限（0＝securities 全部普通股）")
    ap.add_argument("--replace", action="store_true",
                    help="以 30 檔驗證樣本為範本，覆蓋其餘普通股既有的各表資料（正式庫已有全市場行情但歷史不足、"
                         "沒有營收財報時，用來估算「全市場且每檔資料齊全」的最壞情況）")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    if not str(out).startswith("/tmp/"):
        print("--out 必須在 /tmp 底下（合成資料不得進入專案目錄）", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    t0 = time.time()
    src = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
    dst = sqlite3.connect(out)
    src.backup(dst)
    src.close()
    c = dst
    have = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM daily_prices WHERE is_current=1 ORDER BY symbol")]
    if a.replace:
        sys.path.insert(0, str(ROOT / "web"))
        from app.universe import resolve
        have = sorted(resolve("sample30").symbols)
    mk_of = dict(c.execute("SELECT symbol, market FROM securities WHERE is_current=1"))
    tmpl = {m: [s for s in have if mk_of.get(s) == m] for m in ("TWSE", "TPEx")}
    targets = [r[0] for r in c.execute("SELECT symbol FROM securities WHERE is_current=1 AND is_common=1 ORDER BY symbol")
               if r[0] not in have]
    if a.n:
        targets = targets[: max(0, a.n - len(have))]
    c.execute("CREATE TEMP TABLE m(sym TEXT, tpl TEXT, f REAL, vf REAL)")
    rows = []
    for i, s in enumerate(targets):
        pool = tmpl.get(mk_of.get(s)) or have
        h = zlib.crc32(s.encode())
        rows.append((s, pool[i % len(pool)], 0.4 + (h % 1000) / 625.0, 0.3 + ((h >> 10) % 1000) / 500.0))
    c.executemany("INSERT INTO m VALUES (?,?,?,?)", rows)
    if a.replace:
        for t in ("daily_prices", "monthly_revenues", "income_statements", "cash_flow_statements", "corporate_actions",
                  "material_infos"):
            try:
                c.execute(f"DELETE FROM {t} WHERE symbol IN (SELECT sym FROM m)")
            except sqlite3.OperationalError:
                pass

    def clone(table, num_cols, extra_set=None, vol_cols=(), skip=()):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        sel = []
        for col in cols:
            if col == "symbol":
                sel.append("m.sym")
            elif col == "market":
                sel.append("(SELECT market FROM securities WHERE symbol=m.sym AND is_current=1 LIMIT 1)")
            elif col in num_cols:
                sel.append(f"CASE WHEN t.{col} IS NULL THEN NULL ELSE round(t.{col} * m.f, 2) END")
            elif col in vol_cols:
                sel.append(f"CASE WHEN t.{col} IS NULL THEN NULL ELSE round(t.{col} * m.vf) END")
            elif extra_set and col in extra_set:
                sel.append(extra_set[col])
            else:
                sel.append(f"t.{col}")
        c.execute(f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(sel)} FROM {table} t JOIN m ON t.symbol = m.tpl"
                  " WHERE t.is_current = 1")

    clone("daily_prices", PRICE_COLS, vol_cols=("volume_shares", "raw_volume"),
          extra_set={"amount_twd": "CASE WHEN t.amount_twd IS NULL THEN NULL ELSE round(t.amount_twd * m.f * m.vf) END"})
    money = ("revenue_twd", "revenue_prev_month_twd", "revenue_prev_year_twd", "cumulative_twd", "cumulative_prev_year_twd")
    clone("monthly_revenues", money)
    clone("income_statements", ("revenue_twd", "gross_profit_twd", "operating_income_twd", "non_operating_twd",
                                "pretax_income_twd", "net_income_twd", "net_income_parent_twd"))
    clone("cash_flow_statements", ("operating_cf_twd", "investing_cf_twd", "financing_cf_twd", "net_increase_twd",
                                   "cash_begin_twd", "cash_end_twd"))
    clone("corporate_actions", ("prev_close_twd", "reference_twd", "limit_up_twd", "limit_down_twd", "auction_base_twd"))
    clone("material_infos", (), extra_set={"content_hash": "t.content_hash || m.sym"})
    c.commit()
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("VACUUM")
    n = c.execute("SELECT count(DISTINCT symbol) FROM daily_prices WHERE is_current=1").fetchone()[0]
    c.close()
    print(f"合成完成：{out}（{out.stat().st_size / 1e6:.0f} MB，有行情 {n} 檔，{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
