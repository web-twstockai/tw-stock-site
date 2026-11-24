"""籌碼（pipeline/fetchers/chips.py、pipeline/chips_backfill.py）：用 logs/chips_probe 已存原始回應（裁切版）測解析、單位、
欄位版本、恆等式、universe 過濾、版本化冪等、檢查點（重跑 0 請求）、擋頁即停該主機、TDCC opendata／qryStock。不送任何真實請求。"""
import json
from pathlib import Path

import pytest
import requests

from pipeline import chips_backfill as cb, db as pdb, httputil
from pipeline.fetchers import chips
from pipeline.tests.test_halt_fix import _sec

FX = Path(__file__).parent / "fixtures" / "chips"
DAYS = ["2023-11-01", "2026-11-23"]
TWSE_SYMS = ["2330", "2317", "1101", "2412"]
TPEX_SYMS = ["6488", "5347", "8299", "3105"]


def _load(name):
    return json.loads((FX / f"{name}.json").read_text(encoding="utf-8"))


def _uni():
    return {**{s: "TWSE" for s in TWSE_SYMS}, **{s: "TPEx" for s in TPEX_SYMS}}


def _conn(days=DAYS):
    c = pdb.connect()
    pdb.init_db(c)
    for s in TWSE_SYMS:
        _sec(c, s, "TWSE")
    for s in TPEX_SYMS:
        _sec(c, s, "TPEx")
    for d in days:
        c.execute("INSERT OR REPLACE INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES(?,1,'t','t','2026-11-24T00:00:00+08:00')", (d,))
    c.commit()
    return c


class FakeResp:
    def __init__(self, body: bytes, status=200):
        self.content = body
        self.status_code = status
        self.text = body.decode("utf-8", errors="replace")
        self.headers = {}
        self.url = ""

    def json(self):
        return json.loads(self.text)


FILE_FOR = {
    ("twse_t86", "2026-11-23"): "twse_T86_20261123", ("twse_t86", "2023-11-01"): "twse_T86_20231101",
    ("tpex_insti", "2026-11-23"): "tpex_insti_20261123", ("tpex_insti", "2023-11-01"): "tpex_insti_20231101",
    ("twse_margin", "2026-11-23"): "twse_MI_MARGN_20261123", ("twse_margin", "2023-11-01"): "twse_MI_MARGN_20231101",
    ("tpex_margin", "2026-11-23"): "tpex_margin_20261123", ("tpex_margin", "2023-11-01"): "tpex_margin_20231101",
}
URL2KEY = {chips.url_for(ds, d): (ds, d) for (ds, d) in FILE_FOR}


class Fake:
    def __init__(self, block_urls=(), html_urls=()):
        self.calls = []
        self.block, self.html = set(block_urls), set(html_urls)

    def __call__(self, client, method, url, **kw):
        self.calls.append(url)
        if url in self.block:
            raise httputil.RateLimited("rate_limited(限流): test", host=chips.host_of(url), status=302)
        if url in self.html:
            return FakeResp("<html>因為安全性考量，您的請求無法呈現</html>".encode())
        return FakeResp((FX / f"{FILE_FOR[URL2KEY[url]]}.json").read_bytes())


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(chips, "CHIPS_INTERVAL", 0.0)


# ---------------------------------------------------------------- 解析
@pytest.mark.parametrize("day", ["20261123", "20231101"])
def test_t86_fields_units_and_identities(day):
    d = _load(f"twse_T86_{day}")
    assert d["fields"] == chips.T86_FIELDS and d["hints"] == "單位：股"      # 2023-11 與 2026-11 欄位完全相同
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    res = chips.parse_t86(d, _uni(), iso, "u")
    assert res["status"] == "ok" and res["identity_fail"] == 0
    syms = {r["symbol"] for r in res["rows"]}
    assert syms <= set(TWSE_SYMS) and "0050" not in syms and "00632R" not in syms    # ETF 不寫
    r = next(r for r in res["rows"] if r["symbol"] == "2330")
    assert r["unit"] == "股" and r["fields_sig"] == "T86_19" and r["source_table"] == "T86"
    assert r["dealer_net"] == r["dealer_self_net"] + r["dealer_hedge_net"]
    assert r["total_net"] == r["foreign_ex_dealer_net"] + r["foreign_dealer_net"] + r["trust_net"] + r["dealer_net"]
    assert r["foreign_total_net"] is None and r["dealer_total_buy"] is None       # TWSE 沒有的欄位不推算
    raw = next(x for x in d["data"] if x[0].strip() == "2330")
    assert r["foreign_ex_dealer_buy"] == int(raw[2].replace(",", "")) and r["total_net"] == int(raw[18].replace(",", ""))


def test_t86_field_change_is_not_written():
    d = _load("twse_T86_20261123")
    d["fields"] = d["fields"][:11] + d["fields"][12:]          # 模擬改版
    res = chips.parse_t86(d, _uni(), "2026-11-23", "u")
    assert res["status"] == "fields_changed" and res["rows"] == []
    d = _load("twse_T86_20261123")
    for x in d["data"]:                                         # 位置錯亂 → 恆等式大量不符 → 不寫
        x[11], x[14] = x[14], x[11]
        x[18] = "999"
    assert chips.parse_t86(d, _uni(), "2026-11-23", "u")["status"] == "fields_changed"
    assert chips.parse_t86({"stat": "很抱歉，沒有符合條件的資料!"}, _uni(), "2026-11-23", "u")["status"] == "no_data"


@pytest.mark.parametrize("day", ["20261123", "20231101"])
def test_tpex_insti_positional_groups(day):
    d = _load(f"tpex_insti_{day}")
    assert d["tables"][0]["fields"] == chips.TPEX_INSTI_FIELDS
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    res = chips.parse_tpex_insti(d, _uni(), iso, "u")
    assert res["status"] == "ok" and res["identity_fail"] == 0
    assert {r["symbol"] for r in res["rows"]} <= set(TPEX_SYMS)
    r = res["rows"][0]
    raw = next(x for x in d["tables"][0]["data"] if x[0] == r["symbol"])
    v = [int(x.replace(",", "")) for x in raw[2:]]
    assert (r["foreign_ex_dealer_net"], r["foreign_total_net"], r["trust_net"], r["dealer_self_net"], r["dealer_hedge_net"], r["dealer_net"],
            r["total_net"]) == (v[2], v[8], v[11], v[14], v[17], v[20], v[21])
    assert r["unit"] == "股" and r["fields_sig"] == "TPEX_INSTI_24"
    assert chips.parse_tpex_insti({"stat": "ok", "tables": [{"totalCount": 0, "data": []}]}, _uni(), iso, "u")["status"] == "no_data"


@pytest.mark.parametrize("day", ["20261123", "20231101"])
def test_margin_both_markets(day):
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    tw = chips.parse_mi_margn(_load(f"twse_MI_MARGN_{day}"), _uni(), iso, "u")
    tp = chips.parse_tpex_margin(_load(f"tpex_margin_{day}"), _uni(), iso, "u")
    assert tw["status"] == tp["status"] == "ok" and tw["identity_fail"] == tp["identity_fail"] == 0
    r = next(r for r in tw["rows"] if r["symbol"] == "2330")
    assert r["unit"] == "交易單位" and r["limit_basis"] == "twse_next_day_limit" and r["margin_finco"] is None
    assert r["margin_balance"] == r["margin_prev_balance"] + r["margin_buy"] - r["margin_sell"] - r["margin_cash_repay"]
    q = tp["rows"][0]
    assert q["unit"] == "張" and q["limit_basis"] == "tpex_limit" and isinstance(q["margin_usage_pct"], float)
    raw = next(x for x in _load(f"tpex_margin_{day}")["tables"][0]["data"] if x[0] == q["symbol"])
    # TPEx 欄序是「券賣、券買」（與 TWSE「買進、賣出」相反）
    assert q["short_sell"] == int(raw[11].replace(",", "")) and q["short_buy"] == int(raw[12].replace(",", ""))


def test_twse_margin_summary_unit_matches_per_stock():
    """實測：個股『今日餘額』加總＝信用交易統計『融資(交易單位)』今日餘額（證明個股表單位是交易單位）。用完整原始回應。"""
    p = Path(chips.__file__).resolve().parents[2] / "logs" / "chips_probe" / "twse_MI_MARGN_20261123.json"
    if not p.exists():
        pytest.skip("完整原始回應不在此副本")
    d = json.loads(p.read_text(encoding="utf-8"))
    tot = sum(int(r[6].replace(",", "")) for r in d["tables"][1]["data"])
    assert tot == int(d["tables"][0]["data"][0][5].replace(",", ""))


# ---------------------------------------------------------------- 回補：寫入、冪等、檢查點、擋頁
def test_backfill_writes_versioned_and_rerun_zero_requests(monkeypatch):
    c = _conn()
    fk = Fake()
    monkeypatch.setattr(chips, "gated_request", fk)
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23", max_dates=1)
    assert out["stopped"] == "max_dates" and len(fk.calls) == 4 and "20261123" in fk.calls[0]   # 由新到舊、每日 4 個請求
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert len(fk.calls) == 8 and out["dates_left"] == 0
    n_inst = c.execute("SELECT COUNT(*) FROM institutional_trades").fetchone()[0]
    n_mg = c.execute("SELECT COUNT(*) FROM margin_balances").fetchone()[0]
    assert n_inst > 0 and n_mg > 0
    row = c.execute("SELECT * FROM institutional_trades WHERE symbol='2330' AND trade_date='2026-11-23'").fetchone()
    assert row["source"].startswith("https://www.twse.com.tw/rwd/zh/fund/T86?date=20261123") and row["announced_at"] is None
    assert row["checksum"] and row["version"] == 1 and row["fetched_at"]
    st = {(r[0], r[1]): r[2] for r in c.execute("SELECT dataset,data_date,status FROM chips_fetch_status")}
    assert set(st.values()) == {"ok"} and len(st) == 8
    # 重跑：0 請求、0 新列
    fk.calls.clear()
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert fk.calls == [] and out["dates_skipped_done"] == 2 and out["tally"]["rows_added"] == {}
    # 強制重抓同一日（同內容）：unchanged，不升版
    c.execute("DELETE FROM chips_fetch_status")
    cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert c.execute("SELECT COUNT(*) FROM institutional_trades WHERE version>1").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM institutional_trades").fetchone()[0] == n_inst
    cov = {(r["dataset"], r["status"]) for r in c.execute("SELECT * FROM chips_coverage")}
    assert ("twse_t86", "ok") in cov


def test_block_stops_host_only_and_sets_cooldown(monkeypatch):
    c = _conn()
    fk = Fake(block_urls={chips.url_for("twse_t86", "2026-11-23")})
    monkeypatch.setattr(chips, "gated_request", fk)
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    twse_calls = [u for u in fk.calls if "twse.com.tw" in u]
    assert twse_calls == [chips.url_for("twse_t86", "2026-11-23")]          # 擋一次就停：之後 0 個 TWSE 請求
    assert len([u for u in fk.calls if "tpex.org.tw" in u]) == 4            # 另一主機照常
    assert "www.twse.com.tw" in out["stopped_hosts"]
    assert c.execute("SELECT status FROM backfill_state WHERE job='chips' AND key='cooldown:www.twse.com.tw'").fetchone()[0] == "blocked"
    # 冷卻期內的下一個作業也不送 TWSE
    fk2 = Fake()
    monkeypatch.setattr(chips, "gated_request", fk2)
    cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert not [u for u in fk2.calls if "twse.com.tw" in u]


def test_security_page_counts_as_block(monkeypatch):
    c = _conn()
    fk = Fake(html_urls={chips.url_for("tpex_insti", "2026-11-23")})
    monkeypatch.setattr(chips, "gated_request", fk)
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert [u for u in fk.calls if "tpex.org.tw" in u] == [chips.url_for("tpex_insti", "2026-11-23")]
    assert c.execute("SELECT status FROM chips_fetch_status WHERE dataset='tpex_insti' AND data_date='2026-11-23'").fetchone()[0] == "blocked"
    assert out["dates_left"] == 2


def test_real_httputil_302_errors_is_single_request(monkeypatch):
    """走真正的 httputil：302→/errors 判限流、不追轉址、不重試（single_shot），該主機熔斷。"""
    c = _conn(days=["2026-11-23"])
    sent = []

    def fake_request(self, method, url, **kw):
        sent.append(url)
        r = requests.Response()
        r.status_code = 302
        r.headers["Location"] = "/errors"
        r.url = url
        r._content = b""
        return r

    monkeypatch.setattr(httputil._NoErrorsRedirectSession, "request", fake_request)
    monkeypatch.setattr(httputil.tpex, "interval", 0.0)
    monkeypatch.setattr(httputil.twse, "interval", 0.0)
    guard = chips.HostGuard(c)
    r = chips.fetch_dataset_day(c, "tpex_insti", "2026-11-23", _uni(), guard)
    assert r["status"] == "blocked" and len(sent) == 1
    r2 = chips.fetch_dataset_day(c, "tpex_margin", "2026-11-23", _uni(), guard)
    assert r2["status"] == "skipped_host" and len(sent) == 1


def test_calendar_missing_is_reported(monkeypatch):
    c = _conn(days=["2026-11-23"])
    monkeypatch.setattr(chips, "gated_request", Fake())
    out = cb.run_backfill(c, date_from="2023-11-01", date_to="2026-11-23")
    assert "calendar_backfill" in out["calendar_missing"]


# ---------------------------------------------------------------- TDCC
def test_tdcc_opendata_csv_wide_row():
    text = (FX / "tdcc_od_1-5_20261120.csv").read_text(encoding="utf-8")
    res = chips.parse_tdcc_csv(text, {"2330": "TWSE", "6488": "TPEx"}, chips.TDCC_OD_URL)
    assert res["status"] == "ok" and res["data_date"] == "2026-11-20" and {r["symbol"] for r in res["rows"]} == {"2330", "6488"}
    r = next(r for r in res["rows"] if r["symbol"] == "2330")
    assert r["holders_15"] == 1469 and r["shares_15"] == 21820989424 and r["pct_15"] == 84.14     # 千張大戶＝級距 15
    assert r["holders_16"] == 7 and r["shares_17"] == 25932370067 and r["pct_17"] == 100.0
    # 實測（全檔 4,130 檔皆成立）：合計股數＝級距 1–15 加總 − 差異數調整（16）
    assert sum(r[f"shares_{i}"] for i in range(1, 16)) - r["shares_16"] == r["shares_17"]
    assert res["page_symbols"] == 5                                                               # ETF／債券不寫，但有計數


def test_tdcc_opendata_weekly_gate(monkeypatch):
    c = _conn()
    calls = []

    def fake(client, method, url, **kw):
        calls.append(url)
        return FakeResp((FX / "tdcc_od_1-5_20261120.csv").read_bytes())

    monkeypatch.setattr(chips, "gated_request", fake)
    monkeypatch.setattr(chips, "today", lambda: __import__("datetime").date(2026, 11, 24))
    g = chips.HostGuard(c)
    r1 = chips.fetch_tdcc_latest(c, {"2330": "TWSE"}, g)
    r2 = chips.fetch_tdcc_latest(c, {"2330": "TWSE"}, g)                  # 最新週距今 4 天 → 不抓
    assert r1["status"] == "ok" and r2["status"] == "skipped_fresh" and len(calls) == 1
    monkeypatch.setattr(chips, "today", lambda: __import__("datetime").date(2026, 11, 28))
    r3 = chips.fetch_tdcc_latest(c, {"2330": "TWSE"}, g)                  # 8 天 → 抓；同一週同內容 → unchanged
    assert len(calls) == 2 and r3["written"]["unchanged"] == 1
    assert c.execute("SELECT COUNT(*) FROM shareholding_distribution").fetchone()[0] == 1


def test_tdcc_qrystock_parse_and_form():
    form = chips.parse_tdcc_form((FX / "tdcc_qryStock_page.html").read_text(encoding="utf-8"))
    assert form["token"] and len(form["dates"]) == 51 and form["dates"][0] == "2026-11-20" and form["dates"][-1] == "2025-11-28"
    html = (FX / "tdcc_qryStock_2330_20251128.html").read_text(encoding="utf-8")
    res = chips.parse_tdcc_qry(html, "2330", "2025-11-28", "TWSE", chips.TDCC_QRY_URL)
    r = res["row"]
    assert res["status"] == "ok" and r["holders_1"] == 1385135 and r["shares_15"] == 22431959368 and r["pct_15"] == 86.5
    assert r["holders_16"] is None and r["shares_17"] == 25932615521 and r["source_table"] == "tdcc_qryStock"
    assert chips.parse_tdcc_qry(html, "2330", "2025-12-05", "TWSE", "u")["status"] == "error"        # 資料日不符
    assert chips.parse_tdcc_qry("<span>查無此資料</span>", "2330", "2025-12-05", "TWSE", "u")["status"] == "no_data"


def test_tdcc_backfill_session_checkpoint_and_coverage(monkeypatch):
    c = _conn()
    page = (FX / "tdcc_qryStock_page.html").read_bytes()
    res_html = (FX / "tdcc_qryStock_2330_20251128.html").read_text(encoding="utf-8")
    calls = []

    def fake(client, method, url, **kw):
        calls.append((method, (kw.get("data") or {}).get("stockNo"), (kw.get("data") or {}).get("scaDate")))
        if method == "GET":
            return FakeResp(page)
        d = kw["data"]["scaDate"]
        body = res_html.replace("114年11月28日", f"{int(d[:4]) - 1911}年{int(d[4:6])}月{int(d[6:])}日")
        return FakeResp(body.encode())

    monkeypatch.setattr(chips, "gated_request", fake)
    monkeypatch.setattr(cb.config, "SAMPLE30", ["2330", "6488"])
    out = cb.run_tdcc_backfill(c, scope="sample30", date_from="2023-11-01", max_dates=2)
    assert out["oldest_available"] == "2025-11-28" and out["available_weeks"] == 51 and out["target_weeks"] == 51
    assert out["queried"] == 4 and out["stopped"] == "max_dates" and calls[0][0] == "GET"
    assert sum(1 for m, *_ in calls if m == "GET") == 1          # 權杖由回應頁接續，不必每次 GET
    n = len(calls)
    out2 = cb.run_tdcc_backfill(c, scope="sample30", date_from="2023-11-01", max_dates=2)   # 已有的週不再查
    assert out2["weeks_done"] == 2 and out2["queried"] == 4 and len(calls) == n + 1 + 4
    cov = {(r["dataset"], r["status"]): r for r in c.execute("SELECT * FROM chips_coverage WHERE dataset='tdcc_dist'")}
    gap = cov[("tdcc_dist", "資料不足")]
    assert gap["range_start"] == "2023-11-01" and gap["range_end"] == "2025-11-27"


def test_schema_migration_idempotent_and_no_reversion():
    c = pdb.connect()
    pdb.init_db(c)
    pdb.init_db(c)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"institutional_trades", "margin_balances", "shareholding_distribution", "chips_fetch_status", "chips_coverage"} <= names


def test_evening_fetch_recent_requests(monkeypatch):
    """evening：最近 3 個交易日未完成者各 1 個請求＋TDCC 每週 1 個；第二次 0 請求。"""
    import datetime as dt
    c = _conn(days=["2026-11-23"])
    calls = []
    fk = Fake()

    def fake(client, method, url, **kw):
        calls.append(url)
        if "tdcc" in url:
            return FakeResp((FX / "tdcc_od_1-5_20261120.csv").read_bytes())
        return fk(client, method, url, **kw)

    monkeypatch.setattr(chips, "gated_request", fake)
    monkeypatch.setattr(chips, "today", lambda: dt.date(2026, 11, 24))
    r = chips.fetch_recent(c, days=3)
    assert len(calls) == 5 and r["dates"]["2026-11-23"] == {ds: "ok" for ds in chips.DAILY_DATASETS} and r["tdcc"]["status"] == "ok"
    calls.clear()
    r = chips.fetch_recent(c, days=3)
    assert calls == [] and r["tdcc"]["status"] == "skipped_fresh"


@pytest.mark.parametrize("job,expect", [("evening", 1), ("daily", 0)])
def test_run_job_hooks_chips_only_in_evening(monkeypatch, job, expect):
    """evening 會呼叫籌碼增量（chips.fetch_recent）；daily 不加任何籌碼請求。其他步驟一律不連網。"""
    from pipeline import run as prun
    from pipeline.tests.test_skip_isin import _add
    monkeypatch.setenv("PIPELINE_SKIP_ISIN", "1")
    monkeypatch.setattr(prun.calendar, "fetch_and_store", lambda conn: None)
    monkeypatch.setattr(prun, "assert_no_other_pipeline", lambda: None)
    monkeypatch.setattr(prun, "setup_logging", lambda: None)
    bomb = type("X", (), {"__getattr__": lambda self, k: (lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no net")))})()
    for mod in ("securities", "prices", "corporate_actions", "halts", "indices", "revenue", "financials", "material", "market_prices", "material_batch"):
        monkeypatch.setattr(prun, mod, bomb)
    monkeypatch.setattr(prun.analysis_bridge, "run_screen_and_write", lambda *a, **k: {"ok": True})
    called = []
    monkeypatch.setattr(chips, "fetch_recent", lambda conn, **kw: called.append(kw) or {"requests_by_host": {}, "stopped_hosts": {}, "tdcc": {"status": "skipped_fresh"}})
    c = pdb.connect()
    pdb.init_db(c)
    _add(c, 900, 700)
    c.close()
    prun.run_job(job, "sample30")
    assert len(called) == expect
    c = pdb.connect()
    items = c.execute("SELECT success_items FROM job_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
    assert ("chips:" in items) == bool(expect)
