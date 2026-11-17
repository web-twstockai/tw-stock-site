"""fix1：下市股（securities_history 有效起訖、delistings.last_trade_date、行情／除權息不被現行清單過濾）、
financial scope。假回應／真實回應截取，不送請求。"""
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from pipeline import config, db as pdb, dates, delist_backfill as dbf, httputil
from pipeline.fetchers import corporate_actions as ca, delistings as dl, market_prices as mp, prices as pr
from pipeline.tests.test_halt_fix import _row, _sec

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


def _delist(c, mkt, sym, d, name="x", reason=None):
    c.execute("INSERT INTO delistings(market,symbol,name,delist_date,reason,source,fetched_at) VALUES(?,?,?,?,?,'u','2026-11-17T10:00:00+08:00')", (mkt, sym, name, d, reason))
    c.commit()


def _cal(c, days):
    for d in days:
        c.execute("INSERT OR REPLACE INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES(?,1,'t','t','f')", (d,))
    c.commit()


def _full_day(c, mkt, d, extra_syms=()):
    for i in range(100):
        pr._upsert_price(c, _row(f"F{mkt[:2]}{i:03d}", mkt, d, 10.0))
    for s in extra_syms:
        pr._upsert_price(c, _row(s, mkt, d, 10.0))
    c.commit()


# ---------- (a) 行情：下市前的 daily_prices 會被回補 ----------
def test_universe_with_delisted_writes_pre_delist_rows_from_real_mi_index(monkeypatch):
    data = json.loads((FIX / "mi_index_20231101_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    for s in ("1101", "2330"):
        _sec(c, s, "TWSE")                                      # 現行清單只有這兩檔；3682 亞太電（2023-12-15 終止上市）不在
    _delist(c, "TWSE", "3682", "2023-12-15", "亞太電")
    c.commit()
    http: list = []
    out = mp.fetch_twse_day(c, "2023-11-01", mp._universe(c), http)                          # 舊行為：現行清單過濾
    assert out["twse"] == 2 and not c.execute("SELECT 1 FROM daily_prices WHERE symbol='3682'").fetchone()
    out = mp.fetch_twse_day(c, "2023-11-01", mp._universe(c, include_delisted="2023-11-01"), http)   # 回補：含視窗內下市股
    r = c.execute("SELECT close_twd, volume_shares, source_table FROM daily_prices WHERE symbol='3682' AND price_date='2023-11-01' AND is_current=1").fetchone()
    assert out["twse"] == 3 and (r[0], r[1], r[2]) == (6.9, 3519266, "MI_INDEX")
    assert c.execute("SELECT COUNT(*) FROM daily_prices WHERE price_date='2023-11-01' AND is_current=1").fetchone()[0] == 3


def test_markets_needed_counts_delisted_rows_with_same_universe_basis():
    c = _conn()
    for i in range(20):
        _sec(c, f"1{i:03d}", "TWSE")
    _delist(c, "TWSE", "3682", "2023-12-15")
    uni = mp._universe(c, include_delisted="2023-11-01")
    assert len(uni) == 21
    for i in range(18):                                         # 18／21＝0.857 ≥ 0.85
        pr._upsert_price(c, _row(f"1{i:03d}", "TWSE", "2023-11-01", 10.0))
    c.commit()
    assert mp.markets_needed(c, "2023-11-01", uni) == ["TPEx"] or "TWSE" not in mp.markets_needed(c, "2023-11-01", uni)
    pr._upsert_price(c, _row("3682", "TWSE", "2023-11-02", 10.0))    # 下市股當日有成交列也算在分子（同一口徑）
    assert mp.market_day_rows(c, "2023-11-02", uni) == {"TWSE": 1} and mp.market_day_rows(c, "2023-11-02") == {}


# ---------- (a) 除權息區間端點：下市股事件不被現行清單過濾 ----------
def test_corp_range_keeps_delisted_in_window_symbol_but_drops_unknown(monkeypatch):
    data = json.loads((FIX / "tpex_exdailyq_sample.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(data))
    c = _conn()
    _sec(c, "3322", "TPEx")                                     # 現行
    _delist(c, "TPEx", "3587", "2024-06-03")                    # 視窗內下市
    _delist(c, "TPEx", "6182", "2020-01-02")                    # 視窗前下市 → 不納入
    syms = set(mp._universe(c, include_delisted="2023-11-01"))
    assert syms == {"3322", "3587"}
    ca.fetch_tpex_exrights(c, syms, date(2023, 11, 1), date(2026, 11, 17))
    got = {r[0] for r in c.execute("SELECT DISTINCT symbol FROM corporate_actions WHERE is_current=1")}
    assert got == {"3322", "3587"}
    # 覆蓋表：universe 涵蓋全部現行普通股 → all
    assert {r[0] for r in c.execute("SELECT DISTINCT scope FROM corporate_actions_coverage WHERE market='TPEx'")} == {"all"}


# ---------- (b) delistings.last_trade_date ----------
def test_last_trade_date_only_from_official_rows_and_blank_when_gap_or_missing():
    c = _conn()
    days = ["2023-12-11", "2023-12-12", "2023-12-13", "2023-12-14", "2023-12-15"]
    _cal(c, days)
    for d in days:
        _full_day(c, "TWSE", d)
    # A：delist 12-13；最後成交 12-12，12-13 是全市場日但 A 無列（真的沒成交）→ 12-12
    for d in days[:2]:
        pr._upsert_price(c, _row("1111", "TWSE", d, 10.0))
    # B：delist 12-15；最後一列 12-12，但 12-14 不是全市場日（缺行情）→ 不可判定，留空
    for d in days[:2]:
        pr._upsert_price(c, _row("2222", "TWSE", d, 10.0))
    c.execute("DELETE FROM daily_prices WHERE price_date='2023-12-14' AND symbol LIKE 'FTW%'")
    # C：沒有任何行情列 → 留空；D：delist 12-13；12-12 那列是無成交（volume 0）→ 取前一個有成交日 12-11
    _delist(c, "TWSE", "1111", "2023-12-13")
    _delist(c, "TWSE", "2222", "2023-12-15")
    _delist(c, "TWSE", "3333", "2023-12-13")
    _delist(c, "TWSE", "4444", "2023-12-13")
    pr._upsert_price(c, _row("4444", "TWSE", "2023-12-11", 10.0))
    nt = _row("4444", "TWSE", "2023-12-12", 10.0)
    nt.update(volume_shares=0, transactions=0)
    pr._upsert_price(c, nt)
    c.commit()
    r = dl.fill_last_trade_dates(c)
    got = {x[0]: (x[1], x[2]) for x in c.execute("SELECT symbol, last_trade_date, last_trade_basis FROM delistings")}
    assert got["1111"] == ("2023-12-12", "daily_prices:last_traded_row")
    assert got["2222"] == (None, None) and got["3333"] == (None, None)             # 缺行情日／無列 → 不推估
    assert got["4444"][0] == "2023-12-11"                                           # 跳過 volume=0 的無成交列
    assert r["changed"] == 2 and r["blank"] == 2
    # 補上缺的全市場日後重算 → B 變成可判定；冪等
    _full_day(c, "TWSE", "2023-12-14")
    dl.fill_last_trade_dates(c)
    assert c.execute("SELECT last_trade_date FROM delistings WHERE symbol='2222'").fetchone()[0] == "2023-12-12"
    assert dl.fill_last_trade_dates(c)["changed"] == 0


def test_last_trade_columns_migration_backfills_without_new_versions():
    o = _conn()
    for t, cols in (("delistings", ("last_trade_date", "last_trade_basis")), ("securities_history", ("valid_from", "valid_to", "valid_basis"))):
        for col in cols:
            o.execute(f"ALTER TABLE {t} DROP COLUMN {col}")                              # 還原成 3b 版 schema（無新欄）
    assert "last_trade_date" not in {r[1] for r in o.execute("PRAGMA table_info(delistings)")}
    o.execute("INSERT INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES('2024-01-02',1,'t','t','f')")
    o.execute("INSERT INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES('2024-01-03',1,'t','t','f')")
    o.execute("INSERT INTO delistings(market,symbol,name,delist_date,source,fetched_at) VALUES('TWSE','1111','x','2024-01-03','u','f')")
    for i in range(100):
        for d in ("2024-01-02", "2024-01-03"):
            pr._upsert_price(o, _row(f"F{i:03d}", "TWSE", d, 10.0))
    pr._upsert_price(o, _row("1111", "TWSE", "2024-01-02", 10.0))
    o.commit()
    before = o.execute("SELECT COUNT(*), MAX(version), MIN(checksum), MAX(fetched_at) FROM daily_prices").fetchone()[:]
    pdb.migrate(o)
    assert o.execute("SELECT last_trade_date, last_trade_basis FROM delistings").fetchone()[:] == ("2024-01-02", "daily_prices:last_traded_row")
    assert o.execute("SELECT COUNT(*), MAX(version), MIN(checksum), MAX(fetched_at) FROM daily_prices").fetchone()[:] == before   # 價格表完全沒動
    assert {"valid_from", "valid_to", "valid_basis"} <= {r[1] for r in o.execute("PRAGMA table_info(securities_history)")}
    pdb.migrate(o)                                                                  # 重跑冪等
    assert o.execute("SELECT COUNT(*) FROM delistings").fetchone()[0] == 1


def test_upsert_of_delisting_list_keeps_last_trade_date():
    c = _conn()
    _delist(c, "TWSE", "1111", "2024-01-03", "舊名")
    c.execute("UPDATE delistings SET last_trade_date='2024-01-02', last_trade_basis='daily_prices:last_traded_row'")
    dl._upsert(c, "TWSE", "1111", "新名", "2024-01-03", None, "u2")
    assert c.execute("SELECT name, last_trade_date, last_trade_basis FROM delistings").fetchone()[:] == ("新名", "2024-01-02", "daily_prices:last_traded_row")


# ---------- (a) securities_history ----------
def test_sync_securities_history_valid_range_idempotent_and_skips_current_and_etf():
    c = _conn()
    _sec(c, "6423", "TWSE")                                     # 代號被沿用，現行清單優先
    _delist(c, "TWSE", "3682", "2023-12-15", "亞太電")
    _delist(c, "TPEx", "4736", "2023-12-22", "泰博")
    _delist(c, "TWSE", "6423", "2026-01-22", "億而得-創")
    _delist(c, "TWSE", "0050", "2024-01-01", "ETF 樣本")         # 0 開頭＝ETF，不收
    r = dl.sync_securities_history(c)
    assert r == {"inserted": 2, "updated": 0, "unchanged": 0, "skipped_current": 1}
    row = c.execute("SELECT version, market, name, is_common, valid_from, valid_to, valid_basis, cfi_code FROM securities_history WHERE symbol='3682'").fetchone()
    assert row[0] >= 9000 and row[1:] == ("TWSE", "亞太電", 1, None, "2023-12-15", "delistings", "n/a")
    assert c.execute("SELECT COUNT(*) FROM securities_history WHERE symbol IN ('6423','0050')").fetchone()[0] == 0
    assert dl.sync_securities_history(c) == {"inserted": 0, "updated": 0, "unchanged": 2, "skipped_current": 1}
    c.execute("UPDATE delistings SET name='亞太電信' WHERE symbol='3682'")
    assert dl.sync_securities_history(c)["updated"] == 1
    assert c.execute("SELECT COUNT(*) FROM securities_history WHERE symbol='3682'").fetchone()[0] == 1
    # 已有的一般封存列（valid_* 為 NULL）不受影響
    assert c.execute("SELECT COUNT(*) FROM securities_history WHERE valid_basis IS NULL").fetchone()[0] == 0


def test_history_version_does_not_collide_with_later_securities_archive():
    """代號下市後又被沿用：securities 日後更新時封存的 version（1、2…）不會撞到下市補寫列。"""
    c = _conn()
    _delist(c, "TWSE", "3682", "2023-12-15", "亞太電")
    dl.sync_securities_history(c)
    _sec(c, "3682", "TWSE")
    c.commit()
    c.execute("INSERT INTO securities_history(symbol,version,market,name,cfi_code,is_common,data_date,source,fetched_at,unit,archived_at) VALUES('3682',1,'TWSE','新','ESVUFR',1,'2026-11-17','s','f','n/a','a')")
    assert c.execute("SELECT COUNT(*) FROM securities_history WHERE symbol='3682'").fetchone()[0] == 2


def test_delist_backfill_runs_history_and_last_trade_offline(monkeypatch):
    tw = [{"DelistingDate": "112/12/15", "Company": "亞太電", "Code": "3682"}]
    tp = {"tables": [{"fields": [], "data": []}]}
    monkeypatch.setattr(httputil.twse, "request", lambda m, u, **k: Resp(tw))
    monkeypatch.setattr(httputil.tpex, "request", lambda m, u, **k: Resp(tp))
    monkeypatch.setattr(dbf, "today", lambda: date(2023, 12, 20))
    monkeypatch.setattr(dates, "today", lambda: date(2023, 12, 20))
    c = _conn()
    out = dbf.run_backfill(c, date_from="2023-11-01")
    assert out["securities_history"]["inserted"] == 1 and "changed" in out["last_trade"]
    assert c.execute("SELECT valid_to FROM securities_history WHERE symbol='3682'").fetchone()[0] == "2023-12-15"


# ---------- corp_backfill 在 delistings 為空時要警告 ----------
def test_corp_backfill_warns_when_delistings_empty(monkeypatch):
    from pipeline import corp_backfill as cb
    for n in ("fetch_twse_exrights", "fetch_twse_twtauu", "fetch_twse_twtb8u", "fetch_tpex_exrights", "fetch_tpex_revivt", "fetch_tpex_pv_chg"):
        monkeypatch.setattr(ca, n, lambda *a, **k: 0)
    c = _conn()
    out = cb.run_backfill(c, date_from="2023-11-01", max_enrich=0)
    assert out["warnings"] and "delist_backfill" in out["warnings"][0]
    _delist(c, "TWSE", "3682", "2023-12-15")
    out = cb.run_backfill(c, date_from="2023-11-01", max_enrich=0)
    assert "warnings" not in out and out["universe_delisted_in_window"] == 1


# ---------- financials scope ----------
def test_financials_scope_subset_vs_all():
    from pipeline.fetchers import financials as fin
    c = _conn()
    for i in range(150):
        _sec(c, f"1{i:03d}", "TWSE")
    c.commit()
    assert fin._scope_of(c, {f"1{i:03d}" for i in range(120)}, "sii") == "subset"
    assert fin._scope_of(c, {f"1{i:03d}" for i in range(150)}, "sii") == "all"
    assert fin._scope_of(c, {"2330"}, "sii") == "subset" or fin._scope_of(c, {"2330"}, "sii") == "sample30"
    assert fin._scope_of(c, {"2330", "2317"}, "sii") == "sample30"
