"""預設評估日（as_of 未傳入）規則測試——adjust.resolve_as_of／io_sqlite.default_as_of（測試用合成資料）。"""
import sqlite3

import pandas as pd
import pytest

import adjust as A
import io_sqlite as I
import screener as S
import series as SR
import synthetic_data as SD
import verify_independent as VI

D = pd.Timestamp


def test_resolve_pure_rules():
    trade = ["2026-10-22", "2026-10-23", "2026-10-27"]
    r = A.resolve_as_of({"A": "2026-10-22", "B": "2026-10-27", "OUT": "2026-10-27"}, trade, ["A"])
    assert r.as_of == D("2026-10-22") and r.lagging == () and r.rule == A.AS_OF_RULE   # 範圍外較新不影響
    r = A.resolve_as_of({"A": "2026-10-22", "B": "2026-10-27"}, trade, ["A", "B", "C"])
    assert r.as_of == D("2026-10-27") and r.lagging == ("A", "C") and (r.n_scope, r.n_with_row) == (3, 1)
    with pytest.raises(ValueError):
        A.resolve_as_of({"A": "2026-10-25"}, trade, ["A"])        # 非交易日的值＝呼叫端錯誤
    with pytest.raises(ValueError):
        A.resolve_as_of({"OUT": "2026-10-27"}, trade, ["A"])      # 範圍內沒有行情


def test_latest_trade_day_skips_non_trade_dates():
    m = A.latest_trade_day_by_symbol(["A", "A", "B"], ["2026-10-22", "2026-10-25", None], ["2026-10-22", "2026-10-23"])
    assert m == {"A": D("2026-10-22")}


def _db(tmp_path, extra_trade_days=("2026-10-06", "2026-10-07"), rows=()):
    p = SD.build_sqlite(tmp_path / "asof.db")
    con = sqlite3.connect(p)
    for d in extra_trade_days:
        con.execute("INSERT INTO trading_calendar (calendar_date, is_trade_day, source_kind, source, fetched_at, unit) "
                    "VALUES (?, 1, 'weekday_default', '測試用合成資料', '2026-11-10T00:00:00+08:00', 'n/a')", (d,))
    cols = [r[1] for r in con.execute("PRAGMA table_info(daily_prices)")]
    for sym, d in rows:   # 複製 TEST01 最後一列，改代號與日期
        sel = ", ".join("?" if c == "symbol" else ("?" if c == "price_date" else c) for c in cols)
        con.execute(f"INSERT INTO daily_prices ({','.join(cols)}) SELECT {sel} FROM daily_prices "
                    "WHERE symbol='TEST01' AND price_date='2026-10-05'", (sym, d))
    con.commit()
    con.close()
    return p


def test_out_of_scope_newer_date_does_not_move_as_of(tmp_path):
    p = _db(tmp_path, rows=[("OUT1", "2026-10-07")])      # 範圍外（不在 securities、不在 symbols）有較新日期
    assert I.default_as_of(p, ["TEST01", "TEST02"]).as_of == D("2026-10-05")
    assert I.default_as_of(p).as_of == D("2026-10-05")      # 範圍＝securities 現行普通股，OUT1 不在其中
    inp = I.load_screen_inputs(p, symbols=["TEST01", "TEST02"])
    assert inp.as_of == "2026-10-05" and inp.as_of_resolution.rule == A.AS_OF_RULE
    assert I.load_screen_inputs(p, as_of="2026-10-05").as_of_resolution is None
    res = S.screen(**inp.screen_kwargs())
    assert set(res.summary["status"]) != {"EXCLUDED"}


def test_calendar_trade_day_without_scope_rows_is_skipped(tmp_path):
    # 2026-10-06、10-07 在交易日曆是交易日，但範圍內沒有任何行情（同 2026-10-23 的情況）→ 不選中
    p = _db(tmp_path)
    r = I.default_as_of(p, ["TEST01", "TEST02", "TEST03"])
    assert r.as_of == D("2026-10-05") and r.lagging == ()
    assert VI.load_db(p)["as_of"] == pd.Timestamp("2026-10-05").date()


def test_scope_latest_dates_differ(tmp_path):
    p = _db(tmp_path, rows=[("TEST02", "2026-10-06")])    # 範圍內只有 TEST02 有 10-06
    r = I.default_as_of(p, ["TEST01", "TEST02", "TEST11"])
    assert r.as_of == D("2026-10-06") and r.lagging == ("TEST01", "TEST11") and r.n_with_row == 1
    # 範圍不含 TEST02 時不受影響
    assert I.default_as_of(p, ["TEST01", "TEST11"]).as_of == D("2026-10-05")
    # TEST11 最新到 10-02、TEST01 到 10-05 → 取 10-05，TEST11 落後
    r2 = I.default_as_of(p, ["TEST01", "TEST11"])
    assert r2.lagging == ("TEST11",)
    inp = I.load_screen_inputs(p, symbols=["TEST01", "TEST02", "TEST11"])
    s = S.screen(**inp.screen_kwargs()).summary.set_index("symbol")
    assert s.loc["TEST01", "status"] == "EXCLUDED"           # 落後的代號依既有規則排除，as_of 不因它改變


def test_screener_and_series_in_memory_default(T):
    px = T["daily_prices"]
    out = px[px["symbol"] == "TEST01"].tail(1).copy()
    out["symbol"] = "OUT1"
    out["price_date"] = "2026-10-05"
    cal = T["trading_calendar"]
    r = S.screen(prices=pd.concat([px, out]), calendar=cal, actions=T["corporate_actions"], securities=T["securities"],
                 actions_coverage=T["actions_coverage"], universe=["TEST11"])
    assert r.as_of == D("2026-10-02")                        # TEST11 最新 10-02；其他代號較新不影響
    m = SR.ma_series(prices=px, actions=T["corporate_actions"], calendar=cal, symbol="TEST11",
                     securities=T["securities"], actions_coverage=T["actions_coverage"])
    assert m["price_date"].iloc[-1] == D("2026-10-02")
