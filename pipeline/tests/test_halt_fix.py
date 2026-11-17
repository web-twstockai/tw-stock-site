"""halt_fix：官方證據入庫、全期重算（版本化＋change_note）、缺口稽核（資料不足不補造）、快照、冪等。全部用假資料／假回應。"""
import pytest

from pipeline import db as pdb, halt_fix as hf, httputil
from pipeline.fetchers import prices as pr
from pipeline.halt_evidence import STOP_EVIDENCE
from pipeline.httputil import FetchError, RateLimited


def _conn():
    c = pdb.connect()
    pdb.init_db(c)
    return c


def _row(sym, mkt, d, close, *, halted=0, no_trade=None, src="dailyQuotes"):
    r = {"symbol": sym, "market": mkt, "price_date": d, "open_twd": close, "high_twd": close, "low_twd": close, "close_twd": close,
         "change": None, "change_twd": None, "change_sign": None, "change_note": None, "is_halted": halted,
         "is_no_trade": (1 if close is None else 0) if no_trade is None else no_trade, "next_ref_price_twd": None,
         "volume_shares": 0 if close is None else 1000, "amount_twd": 0, "transactions": 0, "raw_volume": "0", "raw_amount": "0",
         "source": "u", "source_table": src, "volume_basis": "incl_block", "fetched_at": "2026-11-17T09:00:00+08:00",
         "unit": "x", "source_unit": "x", "unit_conversion": None}
    return r


def _sec(c, sym, mkt, listed="2000-01-01"):
    c.execute("INSERT INTO securities(symbol,market,name,cfi_code,is_common,listed_date,data_date,source,fetched_at,is_current) VALUES(?,?,?,?,?,?,?,?,?,1)",
              (sym, mkt, sym, "ESVUFR", 1, listed, "2026-11-17", "t", "2026-11-17T00:00:00+08:00"))


def _fill_market(c, mkt, days, symbols):
    """每個全市場日塞滿 ≥100 列「填充股」，使該日被視為全市場日。"""
    for d in days:
        for i in range(hf.FULL_MARKET_MIN_ROWS):
            pr._upsert_price(c, _row(f"F{mkt[:2]}{i:03d}", mkt, d, 10.0))
    c.commit()


def test_evidence_written_versioned_and_idempotent():
    c = _conn()
    out = hf.write_stop_evidence(c)
    assert out["inserted"] == len(STOP_EVIDENCE) >= 11
    assert dict(hf.write_stop_evidence(c)) == {"unchanged": len(STOP_EVIDENCE)}
    r = c.execute("SELECT * FROM trading_halts WHERE symbol='1589' AND is_current=1").fetchone()
    assert r["halt_type"] == "停止買賣" and r["halt_date"] == "2026-04-07" and r["resume_date"] == "2026-11-18" and "證據等級A" in r["announced_at_note"]
    assert c.execute("SELECT resume_date FROM trading_halts WHERE symbol='4804'").fetchone()[0] is None   # 持續中


def test_recompute_fixes_mislabeled_halt_versioned_with_note_and_is_idempotent():
    c = _conn()
    # 官方暫停交易（盤中）：該日 close 為空 → 真停牌；另一列 close 空但無官方證據 → 只是無成交（舊程式誤標停牌）
    c.execute("INSERT INTO trading_halts(market,symbol,halt_date,halt_time,resume_date,halt_type,source,fetched_at) VALUES('TWSE','2330','2026-11-09','','', '暫停交易','u','f')")
    pr._upsert_price(c, _row("2330", "TWSE", "2026-11-09", None, halted=1, no_trade=0))
    pr._upsert_price(c, _row("6488", "TPEx", "2026-11-09", None, halted=1, no_trade=0))    # 誤標
    pr._upsert_price(c, _row("5347", "TPEx", "2026-11-09", 50.0, halted=0))
    c.execute("UPDATE daily_prices SET is_no_trade=NULL WHERE symbol='5347'")   # 缺 is_no_trade：就地補 0
    c.commit()
    cov = hf.Coverage(c)
    out = hf.recompute_flags(c, cov, None, None)
    assert out["counts"].get("updated") == 2 and out["counts"].get("in_place_default") == 1
    a = c.execute("SELECT * FROM daily_prices WHERE symbol='6488' AND is_current=1").fetchone()
    assert (a["version"], a["is_halted"], a["is_no_trade"]) == (2, 0, 1) and "停牌判定重算" in a["change_note"] and "收盤空值＝無成交" in a["change_note"]
    old = c.execute("SELECT is_halted FROM daily_prices WHERE symbol='6488' AND is_current=0").fetchone()
    assert old[0] == 1                                      # 舊值保留在舊版本列
    b = c.execute("SELECT * FROM daily_prices WHERE symbol='2330' AND is_current=1").fetchone()
    assert (b["is_halted"], b["is_no_trade"]) == (1, 1)     # 官方證據涵蓋 → 仍是停牌（同時是無成交）
    assert c.execute("SELECT version FROM daily_prices WHERE symbol='5347' AND is_current=1").fetchone()[0] == 1
    again = hf.recompute_flags(c, hf.Coverage(c), None, None)
    assert again["counts"] == {"unchanged": 2} or again["counts"].get("unchanged", 0) >= 1 and "updated" not in again["counts"]


def test_stop_evidence_conflicting_with_a_close_is_reported_not_applied():
    c = _conn()
    hf.write_stop_evidence(c)
    pr._upsert_price(c, _row("3714", "TWSE", "2026-11-09", 56.7))    # 停止買賣區間內竟有收盤價
    pr._upsert_price(c, _row("3714", "TWSE", "2026-11-10", None))    # 區間內且無成交 → 停牌
    c.commit()
    out = hf.recompute_flags(c, hf.Coverage(c), None, None)
    assert out["conflict_count"] == 1 and "3714 2026-11-09" in out["conflicts"][0]
    assert c.execute("SELECT is_halted FROM daily_prices WHERE symbol='3714' AND price_date='2026-11-09' AND is_current=1").fetchone()[0] == 0
    assert c.execute("SELECT is_halted FROM daily_prices WHERE symbol='3714' AND price_date='2026-11-10' AND is_current=1").fetchone()[0] == 1


def test_gap_audit_statuses_insufficient_is_not_fabricated_and_idempotent():
    c = _conn()
    days = ["2026-09-02", "2026-09-03", "2026-11-05", "2026-11-06"]
    for s, m, ld in (("3714", "TWSE", "2021-01-06"), ("3718", "TPEx", "1999-01-20"), ("9999", "TPEx", "2000-01-01"), ("1111", "TPEx", "2000-01-01"), ("2222", "TPEx", "2030-01-01")):
        _sec(c, s, m, ld)
    _fill_market(c, "TWSE", days, [])
    _fill_market(c, "TPEx", days, [])
    for d in days[:2]:   # 3714 前兩天有行情，之後沒有（減資停止買賣 11-05 起）；1111 全程有行情
        pr._upsert_price(c, _row("3714", "TWSE", d, 50.0))
    for d in days[1:]:   # 3718 於 09-03 才以新代號上櫃：09-02 無行情是正常的
        pr._upsert_price(c, _row("3718", "TPEx", d, 20.0))
    for d in days:
        pr._upsert_price(c, _row("1111", "TPEx", d, 10.0))
    c.commit()
    hf.write_stop_evidence(c)
    out = hf.audit_gaps(c, hf.Coverage(c))
    st = {(r["symbol"], r["price_date"]): r["status"] for r in c.execute("SELECT * FROM price_gaps WHERE is_current=1")}
    assert st[("3714", "2026-11-05")] == "halted_official" and st[("3714", "2026-11-06")] == "halted_official"
    assert st[("3718", "2026-09-02")] == "not_listed" and ("3718", "2026-09-03") not in st    # 新代號 2026-09-03 才上櫃
    assert all(st[("9999", d)] == "insufficient" for d in days)        # 查無證據 → 資料不足
    assert ("1111", "2026-09-02") not in st and ("2222", "2026-09-02") not in st   # 有行情／尚未到上市日者不是缺口
    row = c.execute("SELECT * FROM price_gaps WHERE symbol='9999' AND price_date='2026-09-02'").fetchone()
    assert "資料不足" in row["reason"] and row["evidence_source"] is None and "未補造" in row["note"]
    assert c.execute("SELECT COUNT(*) FROM daily_prices WHERE symbol='9999'").fetchone()[0] == 0    # 沒有補造行情列
    assert out["by_status"] == {"halted_official": 2, "not_listed": 1, "insufficient": 4}
    n = c.execute("SELECT COUNT(*), MAX(version) FROM price_gaps").fetchone()
    hf.audit_gaps(c, hf.Coverage(c))
    assert tuple(c.execute("SELECT COUNT(*), MAX(version) FROM price_gaps").fetchone()) == tuple(n)    # 冪等
    # 後來補抓到 9999 的行情列 → 該缺口退役
    pr._upsert_price(c, _row("9999", "TPEx", "2026-09-02", 5.0))
    c.commit()
    out2 = hf.audit_gaps(c, hf.Coverage(c))
    assert out2["retired"] == 1 and c.execute("SELECT COUNT(*) FROM price_gaps WHERE symbol='9999' AND price_date='2026-09-02' AND is_current=1").fetchone()[0] == 0


class _Resp:
    def __init__(self, d):
        self._d, self.status_code = d, 200

    def json(self):
        return self._d


def _fake(twse_rows=None, cmode=None, twse_exc=None):
    def req(method, url, **kw):
        if "violation/stop" in url:
            if twse_exc:
                raise twse_exc
            return _Resp({"stat": "ok", "tables": [{"data": twse_rows or []}]})
        if "tpex_cmode" in url:
            return _Resp(cmode if cmode is not None else [])
        raise AssertionError(url)
    return req


def test_snapshots_confirm_record_close_and_flag(monkeypatch):
    c = _conn()
    hf.write_stop_evidence(c)
    rows = [["1589", "永冠-KY", "x", "reason", "115年04月07日"], ["9001", "新停牌", "x", "reason", "115年11月10日"]]
    cm = [{"SecuritiesCompanyCode": "4804", "SuspensionOfTrading": "Ｙ"}, {"SecuritiesCompanyCode": "8888", "SuspensionOfTrading": "Ｙ"}]
    f = _fake(rows, cm)
    monkeypatch.setattr(httputil.twse, "request", f)
    monkeypatch.setattr(httputil.tpex, "request", f)
    out = hf.fetch_snapshots(c)
    assert out["requests"] == 2 and not out["errors"]
    assert sorted(out["confirmed"]) == ["1589", "4804"] and out["new_from_snapshot"] == ["9001"] and out["unrecorded_cmode"] == ["8888"]
    r = c.execute("SELECT * FROM trading_halts WHERE symbol='9001' AND is_current=1").fetchone()
    assert r["halt_date"] == "2026-11-10" and r["resume_date"] is None and "證據等級A" in r["announced_at_note"]
    assert c.execute("SELECT COUNT(*) FROM trading_halts WHERE symbol='8888'").fetchone()[0] == 0   # 起日不明：不寫入
    # 再抓一次：冪等；9001 不再列出 → 以快照日為上界關閉；4804 不再標示 → maybe_resumed
    n = c.execute("SELECT COUNT(*) FROM trading_halts").fetchone()[0]
    out2 = hf.fetch_snapshots(c)
    assert out2["new_from_snapshot"] == [] and c.execute("SELECT COUNT(*) FROM trading_halts").fetchone()[0] == n
    f3 = _fake([["1589", "永冠-KY", "x", "r", "115年04月07日"]], [])
    monkeypatch.setattr(httputil.twse, "request", f3)
    monkeypatch.setattr(httputil.tpex, "request", f3)
    out3 = hf.fetch_snapshots(c)
    assert out3["closed"] == ["9001"] and out3["maybe_resumed"] == ["4804"]
    assert c.execute("SELECT resume_date FROM trading_halts WHERE symbol='9001' AND is_current=1").fetchone()[0] is not None


def test_snapshot_failures_are_recorded_not_raised(monkeypatch):
    c = _conn()
    f = _fake(twse_exc=RateLimited("rate_limited(限流): x", host="h", retry_after_s=900, status=302))
    monkeypatch.setattr(httputil.twse, "request", f)
    monkeypatch.setattr(httputil.tpex, "request", f)
    out = hf.fetch_snapshots(c)
    assert out["rate_limited"] and out["twse_stop"] is None and out["tpex_cmode"] == 0
    st = c.execute("SELECT ok, notes FROM source_status WHERE source_id='twse_violation_stop'").fetchone()
    assert st[0] == 0 and st[1].startswith("rate_limited")


def test_run_halt_fix_end_to_end_no_fetch():
    c = _conn()
    _sec(c, "6488", "TPEx")
    _fill_market(c, "TPEx", ["2026-11-09"], [])
    pr._upsert_price(c, _row("6488", "TPEx", "2026-11-09", None, halted=1, no_trade=0))
    c.commit()
    out = hf.run_halt_fix(c, fetch=False)
    assert out["before"]["close_null"]["halted"] == 1 and out["after"]["close_null"] == {"rows": 1, "syms": 1, "halted": 0, "no_trade_only": 1}
    out2 = hf.run_halt_fix(c, fetch=False)
    assert out2["recompute"]["counts"] == {"unchanged": 1} and out2["evidence"] == {"unchanged": len(STOP_EVIDENCE)}
