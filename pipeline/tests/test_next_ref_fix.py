"""next_ref_fix：上櫃從 dailyQuotes 就地回填次日參考價、上市標示來源無此欄、冪等、限流即停。"""
import pytest

from pipeline import db as pdb, httputil, next_ref_fix as nr
from pipeline.fetchers import prices as pr
from pipeline.httputil import RateLimited
from pipeline.tests.test_halt_fix import _row, _sec
from pipeline.tests.test_tpex_volume import FakeResp, FIELDS


def _dq(rows):
    data = []
    for c, ref in rows:
        rec = [c, c, "100.00", "0.00", "99", "101", "98", "100", "1,000", "100,000", "5", "100", "1", "101", "1", "1000", ref, "110.00", "90.00"]
        data.append(rec)
    return {"stat": "ok", "tables": [{"title": "上櫃股票行情", "fields": FIELDS, "data": data}]}


def _conn():
    c = pdb.connect()
    pdb.init_db(c)
    return c


class Fake:
    def __init__(self, by_date, limit_on=None):
        self.by_date, self.limit_on, self.calls = by_date, limit_on, []

    def __call__(self, method, url, **kw):
        self.calls.append(url)
        if self.limit_on and self.limit_on in url:
            raise RateLimited("rate_limited(限流): x", host="h", retry_after_s=900, status=302)
        iso = url.split("date=")[1].split("&")[0].replace("/", "-")
        return FakeResp(_dq(self.by_date[iso]))


def _seed(c):
    for d in ("2026-11-12", "2026-11-13"):
        for s in ("6488", "5347"):
            r = _row(s, "TPEx", d, 100.0, src="tradingStock")
            r["volume_basis"] = "excl_block"
            r["announced_at_note"] = "來源無公告時間；次日參考價：來源無此欄（tradingStock 無此欄，見 dailyQuotes）"
            pr._upsert_price(c, r)
    pr._upsert_price(c, _row("2330", "TWSE", "2026-11-13", 900.0, src="MI_INDEX"))
    c.commit()


def test_fill_in_place_note_idempotent_and_twse_annotation(monkeypatch):
    c = _conn()
    _seed(c)
    fake = Fake({"2026-11-12": [("6488", "101.50"), ("5347", "49.30")], "2026-11-13": [("6488", "102.00"), ("5347", "")]})
    monkeypatch.setattr(httputil.tpex, "request", fake)
    out = nr.run_fix(c)
    assert out["requests"] == 2 and out["filled"] == 3 and out["none_official"] == 1 and out["dates_left"] == 0 and not out["failed"]
    r = c.execute("SELECT * FROM daily_prices WHERE symbol='6488' AND price_date='2026-11-12' AND is_current=1").fetchone()
    assert r["next_ref_price_twd"] == 101.5 and r["version"] == 1 and r["source_table"] == "tradingStock" and "回填" in r["announced_at_note"]
    r0 = c.execute("SELECT * FROM daily_prices WHERE symbol='5347' AND price_date='2026-11-13' AND is_current=1").fetchone()
    assert r0["next_ref_price_twd"] is None and "官方 dailyQuotes 該日無此值" in r0["announced_at_note"]
    t = c.execute("SELECT announced_at_note, next_ref_price_twd FROM daily_prices WHERE symbol='2330'").fetchone()
    assert t[1] is None and "來源無此欄（TWSE" in t[0]
    # checksum 與內容一致：同內容再 upsert 不會升版
    from pipeline.db import checksum
    from pipeline.fetchers.prices import CONTENT_KEYS
    assert r["checksum"] == checksum({k: r[k] for k in r.keys()}, CONTENT_KEYS)
    # 冪等：再跑不再請求、不改任何資料
    fake.calls.clear()
    snap = c.execute("SELECT symbol,price_date,next_ref_price_twd,announced_at_note,version,checksum FROM daily_prices ORDER BY 1,2,5").fetchall()
    out2 = nr.run_fix(c)
    assert not fake.calls and out2["requests"] == 0 and out2["twse_annotated_rows"] == 0
    assert c.execute("SELECT symbol,price_date,next_ref_price_twd,announced_at_note,version,checksum FROM daily_prices ORDER BY 1,2,5").fetchall() == snap


def test_max_dates_chunking_and_resume(monkeypatch):
    c = _conn()
    _seed(c)
    fake = Fake({"2026-11-12": [("6488", "1"), ("5347", "2")], "2026-11-13": [("6488", "3"), ("5347", "4")]})
    monkeypatch.setattr(httputil.tpex, "request", fake)
    o1 = nr.run_fix(c, max_dates=1)
    assert o1["requests"] == 1 and o1["dates_left"] == 1
    o2 = nr.run_fix(c, max_dates=1)
    assert o2["requests"] == 1 and o2["dates_left"] == 0 and len(fake.calls) == 2


def test_rate_limit_stops_immediately(monkeypatch):
    c = _conn()
    _seed(c)
    fake = Fake({}, limit_on="2026/11/12")
    monkeypatch.setattr(httputil.tpex, "request", fake)
    out = nr.run_fix(c)
    assert out["rate_limited"] and len(fake.calls) == 1 and out["dates_left"] == 2 and out["filled"] == 0


def test_cooperates_with_tpex_volume_fix_rows_that_already_have_next_ref(monkeypatch):
    """tpex_volume_fix 先跑（dailyQuotes 列已含次日參考價）→ 本作業無事可做、不請求。"""
    c = _conn()
    r = _row("6488", "TPEx", "2026-11-13", 100.0)
    r["next_ref_price_twd"] = 100.5
    pr._upsert_price(c, r)
    c.commit()
    fake = Fake({})
    monkeypatch.setattr(httputil.tpex, "request", fake)
    out = nr.run_fix(c)
    assert out["todo_dates"] == 0 and not fake.calls
