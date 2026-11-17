"""月營收指標（F-001 月營收公告追蹤表，研究分析部分）：近 12 個月新高、年增率。純函式，不讀寫資料庫。

定義（docs/indicators.md「月營收指標」；F-001 負責人審核 2026-11-10 04:10）：
- 近 12 個月新高（REV_12M_HIGH_VERSION）：當月營收 **嚴格大於** 前 12 個月（不含當月）營收最高值才算；相等不算。
  前 12 個月必須是日曆上連續的 12 個月（m−12 … m−1），任何一個月缺列或營收為 NULL → 資料不足。
- 年增率（REV_YOY_VERSION）：(當月營收 ÷ 去年同月營收 − 1) × 100（百分點）。去年同月取當月列的
  `revenue_prev_year_twd`（官方彙總表同列欄位）。去年同月為 0、負值或 NULL、當月為 NULL → 不計算並標原因；
  絕不產生 inf／NaN。來源 `yoy_pct` 原樣附上（source_yoy_pct），不覆寫。
- 只用 is_current = 1 的列；同一 (symbol, revenue_month) 有多列現行列 → ValueError（資料錯誤，不猜）。
- 金額單位：新台幣元（schema §3.6，來源仟元 ×1000 已由資料工程換算）。金融保險業申報口徑與一般產業不同，不宜跨產業比較（依據見 docs/indicators.md §7.3）。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

__all__ = ["REV_12M_HIGH_VERSION", "REV_YOY_VERSION", "STATUS_TRUE", "STATUS_FALSE", "STATUS_INSUFFICIENT",
           "YOY_OK", "YOY_NOT_APPLICABLE", "REASON_CODES", "OUTPUT_COLUMNS", "month_add", "evaluate_revenue",
           "load_monthly_revenues"]

REV_12M_HIGH_VERSION = "rev_12m_high-v1"
REV_YOY_VERSION = "rev_yoy-v1"
WINDOW = 12

STATUS_TRUE = "true"                  # 是近 12 個月新高
STATUS_FALSE = "false"                # 不是（含與前高相等）
STATUS_INSUFFICIENT = "insufficient"  # 資料不足
YOY_OK = "ok"
YOY_NOT_APPLICABLE = "not_applicable"

REASON_CODES = {
    # 近 12 個月新高
    "REV_CURRENT_MISSING": "當月無營收資料列",
    "REV_CURRENT_NULL": "當月營收為空值",
    "REV_INSUFFICIENT_HISTORY": "歷史不足 13 個月（前 12 個月起點之前沒有資料）",
    "REV_PRIOR_MONTH_MISSING": "前 12 個月有缺月（日曆不連續）",
    "REV_PRIOR_MONTH_NULL": "前 12 個月有營收為空值的月份",
    "REV_EQUAL_PRIOR_HIGH": "與前 12 個月最高值相等（相等不算新高）",
    # 年增率
    "REV_YOY_CURRENT_NULL": "當月營收為空值，不計算年增率",
    "REV_YOY_PREV_YEAR_NULL": "去年同月營收為空值，不計算年增率",
    "REV_YOY_PREV_YEAR_ZERO": "去年同月營收為 0，不適用",
    "REV_YOY_PREV_YEAR_NEGATIVE": "去年同月營收為負值，比率無意義，不適用",
}

OUTPUT_COLUMNS = [
    "symbol", "market", "revenue_month", "revenue_twd",
    "rev_12m_high", "rev_12m_high_status", "rev_12m_high_reason", "prior_12m_max_twd", "prior_12m_max_months",
    "prior_window_start", "prior_window_end", "missing_months", "null_months", "rev_12m_high_version",
    "revenue_prev_year_twd", "yoy_pct", "yoy_status", "yoy_reason", "source_yoy_pct", "yoy_version",
    "source", "fetched_at",
]


def month_add(ym: str, k: int) -> str:
    """'YYYY-MM' 加 k 個月（k 可為負）。格式不符拋 ValueError。"""
    s = str(ym).strip()
    if len(s) != 7 or s[4] != "-":
        raise ValueError(f"revenue_month 必須是 YYYY-MM：{ym!r}")
    y, m = int(s[:4]), int(s[5:])
    if not 1 <= m <= 12:
        raise ValueError(f"revenue_month 月份不合法：{ym!r}")
    n = y * 12 + (m - 1) + k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    f = float(v)
    return None if math.isnan(f) else f


def _rows(revenues: Any) -> list[dict]:
    if isinstance(revenues, pd.DataFrame):
        rows = revenues.to_dict("records")
    else:
        rows = [dict(r) for r in revenues]
    out = []
    for r in rows:
        if "is_current" in r and _num(r.get("is_current")) is not None and int(_num(r["is_current"])) != 1:
            continue
        for c in ("symbol", "revenue_month"):
            if r.get(c) is None or (isinstance(r.get(c), float) and math.isnan(r[c])):
                raise ValueError(f"monthly_revenues 列缺 {c}：{r}")
        out.append(r)
    return out


def _yoy(cur: Optional[float], prev: Optional[float]) -> tuple[Optional[float], str, Optional[str]]:
    if cur is None:
        return None, YOY_NOT_APPLICABLE, "REV_YOY_CURRENT_NULL"
    if prev is None:
        return None, YOY_NOT_APPLICABLE, "REV_YOY_PREV_YEAR_NULL"
    if prev == 0:
        return None, YOY_NOT_APPLICABLE, "REV_YOY_PREV_YEAR_ZERO"
    if prev < 0:
        return None, YOY_NOT_APPLICABLE, "REV_YOY_PREV_YEAR_NEGATIVE"
    return (cur / prev - 1.0) * 100.0, YOY_OK, None


def evaluate_revenue(revenues: Any, month: Optional[str] = None,
                     symbols: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """revenues：monthly_revenues 列（DataFrame 或 dict 序列；欄位 symbol, revenue_month, revenue_twd
    ［, market, revenue_prev_year_twd, yoy_pct, source, fetched_at, is_current］）。

    month：評估的營收月份 'YYYY-MM'；None＝每檔每個有列的月份都評估。
    symbols：限定代號；指定 month 時，清單中當月沒有列的代號也輸出一列（REV_CURRENT_MISSING）。
    回傳 OUTPUT_COLUMNS 的 DataFrame，依 symbol、revenue_month 排序。
    """
    rows = _rows(revenues)
    by: dict[str, dict[str, dict]] = {}
    for r in rows:
        sym, ym = str(r["symbol"]).strip(), month_add(str(r["revenue_month"]).strip(), 0)
        d = by.setdefault(sym, {})
        if ym in d:
            raise ValueError(f"monthly_revenues 現行列重複：{sym} {ym}")
        d[ym] = r
    want = None if symbols is None else [str(s).strip() for s in symbols]
    targets: list[tuple[str, str]] = []
    if month is not None:
        month = month_add(month, 0)
        ids = want if want is not None else sorted(s for s, d in by.items() if month in d)
        targets = [(s, month) for s in ids]
    else:
        ids = want if want is not None else sorted(by)
        targets = [(s, ym) for s in ids for ym in sorted(by.get(s, {}))]

    out = []
    for sym, ym in sorted(targets):
        hist = by.get(sym, {})
        cur = hist.get(ym)
        w0, w1 = month_add(ym, -WINDOW), month_add(ym, -1)
        rec = {c: None for c in OUTPUT_COLUMNS}
        rec.update(symbol=sym, revenue_month=ym, prior_window_start=w0, prior_window_end=w1,
                   rev_12m_high_version=REV_12M_HIGH_VERSION, yoy_version=REV_YOY_VERSION,
                   missing_months="", null_months="", prior_12m_max_months="")
        if cur is None:
            rec.update(rev_12m_high_status=STATUS_INSUFFICIENT, rev_12m_high_reason="REV_CURRENT_MISSING",
                       yoy_status=YOY_NOT_APPLICABLE, yoy_reason="REV_YOY_CURRENT_NULL")
            out.append(rec)
            continue
        v = _num(cur.get("revenue_twd"))
        prev_year = _num(cur.get("revenue_prev_year_twd"))
        rec.update(market=cur.get("market"), revenue_twd=v, revenue_prev_year_twd=prev_year,
                   source_yoy_pct=_num(cur.get("yoy_pct")), source=cur.get("source"), fetched_at=cur.get("fetched_at"))
        yoy, ys, yr = _yoy(v, prev_year)
        rec.update(yoy_pct=yoy, yoy_status=ys, yoy_reason=yr)

        window = [month_add(ym, -k) for k in range(WINDOW, 0, -1)]
        missing = [m for m in window if m not in hist]
        nulls = [m for m in window if m in hist and _num(hist[m].get("revenue_twd")) is None]
        vals = {m: _num(hist[m].get("revenue_twd")) for m in window if m in hist}
        known = [x for x in vals.values() if x is not None]
        if known:
            mx = max(known)
            rec.update(prior_12m_max_twd=mx, prior_12m_max_months=",".join(m for m in window if vals.get(m) == mx))
        rec.update(missing_months=",".join(missing), null_months=",".join(nulls))
        earliest = min(hist)
        if v is None:
            status, reason = STATUS_INSUFFICIENT, "REV_CURRENT_NULL"
        elif earliest > w0:
            status, reason = STATUS_INSUFFICIENT, "REV_INSUFFICIENT_HISTORY"
        elif missing:
            status, reason = STATUS_INSUFFICIENT, "REV_PRIOR_MONTH_MISSING"
        elif nulls:
            status, reason = STATUS_INSUFFICIENT, "REV_PRIOR_MONTH_NULL"
        elif v > rec["prior_12m_max_twd"]:
            status, reason = STATUS_TRUE, None
        elif v == rec["prior_12m_max_twd"]:
            status, reason = STATUS_FALSE, "REV_EQUAL_PRIOR_HIGH"
        else:
            status, reason = STATUS_FALSE, None
        rec.update(rev_12m_high_status=status, rev_12m_high_reason=reason,
                   rev_12m_high={STATUS_TRUE: True, STATUS_FALSE: False}.get(status))
        out.append(rec)
    df = pd.DataFrame(out, columns=OUTPUT_COLUMNS)
    for c in ("rev_12m_high", "rev_12m_high_reason", "yoy_reason", "market", "source", "fetched_at"):
        df[c] = df[c].astype(object).where(df[c].notna(), None)   # 缺值一律 None（不是 NaN 或 "nan"）
    return df


def load_monthly_revenues(db_path: Any, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """唯讀（sqlite URI mode=ro）讀 monthly_revenues 現行列。"""
    from contextlib import closing
    import io_sqlite
    sql = ("SELECT symbol, market, revenue_month, revenue_twd, revenue_prev_year_twd, yoy_pct, source, fetched_at, "
           "is_current FROM monthly_revenues WHERE is_current = 1")
    args: list = []
    if symbols is not None:
        sql += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args = list(symbols)
    with closing(io_sqlite.connect_readonly(db_path)) as con:
        return pd.read_sql_query(sql + " ORDER BY symbol, revenue_month", con, params=args)
