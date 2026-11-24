"""研究用（非正式程式）：用 analysis 的 series.ohlc_series()（與 screener 同一套前復權、可用範圍規則）
算出全市場每檔每日的還原 OHLC／還原量，供回測計算指標與 5 日報酬。

- 以資料最後一個交易日為基準前復權。指標都是價格比例或比較，對任一常數倍數不變，
  所以「以最後一日為基準」與「以訊號日為基準」算出的條件相同；持有期內的除權息因子也自然包含在內（§11.2.1）。
- 已知限制：若某檔在訊號日之後才出現無法解析的事件，ohlc_series 會把邊界前整段標 unavailable，
  該段訊號因此被保守地丟掉（研究稿記為限制）。
用法：python b_adjusted_panel.py DB OUT.csv.gz
"""
import sys, sqlite3, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "analysis"))
import pandas as pd
import io_sqlite as IO
import series as SE


def main():
    db, out = sys.argv[1], sys.argv[2]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    last = con.execute("select max(price_date) from daily_prices where is_current=1").fetchone()[0]
    ntd = con.execute("select count(*) from trading_calendar where is_trade_day=1 and calendar_date between "
                      "(select min(price_date) from daily_prices) and ?", [last]).fetchone()[0]
    dl = pd.read_sql_query(
        "select h.symbol, h.market, h.name, h.listed_date, h.is_common from securities_history h "
        "join delistings d on d.symbol=h.symbol where h.is_common=1 and h.symbol not in "
        "(select symbol from securities where is_current=1) and h.symbol in (select distinct symbol from daily_prices)",
        con).drop_duplicates("symbol")
    con.close()
    inp = IO.load_screen_inputs(db, as_of=last, lookback_trade_days=ntd + 5, coverage_scope="all")
    secs = inp.securities
    if len(dl):
        add = dl.copy(); add["data_date"] = None; add["fetched_at"] = None
        secs = pd.concat([secs, add[secs.columns.intersection(add.columns)]], ignore_index=True)
    px = inp.prices
    pg = {k: g for k, g in px.groupby("symbol", sort=False)}
    ag = {k: g for k, g in inp.actions.groupby("symbol", sort=False)}
    empty_a = inp.actions.iloc[0:0]
    frames = []
    t0 = time.time()
    syms = sorted(set(secs["symbol"]) & set(pg))
    for i, s in enumerate(syms):
        r = SE.ohlc_series(pg[s], ag.get(s, empty_a), inp.calendar, s, as_of=last,
                           actions_coverage=inp.actions_coverage, securities=secs)
        b = r.bars[["price_date", "symbol", "open_twd", "high_twd", "low_twd", "close_twd", "volume_shares",
                    "amount_twd", "status", "adj_open_twd", "adj_high_twd", "adj_low_twd", "adj_close_twd",
                    "adj_volume_shares", "unavailable_code"]]
        frames.append(b)
        if i % 200 == 0:
            print(i, s, f"{time.time()-t0:.0f}s", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df["price_date"] = pd.to_datetime(df["price_date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(out, index=False, compression="gzip")
    print("rows", len(df), "symbols", df["symbol"].nunique(), "status", df["status"].value_counts().to_dict())


if __name__ == "__main__":
    main()
