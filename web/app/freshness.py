"""來源分類與過期判斷（HANDOFF ①②）。網站只顯示結果，模板不比日期。

- category：source_id → quotes／revenue／material_info／financials／news（另有 corporate_actions、reference
  兩類不在頁頂狀態列顯示）。對照集中在 SOURCE_CATEGORY_RULES。
- expected_data_date：依 trading_calendar 與公告期限推出「此刻應已有的最新資料日」。
- stale：以資料表實際最新資料日（使用者看到的）對 expected 比較；表無資料時退回 source_status.last_data_date。
  True＝過期、False＝未過期、None＝不判斷（重大訊息、新聞沒有固定公告頻率；日期無法解析）。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

TZ = dt.timezone(dt.timedelta(hours=8))

# (前綴或完整 id, 類別)；依序比對，先符合者為準
SOURCE_CATEGORY_RULES: list[tuple[str, str]] = [
    ("twse_stock_day", "quotes"), ("twse_mi_index", "quotes"), ("tpex_trading_stock", "quotes"),
    ("tpex_daily_quotes", "quotes"), ("twse_fmtqik", "quotes"), ("tpex_st41", "quotes"),
    ("mops_nas_revenue", "revenue"), ("mops_monthly_revenue", "revenue"), ("mops_revenue", "revenue"),
    ("mops_material", "material_info"),
    ("mops_income", "financials"), ("mops_cf", "financials"), ("mops_financials", "financials"),
    ("news", "news"),
    ("twse_twt49u", "corporate_actions"), ("twse_twtauu", "corporate_actions"), ("twse_twtawu", "corporate_actions"),
    ("twse_twtb8u", "corporate_actions"), ("tpex_exright", "corporate_actions"), ("tpex_pvchgrslt", "corporate_actions"),
    ("tpex_revivt", "corporate_actions"), ("tpex_spendi", "corporate_actions"),
    ("isin_", "reference"), ("twse_holiday", "reference"),
]
CATEGORY_LABELS = {"quotes": "行情", "revenue": "月營收", "material_info": "重大訊息", "financials": "財報",
                   "news": "新聞", "corporate_actions": "除權息／減資", "reference": "基本資料／行事曆",
                   "other": "其他"}
# 行情過期切點（DECISIONS 2026-11-10 04:36：18:00 主更新後留 30 分鐘緩衝）
QUOTES_READY = dt.time(18, 30)
REVENUE_DEADLINE_DAY = 10                                   # 每月 10 日前申報上月營收
QUARTER_DEADLINES = {1: (5, 15), 2: (8, 14), 3: (11, 14)}   # Q4（年報）次年 3/31


def source_category(source_id: str) -> str:
    sid = (source_id or "").lower()
    for prefix, cat in SOURCE_CATEGORY_RULES:
        if sid.startswith(prefix.lower()):
            return cat
    return "other"


def parse_data_date(v) -> Optional[tuple[str, str]]:
    """把混合格式轉成 (粒度, 標準字串)：day→YYYY-MM-DD、month→YYYY-MM、quarter→YYYYQn；無法解析回 None。"""
    s = str(v or "").strip()
    if m := re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})(?:[T ].*)?", s):
        return "day", f"{m[1]}-{m[2]}-{m[3]}"
    if m := re.fullmatch(r"(\d{4})-?(\d{2})", s):
        return "month", f"{m[1]}-{m[2]}"
    if m := re.fullmatch(r"(\d{4})\s*Q([1-4])", s, re.I):
        return "quarter", f"{m[1]}Q{m[2]}"
    return None


def expected_for(category: str, now: dt.datetime, trade_days: Iterable[str]) -> tuple[Optional[str], str]:
    """回傳 (expected_data_date, 依據說明)。"""
    today = now.date()
    if category == "quotes":
        days = sorted(d for d in trade_days if d <= today.isoformat())
        if not days:
            return None, "trading_calendar 沒有今天以前的交易日"
        if days[-1] == today.isoformat() and now.time() < QUOTES_READY:
            days = days[:-1]
        if not days:
            return None, "trading_calendar 沒有前一個交易日"
        return days[-1], f"trading_calendar 最近已收盤交易日（當日 {QUOTES_READY:%H:%M} 後才算）"
    if category == "revenue":
        y, m = today.year, today.month - (1 if today.day > REVENUE_DEADLINE_DAY else 2)
        while m <= 0:
            y, m = y - 1, m + 12
        return f"{y:04d}-{m:02d}", f"每月 {REVENUE_DEADLINE_DAY} 日申報期限"
    if category == "financials":
        cands = []
        for yr in (today.year - 1, today.year):
            for q, (mm, dd) in QUARTER_DEADLINES.items():
                cands.append((dt.date(yr, mm, dd), f"{yr}Q{q}"))
            cands.append((dt.date(yr + 1, 3, 31), f"{yr}Q4"))
        past = [c for c in cands if c[0] < today]
        return (max(past)[1], "季報／年報申報期限") if past else (None, "")
    return None, "不判斷：此類來源沒有固定公告頻率"


def judge(category: str, now: dt.datetime, trade_days: Iterable[str], table_latest: Optional[str],
          last_data_date) -> dict:
    expected, why = expected_for(category, now, trade_days)
    actual, basis = (table_latest, "資料表最新資料日") if table_latest else (last_data_date, "source_status.last_data_date")
    pa = parse_data_date(actual)
    stale, note = None, why
    if expected and pa:
        a = pa[1]
        if category == "quotes" and pa[0] != "day":
            note = f"{why}；{basis}「{actual}」不是日期，無法比較"
        else:
            if category == "revenue":
                a = a[:7]
            stale = a < expected
            note = f"{why}；比較 {basis}"
    elif expected:
        note = f"{why}；沒有可比較的資料日"
    return {"category": category, "category_label": CATEGORY_LABELS.get(category, category),
            "expected_data_date": expected or "", "latest_data_date": (pa[1] if pa else (actual or "")),
            "stale": stale, "stale_basis": basis if expected and pa else "", "stale_note": note}


def fmt_time(v) -> str:
    """ISO 時間 → 'YYYY-MM-DD HH:MM'（HANDOFF ⑧）；非 ISO 原樣回傳。"""
    s = str(v or "")
    if m := re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", s):
        return f"{m[1]} {m[2]}"
    return s
