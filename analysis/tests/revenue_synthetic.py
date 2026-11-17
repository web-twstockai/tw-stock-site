"""【測試用合成資料】月營收情境（代號 TESTR*，金額為虛構）。不得匯入正式資料庫或網站正式頁面。

每個情境的評估月份都是 EVAL_MONTH；EXPECTED 為預期結果。CSV 由 write_fixture() 產生於 tests/fixtures/。
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import synthetic_data as SD

EVAL_MONTH = "2026-10"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "monthly_revenues_synthetic.csv"
COLS = ["symbol", "market", "revenue_month", "revenue_twd", "revenue_prev_month_twd", "revenue_prev_year_twd",
        "mom_pct", "yoy_pct", "cumulative_twd", "cumulative_prev_year_twd", "cumulative_yoy_pct", "industry", "remark",
        "source", "announced_at", "announced_at_note", "fetched_at", "unit", "source_unit", "unit_conversion",
        "version", "is_current", "checksum"]
SRC = "synthetic://測試用合成資料"


def _m(k: int) -> str:
    """EVAL_MONTH 往前 k 個月。"""
    y, m = 2026, 10
    n = y * 12 + (m - 1) - k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _series(sym, values, prev_year=None, market="TWSE", drop=(), nulls=(), extra=()):
    """values[k]＝EVAL_MONTH 往前 k 個月的營收（k=0 為當月）。"""
    rows = []
    for k, v in enumerate(values):
        if k in drop:
            continue
        r = dict(symbol=sym, market=market, revenue_month=_m(k), revenue_twd=None if k in nulls else v,
                 revenue_prev_year_twd=(prev_year if (k == 0 and prev_year is not None) else
                                        (values[k + 12] if k + 12 < len(values) else None)),
                 industry="測試用合成", remark="測試用合成資料", source=SRC, fetched_at="2026-11-10T02:00:00+08:00",
                 unit="TWD", source_unit="新台幣仟元", unit_conversion="*1000", version=1, is_current=1)
        rows.append(r)
    rows.extend(extra)
    return rows


def rows() -> list[dict]:
    base = [100_000_000 + 1_000_000 * (k % 5) for k in range(24)]   # 前高 104,000,000
    out = []
    out += _series("TESTR01", [150_000_000] + base[1:])                     # 嚴格大於 → 新高
    out += _series("TESTR02", [104_000_000] + base[1:])                     # 與前高相等 → 不算
    out += _series("TESTR03", [150_000_000] + base[1:], drop=(5,))          # 視窗內斷月
    out += _series("TESTR04", [150_000_000] + base[1:], nulls=(7,))         # 視窗內 NULL
    out += _series("TESTR05", [150_000_000] + base[1:12])                   # 只有 12 個月（不足 13）
    out += _series("TESTR06", [150_000_000] + base[1:], drop=(15, 16))      # 斷月在視窗外 → 仍可判斷
    out += _series("TESTR07", [150_000_000] + base[1:], prev_year=0)        # 去年同月 0 → 年增率不適用
    out += _series("TESTR08", [150_000_000] + base[1:13])                   # 13 個月剛好、去年同月 NULL
    for r in out:
        if r["symbol"] == "TESTR08" and r["revenue_month"] == EVAL_MONTH:
            r["revenue_prev_year_twd"] = None
    out += _series("TESTR09", [None] + base[1:])                            # 當月 NULL
    out += _series("TESTR10", [150_000_000] + base[1:13] + [999_000_000] + base[14:])  # 13 個月前（視窗外）更高 → 不影響
    out += _series("TESTR13", [150_000_000] + base[1:12] + [999_000_000] + base[13:])  # 12 個月前（視窗內邊界）更高 → 不是
    out += _series("TESTR11", [90_000_000] + base[1:13])                    # 13 個月、低於前高 → 不是
    stale = dict(symbol="TESTR01", market="TWSE", revenue_month=_m(3), revenue_twd=999_000_000,
                 revenue_prev_year_twd=None, source=SRC, fetched_at="2026-11-01T02:00:00+08:00", unit="TWD",
                 version=0, is_current=0)
    out.append(stale)                                                       # 舊版列必須忽略
    out += _series("TESTR12", [-5_000_000] + base[1:], prev_year=-2_000_000, market="TPEx")  # 去年同月負值
    return out


EXPECTED = {   # symbol: (rev_12m_high_status, reason, yoy_status, yoy_reason)
    "TESTR01": ("true", None, "ok", None),
    "TESTR02": ("false", "REV_EQUAL_PRIOR_HIGH", "ok", None),
    "TESTR03": ("insufficient", "REV_PRIOR_MONTH_MISSING", "ok", None),
    "TESTR04": ("insufficient", "REV_PRIOR_MONTH_NULL", "ok", None),
    "TESTR05": ("insufficient", "REV_INSUFFICIENT_HISTORY", "not_applicable", "REV_YOY_PREV_YEAR_NULL"),
    "TESTR06": ("true", None, "ok", None),
    "TESTR07": ("true", None, "not_applicable", "REV_YOY_PREV_YEAR_ZERO"),
    "TESTR08": ("true", None, "not_applicable", "REV_YOY_PREV_YEAR_NULL"),
    "TESTR09": ("insufficient", "REV_CURRENT_NULL", "not_applicable", "REV_YOY_CURRENT_NULL"),
    "TESTR10": ("true", None, "ok", None),
    "TESTR11": ("false", None, "ok", None),
    "TESTR12": ("false", None, "not_applicable", "REV_YOY_PREV_YEAR_NEGATIVE"),
    "TESTR13": ("false", None, "ok", None),
}


def write_fixture(path: Path = FIXTURE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        for r in rows():
            w.writerow(["" if r.get(c) is None else r.get(c) for c in COLS])
    return path


def build_sqlite(path: Path) -> Path:
    """暫存 SQLite：DDL 取自 docs/schema.md §4（monthly_revenues），寫入合成列。"""
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript(SD.schema_ddl())
        con.executemany(f"INSERT INTO monthly_revenues ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                        [[r.get(c) for c in COLS] for r in rows()])
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":
    print(write_fixture())
