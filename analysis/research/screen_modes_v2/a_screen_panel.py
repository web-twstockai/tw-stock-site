"""研究用（非正式程式）：逐日呼叫 analysis 的 screen()（預設參數＝M1），取得每檔每日的
status／exclusion_code 與 screen 算出的指標，當作「可評估遮罩」與 M1 訊號。

- 唯讀快照 DB（不寫正式庫）；單程序、逐日處理、每日寫一個 csv.gz（可續跑）。
- 母體＝securities 現行普通股 ＋ 期間內有行情的已下市普通股（securities_history），減少倖存者偏差。
用法：python a_screen_panel.py DB OUTDIR [start] [end]
"""
import sys, os, time, gc, sqlite3
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "analysis"))
import pandas as pd
import io_sqlite as IO
import screener as S

KEEP = ["symbol", "market", "price_date", "status", "exclusion_code", "close_twd", "change_pct", "volume_shares",
        "amount_twd", "ma_short", "ma_long", "prior_high_twd", "prior_avg_volume_shares", "volume_ratio",
        "pass_trend", "pass_breakout", "pass_volume", "pass_turnover", "n_valid_days"]


def main():
    db, out = sys.argv[1], Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    trade = [r[0] for r in con.execute(
        "select calendar_date from trading_calendar where is_trade_day=1 and calendar_date between "
        "(select min(price_date) from daily_prices) and (select max(price_date) from daily_prices) order by 1")]
    dl = pd.read_sql_query(
        "select h.symbol, h.market, h.name, h.listed_date, h.is_common from securities_history h "
        "join delistings d on d.symbol=h.symbol where h.is_common=1 and h.symbol not in "
        "(select symbol from securities where is_current=1) and h.symbol in (select distinct symbol from daily_prices)", con)
    dl = dl.drop_duplicates("symbol")
    con.close()
    start = sys.argv[3] if len(sys.argv) > 3 else trade[59]
    end = sys.argv[4] if len(sys.argv) > 4 else trade[-1]
    days = [d for d in trade if start <= d <= end]
    for d in days:
        f = out / f"screen_{d}.csv.gz"
        if f.exists():
            continue
        t0 = time.time()
        inp = IO.load_screen_inputs(db, as_of=d, lookback_trade_days=70, coverage_scope="all")
        secs = inp.securities
        if len(dl):
            add = dl.copy(); add["data_date"] = None; add["fetched_at"] = None
            secs = pd.concat([secs, add[secs.columns.intersection(add.columns)]], ignore_index=True)
        kw = inp.screen_kwargs()
        kw["securities"] = secs
        kw.pop("universe", None)
        res = S.screen(**kw, params=S.ScreenParams())
        res.summary[KEEP].to_csv(f, index=False, compression="gzip")
        n = res.summary["status"].value_counts().to_dict()
        print(d, f"{time.time()-t0:.1f}s", n, flush=True)
        del inp, res, kw, secs; gc.collect()


if __name__ == "__main__":
    main()
