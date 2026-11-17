"""market_backfill：由新到舊、DB 檢查點（逐市場略過）、max-dates、預算、限流／連續錯誤即停、no_data、冪等；
fetch_market_prices(use_db_state=True) 只抓還缺的市場。（皆以假的 fetch 函式，不送任何請求。）"""
from pipeline import db as pdb, market_backfill as mb
from pipeline.fetchers import market_prices as mp, prices as pr
from pipeline.tests.test_halt_fix import _row, _sec

DAYS = ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]   # 皆為交易日


def _conn(n_twse=10, n_tpex=10):
    c = pdb.connect()
    pdb.init_db(c)
    for i in range(n_twse):
        _sec(c, f"1{i:03d}", "TWSE")
    for i in range(n_tpex):
        _sec(c, f"6{i:03d}", "TPEx")
    for d in DAYS:
        c.execute("INSERT OR REPLACE INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES(?,1,'t','t','2026-11-17T00:00:00+08:00')", (d,))
    c.commit()
    return c


class Fakes:
    """取代 fetch_twse_day／fetch_tpex_day：寫入該市場所有股票的一列，並記錄呼叫。"""

    def __init__(self, c, fail=None, rate_limit_tpex_on=None, no_data_twse=()):
        self.c, self.calls, self.fail = c, [], fail or {}
        self.rl, self.no_data = rate_limit_tpex_on, set(no_data_twse)

    def _write(self, mkt, iso):
        syms = [r[0] for r in self.c.execute("SELECT symbol FROM securities WHERE market=? AND is_current=1", (mkt,))]
        for s in syms:
            pr._upsert_price(self.c, _row(s, mkt, iso, 10.0, src="MI_INDEX" if mkt == "TWSE" else "dailyQuotes"))
        self.c.commit()
        return len(syms)

    def twse(self, conn, iso, universe, events, **kw):
        self.calls.append(("TWSE", iso))
        if iso in self.no_data:
            return {"twse": 0, "page_twse": 0, "failed": [f"TWSE:{iso}:很抱歉，沒有符合條件的資料!"], "blocked": False, "no_data": True}
        if self.fail.get(("TWSE", iso)):
            return {"twse": 0, "page_twse": 0, "failed": [f"TWSE:{iso}:blocked"], "blocked": True}
        n = self._write("TWSE", iso)
        return {"twse": n, "page_twse": n, "failed": [], "blocked": False}

    def tpex(self, conn, iso, universe, events, **kw):
        self.calls.append(("TPEx", iso))
        if self.rl == iso:
            return {"tpex": 0, "page_tpex": 0, "failed": [f"TPEx:{iso}:rate_limited"], "rate_limited": True}
        n = self._write("TPEx", iso)
        return {"tpex": n, "page_tpex": n, "failed": [], "rate_limited": False}


def _patch(monkeypatch, fk):
    monkeypatch.setattr(mb, "fetch_twse_day", fk.twse)
    monkeypatch.setattr(mb, "fetch_tpex_day", fk.tpex)
    monkeypatch.setattr(mb.tpex_block, "get_context", lambda conn, **k: type("C", (), {"requests": 0})())


def test_newest_first_max_dates_resume_and_idempotent(monkeypatch):
    c = _conn()
    fk = Fakes(c)
    _patch(monkeypatch, fk)
    out = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13", max_dates=2)
    assert [d for m, d in fk.calls if m == "TWSE"] == ["2026-11-13", "2026-11-12"]       # 由新到舊
    assert out["dates_done"] == ["2026-11-13", "2026-11-12"] and out["dates_left"] == 2 and out["stopped"] == "max_dates"
    assert out["oldest_left"] == "2026-11-10" and out["tally"]["rows_added"]["daily_prices"] == 40
    # 續跑：已完成的日期不再請求
    fk.calls.clear()
    out2 = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13")
    assert [d for m, d in fk.calls if m == "TWSE"] == ["2026-11-11", "2026-11-10"] and out2["dates_left"] == 0
    assert out2["dates_skipped_done"] == 2 and out2["stopped"] is None
    # 冪等：全部完成後重跑 0 請求、0 新列
    fk.calls.clear()
    out3 = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13")
    assert fk.calls == [] and out3["dates_skipped_done"] == 4 and out3["tally"]["rows_added"] == {}
    assert c.execute("SELECT COUNT(*) FROM daily_prices").fetchone()[0] == 80 and c.execute("SELECT COUNT(*) FROM daily_prices WHERE version>1").fetchone()[0] == 0
    assert {r[0] for r in c.execute("SELECT key FROM backfill_state WHERE job='market_backfill' AND status='done'")} == set(DAYS)


def test_per_market_resume_only_fetches_missing_market(monkeypatch):
    c = _conn()
    fk = Fakes(c)
    _patch(monkeypatch, fk)
    fk._write("TWSE", "2026-11-13")                      # 上市當日已完整，上櫃缺
    out = mb.run_backfill(c, date_from="2026-11-13", date_to="2026-11-13")
    assert fk.calls == [("TPEx", "2026-11-13")] and out["dates_left"] == 0


def test_budget_expired_stops_and_marks_partial(monkeypatch):
    c = _conn()
    fk = Fakes(c)
    _patch(monkeypatch, fk)
    out = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13", budget_min=1e-9)
    assert out["stopped"] == "budget" and fk.calls == [] and out["dates_left"] == 4
    assert mb.main_job.__name__ == "main_job"


def test_tpex_rate_limit_stops(monkeypatch):
    c = _conn()
    fk = Fakes(c, rate_limit_tpex_on="2026-11-12")
    _patch(monkeypatch, fk)
    out = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13")
    assert out["rate_limited"] and out["stopped"] == "rate_limited:TPEx"
    assert ("TWSE", "2026-11-11") not in fk.calls                       # 限流後不再送任何請求
    assert out["dates_done"] == ["2026-11-13"] and out["dates_left"] == 3


def test_consecutive_errors_stop(monkeypatch):
    c = _conn()
    fk = Fakes(c, fail={("TWSE", d): True for d in DAYS})
    _patch(monkeypatch, fk)
    out = mb.run_backfill(c, date_from="2026-11-10", date_to="2026-11-13")
    assert out["stopped"] == "consecutive_errors:TWSE:3"                 # 上市連 3 天擋頁就停（上櫃成功不會重置上市的計數）
    assert sum(1 for m, _ in fk.calls if m == "TWSE") == 3 and out["dates_left"] == 4 and out["dates_done"] == []
    assert len(out["dates_failed"]) == 3


def test_no_data_date_not_retried_after_two_attempts(monkeypatch):
    c = _conn()
    fk = Fakes(c, no_data_twse=["2026-11-12"])
    _patch(monkeypatch, fk)
    for _ in range(3):
        mb.run_backfill(c, date_from="2026-11-12", date_to="2026-11-12")
    assert [x for x in fk.calls if x == ("TWSE", "2026-11-12")] == [("TWSE", "2026-11-12")] * 2   # 第 3 次起不再請求
    out = mb.run_backfill(c, date_from="2026-11-12", date_to="2026-11-12")
    assert out["dates_no_data"] == ["2026-11-12"] and out["stopped"] is None


def test_fetch_market_prices_use_db_state_only_fetches_missing_market(monkeypatch):
    c = _conn()
    fk = Fakes(c)
    monkeypatch.setattr(mp, "fetch_twse_day", fk.twse)
    monkeypatch.setattr(mp, "fetch_tpex_day", fk.tpex)
    monkeypatch.setattr(mp, "today", lambda: __import__("datetime").date(2026, 11, 14))
    fk._write("TWSE", "2026-11-13")                      # 11-13 上市已完整
    st = mp.fetch_market_prices(c, trade_days=2, use_db_state=True)
    assert ("TWSE", "2026-11-13") not in fk.calls and ("TPEx", "2026-11-13") in fk.calls
    assert ("TWSE", "2026-11-12") in fk.calls and ("TPEx", "2026-11-12") in fk.calls
    fk.calls.clear()
    st2 = mp.fetch_market_prices(c, trade_days=2, use_db_state=True)    # 全部完成 → 0 請求
    assert fk.calls == [] and st2["skipped_done"] == 2
