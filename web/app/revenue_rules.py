"""全站月營收年增率／累計年增率的「不適用」規則（HANDOFF §16.3 (a)、F-001 04:20 補充）。

- 單月年增率：一律採 analysis/revenue.py evaluate_revenue() 的 yoy_pct／yoy_status／yoy_reason（rev_yoy-v1）。
  去年同月為 0 → REV_YOY_PREV_YEAR_ZERO、為負 → REV_YOY_PREV_YEAR_NEGATIVE，顯示「不適用」。
- 累計年增率：analysis 沒有提供累計版本，網站不重算；照來源 cumulative_yoy_pct 顯示，
  但 cumulative_prev_year_twd 為 0 或負值時，同樣以 analysis 的 REV_YOY_PREV_YEAR_ZERO／NEGATIVE 代碼與說明顯示「不適用」
  （只判斷分母正負，不計算比率）。
"""
from __future__ import annotations

from typing import Optional

from .models import STATUS_ERROR, STATUS_INSUFFICIENT, STATUS_NOT_APPLICABLE

NA_CODES = ("REV_YOY_PREV_YEAR_ZERO", "REV_YOY_PREV_YEAR_NEGATIVE")
NA_TEXT = {"REV_YOY_PREV_YEAR_ZERO": "不適用（去年同月為 0）", "REV_YOY_PREV_YEAR_NEGATIVE": "不適用（去年同月為負值）"}
CUM_NA_TEXT = {"REV_YOY_PREV_YEAR_ZERO": "不適用（去年累計為 0）", "REV_YOY_PREV_YEAR_NEGATIVE": "不適用（去年累計為負值）"}


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def reason_codes() -> dict:
    from .screening import analysis_module
    return dict(analysis_module("revenue").REASON_CODES)


def yoy_by_month(rows: list[dict]) -> dict:
    """rows：同一檔的 monthly_revenues 現行列 → {月份: {"yoy_pct", "status", "reason", "reason_text", "version"}}；
    analysis 失敗 → {"__error__": 訊息}。"""
    try:
        from .screening import analysis_module
        R = analysis_module("revenue")
        ev = R.evaluate_revenue(rows, month=None)
        codes = dict(R.REASON_CODES)
    except Exception as e:
        return {"__error__": f"analysis/revenue.py 無法評估年增率（{type(e).__name__}：{e}）"}
    out = {}
    for e in ev.to_dict("records"):
        r = e.get("yoy_reason") if isinstance(e.get("yoy_reason"), str) else ""
        out[e["revenue_month"]] = {"yoy_pct": _num(e.get("yoy_pct")), "status": e.get("yoy_status") or "",
                                   "reason": r, "reason_text": codes.get(r, ""), "version": e.get("yoy_version") or ""}
    return out


def cum_na_reason(prev_cum) -> str:
    """去年累計營收 0／負值 → 對應 analysis 代碼；其他 ""。"""
    v = _num(prev_cum)
    if v is None:
        return ""
    if v == 0:
        return "REV_YOY_PREV_YEAR_ZERO"
    if v < 0:
        return "REV_YOY_PREV_YEAR_NEGATIVE"
    return ""


def yoy_status_for(info: Optional[dict]) -> tuple[str, str]:
    """analysis yoy 結果 → (網站狀態, 說明)。ok 回 ("ok", "")。"""
    if not info:
        return STATUS_INSUFFICIENT, "analysis 沒有此月份的年增率結果"
    if info.get("status") == "ok" and info.get("yoy_pct") is not None:
        return "ok", ""
    r = info.get("reason", "")
    st = STATUS_NOT_APPLICABLE if r in NA_CODES else STATUS_INSUFFICIENT
    return st, f"{NA_TEXT.get(r, '')}｜{r}：{info.get('reason_text', '')}" if r in NA_CODES else f"{r}：{info.get('reason_text', '')}"


__all__ = ["yoy_by_month", "cum_na_reason", "yoy_status_for", "NA_CODES", "NA_TEXT", "CUM_NA_TEXT", "reason_codes",
           "STATUS_ERROR"]
