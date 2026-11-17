"""corp_backfill：區間查詢解析（用官方真實回應截取）、冪等、擋頁規則（每端點 1 次請求、被擋即停該主機、60 分鐘冷卻）、
MOPS 配股補強分批（--max-enrich）與連續失敗停止。全部假回應，不送請求。"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from pipeline import corp_backfill as cb, config, db as pdb, httputil
from pipeline.dates import TZ
from pipeline.fetchers import corporate_actions as ca
from pipeline.httputil import FetchError, RateLimited
from pipeline.runstats import state_get, state_set

FIX = Path(__file__).parent / "fixtures"


class Resp:
    def __init__(self, data):
        self._d, self.status_code, self.headers, self.content, self.text = data, 200, {}, b"x", "x"

    def json(self):
        return self._d


def _conn():
    c = pdb.connect()
    pdb.init_db(c)
    return c


def _n(c, where="1=1"):
    return c.execute(f"SELECT COUNT(*) FROM corporate_actions WHERE is_current=1 AND {where}").fetchone()[0]


def test_twse_twt49u_real_sample_parses_and_is_idempotent(monkeypatch):
    data = json.loads((FIX / "twse_twt49u_sample.json").read_text(encoding="utf-8"))
    urls = []
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: (urls.append(u), Resp(data))[1])
    c = _conn()
    n = ca.fetch_twse_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    assert n == len(data["data"]) == 9 and len(urls) == 1 and "TWT49U" in urls[0] and "startDate=20260302" in urls[0]
    types = {r[0] for r in c.execute("SELECT action_type FROM corporate_actions WHERE market='TWSE'")}
    assert types == {"ex_dividend", "ex_both", "ex_rights"}
    assert _n(c, "market='TWSE' AND stock_dividend_per_thousand IS NULL") == n            # TWT49U 沒有配股欄：留給 MOPS 補強
    kinds = {r[0] for r in c.execute("SELECT action_kind FROM corporate_actions_coverage WHERE market='TWSE' AND coverage_start='2026-03-02' AND coverage_end='2026-11-17'")}
    assert {"cash_dividend", "stock_dividend", "ex_both"} <= kinds                         # 區間涵蓋範圍有記錄
    ca.fetch_twse_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))                  # 重跑：不升版
    assert c.execute("SELECT MAX(version) FROM corporate_actions").fetchone()[0] == 1 and _n(c) == n


def test_tpex_exdailyq_real_sample_parses_and_is_idempotent(monkeypatch):
    data = json.loads((FIX / "tpex_exdailyq_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    n = ca.fetch_tpex_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    assert n == 9 and _n(c, "market='TPEx'") == 9
    r = c.execute("SELECT * FROM corporate_actions WHERE symbol='00758B'").fetchone()
    assert r["action_date"] == "2026-03-03" and r["action_type"] == "ex_dividend" and abs(r["cash_dividend_twd"] - 0.59) < 1e-9
    ca.fetch_tpex_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    assert c.execute("SELECT MAX(version) FROM corporate_actions").fetchone()[0] == 1


def test_single_shot_sends_one_request_per_block_without_retry(monkeypatch):
    calls = []

    class R307:
        status_code, headers, content, text = 307, {"Location": "/rwd/zh/foo"}, b"", ""

    monkeypatch.setattr(httputil.twse.session, "request", lambda *a, **k: (calls.append(1), R307())[1])
    monkeypatch.setattr(httputil.time, "sleep", lambda s: None)
    monkeypatch.setattr(httputil.twse, "_wait", lambda: None)
    with httputil.single_shot():
        with pytest.raises(FetchError):
            httputil.twse.request("GET", "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?x=1")
    assert len(calls) == 1
    with pytest.raises(FetchError):                                                          # 一般模式維持重試（對照）
        httputil.twse.request("GET", "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?x=1")
    assert len(calls) == 1 + config.MAX_RETRIES


class Steps:
    def __init__(self, twse_block=False):
        self.calls, self.twse_block = [], twse_block

    def mk(self, name, host):
        def f(conn, syms, start, end):
            self.calls.append(name)
            if host == "TWSE" and self.twse_block:
                raise FetchError("redirect/http=307 loc=/rwd/x url=u", status=307)
            return 1
        return f


def _patch_steps(monkeypatch, st):
    for host, name, attr in (("TWSE", "twse_ex", "fetch_twse_exrights"), ("TWSE", "twse_cr_restore", "fetch_twse_twtauu"),
                             ("TWSE", "twse_par_value", "fetch_twse_twtb8u"), ("TPEx", "tpex_ex", "fetch_tpex_exrights"),
                             ("TPEx", "tpex_cr_restore", "fetch_tpex_revivt"), ("TPEx", "tpex_par_value", "fetch_tpex_pv_chg")):
        monkeypatch.setattr(ca, attr, st.mk(name, host))


def test_block_stops_all_twse_range_requests_continues_tpex_and_sets_cooldown(monkeypatch):
    c = _conn()
    st = Steps(twse_block=True)
    _patch_steps(monkeypatch, st)
    out = cb.run_backfill(c, max_enrich=0)
    assert st.calls == ["twse_ex", "tpex_ex", "tpex_cr_restore", "tpex_par_value"]            # TWSE 只送第 1 個、其餘跳過；TPEx 照跑
    assert out["stopped_hosts"] == ["TWSE"] and out["stopped"] == "blocked:TWSE"
    assert out["skipped"]["twse_cr_restore"] == "host_stopped_after_block"
    assert state_get(c, "corp_backfill", "block:TWSE")[0] == "blocked"
    # 立刻重跑（Actions 換機器也一樣，狀態在 DB）：冷卻中，TWSE 一個請求都不送
    st2 = Steps(twse_block=True)
    _patch_steps(monkeypatch, st2)
    out2 = cb.run_backfill(c, max_enrich=0)
    assert not [x for x in st2.calls if x.startswith("twse")] and out2["skipped"]["twse_ex"].startswith("cooldown")
    # 61 分鐘後才可再試；成功則解除（status=ok）
    old = (datetime.now(TZ) - timedelta(minutes=61)).isoformat()
    state_set(c, "corp_backfill", "block:TWSE", "blocked", old)
    st3 = Steps(twse_block=False)
    _patch_steps(monkeypatch, st3)
    out3 = cb.run_backfill(c, max_enrich=0)
    assert st3.calls[:3] == ["twse_ex", "twse_cr_restore", "twse_par_value"] and not out3["stopped"]
    assert state_get(c, "corp_backfill", "block:TWSE")[0] == "ok"


def test_rate_limited_also_stops_host_and_max_enrich_zero_skips_mops(monkeypatch):
    c = _conn()
    st = Steps()
    _patch_steps(monkeypatch, st)

    def rl(conn, syms, start, end):
        st.calls.append("tpex_ex")
        raise RateLimited("rate_limited(限流): http=429", host="www.tpex.org.tw", retry_after_s=60)

    monkeypatch.setattr(ca, "fetch_tpex_exrights", rl)
    called = []
    monkeypatch.setattr(ca, "enrich_twse_stock_dividends_from_mops", lambda *a, **k: called.append(1) or {})
    out = cb.run_backfill(c, max_enrich=0)
    assert out["stopped_hosts"] == ["TPEx"] and st.calls == ["twse_ex", "twse_cr_restore", "twse_par_value", "tpex_ex"]
    assert not called


def _seed_stock_rows(c, monkeypatch):
    monkeypatch.setattr(httputil, "warm_mops", lambda *a, **k: None)      # 不連網
    data = json.loads((FIX / "twse_twt49u_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(data))
    ca.fetch_twse_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    return sorted({r[0] for r in c.execute("SELECT symbol FROM corporate_actions WHERE action_type IN ('ex_both','ex_rights') AND stock_dividend_per_thousand IS NULL")})


def test_mops_enrich_batches_by_symbol_rotates_and_is_idempotent(monkeypatch):
    c = _conn()
    syms = _seed_stock_rows(c, monkeypatch)
    assert len(syms) >= 4
    seen = []

    def fake(sym, errors=None):
        seen.append(sym)
        return {"cash_dividend_twd": 1.0, "stock_yuan_per_share": 0.5, "par": 10.0, "source": f"mops?{sym}"}

    monkeypatch.setattr(ca, "_fetch_mops_t05st09", fake)
    r1 = ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=2)
    assert r1["symbols_this_run"] == 2 and len(seen) == 2 and r1["filled"] >= 2
    r2 = ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=2)
    assert len(seen) == len(set(seen)) == 4                                        # 第二批輪到別的代號，不重複請求
    assert r2["pending"] < r1["pending"]
    row = c.execute("SELECT stock_dividend_per_thousand, cash_dividend_twd FROM corporate_actions WHERE symbol=? AND stock_dividend_per_thousand IS NOT NULL", (seen[0],)).fetchone()
    assert abs(row[0] - 50.0) < 1e-9 and row[1] == 1.0                              # 0.5 元 ÷ 面額 10 × 1000
    rest = ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=None)       # 補完後再跑：沒有待補
    assert ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=5)["pending"] == 0 and rest["filled"] >= 0


def test_mops_enrich_stops_after_three_consecutive_errors_and_does_not_burn_attempts(monkeypatch):
    c = _conn()
    syms = _seed_stock_rows(c, monkeypatch)
    n = []

    def bad(sym, errors=None):
        n.append(sym)
        errors.append("redirect/http=307")
        return None

    monkeypatch.setattr(ca, "_fetch_mops_t05st09", bad)
    r = ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=10)
    assert len(n) == config.REDIRECT_STREAK_STOP and r["stopped"].startswith("consecutive_errors")
    assert all(state_get(c, "corp_enrich", s) is None for s in n)                  # 失敗的代號不記嘗試次數


# ---------- 覆蓋表範圍（研究分析 2026-11-17 發現：29 檔 38 列，覆蓋表卻宣稱全市場）----------
def _cov(c, market):
    return {(r["action_kind"]): (r["scope"], r["symbol_count"]) for r in c.execute(
        "SELECT action_kind, scope, symbol_count FROM corporate_actions_coverage WHERE market=? AND coverage_start='2026-03-02'", (market,))}


def _tpex_universe(c, extra=()):
    from pipeline.tests.test_halt_fix import _sec
    for sym in ("6488", "5347", "00751", *extra):
        _sec(c, sym, "TPEx")
    c.commit()


def test_scope_filtered_fetch_is_subset_or_sample30_never_all_and_unfiltered_is_all(monkeypatch):
    data = json.loads((FIX / "tpex_exdailyq_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    _tpex_universe(c)                                                              # 現行上櫃普通股 3 檔
    keep = {"00758B", "00759B"}                                                   # 不在追蹤清單、也沒涵蓋現行清單 → subset
    n = ca.fetch_tpex_exrights(c, keep, date(2026, 3, 2), date(2026, 11, 17))
    assert n == 2
    cov = _cov(c, "TPEx")
    assert set(cov) == {"cash_dividend", "stock_dividend", "ex_both"} and all(v == ("subset", 2) for v in cov.values())
    # 追蹤清單內的過濾 → sample30（另一列，不覆蓋 subset）
    ca.fetch_tpex_exrights(c, {"6488", "5347"}, date(2026, 3, 2), date(2026, 11, 17))
    rows = c.execute("SELECT action_kind, scope, symbol_count FROM corporate_actions_coverage WHERE market='TPEx' AND coverage_start='2026-03-02' ORDER BY 1,2").fetchall()
    assert [(r[1], r[2]) for r in rows if r[0] == "cash_dividend"] == [("sample30", 2), ("subset", 2)]
    # 不過濾：all，symbol_count＝現行上櫃普通股數；先前的 subset／sample30 列仍在，且 all 之後再跑 subset 也不會改動 all 列
    ca.fetch_tpex_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    ca.fetch_tpex_exrights(c, keep, date(2026, 3, 2), date(2026, 11, 17))
    got = {(r[0], r[1]): r[2] for r in c.execute("SELECT action_kind, scope, symbol_count FROM corporate_actions_coverage WHERE market='TPEx' AND coverage_start='2026-03-02'")}
    assert got[("cash_dividend", "all")] == 3 and got[("cash_dividend", "subset")] == 2 and got[("cash_dividend", "sample30")] == 2
    assert len(got) == 9                                                           # 3 類 × 3 種 scope，各一列


def test_scope_big_filter_that_does_not_cover_universe_is_subset_but_full_cover_is_all(monkeypatch):
    from pipeline.scope import scope_of
    data = json.loads((FIX / "tpex_exdailyq_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    _tpex_universe(c, extra=[f"7{i:03d}" for i in range(200)])                    # 現行上櫃 203 檔
    big = {f"{i:04d}" for i in range(1000, 1200)}                                  # 200 檔（>100）但沒涵蓋現行清單
    assert scope_of(big, c, "TPEx") == "subset" and scope_of(big) == "subset"
    ca.fetch_tpex_exrights(c, big, date(2026, 3, 2), date(2026, 11, 17))
    assert all(v == ("subset", 200) for v in _cov(c, "TPEx").values())            # 舊規則會誤判成 all
    full = {r[0] for r in c.execute("SELECT symbol FROM securities WHERE is_current=1 AND market='TPEx'")}
    assert scope_of(full, c, "TPEx") == "all" and scope_of(full | {"9999"}, c, "TPEx") == "all"
    assert scope_of(set(list(full)[:-1]), c, "TPEx") == "subset"
    assert scope_of(None, c, "TPEx") == "all" and scope_of(set(), None) == "all"
    # 只涵蓋上櫃、不涵蓋上市時，上市判定仍是 subset（各市場各自比）
    from pipeline.tests.test_halt_fix import _sec
    _sec(c, "2330", "TWSE")
    assert scope_of(full, c, "TWSE") == "subset"


def test_coverage_primary_key_includes_scope_and_migration_rebuilds_old_table():
    import sqlite3
    c = _conn()
    pk = {r[1] for r in c.execute("PRAGMA table_info(corporate_actions_coverage)") if r[5]}
    assert "scope" in pk                                                           # 新庫
    # 舊庫（3b 版 DDL：主鍵不含 scope）→ 重建並保留資料
    o = sqlite3.connect(":memory:")
    o.row_factory = sqlite3.Row
    o.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8").replace(
        "scope TEXT NOT NULL DEFAULT 'unknown',", "scope TEXT,").replace(
        "PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source, scope)", "PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source)"))
    assert {r[1] for r in o.execute("PRAGMA table_info(corporate_actions_coverage)") if r[5]} == {"market", "action_kind", "coverage_start", "coverage_end", "source"}
    o.execute("INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,scope,symbol_count) VALUES('TWSE','cash_dividend','2026-03-02','2026-11-10','u','f',NULL,NULL)")
    o.execute("INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,scope,symbol_count) VALUES('TPEx','cash_dividend','2026-03-02','2026-11-10','u','f','all',900)")
    o.execute("INSERT INTO corporate_actions(symbol,market,action_date,action_type,source,fetched_at,unit,source_unit,unit_conversion) VALUES('2330','TWSE','2026-06-11','ex_dividend','u','f','x','x','x')")
    o.commit()
    before = o.execute("SELECT COUNT(*), MAX(version), MIN(checksum) FROM corporate_actions").fetchone()[:]
    pdb.migrate(o)
    assert "scope" in {r[1] for r in o.execute("PRAGMA table_info(corporate_actions_coverage)") if r[5]}
    got = {(r[0], r[1]): r[2] for r in o.execute("SELECT market, scope, symbol_count FROM corporate_actions_coverage")}
    assert got == {("TWSE", "sample30"): None, ("TPEx", "all"): 900}               # NULL＋代號全在追蹤清單 → sample30；原有 all 保留
    assert o.execute("SELECT COUNT(*), MAX(version), MIN(checksum) FROM corporate_actions").fetchone()[:] == before   # 不重新版本化
    o.execute("INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,scope) VALUES('TWSE','cash_dividend','2026-03-02','2026-11-10','u','f','subset')")   # 同區間不同 scope 可並存
    pdb.migrate(o)                                                                 # 重跑冪等
    assert o.execute("SELECT COUNT(*) FROM corporate_actions_coverage").fetchone()[0] == 3
    assert not o.execute("SELECT name FROM sqlite_master WHERE name='corporate_actions_coverage_old'").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        o.execute("INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,scope) VALUES('TWSE','cash_dividend','2026-03-02','2026-11-10','u','f','subset')")


def test_twse_stock_dividend_coverage_is_pending_until_mops_ratio_filled(monkeypatch):
    data = json.loads((FIX / "twse_twt49u_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(data))
    monkeypatch.setattr(httputil, "warm_mops", lambda *a, **k: None)
    c = _conn()
    ca.fetch_twse_exrights(c, set(), date(2026, 3, 2), date(2026, 11, 17))
    cov = _cov(c, "TWSE")
    assert cov["cash_dividend"][0] == "all"
    assert cov["stock_dividend"][0] == "all_pending_ratio" and cov["ex_both"][0] == "all_pending_ratio"   # 配股比例還沒補
    assert ca.pending_ratio_rows(c) >= 4
    note = c.execute("SELECT notes FROM corporate_actions_coverage WHERE market='TWSE' AND action_kind='stock_dividend'").fetchone()[0]
    assert "配股比例待補" in note
    # MOPS 補強：有資料的補上；查無配股資料（回 None 但沒有請求錯誤）的視為已查過，不再算待補
    calls = []

    def fake(sym, errors=None):
        calls.append(sym)
        if len(calls) == 1:
            return None
        return {"cash_dividend_twd": 1.0, "stock_yuan_per_share": 0.5, "par": 10.0, "source": f"mops?{sym}"}

    monkeypatch.setattr(ca, "_fetch_mops_t05st09", fake)
    out = ca.enrich_twse_stock_dividends_from_mops(c, set(), max_items=None)
    assert out["coverage"]["pending_ratio_rows"] == 0 and _cov(c, "TWSE")["stock_dividend"][0] == "all"


def test_twse_failed_mops_requests_do_not_count_as_checked(monkeypatch):
    data = json.loads((FIX / "twse_twt49u_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(data))
    monkeypatch.setattr(httputil, "warm_mops", lambda *a, **k: None)
    c2 = _conn()
    ca.fetch_twse_exrights(c2, set(), date(2026, 3, 2), date(2026, 11, 17))
    monkeypatch.setattr(ca, "_fetch_mops_t05st09", lambda sym, errors=None: (errors.append("redirect/http=307"), None)[1])
    out2 = ca.enrich_twse_stock_dividends_from_mops(c2, set(), max_items=None)
    assert out2["coverage"]["pending_ratio_rows"] >= 1 and _cov(c2, "TWSE")["stock_dividend"][0] == "all_pending_ratio"


def test_migration_marks_unknown_when_corporate_actions_has_symbols_outside_tracking_list():
    import sqlite3
    o = sqlite3.connect(":memory:")
    o.row_factory = sqlite3.Row
    o.executescript(config.SCHEMA_SQL.read_text(encoding="utf-8").replace(
        "scope TEXT NOT NULL DEFAULT 'unknown',", "scope TEXT,").replace(
        "PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source, scope)", "PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source)"))
    o.execute("INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at) VALUES('TWSE','cash_dividend','2025-10-06','2026-11-10','u','f')")
    o.execute("INSERT INTO corporate_actions(symbol,market,action_date,action_type,source,fetched_at,unit,source_unit,unit_conversion) VALUES('1234','TWSE','2026-06-11','ex_dividend','u','f','x','x','x')")
    o.commit()
    pdb.migrate(o)
    assert o.execute("SELECT scope FROM corporate_actions_coverage").fetchone()[0] == "unknown"      # 追蹤清單以外的代號 → 無法判定，不假設
