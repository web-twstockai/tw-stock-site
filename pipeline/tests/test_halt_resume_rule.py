"""停牌統一規則（對齊研究分析 0.9.1）：有恢復日→停到恢復日前一天；沒有→停到停牌日之後第一個有官方成交列的前一天（依成交推定）；
一直沒成交→仍在停牌；資料不足→不推測。全部假資料。"""
from pipeline import db as pdb, halt_fix as hf
from pipeline.fetchers import prices as pr
from pipeline.tests.test_halt_fix import _conn, _fill_market, _row

DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]


def _cal(c, days):
    for d in days:
        c.execute("INSERT OR REPLACE INTO trading_calendar(calendar_date,is_trade_day,source_kind,source,fetched_at) VALUES(?,1,'t','t','f')", (d,))


def _halt(c, mkt, sym, hd, rd=None, ht="暫停交易"):
    c.execute("INSERT INTO trading_halts(market,symbol,halt_date,halt_time,resume_date,halt_type,source,fetched_at) VALUES(?,?,?,?,?,?,'u','f')",
              (mkt, sym, hd, "", rd, ht))


def _basis(c, sym):
    r = c.execute("SELECT resume_basis, resume_date_inferred, resume_basis_note FROM trading_halts WHERE symbol=? AND is_current=1", (sym,)).fetchone()
    return r["resume_basis"], r["resume_date_inferred"], r["resume_basis_note"]


def _flag(c, sym, d):
    r = c.execute("SELECT is_halted FROM daily_prices WHERE symbol=? AND price_date=? AND is_current=1", (sym, d)).fetchone()
    return None if r is None else r[0]


def _setup():
    c = _conn()
    _cal(c, DAYS)
    _fill_market(c, "TPEx", DAYS, [])
    return c


def test_no_resume_date_is_inferred_from_first_trade_row_and_flags_halted_days():
    c = _setup()
    # 6001：11-03 停牌、無恢復日；11-03 當天（盤中停牌）有成交；11-04、11-05 無成交列（NULL close）；11-06 恢復成交
    _halt(c, "TPEx", "6001", "2026-11-03")
    for d, cl in (("2026-11-02", 10.0), ("2026-11-03", 10.0), ("2026-11-04", None), ("2026-11-05", None), ("2026-11-06", 11.0), ("2026-11-09", 11.0)):
        pr._upsert_price(c, _row("6001", "TPEx", d, cl))
    c.commit()
    out = hf.apply_unified_rule(c)
    assert _basis(c, "6001")[:2] == ("inferred_by_trade", "2026-11-06")
    assert "依成交推定" in _basis(c, "6001")[2]
    assert [_flag(c, "6001", d) for d in DAYS] == [0, 1, 1, 1, 0, 0]        # 停牌日起、到推定恢復日前一天
    assert out["resume_rule"]["by_basis"] == {"inferred_by_trade": 1}
    # 冪等
    again = hf.apply_unified_rule(c)
    assert again["resume_rule"]["rows_updated"] == 0 and again["recompute"]["counts"].get("updated", 0) == 0
    # 恢復日欄位本身不被改寫（官方原值保持空），推定值只在新欄位
    assert c.execute("SELECT resume_date FROM trading_halts WHERE symbol='6001'").fetchone()[0] in (None, "")


def test_official_resume_date_wins_and_is_not_inferred():
    c = _setup()
    _halt(c, "TPEx", "6002", "2026-11-03", "2026-11-05")
    for d in DAYS:
        pr._upsert_price(c, _row("6002", "TPEx", d, None if d in ("2026-11-03", "2026-11-04") else 12.0))
    c.commit()
    hf.apply_unified_rule(c)
    assert _basis(c, "6002") == ("official", None, None)
    assert [_flag(c, "6002", d) for d in DAYS] == [0, 1, 1, 0, 0, 0]


def test_never_traded_again_is_ongoing_still_halted():
    c = _setup()
    _halt(c, "TPEx", "6003", "2026-11-04")
    pr._upsert_price(c, _row("6003", "TPEx", "2026-11-03", 9.0))
    pr._upsert_price(c, _row("6003", "TPEx", "2026-11-05", None))     # 之後只有無成交列
    pr._upsert_price(c, _row("6003", "TPEx", "2026-11-09", None))
    c.commit()
    hf.apply_unified_rule(c)
    assert _basis(c, "6003")[:2] == ("ongoing", None)
    assert _flag(c, "6003", "2026-11-05") == 1 and _flag(c, "6003", "2026-11-09") == 1 and _flag(c, "6003", "2026-11-03") == 0


def test_insufficient_when_before_coverage_or_no_price_rows_or_holes_and_flags_unchanged():
    c = _setup()
    _halt(c, "TPEx", "6004", "2026-10-01")                  # 早於全市場行情起始日（11-02）
    pr._upsert_price(c, _row("6004", "TPEx", "2026-11-05", 5.0))
    _halt(c, "TPEx", "6005", "2026-11-03")                  # 沒有任何行情列（例如權證／非普通股）
    _halt(c, "TPEx", "6006", "2026-11-03")                  # 推定區間含缺全市場行情的交易日
    pr._upsert_price(c, _row("6006", "TPEx", "2026-11-02", 5.0))
    _cal(c, ["2026-11-10"])                                  # 11-10 是交易日，但沒有全市場行情 → 洞
    pr._upsert_price(c, _row("6006", "TPEx", "2026-11-11", 6.0))
    pr._upsert_price(c, _row("FILL", "TPEx", "2026-11-11", 6.0))
    _cal(c, ["2026-11-11"])
    c.commit()
    hf.apply_unified_rule(c)
    for sym in ("6004", "6005", "6006"):
        assert _basis(c, sym)[0] == "insufficient" and _basis(c, sym)[1] is None and _basis(c, sym)[2]
    # 資料不足 → 維持舊語意（只標停牌日當天），不外推
    assert _flag(c, "6004", "2026-11-05") == 0


def test_stop_evidence_without_resume_is_ongoing_and_with_trade_after_is_inferred():
    c = _setup()
    hf.write_stop_evidence(c)                                # 4804（無恢復日、無成交）等
    _halt(c, "TPEx", "6007", "2026-11-03", ht="停止買賣")
    for d in DAYS:
        pr._upsert_price(c, _row("6007", "TPEx", d, None if d in ("2026-11-03", "2026-11-04") else 7.0))
    c.commit()
    hf.apply_unified_rule(c)
    assert _basis(c, "6007")[:2] == ("inferred_by_trade", "2026-11-05")
    assert [_flag(c, "6007", d) for d in DAYS] == [0, 1, 1, 0, 0, 0]


def test_migration_adds_columns_to_old_db_and_coverage_ignores_null_basis():
    c = _conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(trading_halts)")}
    assert {"resume_date_inferred", "resume_basis", "resume_basis_note"} <= cols
    _halt(c, "TPEx", "6008", "2026-11-03")
    cov = hf.Coverage(c)                                       # 尚未推定：舊語意（只標停牌日）
    assert cov.pause("6008", "2026-11-03") and not cov.pause("6008", "2026-11-04")
