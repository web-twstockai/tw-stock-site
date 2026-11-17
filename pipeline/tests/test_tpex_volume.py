"""上櫃成交量口徑：dailyQuotes（含鉅額）為正式來源；鉅額欄位；tradingStock 版本化覆寫；migration；冪等。全部用假回應，不連網。"""
import json
import sqlite3

import pytest

from pipeline import db as pdb, httputil
from pipeline.fetchers import market_prices as mp, prices as pr, tpex_block as tb
from pipeline.httputil import FetchError, RateLimited
from pipeline import tpex_volume_fix as fix

FIELDS = ['代號', '名稱', '收盤', '漲跌', '開盤', '最高', '最低', '均價', '成交股數', '成交金額(元)', '成交筆數',
          '最後買價', '最後買量(張數)', '最後賣價', '最後賣量(張數)', '發行股數', '次日 參考價', '次日 漲停價', '次日 跌停價']


def _dq_json(rows):
    data = [[c, n, "100.00", "0.00", "99.00", "101.00", "98.00", "100.00", f"{v:,}", f"{a:,}", "10",
             "100", "1", "101", "1", "1000", "100.00", "110.00", "90.00"] for c, n, v, a in rows]
    return {"stat": "ok", "tables": [{"title": "上櫃股票行情", "fields": FIELDS, "data": data}]}


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.headers, self.content = data, status, {}, b"x"

    def json(self):
        return self._d


class FakeTpex:
    """依 URL 回假資料並計數。daily: {iso: rows}；ticks／hist 可設。"""

    def __init__(self, daily, ticks=None, hist=None, limit_on=None):
        self.daily, self.ticks, self.hist, self.limit_on = daily, ticks, hist, limit_on
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append(url)
        if self.limit_on and self.limit_on in url:
            raise RateLimited("rate_limited(限流): fake", host="x", retry_after_s=900, status=302)
        if "dailyQuotes" in url:
            iso = url.split("date=")[1].split("&")[0].replace("/", "-")
            if iso not in self.daily:
                raise FetchError("fake no data", status=404)
            return FakeResp(_dq_json(self.daily[iso]))
        if "block_day_result" in url:
            return FakeResp(self.ticks or {"date": "20000101", "tables": []})
        if "tpex_daily_trading_block" in url:
            return FakeResp(self.hist or [])
        raise AssertionError(url)

    def dq_calls(self):
        return [u for u in self.calls if "dailyQuotes" in u]


def _conn():
    conn = pdb.connect()
    pdb.init_db(conn)
    return conn


def _sec(conn, code, name):
    conn.execute("INSERT INTO securities(symbol,market,name,cfi_code,is_common,data_date,source,fetched_at,is_current) "
                 "VALUES(?,?,?,?,?,?,?,?,1)", (code, "TPEx", name, "ESVUFR", 1, "2026-11-16", "t", "2026-11-17T00:00:00+08:00"))
    conn.commit()


def _seed_tradingstock(conn, code, iso, vol_lots, close="100.00"):
    row = {"symbol": code, "market": "TPEx", "price_date": iso, "open_twd": 99.0, "high_twd": 101.0, "low_twd": 98.0,
           "close_twd": 100.0, "change": 0.0, "change_twd": 0.0, "change_sign": None, "change_note": None,
           "is_halted": 0, "is_no_trade": 0, "next_ref_price_twd": None,
           "volume_shares": vol_lots * 1000, "amount_twd": vol_lots * 100000, "transactions": 10,
           "raw_volume": str(vol_lots), "raw_amount": "x", "source": "https://x/tradingStock", "source_table": "tradingStock",
           "volume_basis": "excl_block", "fetched_at": "2026-11-17T09:00:00+08:00", "unit": "volume=股;amount=新台幣元",
           "source_unit": "成交張數;成交仟元", "unit_conversion": "shares=lots*1000; amount=thousand_twd*1000"}
    pr._upsert_price(conn, row)
    conn.commit()


def _cur(conn, code, iso):
    return conn.execute("SELECT * FROM daily_prices WHERE symbol=? AND price_date=? AND is_current=1", (code, iso)).fetchone()


# ---------- schema / migration ----------
def test_new_db_has_columns_and_check_constraint():
    conn = _conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_prices)")}
    assert {"block_shares", "block_amount_twd", "block_source", "volume_basis"} <= cols
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO daily_prices(symbol,market,price_date,source,fetched_at,unit,volume_basis) "
                     "VALUES('1','TPEx','2026-01-01','s','f','u','bogus')")


def test_migration_adds_columns_backfills_basis_and_is_idempotent(tmp_path):
    """用「移除四個新欄位的舊版 schema.sql」建舊庫 → migrate() 補欄位並回填口徑 → 可重跑不變。"""
    from pipeline import config
    lines = [l for l in config.SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
             if not l.strip().startswith(("block_shares", "block_amount_twd", "block_source", "volume_basis"))]
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript("\n".join(lines))
    old.execute("ALTER TABLE daily_prices ADD COLUMN source_table TEXT")   # source_table 本身也是 migrate 補的舊欄位
    assert "volume_basis" not in {r[1] for r in old.execute("PRAGMA table_info(daily_prices)")}
    for i, st in enumerate(["STOCK_DAY", "MI_INDEX", "dailyQuotes", "tradingStock", None]):
        old.execute("INSERT INTO daily_prices(symbol,market,price_date,source,fetched_at,unit,source_table) VALUES(?,?,?,?,?,?,?)",
                    (f"{i}", "X", "2026-01-01", "s", "f", "u", st))
    old.commit()
    pdb.migrate(old)
    got = dict(old.execute("SELECT COALESCE(source_table,'NULL'), volume_basis FROM daily_prices").fetchall())
    assert got == {"STOCK_DAY": "incl_block", "MI_INDEX": "incl_block", "dailyQuotes": "incl_block", "tradingStock": "excl_block", "NULL": None}
    assert {"block_shares", "block_amount_twd", "block_source"} <= {r[1] for r in old.execute("PRAGMA table_info(daily_prices)")}
    snap = old.execute("SELECT * FROM daily_prices ORDER BY symbol").fetchall()
    pdb.migrate(old)   # 可重跑、不變
    assert old.execute("SELECT * FROM daily_prices ORDER BY symbol").fetchall() == snap


# ---------- 鉅額解析與查詢 ----------
TICKS = {"date": "20261116", "tables": [{"fields": ["交易型態", "交割期別", "代號", "名稱", "成交價格(元)", "成交股數", "成交值(元)", "成交時間"],
         "data": [["逐筆", "T+2", "4527", "方土霖", "40", "2,000,000", "80,000,000", "14:00"],
                  ["逐筆", "T+2", "4527", "方土霖", "40", "5,250,000", "210,000,000", "14:00"],
                  ["配對", "T+2", "23444", "華邦電四", "100", "190,000", "19,000,000", "13:59"]]}]}
HIST = [{"TradingDate": "1151113", "Name": "環球晶         ", "NumberOfSharesTraded": "100000", "TradingValue": "63000000"},
        {"TradingDate": "1151112", "Name": "世界", "NumberOfSharesTraded": "600000", "TradingValue": "80000000"},
        {"TradingDate": "1151112", "Name": "世界", "NumberOfSharesTraded": "400000", "TradingValue": "58295300"},
        {"TradingDate": "1151112", "Name": "同名", "NumberOfSharesTraded": "1", "TradingValue": "1"},
        {"TradingDate": "1151116", "Name": "方土霖", "NumberOfSharesTraded": "7250000", "TradingValue": "290000000"}]


def test_parse_ticks_and_hist():
    iso, m = tb.parse_block_ticks(TICKS)
    assert iso == "2026-11-16" and m["4527"] == (7250000, 290000000) and m["23444"] == (190000, 19000000)
    h, lo, hi = tb.parse_block_hist(HIST)
    assert h[("2026-11-12", "世界")] == (1000000, 138295300) and h[("2026-11-13", "環球晶")] == (100000, 63000000)
    assert (lo, hi) == ("2026-11-12", "2026-11-16")


def _ctx(conn):
    for c, n in (("6488", "環球晶"), ("5347", "世界"), ("4527", "方土霖"), ("1111", "同名"), ("2222", "同名"), ("3293", "鈊象")):
        _sec(conn, c, n)
    ctx = tb.BlockContext()
    ctx.ticks_date, ctx.ticks = tb.parse_block_ticks(TICKS)
    ctx.hist, ctx.cov_start, ctx.cov_end = tb.parse_block_hist(HIST)
    ctx.code_to_name, ctx.name_to_codes = tb._names(conn)
    return ctx


def test_lookup_rules():
    conn = _conn()
    ctx = _ctx(conn)
    assert ctx.lookup("2026-11-16", "4527") == (7250000, 290000000, tb.SRC_TICKS)       # 逐筆（有代號）
    assert ctx.lookup("2026-11-16", "3293") == (0, 0, tb.SRC_TICKS)                    # 逐筆日當日無鉅額＝0
    assert ctx.lookup("2026-11-13", "6488") == (100000, 63000000, tb.SRC_NAME)         # 歷史彙總：名稱比對
    assert ctx.lookup("2026-11-13", "3293") == (0, 0, tb.SRC_NAME)                     # 涵蓋區間內無資料＝0
    assert ctx.lookup("2026-11-12", "1111") is None and ctx.lookup("2026-11-12", "2222") is None   # 同名歧義→NULL
    assert ctx.lookup("2026-03-02", "6488") is None                                    # 區間外＝未知，不補 0


# ---------- 寫入：dailyQuotes 為正式來源、版本化覆寫、冪等 ----------
def test_tradingstock_overwritten_by_dailyquotes_versioned_and_idempotent(monkeypatch):
    conn = _conn()
    ctx = _ctx(conn)
    _seed_tradingstock(conn, "6488", "2026-11-13", 8791)          # 官方：dailyQuotes 8,891,386（含鉅額 100,000）
    fake = FakeTpex({"2026-11-13": [("6488", "環球晶", 8891386, 5692722244), ("9999", "非目標", 5, 5)]})
    monkeypatch.setattr(httputil.tpex, "request", fake)
    st = mp.fetch_tpex_day(conn, "2026-11-13", {"6488": "TPEx"}, [], block_ctx=ctx)
    assert st["tpex"] == 1 and not st["failed"]
    new = _cur(conn, "6488", "2026-11-13")
    assert new["version"] == 2 and new["source_table"] == "dailyQuotes" and new["volume_basis"] == "incl_block"
    assert new["volume_shares"] == 8891386 and new["amount_twd"] == 5692722244      # 含鉅額總量，不扣 block
    assert new["block_shares"] == 100000 and new["block_amount_twd"] == 63000000 and new["block_source"] == "name_match"
    assert mp.SWITCH_NOTE in new["change_note"]
    olds = conn.execute("SELECT * FROM daily_prices WHERE symbol='6488' AND price_date='2026-11-13' AND is_current=0").fetchall()
    assert len(olds) == 1 and olds[0]["source_table"] == "tradingStock" and olds[0]["volume_shares"] == 8791000 and olds[0]["volume_basis"] == "excl_block"
    assert conn.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol='9999'").fetchone()[0] == 0   # 只寫 universe 內
    # 重跑：不升版
    mp.fetch_tpex_day(conn, "2026-11-13", {"6488": "TPEx"}, [], block_ctx=ctx)
    mp.fetch_tpex_day(conn, "2026-11-13", {"6488": "TPEx"}, [], block_ctx=ctx)
    assert _cur(conn, "6488", "2026-11-13")["version"] == 2
    assert conn.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol='6488'").fetchone()[0] == 2


def test_tradingstock_never_downgrades_dailyquotes_row(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(httputil.tpex, "request", FakeTpex({"2026-11-13": [("6488", "環球晶", 8891386, 1)]}))
    mp.fetch_tpex_day(conn, "2026-11-13", {"6488": "TPEx"}, [], block_ctx=tb.BlockContext())
    # tradingStock 回應（不含鉅額）不得覆寫已有的 dailyQuotes 列
    class TS(FakeTpex):
        def __call__(self, method, url, **kw):
            return FakeResp({"tables": [{"data": [["115/11/13", "8,791", "5,629,722", "99", "101", "98", "100", "0.00", "10"]]}]})
    monkeypatch.setattr(httputil.tpex, "request", TS({}))
    pr.fetch_tpex_trading_stock(conn, "6488", __import__("datetime").date(2026, 11, 1))
    assert _cur(conn, "6488", "2026-11-13")["source_table"] == "dailyQuotes"


# ---------- 多日流程、略過、退而求其次、限流 ----------
def test_quotes_days_skip_done_fallback_and_rate_limit(monkeypatch):
    conn = _conn()
    ctx_fake = FakeTpex({"2026-11-12": [("6488", "環球晶", 15508871, 1)], "2026-11-13": [("6488", "環球晶", 8891386, 1)]})
    monkeypatch.setattr(httputil.tpex, "request", ctx_fake)
    out = pr.fetch_tpex_quotes_days(conn, ["6488"], ["2026-11-12", "2026-11-13"])
    assert out["fetched_days"] == 2 and len(ctx_fake.dq_calls()) == 2
    # 冪等：全部完成 → 不再請求 dailyQuotes
    ctx_fake.calls.clear()
    out2 = pr.fetch_tpex_quotes_days(conn, ["6488"], ["2026-11-12", "2026-11-13"])
    assert out2["skipped_days"] == 2 and out2["fetched_days"] == 0 and not ctx_fake.calls
    # 取不到的日期：failed_days 記錄、該代號列入 fallback（之後用 tradingStock，excl_block）
    out3 = pr.fetch_tpex_quotes_days(conn, ["6488"], ["2026-11-12", "2026-11-16"])
    assert out3["failed_days"] == ["2026-11-16"] and out3["fallback_symbols"] == ["6488"]
    # 限流：立即停止，不再請求後面的日期
    conn2 = _conn()
    lim = FakeTpex({}, limit_on="2026/11/12")
    monkeypatch.setattr(httputil.tpex, "request", lim)
    out4 = pr.fetch_tpex_quotes_days(conn2, ["5347"], ["2026-11-12", "2026-11-13", "2026-11-16"])
    assert out4["rate_limited"] and len(lim.dq_calls()) == 1
    st = conn2.execute("SELECT ok, notes FROM source_status WHERE source_id='tpex_daily_quotes_2026-11-12'").fetchone()
    assert st[0] == 0 and st[1].startswith("rate_limited")


def test_run_fix_end_to_end_idempotent_and_leaves_unfetchable_as_excl_block(monkeypatch):
    conn = _conn()
    for c, n in (("6488", "環球晶"), ("5347", "世界")):
        _sec(conn, c, n)
    dates = ["2026-11-12", "2026-11-13", "2026-11-16"]
    for d in dates:
        _seed_tradingstock(conn, "6488", d, 8000)
        _seed_tradingstock(conn, "5347", d, 9000)
    # 另一檔 dailyQuotes 舊列（block 欄位 NULL，應被就地補齊）
    _sec(conn, "4527", "方土霖")
    mp._upsert_preserving(conn, mp.parse_daily_quotes(_dq_json([("4527", "方土霖", 7257469, 290319898)]), {"4527": "TPEx"}, "2026-11-16", "u")[0])
    conn.commit()
    daily = {d: [("6488", "環球晶", 8100000, 1), ("5347", "世界", 9100000, 1)] for d in dates[:2]}   # 11-16 取不到
    fake = FakeTpex(daily, ticks=TICKS, hist=HIST)
    monkeypatch.setattr(httputil.tpex, "request", fake)
    out = fix.run_fix(conn)
    assert out["fetched_days"] == 2 and out["failed_days"] == ["2026-11-16"] and not out["rate_limited"]
    assert out["left_tradingStock_dates"] == ["2026-11-16"] and out["left_tradingStock_rows"] == 2
    for d in dates[:2]:
        for c in ("6488", "5347"):
            r = _cur(conn, c, d)
            assert r["source_table"] == "dailyQuotes" and r["version"] == 2 and r["volume_basis"] == "incl_block"
    r = _cur(conn, "6488", "2026-11-16")      # 取不到官方含鉅額值：維持 tradingStock，標 excl_block，不補造
    assert r["source_table"] == "tradingStock" and r["volume_basis"] == "excl_block" and r["block_shares"] is None
    assert _cur(conn, "5347", "2026-11-12")["block_shares"] == 1000000      # 世界 11-12 名稱比對（兩筆加總）
    assert _cur(conn, "4527", "2026-11-16")["block_shares"] == 7250000 and _cur(conn, "4527", "2026-11-16")["block_source"] == tb.SRC_TICKS
    assert out["block_fill"]["filled"] >= 1
    # 重跑：只重試失敗日（1 個 dailyQuotes 請求）；其餘不再請求、不升版
    snap = conn.execute("SELECT symbol, price_date, version FROM daily_prices ORDER BY 1,2,3").fetchall()
    fake.calls.clear()
    tb.reset_context()
    out2 = fix.run_fix(conn)
    assert len(fake.dq_calls()) == 1 and out2["fetched_days"] == 0
    assert conn.execute("SELECT symbol, price_date, version FROM daily_prices ORDER BY 1,2,3").fetchall() == snap


def test_mi_index_and_stock_day_rows_are_incl_block():
    rows = mp.parse_mi_index({"tables": [{"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"],
                                          "data": [["2330", "台積電", "1,000", "1", "1,000", "1", "1", "1", "1", "+", "0"]]}]},
                             {"2330": "TWSE"}, "2026-11-16", "u")
    assert rows and rows[0]["volume_basis"] == "incl_block"
