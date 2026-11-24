"""篩選中心新增模式 M9～M21（analysis-0.10.0）與全部模式登記表（M1～M21）——純函式模組。

只做計算：無檔案／網路／資料庫 I/O（讀取用 io_sqlite.load_mode_inputs）；相同輸入得相同輸出，不修改傳入的 DataFrame。
規格：docs/indicators.md 第 12 節（專案負責人核可 2026-11-24 16:05，暫定分級）；研究依據 docs/rd/SCREEN_MODES_v2.md。

共同規則
- 可評估：先跑 screener.screen()（預設參數＝M1 的排除規則），當日 status 為 EXCLUDED 者在每個新模式都以同一排除碼排除
  （停牌、無成交、上市未滿、視窗缺口、調整因子缺漏…；NOT_EVALUABLE_CODES 與 95% 覆蓋率口徑沿用 screener.coverage_stats）。
- 價格：series.ohlc_series()（與 screener 同一套前復權與可用範圍，以評估日為基準）；N 日視窗以「有效交易日」計
  （跳過停牌／無成交日）。模式需要的有效交易日數超過可用範圍 → 以可用範圍的原因碼排除；行情不夠長 → 上市未滿
  （securities.listed_date 顯示上市日在視窗內）記 EXCL_NEW_LISTING，否則 EXCL_INSUFFICIENT_HISTORY。
- 不前視：只用 ≤ 評估日的行情與事件；月營收可得日＝次月 10 日、財報＝5/15、8/14、11/14、次年 3/31（法定期限，§11.0）；
  評估日已過期限的最新月份／季度若無資料 → 資料不足（不以更舊的月份替代）。
- 橫斷面排名（M12、M14、M19）：在「本次評估範圍內、當日可評估且有該數值」的股票之間排名（pandas rank pct，平均名次）；
  評估範圍不是全市場時，排名意義不同，網站須標示。
- 零檔就是零檔，不放寬；勝率不在這裡計算，網站一律顯示 WINRATE_DISPLAY（「計算中」）直到 3 年回補後重算。
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Optional

import numpy as np
import pandas as pd

import adjust as A
import revenue as RV
import screener as S
import series as SE
from version import ANALYSIS_VERSION

__all__ = ["REGISTRY_VERSION", "TIER_BASIS", "WINRATE_DISPLAY", "FIN_CODES", "MODE_EXCLUSION_CODES", "REGISTRY",
           "registry", "registry_json", "mode", "NEW_MODE_IDS", "SUMMARY_COLUMNS", "CONDITION_COLUMNS",
           "ModesResult", "evaluate_modes", "eps_quarter_state", "revenue_month_available", "quarter_available"]

REGISTRY_VERSION = "hub-modes-v1"
TIER_BASIS = ("暫定分級（專案負責人核可 2026-11-24 16:05）：依每日符合檔數（中位數）與 2026-05-27～2026-11-16 的 5 日歷史統計"
              "（docs/rd/SCREEN_MODES_v2.md）；3 年回補後依同檔「預先登記」規則重算再定。等級只是標示，不代表保證或較準。")
WINRATE_DISPLAY = "計算中"
WINRATE_NOTE = "歷史勝率待 3 年行情回補後依 docs/indicators.md §11 重算；初步研究數字不對外顯示。"
TV_MIN = 100_000_000.0
FEE_NOTE = "歷史統計，不代表未來，非投資建議"

FIN_CODES = MappingProxyType({
    "FIN_CURRENT_MISSING": "最新已過申報期限的季度沒有財報資料列，或 EPS 為空值（資料不足）",
    "FIN_PREV_CUM_MISSING": "缺上一季累計 EPS，無法推算單季 EPS（資料不足）",
    "FIN_PRIOR_YEAR_MISSING": "去年同季單季 EPS 無法推算（資料不足）",
})
MODE_EXCLUSION_CODES = MappingProxyType({**S.EXCLUSION_CODES, **RV.REASON_CODES, **FIN_CODES})

# ---------------------------------------------------------------- 登記表 ----
_ENG_NEW = "analysis/modes.py evaluate_modes()"
_W = {"display": WINRATE_DISPLAY, "note": WINRATE_NOTE}
_WQ = {"display": "樣本不足", "note": "季頻訊號，一年約 4 個訊號日（indicators.md §11.6.1）"}


def _m(id, tier, name, desc, conditions, *, research_id=None, tier_prev=None, engine, params=None, full_params=None,
       history_days=60, full_history_days=None, data=("daily_prices",), metric=None, win=None, cross=False, note=""):
    return MappingProxyType(dict(
        id=id, tier=tier, tier_prev=tier_prev, name=name, desc=desc, conditions=tuple(conditions),
        research_id=research_id, engine=engine, params=MappingProxyType(dict(params or {})),
        full_params=None if full_params is None else MappingProxyType(dict(full_params)),
        active_variant="short", history_days=history_days, full_history_days=full_history_days,
        data=tuple(data), metric=MappingProxyType(_metric(metric)), win_rate=MappingProxyType(dict(win or _W)),
        cross_sectional=cross, state="production" if engine == _ENG_NEW else "existing", note=note))


def _metric(m):
    """排序指標：summary.metric 為原始比例；scale＝顯示時乘上的倍數（百分比為 100）。"""
    if not m:
        return {}
    return dict(m, scale=100 if "%" in m["label"] else 1)


_PX = ("daily_prices", "corporate_actions", "trading_calendar", "securities")
REGISTRY = (
    # ---- 現有 M1～M8：規則不變，只依暫定分級重新標示等級（tier_prev＝原等級）
    _m("M1", "進階", "趨勢＋突破＋量能", "網站預設的四個條件同時成立",
       ["收盤 > MA20 > MA60", "收盤 > 前 20 個交易日最高價（不含當日）", "成交量 ≥ 前 20 日均量 × 1.5（不含當日）", "成交金額 ≥ 1 億元"],
       tier_prev="基本", engine="analysis/screener.py screen()（預設參數）", data=_PX,
       metric=dict(key="volume_ratio", label="量比（倍）", digits=2), note="這段研究期間相對基準沒有優勢；規則不變"),
    _m("M2", "基本", "均線多頭排列", "股價在 5、20、60 日均線之上且均線由短到長依序排列",
       ["收盤 > MA5 > MA20 > MA60", "成交金額 ≥ 1 億元"], tier_prev="基本",
       engine="analysis/screener.py screen() 兩次（web/app/hub.py）", data=_PX,
       metric=dict(key="dist_ma60", label="收盤距 60 日均線（%）", digits=2)),
    _m("M3", "基本", "月營收創 12 個月新高", "最新可得月營收比前 12 個月都高",
       ["最新可得月份營收嚴格大於前 12 個月最高（rev_12m_high-v1）"], tier_prev="基本",
       engine="analysis/revenue.py evaluate_revenue()", data=("monthly_revenues",),
       metric=dict(key="yoy", label="月營收年增率（%）", digits=2)),
    _m("M4", "基本", "營收連續 3 個月年增", "最近 3 個月營收都比去年同月高",
       ["最近可得連續 3 個月年增率都 > 0（rev_yoy-v1）"], tier_prev="進階",
       engine="analysis/revenue.py evaluate_revenue()", data=("monthly_revenues",),
       metric=dict(key="yoy", label="最近月年增率（%）", digits=2)),
    _m("M5", "基本", "單季 EPS 年增", "最新一季每股盈餘為正且高於去年同季",
       ["單季 EPS > 0 且 > 去年同季單季 EPS"], tier_prev="進階",
       engine="尚未提供（可用 analysis/modes.py eps_quarter_state()）", data=("income_statements",), win=_WQ),
    _m("M6", "進階", "創 250 日新高", "收盤創 250 個交易日新高",
       ["收盤 > 前 250 個交易日最高價（不含當日）", "成交金額 ≥ 1 億元"], tier_prev="進階",
       engine="analysis/screener.py screen()（突破 250 日）", data=_PX, history_days=251,
       metric=dict(key="dist_high", label="收盤距前高（%）", digits=2),
       note="行情回補前無法評估；等級待回補後依預先登記規則判定（暫維持原等級）"),
    _m("M7", "基本", "營收創高＋趨勢", "營收創 12 個月新高且股價在均線之上",
       ["M3 為真", "收盤 > MA20 > MA60"], tier_prev="高級",
       engine="analysis/revenue.py evaluate_revenue() ＋ analysis/screener.py screen()", data=_PX + ("monthly_revenues",),
       metric=dict(key="yoy", label="月營收年增率（%）", digits=2)),
    _m("M8", "基本", "EPS 成長＋營收連續成長＋趨勢", "EPS 年增、營收連 3 月年增且股價在均線之上",
       ["M5 為真", "M4 為真", "收盤 > MA20 > MA60"], tier_prev="高級",
       engine="尚未提供（可用 analysis/modes.py eps_quarter_state()）", data=_PX + ("monthly_revenues", "income_statements"), win=_WQ),
    # ---- 新增 M9～M21
    _m("M9", "基本", "站上季線", "股價在 60 日均線之上、成交金額 1 億元以上",
       ["收盤 > MA60", "成交金額 ≥ 1 億元"], research_id="C28", engine=_ENG_NEW, data=_PX,
       params=dict(ma=60, min_turnover=TV_MIN), history_days=60,
       metric=dict(key="dist_ma", label="收盤距 60 日均線（%）", digits=2)),
    _m("M10", "基本", "營收創高＋站上季線", "最新月營收創 12 個月新高，股價在季線之上",
       ["最新可得月營收創 12 個月新高（rev_12m_high-v1）", "收盤 > MA60", "成交金額 ≥ 1 億元"], research_id="C29",
       engine=_ENG_NEW, data=_PX + ("monthly_revenues",), params=dict(ma=60, min_turnover=TV_MIN), history_days=60,
       metric=dict(key="yoy", label="最新月營收年增率（%）", digits=2)),
    _m("M11", "基本", "營收年增加速", "最近 3 個月營收年增率一個月比一個月高",
       ["最近 3 個可得月份年增率 y(m) > y(m−1) > y(m−2)，且 y(m) > 0", "收盤 > MA20", "成交金額 ≥ 1 億元"],
       research_id="C31", engine=_ENG_NEW, data=_PX + ("monthly_revenues",), params=dict(ma=20, min_turnover=TV_MIN),
       history_days=60, metric=dict(key="yoy", label="最新月營收年增率（%）", digits=2)),
    _m("M12", "基本", "短期超跌", "近 5 日跌幅在評估範圍最深的 10%（逆勢，可能續跌）",
       ["近 5 個有效交易日漲跌幅排名在當日可評估股票最低 10%", "成交金額 ≥ 1 億元"], research_id="C24",
       engine=_ENG_NEW, data=_PX, params=dict(ret_days=5, max_pct_rank=0.10, min_turnover=TV_MIN), history_days=60,
       metric=dict(key="ret", label="近 5 日漲跌幅（%）", digits=2), cross=True),
    _m("M13", "進階", "接近 120 日高點", "收盤在近 120 日最高價 5% 以內，且在季線之上",
       ["收盤 ≥ 近 120 個有效交易日最高價（含當日）× 0.95", "收盤 > MA60", "成交金額 ≥ 1 億元"], research_id="C05",
       engine=_ENG_NEW, data=_PX, params=dict(high_days=120, near=0.95, ma=60, min_turnover=TV_MIN),
       full_params=dict(high_days=250, near=0.95, ma=60, min_turnover=TV_MIN), history_days=120, full_history_days=250,
       metric=dict(key="dist_high", label="收盤距 120 日高（%）", digits=2), note="縮短版：原策略為 52 週高點（250 日）"),
    _m("M14", "進階", "趨勢樣板（Minervini 縮短版）", "均線由短到長依序排列、長均線上升、離低點夠遠、離高點夠近、相對強勢",
       ["收盤 > MA20 > MA60 > MA120", "MA120 > 20 個有效交易日前的 MA120", "收盤 ≥ 近 120 日最低價 × 1.3",
        "收盤 ≥ 近 120 日最高價 × 0.75", "近 60 日漲幅排名前 30%（評估範圍內）", "成交金額 ≥ 1 億元"], research_id="C01",
       engine=_ENG_NEW, data=_PX,
       params=dict(ma_s=20, ma_m=60, ma_l=120, slope_days=20, range_days=120, above_low=1.3, below_high=0.75,
                   rs_days=60, min_rs_pct=0.70, min_turnover=TV_MIN),
       full_params=dict(ma_s=50, ma_m=150, ma_l=200, slope_days=20, range_days=250, above_low=1.3, below_high=0.75,
                        rs_days=250, min_rs_pct=0.70, min_turnover=TV_MIN),
       history_days=140, full_history_days=250, metric=dict(key="ret", label="近 60 日漲幅（%）", digits=2), cross=True,
       note="縮短版：原版為 50／150／200 日均線、52 週高低與 12 個月相對強度"),
    _m("M15", "進階", "RSI(2) 多頭回檔", "季線之上的股票短線急跌（RSI(2) ≤ 10）",
       ["收盤 > MA60", "RSI(2) ≤ 10（Wilder 平滑，取最近 60 個有效交易日計算）", "成交金額 ≥ 1 億元"], research_id="C09",
       engine=_ENG_NEW, data=_PX, params=dict(ma=60, rsi_n=2, rsi_max=10.0, min_turnover=TV_MIN),
       full_params=dict(ma=200, rsi_n=2, rsi_max=10.0, min_turnover=TV_MIN), history_days=60, full_history_days=200,
       metric=dict(key="rsi", label="RSI(2)", digits=1), note="縮短版：原版（Connors）用 200 日均線"),
    _m("M16", "進階", "多頭回檔月線", "上升中的月線，量縮回檔碰到月線後收紅",
       ["MA20 > MA60", "MA20 > 5 個有效交易日前的 MA20", "最低價 ≤ MA20 × 1.01", "收盤 > MA20", "收盤 > 開盤（紅 K）",
        "成交量 < 前 20 日均量（不含當日）", "成交金額 ≥ 1 億元"], research_id="C13", engine=_ENG_NEW, data=_PX,
       params=dict(ma_s=20, ma_l=60, slope_days=5, touch=1.01, vol_days=20, min_turnover=TV_MIN), history_days=60,
       metric=dict(key="dist_ma", label="收盤距 20 日均線（%）", digits=2)),
    _m("M17", "進階", "均線糾結突破", "5／10／20 日均線糾結後，帶量站上三條均線",
       ["前 5 個有效交易日 MA5／MA10／MA20 最大÷最小 − 1 都 < 2%", "收盤 > max(MA5, MA10, MA20)", "收盤 > 前一日收盤",
        "成交量 ≥ 前 20 日均量 × 1.25（不含當日）", "成交金額 ≥ 1 億元"], research_id="C17", engine=_ENG_NEW, data=_PX,
       params=dict(mas=(5, 10, 20), spread_max=0.02, spread_days=5, vol_days=20, vol_mult=1.25, min_turnover=TV_MIN),
       history_days=60, metric=dict(key="volume_ratio", label="量比（倍）", digits=2)),
    _m("M18", "高級", "創 120 日新高", "收盤創 120 個交易日新高（M6 的縮短版）",
       ["收盤 > 前 120 個有效交易日最高價（不含當日）", "成交金額 ≥ 1 億元"], research_id="M6s", engine=_ENG_NEW, data=_PX,
       params=dict(high_days=120, min_turnover=TV_MIN), full_params=dict(high_days=250, min_turnover=TV_MIN),
       history_days=121, full_history_days=251, metric=dict(key="dist_high", label="收盤距前高（%）", digits=2),
       note="縮短版；完整版（250 日）即現有 M6 的定義"),
    _m("M19", "高級", "CANSLIM 簡化", "EPS 年增 25% 以上、股價接近高點且相對強勢，今天帶量突破",
       ["最新已過申報期限的單季 EPS > 0、去年同季 > 0，且 ≥ 去年同季 × 1.25", "收盤 ≥ 近 120 日最高價 × 0.85",
        "近 60 日漲幅排名前 20%（評估範圍內）", "收盤 > 前 20 日最高價（不含當日）", "成交量 ≥ 前 50 日均量 × 1.4（不含當日）",
        "成交金額 ≥ 1 億元"], research_id="C35", engine=_ENG_NEW, data=_PX + ("income_statements",),
       params=dict(eps_growth=1.25, high_days=120, near=0.85, rs_days=60, min_rs_pct=0.80, breakout_days=20,
                   vol_days=50, vol_mult=1.4, min_turnover=TV_MIN),
       full_params=dict(eps_growth=1.25, high_days=250, near=0.85, rs_days=250, min_rs_pct=0.80, breakout_days=20,
                        vol_days=50, vol_mult=1.4, min_turnover=TV_MIN),
       history_days=120, full_history_days=250, metric=dict(key="eps_yoy", label="單季 EPS 年增率（%）", digits=1), cross=True,
       note="縮短版：原版用 52 週高與 12 個月相對強度"),
    _m("M20", "高級", "三白兵（低檔轉強）", "連 3 天紅 K 收高，站上月線但仍在季線下",
       ["連續 3 個有效交易日收盤 > 前一日收盤且收盤 > 開盤", "收盤 > MA20", "收盤 < MA60", "成交金額 ≥ 1 億元"],
       research_id="C26", engine=_ENG_NEW, data=_PX, params=dict(days=3, ma_s=20, ma_l=60, min_turnover=TV_MIN),
       history_days=60, metric=dict(key="ret", label="近 3 日漲幅（%）", digits=2)),
    _m("M21", "高級", "月線上穿季線", "20 日均線今天由下往上穿過 60 日均線",
       ["MA20 > MA60", "前一有效交易日 MA20 ≤ MA60", "成交金額 ≥ 1 億元"], research_id="C06", engine=_ENG_NEW, data=_PX,
       params=dict(ma_s=20, ma_l=60, min_turnover=TV_MIN), history_days=61,
       metric=dict(key="dist_ma", label="收盤距 60 日均線（%）", digits=2)),
)
_BY_ID = {m["id"]: m for m in REGISTRY}
NEW_MODE_IDS = tuple(m["id"] for m in REGISTRY if m["engine"] == _ENG_NEW)
TIERS = (("基本", "每日中位符合檔數約 75 檔以上"), ("進階", "其餘通過篩選的模式"),
         ("高級", "每日中位 15 檔以下，且研究期間 5 日勝率顯著高於同日基準"))


def mode(mid: str):
    return _BY_ID[mid]


def _plain(x):
    if isinstance(x, MappingProxyType):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def registry() -> dict:
    """可 JSON 化的登記表（網站讀這份；檔案版 analysis/mode_registry.json 由 registry_json() 產生，測試檢查一致）。"""
    return dict(registry_version=REGISTRY_VERSION, analysis_version=ANALYSIS_VERSION, tier_basis=TIER_BASIS,
                tiers=[dict(name=n, rule=r) for n, r in TIERS], winrate_display=WINRATE_DISPLAY, winrate_note=WINRATE_NOTE,
                disclaimer=FEE_NOTE, sort_note="排序僅為排列方式，不代表優劣。",
                cross_sectional_note="排名在本次評估範圍內計算；評估範圍不是全市場時，排名意義不同。",
                modes=[_plain(m) for m in REGISTRY])


def registry_json() -> str:
    return json.dumps(registry(), ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------- 可得日 ----
def revenue_month_available(as_of: Any) -> str:
    """評估日可用的最新營收月份 m：次月 10 日（法定期限）≤ 評估日（§11.0）。"""
    d = A.to_ts(as_of).date()
    k = 1 if d.day >= 10 else 2
    return RV.month_add(f"{d.year:04d}-{d.month:02d}", -k)


def _q_deadline(y: int, q: int) -> dt.date:
    return {1: dt.date(y, 5, 15), 2: dt.date(y, 8, 14), 3: dt.date(y, 11, 14), 4: dt.date(y + 1, 3, 31)}[q]


def quarter_available(as_of: Any) -> tuple[int, int]:
    """評估日可用的最新季度（法定期限 ≤ 評估日）。"""
    d = A.to_ts(as_of).date()
    for y in (d.year, d.year - 1, d.year - 2):
        for q in (4, 3, 2, 1):
            if _q_deadline(y, q) <= d:
                return y, q
    raise ValueError("找不到可得季度")


def eps_quarter_state(income: Optional[pd.DataFrame], as_of: Any, symbols: Iterable[str]) -> dict[str, dict]:
    """每檔在評估日可得的最新季度單季 EPS 與去年同季（只用 period_type=累計、report_basis=合併 的現行列；
    單季＝本季累計−上季累計，Q1＝累計）。回傳 {symbol: dict(year, quarter, eps_q, eps_q_ly, code)}；code 為 FIN_* 或 None。"""
    y, q = quarter_available(as_of)
    cum: dict[tuple, float] = {}
    if income is not None and len(income):
        df = income
        if "is_current" in df.columns:
            df = df[pd.to_numeric(df["is_current"], errors="coerce") == 1]
        if "period_type" in df.columns:
            df = df[df["period_type"] == "累計"]
        if "report_basis" in df.columns:
            df = df[df["report_basis"] == "合併"]
        keys = list(zip(df["symbol"].astype(str), df["fiscal_year"].astype(int), df["fiscal_quarter"].astype(int)))
        if len(keys) != len(set(keys)):
            raise ValueError("income_statements 現行列重複（symbol, fiscal_year, fiscal_quarter）")
        for k, e in zip(keys, pd.to_numeric(df["eps_twd"], errors="coerce")):
            cum[k] = None if pd.isna(e) else float(e)

    def single(s, yy, qq):
        c = cum.get((s, yy, qq))
        if c is None:
            return None, "missing"
        if qq == 1:
            return c, None
        p = cum.get((s, yy, qq - 1))
        if p is None:
            return None, "prev"
        return c - p, None

    out = {}
    for s in symbols:
        s = str(s)
        eq, why = single(s, y, q)
        code = None
        if why == "missing":
            code = "FIN_CURRENT_MISSING"
        elif why == "prev":
            code = "FIN_PREV_CUM_MISSING"
        ely = None
        if code is None:
            ely, why2 = single(s, y - 1, q)
            if why2 is not None:
                code = "FIN_PRIOR_YEAR_MISSING"
        out[s] = dict(year=y, quarter=q, eps_q=eq, eps_q_ly=ely, code=code)
    return out


def _revenue_state(revenues: Optional[pd.DataFrame], as_of: Any, symbols: list[str]) -> dict[str, dict]:
    """每檔：目標月份 m 的 12 個月新高狀態、m／m−1／m−2 年增率與原因碼（revenue.evaluate_revenue）。"""
    m = revenue_month_available(as_of)
    months = [RV.month_add(m, -k) for k in range(0, 15)]
    out = {s: dict(month=m, rows={}) for s in symbols}
    if revenues is None or not len(revenues):
        return out
    rv = revenues[revenues["revenue_month"].astype(str).isin(months) & revenues["symbol"].astype(str).isin(set(symbols))]
    if not len(rv):
        return out
    ev = RV.evaluate_revenue(rv)
    for r in ev.to_dict("records"):
        out[str(r["symbol"])]["rows"][r["revenue_month"]] = r
    return out


# ---------------------------------------------------------------- 指標 ----
def _sma(c: np.ndarray, n: int, k: int = 0) -> float:
    """以有效交易日計的 n 日簡單平均，結束於倒數第 k 日（k=0＝評估日）。不足回 nan。"""
    end = len(c) - k
    if n <= 0 or end - n < 0:
        return math.nan
    return float(np.mean(c[end - n:end]))


def _max(x: np.ndarray, n: int, prior: bool) -> float:
    end = len(x) - (1 if prior else 0)
    if end - n < 0:
        return math.nan
    return float(np.max(x[end - n:end]))


def _min(x: np.ndarray, n: int) -> float:
    if len(x) < n:
        return math.nan
    return float(np.min(x[len(x) - n:]))


def _avg_prior(v: np.ndarray, n: int) -> float:
    if len(v) - 1 - n < 0:
        return math.nan
    return float(np.mean(v[len(v) - 1 - n:len(v) - 1]))


def _ret(c: np.ndarray, n: int) -> float:
    if len(c) - 1 - n < 0:
        return math.nan
    return float(c[-1] / c[-1 - n] - 1)


def _rsi(c: np.ndarray, n: int, window: int = 60) -> float:
    """Wilder RSI：取最近 window 個有效交易日（含評估日），alpha=1/n、adjust=False。"""
    x = pd.Series(c[-window:])
    if len(x) < n + 1:
        return math.nan
    d = x.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]
    if ad == 0 or pd.isna(ad):
        return math.nan
    return float(100 - 100 / (1 + au / ad))


# ---------------------------------------------------------------- 文字 ----
def _p(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.2f} 元"


def _pct(x, d=2):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{d}f}%"


def _yi(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x / 1e8:,.2f} 億元"


def _sh(x):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.0f} 股"


def _cmp(a, b) -> bool:
    return not (a is None or b is None or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)))


def _C(cid, name, ok, actual, threshold, value=None):
    return dict(condition_id=cid, condition_name=name, passed=bool(ok), actual_text=actual, threshold_text=threshold,
                actual_value=value)


def _tv_cond(amount, p):
    return _C("TURNOVER", "成交金額", _cmp(amount, 0) and amount >= p["min_turnover"], _yi(amount),
              f"≥ {p['min_turnover'] / 1e8:,.0f} 億元", amount)


# ---------------------------------------------------------------- 各模式 ----
# 每個函式：f＝該檔資料（C,H,L,O,V 有效日陣列、amount、rank、revenue、eps），p＝參數；
# 回傳 (需要的有效交易日數, 條件清單, 排序指標) 或 ("EXCL", 代碼)。

def _need(p, mid, variant):
    m = _BY_ID[mid]
    return m["full_history_days"] if (variant == "full" and m["full_history_days"]) else m["history_days"]


def _m9(f, p):
    C = f["C"]; ma = _sma(C, p["ma"])
    return [_C("MA", f"收盤 > MA{p['ma']}", C[-1] > ma, _p(C[-1]), f"> {_p(ma)}", C[-1]), _tv_cond(f["amount"], p)], C[-1] / ma - 1


def _m10(f, p):
    rs = f["rev"]; r = rs["rows"].get(rs["month"])
    if r is None:
        return "EXCL", "REV_CURRENT_MISSING"
    if r["rev_12m_high_status"] == RV.STATUS_INSUFFICIENT:
        return "EXCL", r["rev_12m_high_reason"]
    C = f["C"]; ma = _sma(C, p["ma"])
    hi = r["rev_12m_high_status"] == RV.STATUS_TRUE
    yoy = r["yoy_pct"]
    return [_C("REV_HIGH", f"{rs['month']} 月營收創 12 個月新高", hi, _yi(r["revenue_twd"]),
               f"> 前 12 個月最高 {_yi(r['prior_12m_max_twd'])}", r["revenue_twd"]),
            _C("MA", f"收盤 > MA{p['ma']}", C[-1] > ma, _p(C[-1]), f"> {_p(ma)}", C[-1]),
            _tv_cond(f["amount"], p)], (None if yoy is None else yoy / 100)


def _m11(f, p):
    rs = f["rev"]; m = rs["month"]
    rows = [rs["rows"].get(RV.month_add(m, -k)) for k in (0, 1, 2)]
    if rows[0] is None:
        return "EXCL", "REV_CURRENT_MISSING"
    if rows[1] is None or rows[2] is None:
        return "EXCL", "REV_PRIOR_MONTH_MISSING"
    for r in rows:
        if r["yoy_status"] != RV.YOY_OK:
            return "EXCL", r["yoy_reason"]
    y0, y1, y2 = (float(r["yoy_pct"]) for r in rows)
    C = f["C"]; ma = _sma(C, p["ma"])
    txt = f"{RV.month_add(m, -2)} {y2:+.2f}%、{RV.month_add(m, -1)} {y1:+.2f}%、{m} {y0:+.2f}%"
    return [_C("YOY_ACCEL", "營收年增率逐月上升且最新 > 0", (y0 > y1) and (y1 > y2) and (y0 > 0), txt, "y(m) > y(m−1) > y(m−2)，y(m) > 0", y0),
            _C("MA", f"收盤 > MA{p['ma']}", C[-1] > ma, _p(C[-1]), f"> {_p(ma)}", C[-1]),
            _tv_cond(f["amount"], p)], y0 / 100


def _m12(f, p):
    r = f["ret5"]; rk = f["ret5_pct"]
    return [_C("RET_RANK", f"近 {p['ret_days']} 日漲跌幅排名", _cmp(rk, 0) and rk <= p["max_pct_rank"],
               f"{_pct(r)}（由低到高第 {rk * 100:.1f} 百分位）" if _cmp(rk, 0) else "—",
               f"≤ 第 {p['max_pct_rank'] * 100:.0f} 百分位", rk),
            _tv_cond(f["amount"], p)], r


def _m13(f, p):
    C, H = f["C"], f["H"]; hh = _max(H, p["high_days"], prior=False); ma = _sma(C, p["ma"])
    return [_C("NEAR_HIGH", f"收盤 ≥ 近 {p['high_days']} 日最高 × {p['near']}", C[-1] >= p["near"] * hh, _p(C[-1]),
               f"≥ {_p(p['near'] * hh)}（最高 {_p(hh)}）", C[-1]),
            _C("MA", f"收盤 > MA{p['ma']}", C[-1] > ma, _p(C[-1]), f"> {_p(ma)}", C[-1]),
            _tv_cond(f["amount"], p)], C[-1] / hh - 1


def _m14(f, p):
    C, H, L = f["C"], f["H"], f["L"]
    s, m, l = _sma(C, p["ma_s"]), _sma(C, p["ma_m"]), _sma(C, p["ma_l"])
    l_prev = _sma(C, p["ma_l"], p["slope_days"])
    hh, ll = _max(H, p["range_days"], prior=False), _min(L, p["range_days"])
    rk = f["rs_pct"]
    return [_C("MA_STACK", f"收盤 > MA{p['ma_s']} > MA{p['ma_m']} > MA{p['ma_l']}", (C[-1] > s) and (s > m) and (m > l),
               f"{_p(C[-1])}／{_p(s)}／{_p(m)}／{_p(l)}", "由左到右遞減", C[-1]),
            _C("MA_SLOPE", f"MA{p['ma_l']} 高於 {p['slope_days']} 日前", l > l_prev, _p(l), f"> {_p(l_prev)}", l),
            _C("ABOVE_LOW", f"收盤 ≥ 近 {p['range_days']} 日最低 × {p['above_low']}", C[-1] >= p["above_low"] * ll, _p(C[-1]),
               f"≥ {_p(p['above_low'] * ll)}", C[-1]),
            _C("NEAR_HIGH", f"收盤 ≥ 近 {p['range_days']} 日最高 × {p['below_high']}", C[-1] >= p["below_high"] * hh,
               _p(C[-1]), f"≥ {_p(p['below_high'] * hh)}", C[-1]),
            _C("RS", f"近 {p['rs_days']} 日漲幅排名", _cmp(rk, 0) and rk >= p["min_rs_pct"],
               f"{_pct(f['rs'])}（第 {rk * 100:.1f} 百分位）" if _cmp(rk, 0) else "—", f"≥ 第 {p['min_rs_pct'] * 100:.0f} 百分位", rk),
            _tv_cond(f["amount"], p)], f["rs"]


def _m15(f, p):
    C = f["C"]; ma = _sma(C, p["ma"]); r = _rsi(C, p["rsi_n"])
    return [_C("MA", f"收盤 > MA{p['ma']}", C[-1] > ma, _p(C[-1]), f"> {_p(ma)}", C[-1]),
            _C("RSI", f"RSI({p['rsi_n']}) ≤ {p['rsi_max']:g}", _cmp(r, 0) and r <= p["rsi_max"], "—" if not _cmp(r, 0) else f"{r:.2f}",
               f"≤ {p['rsi_max']:g}", r),
            _tv_cond(f["amount"], p)], r


def _m16(f, p):
    C, L, O, V = f["C"], f["L"], f["O"], f["V"]
    s, l, s_prev = _sma(C, p["ma_s"]), _sma(C, p["ma_l"]), _sma(C, p["ma_s"], p["slope_days"])
    av = _avg_prior(V, p["vol_days"])
    return [_C("MA_ORDER", f"MA{p['ma_s']} > MA{p['ma_l']}", s > l, _p(s), f"> {_p(l)}", s),
            _C("MA_SLOPE", f"MA{p['ma_s']} 高於 {p['slope_days']} 日前", s > s_prev, _p(s), f"> {_p(s_prev)}", s),
            _C("TOUCH", f"最低價 ≤ MA{p['ma_s']} × {p['touch']}", L[-1] <= s * p["touch"], _p(L[-1]), f"≤ {_p(s * p['touch'])}", L[-1]),
            _C("CLOSE_ABOVE", f"收盤 > MA{p['ma_s']}", C[-1] > s, _p(C[-1]), f"> {_p(s)}", C[-1]),
            _C("RED", "收盤 > 開盤（紅 K）", C[-1] > O[-1], _p(C[-1]), f"> 開盤 {_p(O[-1])}", C[-1]),
            _C("VOL_DRY", f"成交量 < 前 {p['vol_days']} 日均量", V[-1] < av, _sh(V[-1]), f"< {_sh(av)}", V[-1]),
            _tv_cond(f["amount"], p)], C[-1] / s - 1


def _m17(f, p):
    C, V = f["C"], f["V"]
    sp = []
    for k in range(1, p["spread_days"] + 1):
        ms = [_sma(C, n, k) for n in p["mas"]]
        sp.append(max(ms) / min(ms) - 1)
    spmax = max(sp) if all(_cmp(x, 0) for x in sp) else math.nan
    mx = max(_sma(C, n) for n in p["mas"])
    av = _avg_prior(V, p["vol_days"])
    names = "／".join(f"MA{n}" for n in p["mas"])
    return [_C("TANGLE", f"前 {p['spread_days']} 日 {names} 糾結（最大÷最小−1）", _cmp(spmax, 0) and spmax < p["spread_max"],
               _pct(spmax), f"< {p['spread_max'] * 100:.0f}%", spmax),
            _C("ABOVE_ALL", "收盤 > 三條均線最高者", C[-1] > mx, _p(C[-1]), f"> {_p(mx)}", C[-1]),
            _C("UP", "收盤 > 前一日收盤", C[-1] > C[-2], _p(C[-1]), f"> {_p(C[-2])}", C[-1]),
            _C("VOLUME", f"成交量 ≥ 前 {p['vol_days']} 日均量 × {p['vol_mult']}", V[-1] >= p["vol_mult"] * av, _sh(V[-1]),
               f"≥ {_sh(p['vol_mult'] * av)}", V[-1]),
            _tv_cond(f["amount"], p)], V[-1] / av


def _m18(f, p):
    C, H = f["C"], f["H"]; hh = _max(H, p["high_days"], prior=True)
    return [_C("NEW_HIGH", f"收盤 > 前 {p['high_days']} 日最高價（不含當日）", C[-1] > hh, _p(C[-1]), f"> {_p(hh)}", C[-1]),
            _tv_cond(f["amount"], p)], C[-1] / hh - 1


def _m19(f, p):
    e = f["eps"]
    if e["code"]:
        return "EXCL", e["code"]
    C, H, V = f["C"], f["H"], f["V"]
    eq, ely = e["eps_q"], e["eps_q_ly"]
    hh = _max(H, p["high_days"], prior=False); bo = _max(H, p["breakout_days"], prior=True); av = _avg_prior(V, p["vol_days"])
    rk = f["rs_pct"]
    grow = (eq > 0) and (ely > 0) and (eq >= p["eps_growth"] * ely)
    return [_C("EPS", f"{e['year']}Q{e['quarter']} 單季 EPS 年增 ≥ {int(round((p['eps_growth'] - 1) * 100))}%", grow,
               f"{eq:.2f} 元（去年同季 {ely:.2f} 元）", f"> 0 且 ≥ {p['eps_growth'] * ely:.2f} 元", eq),
            _C("NEAR_HIGH", f"收盤 ≥ 近 {p['high_days']} 日最高 × {p['near']}", C[-1] >= p["near"] * hh, _p(C[-1]),
               f"≥ {_p(p['near'] * hh)}", C[-1]),
            _C("RS", f"近 {p['rs_days']} 日漲幅排名", _cmp(rk, 0) and rk >= p["min_rs_pct"],
               f"{_pct(f['rs'])}（第 {rk * 100:.1f} 百分位）" if _cmp(rk, 0) else "—", f"≥ 第 {p['min_rs_pct'] * 100:.0f} 百分位", rk),
            _C("BREAKOUT", f"收盤 > 前 {p['breakout_days']} 日最高價（不含當日）", C[-1] > bo, _p(C[-1]), f"> {_p(bo)}", C[-1]),
            _C("VOLUME", f"成交量 ≥ 前 {p['vol_days']} 日均量 × {p['vol_mult']}", V[-1] >= p["vol_mult"] * av, _sh(V[-1]),
               f"≥ {_sh(p['vol_mult'] * av)}", V[-1]),
            _tv_cond(f["amount"], p)], (eq / ely - 1) if ely and ely > 0 else None


def _m20(f, p):
    C, O = f["C"], f["O"]; n = p["days"]
    ups = [(C[-1 - k] > C[-2 - k]) and (C[-1 - k] > O[-1 - k]) for k in range(n)] if len(C) >= n + 1 else [False]
    s, l = _sma(C, p["ma_s"]), _sma(C, p["ma_l"])
    return [_C("THREE_UP", f"連續 {n} 日紅 K 且收盤逐日走高", all(ups),
               "、".join(_p(x) for x in C[-n:]), "每日收盤 > 前一日收盤且 > 開盤", C[-1]),
            _C("ABOVE_MA_S", f"收盤 > MA{p['ma_s']}", C[-1] > s, _p(C[-1]), f"> {_p(s)}", C[-1]),
            _C("BELOW_MA_L", f"收盤 < MA{p['ma_l']}", C[-1] < l, _p(C[-1]), f"< {_p(l)}", C[-1]),
            _tv_cond(f["amount"], p)], _ret(C, n)


def _m21(f, p):
    C = f["C"]
    s, l, s1, l1 = _sma(C, p["ma_s"]), _sma(C, p["ma_l"]), _sma(C, p["ma_s"], 1), _sma(C, p["ma_l"], 1)
    return [_C("CROSS_NOW", f"MA{p['ma_s']} > MA{p['ma_l']}", s > l, _p(s), f"> {_p(l)}", s),
            _C("CROSS_PREV", f"前一日 MA{p['ma_s']} ≤ MA{p['ma_l']}", s1 <= l1, _p(s1), f"≤ {_p(l1)}", s1),
            _tv_cond(f["amount"], p)], C[-1] / l - 1


_FUNCS: dict[str, Callable] = {"M9": _m9, "M10": _m10, "M11": _m11, "M12": _m12, "M13": _m13, "M14": _m14, "M15": _m15,
                               "M16": _m16, "M17": _m17, "M18": _m18, "M19": _m19, "M20": _m20, "M21": _m21}

SUMMARY_COLUMNS = ["mode_id", "symbol", "name", "market", "price_date", "status", "exclusion_code", "exclusion_reason",
                   "reason_zh", "metric", "close_twd", "amount_twd", "variant"]
CONDITION_COLUMNS = ["mode_id", "symbol", "price_date", "condition_id", "condition_name", "actual_value", "actual_text",
                     "threshold_text", "passed"]


@dataclass(frozen=True)
class ModesResult:
    as_of: pd.Timestamp
    variant: str
    summary: pd.DataFrame
    conditions: pd.DataFrame
    screen_result: Any
    analysis_version: str = ANALYSIS_VERSION
    registry_version: str = REGISTRY_VERSION

    def mode_summary(self, mid: str) -> pd.DataFrame:
        return self.summary[self.summary["mode_id"] == mid].reset_index(drop=True)

    def selected(self, mid: str) -> pd.DataFrame:
        """符合（PASS）股票；零檔就是空表，不放寬。"""
        s = self.mode_summary(mid)
        return s[s["status"] == "PASS"].reset_index(drop=True)

    def coverage_stats(self, mid: str) -> dict:
        """95% 覆蓋率口徑同 screener.coverage_stats（NOT_EVALUABLE_CODES 不入分母）。"""
        return S.coverage_stats(self.mode_summary(mid))

    def counts(self) -> dict[str, int]:
        s = self.summary
        return {m: int(((s["mode_id"] == m) & (s["status"] == "PASS")).sum()) for m in dict.fromkeys(s["mode_id"])}


def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    s = pd.Series({k: v for k, v in values.items() if _cmp(v, 0)}, dtype=float)
    if not len(s):
        return {}
    return s.rank(pct=True).to_dict()


def evaluate_modes(prices: pd.DataFrame, calendar: pd.DataFrame, actions: Optional[pd.DataFrame] = None,
                   securities: Optional[pd.DataFrame] = None, as_of: Any = None, actions_coverage: Any = None,
                   market_days: Optional[pd.DataFrame] = None, halts: Optional[pd.DataFrame] = None,
                   coverage_scope_note: Optional[str] = None, revenues: Optional[pd.DataFrame] = None,
                   income_statements: Optional[pd.DataFrame] = None, mode_ids: Optional[Iterable[str]] = None,
                   variant: str = "short", universe: Optional[Iterable[Any]] = None, screen_result: Any = None
                   ) -> ModesResult:
    """評估新模式（預設 M9～M21）。screen_result：可傳入同一 as_of 的 screener.screen() 預設參數結果以省時（不傳則內部計算）。
    variant："short"（目前行情長度可用的縮短版，registry params）或 "full"（registry full_params；沒有 full_params 的模式同 short）。"""
    if variant not in ("short", "full"):
        raise ValueError("variant 只能是 short／full")
    ids = list(mode_ids) if mode_ids is not None else list(NEW_MODE_IDS)
    bad = [m for m in ids if m not in _FUNCS]
    if bad:
        raise ValueError(f"evaluate_modes 只計算 {NEW_MODE_IDS}，收到 {bad}")
    if screen_result is None:
        screen_result = S.screen(prices=prices, calendar=calendar, actions=actions, securities=securities,
                                 params=S.ScreenParams(), as_of=as_of, universe=universe, actions_coverage=actions_coverage,
                                 market_days=market_days, halts=halts, coverage_scope_note=coverage_scope_note)
    elif screen_result.params != S.ScreenParams().to_dict():
        raise ValueError("screen_result 必須是預設參數（M1）的結果")
    as_of_ts = screen_result.as_of
    summ = screen_result.summary
    evaluable = summ[summ["status"] != "EXCLUDED"]
    cal = A.normalize_calendar(calendar)
    trade = [d for d in sorted(cal.loc[cal["is_trade_day"], "calendar_date"]) if d <= as_of_ts]
    secs = A.normalize_securities(securities)

    # 每檔還原序列（只算可評估股票）
    px = prices[prices["symbol"].astype(str).isin(set(evaluable["symbol"].astype(str)))]
    pg = {k: g for k, g in px.groupby(px["symbol"].astype(str), sort=False)}
    acts = actions if actions is not None else pd.DataFrame(columns=["symbol", "action_date", "action_type"])
    ag = {k: g for k, g in acts.groupby(acts["symbol"].astype(str), sort=False)} if len(acts) else {}
    empty_a = acts.iloc[0:0]
    feats: dict[str, dict] = {}
    for sid in evaluable["symbol"].astype(str):
        r = SE.ohlc_series(pg[sid], ag.get(sid, empty_a), calendar, sid, as_of=as_of_ts, actions_coverage=actions_coverage,
                           securities=securities)
        b = r.bars
        ok = b[b["status"] == "ok"]
        unavailable = b[b["status"] == "unavailable"]
        last = b.iloc[-1] if len(b) else None
        feats[sid] = dict(C=ok["adj_close_twd"].to_numpy(float), H=ok["adj_high_twd"].to_numpy(float),
                          L=ok["adj_low_twd"].to_numpy(float), O=ok["adj_open_twd"].to_numpy(float),
                          V=ok["adj_volume_shares"].to_numpy(float),
                          amount=None if last is None or pd.isna(last["amount_twd"]) else float(last["amount_twd"]),
                          last_ok=(last is not None and last["status"] == "ok" and pd.Timestamp(last["price_date"]) == as_of_ts),
                          unavail_code=(unavailable["unavailable_code"].dropna().iloc[-1] if len(unavailable)
                                        and unavailable["unavailable_code"].notna().any() else None))
    # 橫斷面排名（在可評估且有數值的股票之間）
    def ranks(n):
        vals = {s: (_ret(f["C"], n) if f["last_ok"] else math.nan) for s, f in feats.items()}
        return vals, _pct_rank(vals)
    rank_cache: dict[int, tuple] = {}
    need_rank = {"M12": "ret_days", "M14": "rs_days", "M19": "rs_days"}
    rev_state = _revenue_state(revenues, as_of_ts, list(feats)) if any(m in ids for m in ("M10", "M11")) else {}
    eps_state = eps_quarter_state(income_statements, as_of_ts, list(feats)) if "M19" in ids else {}

    rows, conds = [], []
    info = summ.set_index(summ["symbol"].astype(str))
    for mid in ids:
        spec = _BY_ID[mid]
        p = dict(spec["full_params"] if (variant == "full" and spec["full_params"]) else spec["params"])
        if "mas" in p:
            p["mas"] = tuple(p["mas"])
        need = _need(p, mid, variant)
        rk = None
        if mid in need_rank:
            n = p[need_rank[mid]]
            if n not in rank_cache:
                rank_cache[n] = ranks(n)
            rk = rank_cache[n]
        for sid, r0 in info.iterrows():
            base = dict(mode_id=mid, symbol=sid, name=r0.get("name"), market=r0.get("market"),
                        price_date=as_of_ts.strftime("%Y-%m-%d"), close_twd=r0.get("close_twd"),
                        amount_twd=r0.get("amount_twd"), variant=variant, metric=None, reason_zh=None,
                        exclusion_code=None, exclusion_reason=None)

            def excl(code, extra=""):
                base.update(status="EXCLUDED", exclusion_code=code,
                            exclusion_reason=MODE_EXCLUSION_CODES.get(code, code) + extra)
                rows.append(base)

            if r0["status"] == "EXCLUDED":
                excl(r0["exclusion_code"]); continue
            f = feats[sid]
            if not f["last_ok"]:
                excl(f["unavail_code"] or "EXCL_INSUFFICIENT_HISTORY"); continue
            if len(f["C"]) < need:
                if f["unavail_code"]:
                    excl(f["unavail_code"], f"（本模式需 {need} 個有效交易日）"); continue
                listed = None
                if secs is not None and sid in secs.index and pd.notna(secs.loc[sid, "listed_date"]):
                    listed = pd.Timestamp(secs.loc[sid, "listed_date"])
                young = listed is not None and listed >= trade[0] and len([d for d in trade if d >= listed]) < need
                excl("EXCL_NEW_LISTING" if young else "EXCL_INSUFFICIENT_HISTORY",
                     f"（本模式需 {need} 個有效交易日，目前 {len(f['C'])} 日）"); continue
            g = dict(f)
            if rk is not None:
                n = p[need_rank[mid]]
                g["rs"] = g["ret5"] = rk[0].get(sid, math.nan)
                g["rs_pct"] = g["ret5_pct"] = rk[1].get(sid, math.nan)
            if mid in ("M10", "M11"):
                g["rev"] = rev_state[sid]
            if mid == "M19":
                g["eps"] = eps_state[sid]
            out = _FUNCS[mid](g, p)
            if out[0] == "EXCL":
                excl(out[1]); continue
            cl, metric = out
            ok = all(c["passed"] for c in cl)
            base.update(status="PASS" if ok else "FAIL", metric=None if metric is None or not _cmp(metric, 0) else float(metric))
            if ok:
                base["reason_zh"] = "符合：" + "；".join(f"{c['condition_name']}（{c['actual_text']}，門檻 {c['threshold_text']}）" for c in cl)
            else:
                base["reason_zh"] = "未符合：" + "；".join(f"{c['condition_name']}（{c['actual_text']}，門檻 {c['threshold_text']}）"
                                                       for c in cl if not c["passed"])
            rows.append(base)
            for c in cl:
                conds.append(dict(mode_id=mid, symbol=sid, price_date=base["price_date"], **c))
    sm = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    cd = pd.DataFrame(conds, columns=CONDITION_COLUMNS)
    return ModesResult(as_of=as_of_ts, variant=variant, summary=sm, conditions=cd, screen_result=screen_result)


if __name__ == "__main__":   # python modes.py --write-registry：更新 analysis/mode_registry.json
    import sys
    from pathlib import Path
    if "--write-registry" in sys.argv:
        (Path(__file__).resolve().parent / "mode_registry.json").write_text(registry_json(), encoding="utf-8")
        print("wrote mode_registry.json")
