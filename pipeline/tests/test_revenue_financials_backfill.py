"""revenue_backfill（scope 修正、max-pages、續跑冪等、連續 3xx 停、限流停）與 financials_backfill
（financial_fetch_status、頁級失敗重試、連續 3 次 307 就停、max-pages、續跑）。假回應，不送請求。"""
from datetime import date, datetime

import pytest

from pipeline import config, dates, db as pdb, financials_backfill as fb, httputil, revenue_backfill as rb
from pipeline.fetchers import financials as fin, revenue as rev
from pipeline.httputil import FetchError, RateLimited
from pipeline.tests.test_halt_fix import _sec

TODAY = date(2026, 11, 17)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(dates, "today", lambda: TODAY)
    monkeypatch.setattr(rb, "today", lambda: TODAY)
    monkeypatch.setattr(httputil.time, "sleep", lambda s: None)


def _conn(n=105):
    c = pdb.connect()
    pdb.init_db(c)
    for i in range(n):
        _sec(c, f"1{i:03d}", "TWSE")
        _sec(c, f"6{i:03d}", "TPEx")
    c.commit()
    return c


class Resp:
    def __init__(self, content: bytes, status=200):
        self.content, self.status_code, self.headers = content, status, {"content-type": "text/html"}
        self.text = content.decode("utf-8", errors="replace")


def _nas(prefix: str, n=105) -> bytes:
    rows = "".join(f"<tr><td>{prefix}{i:03d}</td><td>公司{i}</td><td>1,000</td><td>900</td><td>800</td><td>11.1</td><td>25.0</td><td>9,000</td><td>8,000</td><td>12.5</td><td>備註</td></tr>"
                   for i in range(n))
    return ("<html><body>t21sc03 公司代號<table>" + rows + "</table></body></html>").encode("cp950")


class FakeMops:
    def __init__(self, fail_after=None, redirect_all=False, rate_limit=False):
        self.calls, self.redirect_all, self.rate_limit = [], redirect_all, rate_limit

    def __call__(self, method, url, **kw):
        self.calls.append(url)
        if self.rate_limit:
            raise RateLimited("rate_limited(限流): http=429", host="mopsov.twse.com.tw", retry_after_s=60)
        if self.redirect_all:
            raise FetchError(f"redirect/http=307 loc=/x url={url}", status=307)
        return Resp(_nas("1" if "/sii/" in url else "6"))


def _status(c, market, ym):
    return c.execute("SELECT status, scope, page_rows, row_count FROM revenue_fetch_status WHERE market=? AND revenue_month=?", (market, ym)).fetchone()


def test_target_months_24_skips_current_month():
    ms = rb.target_months(24)
    assert len(ms) == 24 and ms[0] == "2026-10" and ms[-1] == "2024-11"


def test_old_sample30_ok_is_not_done_and_all_scope_page_is_fetched_then_skipped(monkeypatch):
    c = _conn()
    # 舊資料：sample30 過濾後的 ok（scope NULL）、與另一頁 scope=sample30 → 都不可當全市場完成
    c.execute("INSERT INTO revenue_fetch_status(market,revenue_month,status,http_status,row_count,source,fetched_at) VALUES('TWSE','2026-10','ok',200,28,'u','2026-11-12T10:00:00+08:00')")
    c.execute("INSERT INTO revenue_fetch_status(market,revenue_month,status,http_status,row_count,source,fetched_at,scope) VALUES('TPEx','2026-10','ok',200,30,'u','2026-11-12T10:00:00+08:00','sample30')")
    c.commit()
    assert not rb.page_done(c, "TWSE", "2026-10") and not rb.page_done(c, "TPEx", "2026-10")
    fk = FakeMops()
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = rb.run_backfill(c, months=1)
    assert out["fetched"] == 2 and len(fk.calls) == 2 and out["rows"] == 210 and out["pages_left"] == 0
    st = _status(c, "TWSE", "2026-10")
    assert (st["status"], st["scope"], st["page_rows"], st["row_count"]) == ("ok", "all", 105, 105)
    assert c.execute("SELECT COUNT(*) FROM monthly_revenues WHERE revenue_month='2026-10' AND is_current=1").fetchone()[0] == 210
    out2 = rb.run_backfill(c, months=1)                                  # 冪等續跑：0 請求
    assert out2["fetched"] == 0 and out2["skipped_done"] == 2 and len(fk.calls) == 2


def test_done_requires_fetch_after_announce_deadline(monkeypatch):
    c = _conn()
    # 2026-10 的公告期限是 11/10：11/05 抓到的 ok（公告期內可能不完整）不算完成
    c.execute("INSERT INTO revenue_fetch_status(market,revenue_month,status,http_status,row_count,source,fetched_at,scope,page_rows) VALUES('TWSE','2026-10','ok',200,60,'u','2026-11-05T10:00:00+08:00','all',60)")
    c.execute("INSERT INTO revenue_fetch_status(market,revenue_month,status,http_status,row_count,source,fetched_at,scope,page_rows) VALUES('TWSE','2026-09','ok',200,900,'u','2026-11-12T10:00:00+08:00','all',900)")
    c.commit()
    assert not rb.page_done(c, "TWSE", "2026-10") and rb.page_done(c, "TWSE", "2026-09")


def test_sample30_run_writes_scope_sample30(monkeypatch):
    c = _conn(n=20)                                                     # 現行上市 20 檔；只過濾其中 10 檔
    monkeypatch.setattr(httputil.mops, "request", FakeMops())
    n = rev.fetch_month(c, "2026-10", {f"1{i:03d}" for i in range(10)}, "TWSE")      # 非追蹤清單的 10 檔 → subset（不是 sample30、更不是 all）
    st = _status(c, "TWSE", "2026-10")
    assert n == 10 and st["scope"] == "subset" and st["page_rows"] == 105
    assert not rb.page_done(c, "TWSE", "2026-10")
    n = rev.fetch_month(c, "2026-10", {"2330", "2317"}, "TWSE")                      # 追蹤清單內 → sample30
    assert _status(c, "TWSE", "2026-10")["scope"] == "sample30"
    assert not rb.page_done(c, "TWSE", "2026-10")


def test_big_filter_not_covering_universe_is_subset_not_all(monkeypatch):
    c = _conn(n=150)                                                    # 現行普通股 300 檔（上市 150）
    monkeypatch.setattr(httputil.mops, "request", FakeMops())
    rev.fetch_month(c, "2026-10", {f"1{i:03d}" for i in range(120)}, "TWSE")        # 120 檔（>100）但沒涵蓋上市全部 150 檔
    assert _status(c, "TWSE", "2026-10")["scope"] == "subset" and not rb.page_done(c, "TWSE", "2026-10")
    rev.fetch_month(c, "2026-10", {f"1{i:03d}" for i in range(150)}, "TWSE")        # 涵蓋全部現行上市普通股 → all
    assert _status(c, "TWSE", "2026-10")["scope"] == "all"


def test_max_pages_limits_requests_and_resumes(monkeypatch):
    c = _conn()
    fk = FakeMops()
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = rb.run_backfill(c, months=3, max_pages=2)
    assert len(fk.calls) == 2 and out["stopped"] == "max_pages" and out["pages_left"] == 4
    out = rb.run_backfill(c, months=3, max_pages=10)
    assert len(fk.calls) == 6 and out["pages_left"] == 0 and not out["stopped"]


def test_three_consecutive_redirects_stop_and_rate_limit_stops(monkeypatch):
    c = _conn()
    fk = FakeMops(redirect_all=True)
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = rb.run_backfill(c, months=6)
    assert len(fk.calls) == config.REDIRECT_STREAK_STOP and out["stopped"] == "consecutive_redirects:3" and len(out["failed"]) == 3
    assert _status(c, "TWSE", "2026-10")["status"] == "fail"
    fk2 = FakeMops(rate_limit=True)
    monkeypatch.setattr(httputil.mops, "request", fk2)
    out = rb.run_backfill(c, months=6)
    assert len(fk2.calls) == 1 and out["stopped"] == "rate_limited"


# ---------------- financials ----------------
def _cf_html(prefix, n=105):
    rows = "".join(f"<tr><td>{prefix}{i:03d}</td><td>公司</td><td>100</td><td>-50</td><td>-20</td><td>1</td><td>31</td><td>500</td><td>531</td></tr>" for i in range(n))
    return f"<html><body><table>{rows}</table></body></html>".encode("utf-8")


def _inc_html(prefix, n=105):
    head = "<tr><td>公司代號</td><td>公司名稱</td><td>營業收入</td><td>營業毛利（毛損）</td><td>營業利益（損失）</td><td>基本每股盈餘（元）</td></tr>"
    rows = "".join(f"<tr><td>{prefix}{i:03d}</td><td>公司</td><td>1,000</td><td>300</td><td>100</td><td>1.5</td></tr>" for i in range(n))
    return f"<html><body><table>{head}{rows}</table></body></html>".encode("utf-8")


class FakeFin:
    def __init__(self, script=None, always_redirect=False):
        self.calls, self.script, self.always = [], list(script or []), always_redirect

    def __call__(self, method, url, data=None, **kw):
        key = (url.rsplit("_", 1)[-1], data["TYPEK"], data["year"], data["season"])
        self.calls.append(key)
        if self.always or (self.script and self.script.pop(0) == "307"):
            raise FetchError(f"redirect/http=307 loc=/x url={url}", status=307)
        pre = "1" if data["TYPEK"] == "sii" else "6"
        return Resp(_inc_html(pre) if "t163sb04" in url else _cf_html(pre))


@pytest.fixture
def _q(monkeypatch):
    monkeypatch.setattr(fin, "_latest_filed_quarter", lambda: (2026, 2))


def test_financials_pages_status_rows_and_resume(monkeypatch, _q):
    c = _conn()
    fk = FakeFin()
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = fb.run_backfill(c, quarters=1)
    assert len(fk.calls) == 4 and out["pages_left"] == 0 and not out["failed"]
    assert out["rows"] == {"income": 210, "cashflow": 210}
    rows = c.execute("SELECT market,table_kind,status,scope,row_count,attempts FROM financial_fetch_status ORDER BY 1,2").fetchall()
    assert [tuple(r) for r in rows] == [("TPEx", "cashflow", "ok", "all", 105, 1), ("TPEx", "income", "ok", "all", 105, 1),
                                         ("TWSE", "cashflow", "ok", "all", 105, 1), ("TWSE", "income", "ok", "all", 105, 1)]
    out2 = fb.run_backfill(c, quarters=1)                                 # 續跑：已完成的頁不再請求
    assert out2["fetched"] == 0 and out2["skipped_done"] == 4 and len(fk.calls) == 4


def test_financials_max_pages_and_sample30_status_not_reused(monkeypatch, _q):
    c = _conn()
    # 舊的 sample30 範圍 ok 不可視為全市場完成
    c.execute("INSERT INTO financial_fetch_status(market,fiscal_year,fiscal_quarter,table_kind,status,row_count,scope,attempts,source,fetched_at) VALUES('TWSE',2026,2,'income','ok',28,'sample30',1,'u','f')")
    c.commit()
    fk = FakeFin()
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = fb.run_backfill(c, quarters=2, max_pages=3)
    assert len(fk.calls) == 3 and out["stopped"] == "max_pages" and out["pages_left"] == 5
    assert ("t163sb04", "sii", "115", "02") in fk.calls                        # sample30 的 ok 被重抓為 all
    assert c.execute("SELECT scope FROM financial_fetch_status WHERE market='TWSE' AND table_kind='income' AND fiscal_quarter=2").fetchone()[0] == "all"


def test_financials_page_level_retry_recovers_one_failed_page(monkeypatch, _q):
    c = _conn()
    fk = FakeFin(script=["ok", "307", "ok", "ok"])                        # 第 2 頁第一次 307，其餘成功
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = fb.run_backfill(c, quarters=1)
    assert out["retried"] == 1 and out["retry_ok"] == 1 and out["failed"] == [] and out["pages_left"] == 0
    assert len(fk.calls) == 5
    assert max(r[0] for r in c.execute("SELECT attempts FROM financial_fetch_status")) == 2
    assert {r[0] for r in c.execute("SELECT status FROM financial_fetch_status")} == {"ok"}


def test_financials_three_consecutive_307_stop(monkeypatch, _q):
    c = _conn()
    fk = FakeFin(always_redirect=True)
    monkeypatch.setattr(httputil.mops, "request", fk)
    out = fb.run_backfill(c, quarters=4)
    assert len(fk.calls) == config.REDIRECT_STREAK_STOP and out["stopped"] == "consecutive_redirects:3"
    assert out["retried"] == 0 and out["pages_left"] == 16               # 停止後不再重試、不再送請求
    assert {r[0] for r in c.execute("SELECT status FROM financial_fetch_status")} == {"fail"}


def test_financials_rate_limited_stops_immediately(monkeypatch, _q):
    c = _conn()

    def rl(*a, **k):
        raise RateLimited("rate_limited(限流): http=429", host="mopsov.twse.com.tw", retry_after_s=60)

    monkeypatch.setattr(httputil.mops, "request", rl)
    out = fb.run_backfill(c, quarters=1)
    assert out["stopped"].startswith("rate_limited") and out["fetched"] == 0


def test_evening_all_mode_fetch_all_uses_only_recent_quarters(monkeypatch, _q):
    c = _conn()
    fk = FakeFin()
    monkeypatch.setattr(httputil.mops, "request", fk)
    syms = [r[0] for r in c.execute("SELECT symbol FROM securities")]
    r = fin.fetch_all(c, syms, quarters=config.ALL_EVENING_FINANCIAL_QUARTERS)
    assert r["pages"] == 8 and len(fk.calls) == 8 and r["income"] == 420 and r["cf"] == 420


@pytest.mark.parametrize("d,expect", [(date(2026, 11, 17), (2026, 3)), (date(2026, 11, 14), (2026, 2)), (date(2026, 8, 15), (2026, 2)),
                                      (date(2026, 8, 14), (2026, 1)), (date(2026, 5, 16), (2026, 1)), (date(2026, 4, 1), (2025, 4)),
                                      (date(2026, 3, 31), (2025, 3)), (date(2026, 1, 5), (2025, 3))])
def test_latest_filed_quarter_follows_filing_deadlines(monkeypatch, d, expect):
    monkeypatch.setattr(fin, "today", lambda: d)
    assert fin._latest_filed_quarter() == expect
