"""基本品質檢查。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class QualityReport:
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def add_err(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def add_warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_gap(self, msg: str) -> None:
        self.gaps.append(msg)


def check_db(conn: sqlite3.Connection, symbols: list[str], min_price_days: int = 150) -> QualityReport:
    r = QualityReport()
    # securities range
    n_twse = conn.execute("SELECT COUNT(*) FROM securities WHERE market='TWSE'").fetchone()[0]
    n_tpex = conn.execute("SELECT COUNT(*) FROM securities WHERE market='TPEx'").fetchone()[0]
    if not (1000 <= n_twse <= 1200):
        r.add_warn(f"上市普通股筆數 {n_twse} 超出預期 1000–1200")
    if not (800 <= n_tpex <= 1000):
        r.add_warn(f"上櫃普通股筆數 {n_tpex} 超出預期 800–1000")

    # negative volume/amount
    bad = conn.execute(
        """SELECT symbol, price_date FROM daily_prices
           WHERE is_current=1 AND (volume_shares<0 OR amount_twd<0) LIMIT 20"""
    ).fetchall()
    if bad:
        r.add_err(f"行情量／金額為負: {bad}")

    # extreme daily change (>35% for non-limit special; warn only)
    extreme = conn.execute(
        """SELECT symbol, price_date, change_twd, close_twd FROM daily_prices
           WHERE is_current=1 AND close_twd>0 AND ABS(change_twd)/close_twd > 0.35
           LIMIT 30"""
    ).fetchall()
    if extreme:
        r.add_warn(f"單日漲跌幅異常(>35%) {len(extreme)} 筆例: {extreme[:5]}")

    for sym in symbols:
        n = conn.execute(
            "SELECT COUNT(*) FROM daily_prices WHERE symbol=? AND is_current=1", (sym,)
        ).fetchone()[0]
        if n < min_price_days:
            r.add_gap(f"{sym} 行情僅 {n} 日（目標>={min_price_days}）")
        # revenue
        nm = conn.execute(
            "SELECT COUNT(*) FROM monthly_revenues WHERE symbol=? AND is_current=1", (sym,)
        ).fetchone()[0]
        if nm < 36:
            r.add_gap(f"{sym} 營收僅 {nm} 月（目標>=36）")
        # income
        ni = conn.execute(
            "SELECT COUNT(*) FROM income_statements WHERE symbol=? AND is_current=1", (sym,)
        ).fetchone()[0]
        if ni < 8:
            r.add_gap(f"{sym} 綜合損益僅 {ni} 季（目標>=8）")
        # cashflow
        nc = conn.execute(
            "SELECT COUNT(*) FROM cash_flow_statements WHERE symbol=? AND is_current=1", (sym,)
        ).fetchone()[0]
        if nc < 8:
            r.add_gap(f"{sym} 現金流量僅 {nc} 季（目標>=8）")
        # material last 30d approx by count
        nm2 = conn.execute(
            "SELECT COUNT(*) FROM material_infos WHERE symbol=? AND is_current=1", (sym,)
        ).fetchone()[0]
        # 0 is possible (no announcements) — not a gap; note only if fetch failed tracked elsewhere

    # date continuity for sample: warn large gaps
    for sym in symbols[:5]:
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT price_date FROM daily_prices WHERE symbol=? AND is_current=1 ORDER BY price_date",
                (sym,),
            )
        ]
        if len(dates) >= 2:
            from datetime import date
            gaps = 0
            for a, b in zip(dates, dates[1:]):
                da, db = date.fromisoformat(a), date.fromisoformat(b)
                if (db - da).days > 10:
                    gaps += 1
            if gaps:
                r.add_warn(f"{sym} 行情日期間隔>10天共 {gaps} 處（可能含長假）")

    return r
