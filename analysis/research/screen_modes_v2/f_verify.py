"""研究用（非正式程式）：抽樣獨立核對 5 日報酬（純 sqlite＋Python，不用 analysis／pandas 特徵）。
只抽「S 到出場日之間沒有任何 corporate_actions」的事件，此時還原報酬＝原始價報酬。
核對：T1＝S 後第一個交易日、T1 有成交、出場日＝T1 起第 5 個交易日（無成交則順延）、扣成本報酬。
用法：python f_verify.py DB EVENTS_CSV_GZ [n]
"""
import sys, sqlite3, gzip, csv, random
FEE, TAX = 0.001425, 0.003


def main():
    db, evp = sys.argv[1], sys.argv[2]; n = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    td = [r[0] for r in con.execute("select calendar_date from trading_calendar where is_trade_day=1 order by 1")]
    pos = {d: i for i, d in enumerate(td)}
    acts = {}
    for s, d in con.execute("select symbol, action_date from corporate_actions where is_current=1"):
        acts.setdefault(s, []).append(d)
    rows = [r for r in csv.DictReader(gzip.open(evp, "rt")) if r["fflag"] == "ok"]
    random.seed(7); sample = random.sample(rows, min(n, len(rows)))
    checked = mism = skipped = 0; maxdiff = 0.0
    for r in sample:
        s, S = r["symbol"], r["date"]
        i = pos[S]; t1 = td[i + 1]
        px = {d: (o, c, v) for d, o, c, v in con.execute(
            "select price_date, open_twd, close_twd, volume_shares from daily_prices where is_current=1 and symbol=? "
            "and price_date between ? and ?", [s, S, td[min(i + 30, len(td) - 1)]])}
        k = i + 1 + 4
        while not (td[k] in px and px[td[k]][2] and px[td[k]][2] > 0 and px[td[k]][1] is not None):
            k += 1
        ex = td[k]
        if any(S < a <= ex for a in acts.get(s, [])):
            skipped += 1; continue
        o1 = px[t1][0]; c5 = px[ex][1]
        ret = c5 * (1 - FEE - TAX) / (o1 * (1 + FEE)) - 1
        d = abs(ret - float(r["fret"])); maxdiff = max(maxdiff, d); checked += 1
        if d > 1e-9:
            mism += 1
            if mism <= 5:
                print("MISMATCH", r["mode"], s, S, t1, ex, ret, r["fret"])
    print(f"checked={checked} mismatches={mism} skipped_with_actions={skipped} max_abs_diff={maxdiff:.3e}")


if __name__ == "__main__":
    main()
