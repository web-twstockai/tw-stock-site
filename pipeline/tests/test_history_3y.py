"""3 年歷史回補（--fix-from 2023-11-01）：上櫃暫停／恢復配對、櫃買停牌歷史、下市清單、delisted 納入 universe、
revenue／financials 的 --fix-from、交易日曆歷史回補。假回應，不送請求。"""
import json
from datetime import date
from pathlib import Path

import pytest

from pipeline import (calendar_backfill as cb, db as pdb, delist_backfill as db_, dates, financials_backfill as fb,
                      halt_fix, halts_backfill as hb, httputil, revenue_backfill as rb)
from pipeline.fetchers import calendar as cal, delistings, financials as fin, halts, market_prices as mp
from pipeline.tests.test_halt_fix import _sec

FIX = Path(__file__).parent / "fixtures"


def _conn():
    c = pdb.connect()
    pdb.init_db(c)
    return c


class Resp:
    def __init__(self, data):
        self._d, self.status_code, self.headers, self.content, self.text = data, 200, {}, b"x", "x"

    def json(self):
        return self._d


# ---------- 上櫃暫停／恢復是分開的兩列：配對 ----------
def test_pair_tpex_halts_pairs_separate_resume_rows_across_years():
    items = [
        {"symbol": "1", "halt_date": "2023-12-28", "halt_time": "080000", "resume_date": None, "resume_time": None},
        {"symbol": "1", "halt_date": None, "halt_time": "", "resume_date": "2024-01-03", "resume_time": "080000"},      # 跨年
        {"symbol": "2", "halt_date": "2024-02-01", "halt_time": "080000", "resume_date": None, "resume_time": None},
        {"symbol": "2", "halt_date": "2024-03-01", "halt_time": "080000", "resume_date": None, "resume_time": None},
        {"symbol": "2", "halt_date": None, "halt_time": "", "resume_date": "2024-02-02", "resume_time": "080000"},
        # 第二次停牌沒有恢復列：維持 None（不外推）
        {"symbol": "3", "halt_date": None, "halt_time": "", "resume_date": "2024-01-02", "resume_time": None},          # 孤立恢復列：略過
    ]
    out = {(h["symbol"], h["halt_date"]): h["resume_date"] for h in halts.pair_tpex_halts(items)}
    assert out == {("1", "2023-12-28"): "2024-01-03", ("2", "2024-02-01"): "2024-02-02", ("2", "2024-03-01"): None}


def test_fetch_tpex_openapi_real_sample_pairs_resume_rows_and_is_idempotent(monkeypatch):
    data = json.loads((FIX / "tpex_spendi_history_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    assert halts.fetch_tpex(c) == 4                                     # 4 檔各 1 次停牌（舊版：恢復列被丟掉）
    rows = {r["symbol"]: r["resume_date"] for r in c.execute("SELECT symbol, resume_date FROM trading_halts WHERE is_current=1")}
    assert rows == {"1788": "2026-06-22", "1799": "2026-03-13", "3226": "2026-01-12", "3426": "2026-02-10"}
    halts.fetch_tpex(c)
    assert c.execute("SELECT MAX(version) FROM trading_halts").fetchone()[0] == 1


def test_old_null_resume_rows_get_versioned_resume_date_after_fix(monkeypatch):
    data = json.loads((FIX / "tpex_spendi_history_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    # 舊程式寫入的狀態：有停牌日、沒有恢復日
    for sym, hd in (("1788", "2026-06-18"), ("1799", "2026-03-12")):
        c.execute("INSERT INTO trading_halts(market,symbol,halt_date,halt_time,resume_date,halt_type,source,fetched_at) VALUES('TPEx',?,?,'080000',NULL,'暫停交易','u','f')", (sym, hd))
    c.commit()
    halts.fetch_tpex(c)
    r = c.execute("SELECT version, resume_date FROM trading_halts WHERE symbol='1788' AND is_current=1").fetchone()
    assert (r[0], r[1]) == (2, "2026-06-22")                             # 版本化：舊列保留 is_current=0
    assert c.execute("SELECT COUNT(*) FROM trading_halts WHERE symbol='1788'").fetchone()[0] == 2


def test_tpex_sprchis_history_only_stocks_normalized_time_and_no_duplicates(monkeypatch):
    data = json.loads((FIX / "tpex_sprchis_2024_sample.json").read_text(encoding="utf-8"))
    urls = []
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: (urls.append(u), Resp(data))[1])
    c = _conn()
    r = halts.fetch_tpex_history(c, [2024])
    assert urls == ["https://www.tpex.org.tw/www/zh-tw/bulletin/sprcHis?date=2024&cate=&response=json"]
    assert r["rows_by_year"] == {2024: 8} and r["halts"] == 4 and r["unpaired"] == 0           # 權證／興櫃不收
    x = c.execute("SELECT halt_time, resume_date, resume_time FROM trading_halts WHERE symbol='2718' AND is_current=1").fetchone()
    assert (x[0], x[1], x[2]) == ("080000", "2024-02-22", "080000")                              # '8:00' → '080000'
    halts.fetch_tpex_history(c, [2024])
    assert c.execute("SELECT COUNT(*), MAX(version) FROM trading_halts").fetchone()[:] == (4, 1)


def test_halts_backfill_runs_unified_rule_and_twse_optional(monkeypatch):
    data = json.loads((FIX / "tpex_sprchis_2024_sample.json").read_text(encoding="utf-8"))
    calls = []
    api = json.loads((FIX / "tpex_spendi_history_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: (calls.append(("tpex", u)), Resp(api if "openapi" in u else data))[1])
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: (calls.append(("twse", u)), Resp({"stat": "OK", "data": []}))[1])
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(hb, "today", lambda: date(2026, 11, 17))
    c = _conn()
    out = hb.run_backfill(c, date_from="2023-11-01", twse=False)
    assert [k for k, _ in calls] == ["tpex"] * 5 and out["tpex"]["halts"] > 0 and "twse" not in out      # openapi 1 + 2023～2026 各 1 個請求
    assert out["halt_rule"]["by_basis"]
    calls.clear()
    out = hb.run_backfill(c, date_from="2023-11-01", twse=True)
    assert [k for k, _ in calls].count("twse") == 1 and out["twse"] == 0


# ---------- 下市 ----------
def test_delistings_twse_and_tpex_parse_idempotent_and_overlap_report(monkeypatch):
    tw = [{"DelistingDate": "115/09/01", "Company": "三商壽", "Code": "2867"}, {"DelistingDate": "113/11/19", "Company": "昶虹", "Code": "2443"},
          {"DelistingDate": "090/01/20", "Company": "彥武企業", "Code": "2011"}]
    tp = {"tables": [{"fields": ["股票代號", "公司名稱", "終止上櫃日期", "終止上櫃原因", "網址"],
                      "data": [["8908", "欣雄", "115-10-16", "規則第12條之2", "u"], ["4736", "泰博", "112-12-22", "轉上市", "u"]]}]}
    seen = []

    def fake_tpex(m, u, **k):
        seen.append(u)
        return Resp(tp)

    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(tw))
    monkeypatch.setattr(httputil.tpex, "request", fake_tpex)
    monkeypatch.setattr(db_, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    c = _conn()
    _sec(c, "4736", "TPEx")                                              # 現行清單仍有 4736（轉上市後重新出現）
    c.commit()
    out = db_.run_backfill(c, date_from="2023-11-01")
    assert len(seen) == 4 and out["tpex"]["by_year"] == {2023: 2, 2024: 2, 2025: 2, 2026: 2}
    assert out["twse"] == {"inserted": 3} and c.execute("SELECT COUNT(*) FROM delistings").fetchone()[0] == 5   # 上櫃同一筆被 4 年重複回傳，PK 去重
    assert c.execute("SELECT reason FROM delistings WHERE symbol='8908'").fetchone()[0].startswith("規則")
    assert out["since_from"] == {"TWSE": 2, "TPEx": 2} and out["also_in_current_list"] == ["TPEx:4736:2023-12-22"]
    out2 = db_.run_backfill(c, date_from="2023-11-01")
    assert out2["twse"] == {"unchanged": 3}


def test_universe_includes_only_in_window_delisted_four_digit_codes():
    c = _conn()
    _sec(c, "2330", "TWSE")
    for mkt, sym, d in (("TWSE", "2443", "2024-11-19"), ("TWSE", "2011", "2001-01-20"), ("TPEx", "6288", "2025-08-15"),
                        ("TPEx", "00679B", "2025-01-01"), ("TWSE", "2330", "2024-01-01")):
        c.execute("INSERT INTO delistings(market,symbol,name,delist_date,source,fetched_at) VALUES(?,?,?,?,'u','f')", (mkt, sym, sym, d))
    c.commit()
    assert mp._universe(c) == {"2330": "TWSE"}
    assert mp._universe(c, include_delisted="2023-11-01") == {"2330": "TWSE", "2443": "TWSE", "6288": "TPEx"}


# ---------- --fix-from ----------
def test_revenue_months_since_and_financial_quarters_since(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(rb, "today", lambda: date(2026, 11, 17))
    assert rb.months_since("2023-11-01") == 36
    ms = rb.target_months(rb.months_since("2023-11-01"))
    assert ms[0] == "2026-10" and ms[-1] == "2023-11" and len(ms) == 36          # 2023-11～2026-10 共 36 個月
    assert fb.quarters_since("2023-11-01", 2026, 3)[-1] == (2023, 3) and len(fb.quarters_since("2023-11-01", 2026, 3)) == 13
    assert fb.filing_deadline(2025, 4) == date(2026, 3, 31)


def test_revenue_backfill_fix_from_uses_month_window_and_delisted_universe(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(rb, "today", lambda: date(2026, 11, 17))
    c = _conn()
    for i in range(30):
        _sec(c, f"1{i:03d}", "TWSE")
    c.execute("INSERT INTO delistings(market,symbol,name,delist_date,source,fetched_at) VALUES('TWSE','2443','x','2024-11-19','u','f')")
    c.commit()
    seen = {}
    monkeypatch.setattr(rb.rev, "fetch_month", lambda conn, ym, symset, market: seen.setdefault("syms", set(symset)) and 0)
    out = rb.run_backfill(c, date_from="2023-11-01", max_pages=1)
    assert out["months"][-1] == "2023-11" and out["pages_total"] == 72 and "2443" in seen["syms"]


# ---------- 交易日曆歷史回補 ----------
def test_calendar_backfill_from_fmtqik_st41_only(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(cb, "today", lambda: date(2026, 11, 17))
    monkeypatch.setattr(cal, "today", lambda: date(2026, 11, 17))
    tw, tp = {}, {}

    def fake_twse(m, u, **k):
        assert "holidaySchedule" not in u                                          # 回補模式不打 holidaySchedule
        ym = u.split("date=")[1][:6]
        days = [d for d in ("01", "02", "03", "06") if True]
        return Resp({"stat": "OK", "data": [[f"{int(ym[:4]) - 1911}/{ym[4:]}/{d}", "1", "1"] for d in days if not (ym == "202311" and d == "02")]})

    def fake_tpex(m, u, **k):
        roc = u.split("d=")[1].split("&")[0]
        y, mo = roc.split("/")
        return Resp({"tables": [{"data": [[f"{y}/{mo}/{d}", "1"] for d in ("01", "02", "03", "06") if not (roc == "112/11" and d == "02")]}]})

    monkeypatch.setattr(httputil.twse, "request", fake_twse)
    monkeypatch.setattr(httputil.tpex, "request", fake_tpex)
    c = _conn()
    out = cb.run_backfill(c, date_from="2023-11-01")
    assert out["years"] == [2023, 2024] and not out["failed"]
    r = {x["calendar_date"]: (x["is_trade_day"], x["source_kind"]) for x in c.execute("SELECT calendar_date,is_trade_day,source_kind FROM trading_calendar WHERE calendar_date BETWEEN '2023-10-30' AND '2023-11-07'")}
    assert "2023-10-31" not in r                                                   # 起始日之前不寫
    assert r["2023-11-01"][0] == 1 and r["2023-11-02"] == (0, "market_closed_xref") and r["2023-11-04"] == (0, "weekend_rule")
    assert cal.fetch_and_store.__defaults__ is not None
