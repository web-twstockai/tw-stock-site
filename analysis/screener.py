"""台股技術篩選器（第一版：趨勢＋突破＋量能）——純函式模組。

只做計算：無檔案／網路／資料庫 I/O、無可變全域狀態；相同輸入得相同輸出，不修改傳入的 DataFrame。
價格調整、跳空偵測、覆蓋檢查、漲跌幅基準與 change 交叉檢查共用 analysis/adjust.py（與 series.py 同一套）。
讀取 SQLite 請用 analysis/io_sqlite.py。規格：docs/SPEC.md 第 4 節；定義：docs/indicators.md；呼叫方式：analysis/USAGE.md。

輸入（欄位名稱＝docs/schema.md 欄位名稱）
  prices      daily_prices：symbol, price_date, open_twd, high_twd, low_twd, close_twd, volume_shares（股）,
              amount_twd（元）；可選 market, change_twd, change_sign, is_current, source, fetched_at
  calendar    trading_calendar：calendar_date, is_trade_day
  actions     corporate_actions：symbol, action_date, action_type；可選 prev_close_twd, reference_twd,
              dividend_value_twd, stock_dividend_per_thousand, capital_reduction_ratio, refund_per_share_twd,
              cash_subscription_ratio, share_exchange_ratio（後兩欄 schema 待遷移，可不存在）
  securities  securities：symbol；可選 market, name, listed_date
  actions_coverage  corporate_actions_coverage：DataFrame[market, action_kind, coverage_start（, coverage_end）]；
              None＝表不存在、空表＝表為空，兩者在 strict 下都會排除並寫明 coverage_note。
  halts       trading_halts：symbol, halt_date（可選 resume_date, is_current）；None＝無停牌資訊。
              prices 可選 is_halted（1＝停牌）。確認停牌 → EXCL_SUSPENDED；其餘無資料列 → EXCL_NO_ROW_SUSPENDED_OR_MISSING。
輸出
  ScreenResult.summary／conditions（欄位見 USAGE.md 第 4 節）。零檔就是零檔，不自動放寬。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

import adjust as A

__all__ = [
    "ANALYSIS_VERSION", "EXCLUSION_CODES", "CONDITION_NAMES", "PARAM_RANGES", "SUMMARY_COLUMNS",
    "CONDITION_COLUMNS", "ScreenParams", "ScreenResult", "screen", "params_hash",
    "NOT_EVALUABLE_CODES", "coverage_stats",
]

from version import ANALYSIS_VERSION  # 版本號集中於 version.py

EXCLUSION_CODES = MappingProxyType({
    "EXCL_NOT_COMMON_STOCK": "不在普通股清單（securities）中：第一版排除 ETF、權證、特別股、存託憑證等",
    "EXCL_BAD_DATA": "資料異常（價格關係不合理、負值、重複列、非交易日卻有行情）",
    "EXCL_NO_DATA": "評估日該市場無任何行情資料（尚未更新或抓取失敗），資料不足",
    "EXCL_SUSPENDED": "停牌（經 trading_halts 或 daily_prices.is_halted 確認，或減資／變更面額停止買賣期間）",
    "EXCL_NO_ROW_SUSPENDED_OR_MISSING": "停牌或資料缺漏，待確認（評估日為交易日，但本股無資料列，且無法確認停牌：無停牌紀錄，或推定停牌區間內該市場當日整個市場都缺行情）",
    "EXCL_NO_TRADE": "評估日有行情列但無成交（成交量為零或無收盤價）",
    "EXCL_VOLUME_BASIS": "量額口徑非總量（不含鉅額或口徑不明），量能／成交金額條件無法評估",
    "EXCL_NEW_LISTING": "新上市／上櫃未滿最低交易日數",
    "EXCL_INSUFFICIENT_HISTORY": "有效交易日歷史不足（含交易日曆或行情資料未涵蓋）",
    "EXCL_WINDOW_GAPS": "計算視窗內缺資料的交易日過多（官方有行情但本庫無資料列或資料錯誤，屬資料不齊）",
    "EXCL_WINDOW_NO_TRADE": "無法評估：計算視窗內官方無成交的交易日過多（流動性不足，不算資料不齊）",
    "EXCL_WINDOW_SUSPENDED": "無法評估：計算視窗內官方停牌或減資／變更面額停止買賣的交易日過多（不算資料不齊）",
    "EXCL_ADJ_COVERAGE_GAP": "除權息／減資資料（corporate_actions）未完整涵蓋計算視窗，無法確認期間內有無調整事件",
    "EXCL_ADJ_FACTOR_MISSING": "視窗內有除權息／減資事件但無法取得價格或股數調整因子",
    "EXCL_ADJ_FACTOR_INCONSISTENT": "調整事件資料與行情不一致（來源前收不符、換股率或參考價不合理、同日重複事件）",
    "EXCL_UNEXPLAINED_GAP": "視窗內出現 corporate_actions 無法解釋且超過漲跌幅門檻的價格跳動（疑似缺調整事件）",
})

# 「無法評估」：官方事實造成、不算資料不齊，個別標示並自 95% 覆蓋率分母移除（indicators.md §8.7；H AO 核可 2026-11-24 13:32）
NOT_EVALUABLE_CODES = frozenset({
    "EXCL_NEW_LISTING", "EXCL_SUSPENDED", "EXCL_NO_TRADE", "EXCL_WINDOW_NO_TRADE", "EXCL_WINDOW_SUSPENDED",
})


def coverage_stats(summary: pd.DataFrame) -> dict:
    """95% 覆蓋率口徑：分母＝母體 − 無法評估（NOT_EVALUABLE_CODES）；可評估＝status 不是 EXCLUDED。
    其餘排除（資料缺漏、調整因子、資料錯誤等）留在分母，算資料不齊。不改任何篩選結果。"""
    st = summary["status"].astype(str)
    code = summary["exclusion_code"]
    universe = int(len(summary))
    evaluable = int((st != "EXCLUDED").sum())
    ne_mask = (st == "EXCLUDED") & code.isin(NOT_EVALUABLE_CODES)
    by = {c: int(n) for c, n in code[ne_mask].value_counts().sort_index().items()}
    den = universe - int(ne_mask.sum())
    return dict(universe=universe, evaluable=evaluable, not_evaluable=int(ne_mask.sum()), not_evaluable_by_code=by,
                denominator=den, data_incomplete=max(den - evaluable, 0),
                coverage_ratio=(evaluable / den) if den > 0 else 0.0)


CONDITION_NAMES = MappingProxyType({
    "C1_TREND": "趨勢：收盤 > MA短 > MA長",
    "C2_BREAKOUT": "突破：收盤 > 前 N 個交易日最高價（不含當日）",
    "C3_VOLUME": "量能：成交量 ≥ 前 N 個交易日平均量（不含當日）× 倍數",
    "C4_TURNOVER": "成交金額 ≥ 門檻（新台幣元）",
})

PARAM_RANGES = MappingProxyType({
    "ma_short": (int, 2, 250),
    "ma_long": (int, 3, 500),
    "breakout_lookback": (int, 1, 250),
    "volume_lookback": (int, 1, 250),
    "volume_multiplier": (float, 1.0, 20.0),
    "min_turnover": (float, 0.0, 1_000_000_000_000.0),
    "max_gap_days": (int, 0, 20),
    "max_daily_move": (float, 0.05, 1.0),
    "legacy_max_daily_move": (float, 0.03, 1.0),
    "min_listing_days": (int, 0, 1000),
    "new_listing_no_limit_days": (int, 0, 20),
    "adj_prev_close_tolerance": (float, 0.0, 0.05),
})
_BOOL_PARAMS = ("enable_trend", "enable_breakout", "enable_volume", "enable_turnover",
                "check_unexplained_gap", "derive_share_factor")
_COVERAGE_POLICIES = ("strict", "gap_check")


@dataclass(frozen=True)
class ScreenParams:
    """篩選參數；建立時即驗證，越界拋 ValueError，型別錯誤拋 TypeError，不自動修正。
    正式結果一律 coverage_policy="strict"；gap_check 只供研究比較，程式不會自動切換。"""

    ma_short: int = 20
    ma_long: int = 60
    breakout_lookback: int = 20
    volume_lookback: int = 20
    volume_multiplier: float = 1.5
    min_turnover: float = 100_000_000.0  # 新台幣 1 億元
    max_gap_days: int = 3
    max_daily_move: float = 0.11          # 2015-06-01 起
    legacy_max_daily_move: float = 0.07   # 2015-06-01 前（回測用）
    min_listing_days: int = 60
    new_listing_no_limit_days: int = 5
    adj_prev_close_tolerance: float = 0.005
    coverage_policy: str = "strict"
    derive_share_factor: bool = True
    enable_trend: bool = True
    enable_breakout: bool = True
    enable_volume: bool = True
    enable_turnover: bool = True
    check_unexplained_gap: bool = True

    def __post_init__(self) -> None:
        for name, (typ, lo, hi) in PARAM_RANGES.items():
            v = getattr(self, name)
            if isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, float, np.integer, np.floating)):
                raise TypeError(f"參數 {name} 必須是數值，收到 {v!r}")
            if typ is int and not float(v).is_integer():
                raise TypeError(f"參數 {name} 必須是整數，收到 {v!r}")
            if not math.isfinite(float(v)) or not (lo <= v <= hi):
                raise ValueError(f"參數 {name}={v!r} 超出合法範圍 [{lo}, {hi}]")
            object.__setattr__(self, name, typ(v))
        for name in _BOOL_PARAMS:
            if not isinstance(getattr(self, name), (bool, np.bool_)):
                raise TypeError(f"參數 {name} 必須是 bool")
            object.__setattr__(self, name, bool(getattr(self, name)))
        if not isinstance(self.coverage_policy, str):
            raise TypeError("參數 coverage_policy 必須是字串")
        if self.coverage_policy not in _COVERAGE_POLICIES:
            raise ValueError(f"coverage_policy 只能是 {_COVERAGE_POLICIES}")
        if self.ma_long <= self.ma_short:
            raise ValueError(f"ma_long（{self.ma_long}）必須大於 ma_short（{self.ma_short}）")
        if not (self.enable_trend or self.enable_breakout or self.enable_volume or self.enable_turnover):
            raise ValueError("至少要啟用一個篩選條件")

    @property
    def required_valid_days(self) -> int:
        return max(self.ma_long, self.ma_short, self.breakout_lookback + 1, self.volume_lookback + 1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def params_hash(params: "ScreenParams | dict[str, Any]") -> str:
    """參數雜湊：sha256(鍵排序的 JSON)，供 screen_results 冪等鍵。"""
    d = params.to_dict() if isinstance(params, ScreenParams) else dict(params)
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ScreenResult:
    as_of: pd.Timestamp
    params: dict[str, Any]
    params_hash: str
    analysis_version: str
    summary: pd.DataFrame
    conditions: pd.DataFrame
    computed_at: str = ""      # 計算時間（ISO-8601，Asia/Taipei +08:00）；寫入 *_results.computed_at
    volume_basis_status: str = "ok"   # ok／volume_basis_column_missing（輸入缺 volume_basis 欄：未檢查口徑，writer 拒寫）
    coverage_scope_note: Optional[str] = None   # 覆蓋表 scope 篩選說明（io_sqlite；資料庫無 scope 欄為 None）
    lagging_symbols: tuple = ()       # 評估範圍內最新行情列（交易日）早於 as_of 或無行情的代號（規則同 adjust.AS_OF_RULE 第 4 點；摘要用）

    def selected(self) -> pd.DataFrame:
        """通過全部已啟用條件的股票；零檔時回傳空表（不放寬）。"""
        return self.summary[self.summary["status"] == "PASS"].reset_index(drop=True)

    def coverage_stats(self) -> dict:
        """95% 覆蓋率口徑（見模組函式 coverage_stats）。"""
        return coverage_stats(self.summary)

    def exclusion_counts(self) -> dict[str, int]:
        s = self.summary
        return {str(k): int(v) for k, v in
                s.loc[s["status"] == "EXCLUDED", "exclusion_code"].value_counts().sort_index().items()}


SUMMARY_COLUMNS = [
    "symbol", "name", "market", "price_date", "status", "exclusion_code", "exclusion_reason",
    "close_twd", "change_pct", "change_base_twd", "source_change_twd", "change_check", "change_check_diff_twd",
    "volume_shares", "amount_twd", "ma_short", "ma_long",
    "prior_high_twd", "prior_avg_volume_shares", "volume_ratio",
    "pass_trend", "pass_breakout", "pass_volume", "pass_turnover",
    "n_valid_days", "window_start", "window_gap_days", "window_halt_days", "window_missing_days",
    "window_no_trade_days", "window_bad_data_days", "volume_basis", "window_volume_basis_days",
    "n_adj_events", "adj_events",
    "coverage_note", "data_note", "price_source", "price_fetched_at", "price_version",
]
CONDITION_COLUMNS = [
    "symbol", "price_date", "condition_id", "condition_name", "enabled",
    "actual_value", "actual_text", "threshold_value", "threshold_text",
    "passed", "reason", "exclusion_code",
]


def screen(
    prices: pd.DataFrame,
    calendar: pd.DataFrame,
    actions: Optional[pd.DataFrame] = None,
    securities: Optional[pd.DataFrame] = None,
    params: Optional[ScreenParams] = None,
    as_of: Any = None,
    universe: Optional[Iterable[Any]] = None,
    actions_coverage: Any = None,
    market_days: Optional[pd.DataFrame] = None,
    halts: Optional[pd.DataFrame] = None,
    coverage_scope_note: Optional[str] = None,
) -> ScreenResult:
    """對 as_of（交易日）執行篩選；只使用 <= as_of 的資料（無前視）。
    market_days 有 n_rows 欄（該市場該日現行列數）時，n_rows < adjust.FULL_MARKET_MIN_ROWS 的日子視為「整個市場都缺行情」
    （停牌 insufficient 規則，docs/indicators.md 第 10 節）；沒有 n_rows 欄時，market_days 內的日子都視為有全市場行情。
    coverage_scope_note：io_sqlite 讀覆蓋表時的 scope 篩選說明（資料庫有 scope 欄才有值），只附在覆蓋有問題（coverage_note 非空）的列的 data_note；完整說明在 ScreenResult.coverage_scope_note。"""
    p = ScreenParams() if params is None else params
    if not isinstance(p, ScreenParams):
        raise TypeError("params 必須是 ScreenParams")

    cal = A.normalize_calendar(calendar)
    trade_all = sorted(cal.loc[cal["is_trade_day"], "calendar_date"])
    if not trade_all:
        raise ValueError("交易日曆沒有任何交易日")
    px = A.normalize_prices(prices)
    vb_present = "volume_basis" in prices.columns     # 缺欄＝研究模式：照算、不檢查口徑、結果標 volume_basis_column_missing
    if as_of is None:
        # 預設評估日：追蹤範圍內有行情的最新交易日（adjust.resolve_as_of，與 io_sqlite.default_as_of 同一規則）
        if universe is not None:
            scope = list(dict.fromkeys(str(x).strip() for x in universe))
        else:
            _secs = A.normalize_securities(securities)
            scope = sorted(_secs.index) if _secs is not None else sorted(px["symbol"].unique())
        latest = A.latest_trade_day_by_symbol(px["symbol"], px["date"], trade_all)
        as_of_ts = A.resolve_as_of(latest, trade_all, scope).as_of
    else:
        as_of_ts = A.to_ts(as_of)
    if as_of_ts not in set(trade_all):
        raise ValueError(f"as_of {as_of_ts.date()} 不是交易日（依 trading_calendar）")
    trade = [d for d in trade_all if d <= as_of_ts]
    tpos = {d: i for i, d in enumerate(trade)}
    non_trade = set(cal.loc[~cal["is_trade_day"], "calendar_date"])
    cal_start = cal["calendar_date"].min()

    px = px[(px["date"] <= as_of_ts) & (px["date"] >= cal_start)]
    acts_all = A.normalize_actions(actions)
    acts = acts_all[acts_all["action_date"] <= as_of_ts]
    secs = A.normalize_securities(securities)
    coverage = A.normalize_coverage(actions_coverage)
    halt_map = A.normalize_halts(halts)

    if market_days is not None:
        md = market_days.copy()
        md["price_date"] = pd.to_datetime(md["price_date"]).dt.normalize()
        mkt_dates = {(str(m), d) for m, d in zip(md["market"], md["price_date"])}
        if "n_rows" in md.columns:
            n = pd.to_numeric(md["n_rows"], errors="coerce").fillna(0)
            mkt_full = {(str(m), d) for m, d, k in zip(md["market"], md["price_date"], n) if k >= A.FULL_MARKET_MIN_ROWS}
        else:
            mkt_full = set(mkt_dates)
    else:
        mkt_dates = {(str(m), d) for m, d in zip(px["market"], px["date"])}
        mkt_full = set(mkt_dates)

    if universe is not None:
        ids = list(dict.fromkeys(str(x).strip() for x in universe))
    elif secs is not None:
        ids = sorted(secs.index)
    else:
        ids = sorted(px["symbol"].unique())

    latest_by_sym = A.latest_trade_day_by_symbol(px["symbol"], px["date"], trade)
    lagging = tuple(sorted(sid for sid in ids if latest_by_sym.get(sid) != as_of_ts))
    pgroups = {k: g for k, g in px.groupby("symbol", sort=False)}
    agroups = {k: g for k, g in acts.groupby("symbol", sort=False)}
    sgroups = {k: g for k, g in acts_all[acts_all["action_type"].isin(A.SUSPENSION_ACTION_TYPES)].groupby("symbol", sort=False)}
    ctx = dict(p=p, as_of=as_of_ts, trade=trade, tpos=tpos, non_trade=non_trade, coverage=coverage,
               mkt_dates=mkt_dates, mkt_full=mkt_full, halts=halt_map, vb=vb_present,
               cov_scope_note=coverage_scope_note, susp_acts=sgroups)
    summaries, conds = [], []
    for sid in ids:
        sec = None if secs is None else (secs.loc[sid] if sid in secs.index else None)
        s, c = _evaluate(sid, pgroups.get(sid, px.iloc[0:0]), agroups.get(sid, acts.iloc[0:0]),
                         sec, secs is not None, ctx)
        summaries.append(s)
        conds.extend(c)
    return ScreenResult(as_of=as_of_ts, params=p.to_dict(), params_hash=params_hash(p),
                        analysis_version=ANALYSIS_VERSION,
                        summary=pd.DataFrame(summaries, columns=SUMMARY_COLUMNS),
                        conditions=pd.DataFrame(conds, columns=CONDITION_COLUMNS),
                        computed_at=_now_taipei(),
                        volume_basis_status="ok" if vb_present else A.VOLUME_BASIS_COLUMN_MISSING,
                        coverage_scope_note=coverage_scope_note, lagging_symbols=lagging)


def _now_taipei() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat(timespec="seconds")


# ------------------------------------------------------------------
def _row_problem(r: Any, check_turnover: bool) -> Optional[str]:
    o, h, l, c, v = r.open, r.high, r.low, r.close, r.volume
    if any(pd.isna(x) for x in (o, h, l, c, v)):
        return "開高低收或成交量缺漏"
    if min(o, h, l, c) <= 0:
        return "價格非正數"
    if v < 0:
        return "成交量為負"
    if h < l or not (l <= c <= h) or not (l <= o <= h):
        return "價格關係不合理（需 最低 ≤ 開、收 ≤ 最高）"
    if check_turnover and (pd.isna(r.turnover) or r.turnover < 0):
        return "成交金額缺漏或為負"
    return None


def _enabled(p: ScreenParams) -> dict[str, bool]:
    return {"C1_TREND": p.enable_trend, "C2_BREAKOUT": p.enable_breakout,
            "C3_VOLUME": p.enable_volume, "C4_TURNOVER": p.enable_turnover}


def _fp(x: float) -> str:
    return f"{x:,.2f}"


def _fi(x: float) -> str:
    return f"{x:,.0f}"


def _evaluate(sid: str, g: pd.DataFrame, acts: pd.DataFrame, sec: Any, has_secs: bool, ctx: dict):
    p: ScreenParams = ctx["p"]
    as_of, tpos, trade = ctx["as_of"], ctx["tpos"], ctx["trade"]
    g = g.sort_values("date", kind="mergesort")
    name = A.clean_str(sec["name"]) if sec is not None else None
    market = (A.clean_str(sec["market"]) if sec is not None else None) or (
        A.clean_str(g["market"].iloc[-1]) if len(g) else None)
    base = {c: None for c in SUMMARY_COLUMNS}
    base.update(symbol=sid, name=name, market=market, price_date=as_of)
    en = _enabled(p)

    def X(code: str, detail: str = "", **extra: Any):
        s = dict(base)
        s.update(extra)
        s.update(status="EXCLUDED", exclusion_code=code,
                 exclusion_reason=f"{EXCLUSION_CODES[code]}：{detail}" if detail else EXCLUSION_CODES[code])
        cs = [{"symbol": sid, "price_date": as_of, "condition_id": cid, "condition_name": CONDITION_NAMES[cid],
               "enabled": en[cid], "actual_value": None, "actual_text": None, "threshold_value": None,
               "threshold_text": None, "passed": None, "reason": f"未評估（排除：{code}）",
               "exclusion_code": code} for cid in CONDITION_NAMES]
        return s, cs

    if has_secs and sec is None:
        return X("EXCL_NOT_COMMON_STOCK", f"{sid} 不在 securities")
    if g["date"].duplicated().any():
        return X("EXCL_BAD_DATA", f"重複行情列 {g.loc[g['date'].duplicated(), 'date'].iloc[0].date()}")
    off = g[~g["date"].isin(tpos.keys())]
    if len(off):
        d0 = off["date"].iloc[0]
        why = "非交易日卻有行情" if d0 in ctx["non_trade"] else "日期不在 trading_calendar 中"
        return X("EXCL_BAD_DATA", f"{d0.date()} {why}")

    # 停牌區間（docs/indicators.md 停牌規則，2026-11-17 18:31 核可）：無官方恢復日時以停牌日後第一個有官方成交的
    # 交易日推定恢復；只用評估日（含）以前的成交（g 已限 ≤ as_of），不前視
    # insufficient 規則（2026-11-17 19:41 核可）：推定區間內「整個市場都缺行情」的日子，本股無資料列時不算停牌
    full, mk = ctx["mkt_full"], market or ""
    if market:
        def is_hole(d):
            return (mk, d) not in full
    else:
        mkts = {m for m, _ in full} or {""}
        def is_hole(d):
            return all((m, d) not in full for m in mkts)
    hiv = A.resolve_halts(ctx["halts"].get(sid, []), g.loc[(g["volume"] > 0) & g["close"].notna(), "date"], as_of,
                          trade_days=trade, is_hole=is_hole)
    hnotes = [A.halt_note(h) for h in hiv if h.resume is None or h.resume > ctx["trade"][0]]
    if hnotes:
        base["data_note"] = "停牌紀錄：" + "；".join(hnotes)
    sa = ctx["susp_acts"].get(sid)
    susp = A.corp_suspension_dates(sa, sid, trade, g["date"], as_of) if sa is not None else set()
    today = g[g["date"] == as_of]
    no_trade_today = today.empty or pd.isna(today["close"].iloc[0]) or (
        pd.notna(today["volume"].iloc[0]) and today["volume"].iloc[0] == 0)
    if today.empty:
        if (market or "", as_of) in ctx["mkt_dates"] or (market is None and any(d == as_of for _, d in ctx["mkt_dates"])):
            hs = A.halt_span_on(hiv, as_of, has_row=False)
            if hs is not None:
                return X("EXCL_SUSPENDED", f"{as_of.date()} 本股無資料列；trading_halts {A.halt_note(hs)}")
            hi = A.halt_span_on(hiv, as_of)
            if hi is not None:   # 推定區間內、評估日整個市場都缺行情 → 不算停牌（insufficient）
                return X("EXCL_NO_ROW_SUSPENDED_OR_MISSING",
                         f"{as_of.date()} 本股無資料列，且該市場當日整個市場都缺行情；trading_halts {A.halt_note(hi)}")
            if as_of in susp:
                return X("EXCL_SUSPENDED", f"{as_of.date()} 本股無資料列；減資／變更面額停止買賣期間（corporate_actions）")
            return X("EXCL_NO_ROW_SUSPENDED_OR_MISSING", f"{as_of.date()} 同市場有行情但本股無資料列")
        return X("EXCL_NO_DATA", f"{as_of.date()} 市場 {market or '未知'} 無行情資料")
    t = today.iloc[0]
    if ctx["vb"]:
        base["volume_basis"] = A.clean_str(t.volume_basis)
    nt_note = A.no_trade_flag_conflict(t.close, t.volume, t.is_no_trade)
    if nt_note:
        base["data_note"] = "；".join(x for x in (base["data_note"], f"is_no_trade 交叉檢查：{as_of.date()} {nt_note}") if x)   # 只標註，不改結果碼（核可 ③）
    hs = A.halt_span_on(hiv, as_of)
    if no_trade_today and (bool(t.is_halted) or hs is not None):
        src = "daily_prices.is_halted＝1" if bool(t.is_halted) else f"trading_halts {A.halt_note(hs)}"
        return X("EXCL_SUSPENDED", f"{as_of.date()} {src}")
    if no_trade_today:
        return X("EXCL_NO_TRADE", f"{as_of.date()} 無成交")
    if A.row_kind(t.close, t.volume) == "bad_volume":
        return X("EXCL_BAD_DATA", f"{as_of.date()} 有收盤價但成交量缺漏（NULL）")
    prob = _row_problem(t, check_turnover=True)
    if prob:
        return X("EXCL_BAD_DATA", f"{as_of.date()} {prob}")
    # 5a（§9.2）：評估日量額口徑非總量 → 排除；C3、C4 都停用時不檢查
    if ctx["vb"] and (p.enable_volume or p.enable_turnover) and not A.is_total_basis(t.volume_basis):
        return X("EXCL_VOLUME_BASIS", f"{as_of.date()} 量額口徑 {A.basis_label(t.volume_basis)}")

    listing_idx0: Optional[int] = None
    if sec is not None and pd.notna(sec["listed_date"]):
        ld = pd.Timestamp(sec["listed_date"])
        if ld >= trade[0]:
            listing_idx0 = bisect.bisect_left(list(trade), ld)
            days = tpos[as_of] - listing_idx0 + 1
            if days < p.min_listing_days:
                return X("EXCL_NEW_LISTING", f"上市日 {ld.date()}，上市後第 {max(days, 0)} 個交易日 < {p.min_listing_days}")

    valid = g[(g["volume"] > 0) & g["close"].notna()].reset_index(drop=True)
    need = p.required_valid_days
    if len(valid) < need:
        return X("EXCL_INSUFFICIENT_HISTORY", f"有效交易日 {len(valid)} < 需要 {need}", n_valid_days=len(valid))
    W = valid.iloc[-need:].reset_index(drop=True)
    w0 = W["date"].iloc[0]
    gaps = tpos[as_of] - tpos[w0] + 1 - need
    # 缺口拆解（只供說明；門檻仍以合計 gaps 判斷，邏輯不變）
    rows_by_date = {d: r for d, r in zip(g["date"], g.itertuples(index=False))}
    vset = set(valid["date"])
    n_halt = n_miss = n_nt = n_bad = n_corp = 0
    first_bad = None
    flag_conflicts = []
    for d in trade[tpos[w0]:tpos[as_of] + 1]:
        r = rows_by_date.get(d)
        if r is not None and d != as_of and A.no_trade_flag_conflict(r.close, r.volume, r.is_no_trade):
            flag_conflicts.append(d)
        if d in vset:
            continue
        if r is not None and A.row_kind(r.close, r.volume) == "bad_volume":
            n_bad += 1                      # 核可 ①：有收盤價但量 NULL＝資料錯誤（仍計入缺口合計）
            first_bad = first_bad or d
        elif (r is not None and bool(r.is_halted)) or A.is_halt_day(hiv, d, has_row=r is not None):
            n_halt += 1
        elif r is None:
            n_miss += 1
            if d in susp:
                n_corp += 1
        else:
            n_nt += 1
    if flag_conflicts:
        msg = f"is_no_trade 交叉檢查：視窗內 {len(flag_conflicts)} 日旗標與價量不一致（首日 {flag_conflicts[0].date()}）"
        base["data_note"] = "；".join(x for x in (base["data_note"], msg) if x)
    extra = {"n_valid_days": len(valid), "window_start": w0, "window_gap_days": gaps,
             "window_halt_days": n_halt, "window_missing_days": n_miss, "window_no_trade_days": n_nt,
             "window_bad_data_days": n_bad}
    if gaps > p.max_gap_days:
        # 篩選門檻不變（合計 gaps 仍與 max_gap_days 比）。只區分原因：官方無成交＋確認停牌＋減資／變更面額停止買賣
        # 單獨已超過門檻 → 無法評估（不進 95% 分母）；否則（真缺資料／資料錯誤造成或合併才超過）→ 資料不齊
        detail = (f"視窗 {w0.date()}～{as_of.date()} 內 {gaps} 個交易日非有效日 > {p.max_gap_days}"
                  f"（確認停牌 {n_halt}、減資／變更面額停止買賣 {n_corp}、無成交 {n_nt}、其他缺列 {n_miss - n_corp}、資料錯誤 {n_bad}）")
        official = n_halt + n_corp + n_nt
        if official > p.max_gap_days:
            code = "EXCL_WINDOW_SUSPENDED" if (n_halt + n_corp) >= n_nt else "EXCL_WINDOW_NO_TRADE"
            return X(code, detail, **extra)
        return X("EXCL_WINDOW_GAPS", detail, **extra)
    if first_bad is not None:
        return X("EXCL_BAD_DATA", f"{first_bad.date()} 有收盤價但成交量缺漏（NULL）（視窗內，共 {n_bad} 日）", **extra)
    for r in W.itertuples(index=False):
        prob = _row_problem(r, check_turnover=False)
        if prob:
            return X("EXCL_BAD_DATA", f"{r.date.date()} {prob}", **extra)
    # 9a（§9.3 方案 A）：均量窗口（評估日前 volume_lookback 個有效交易日）任一日非總量 → 排除（C3 停用時只記數）
    if ctx["vb"]:
        aw = W.iloc[len(W) - 1 - p.volume_lookback:len(W) - 1]
        off = [(d, v) for d, v in zip(aw["date"], aw["volume_basis"]) if not A.is_total_basis(v)]
        extra["window_volume_basis_days"] = len(off)
        if p.enable_volume and off:
            return X("EXCL_VOLUME_BASIS",
                     f"均量窗口 {aw['date'].iloc[0].date()}～{aw['date'].iloc[-1].date()} 內 {len(off)} 日非總量口徑"
                     f"（首日 {off[0][0].date()}：{A.basis_label(off[0][1])}）", **extra)

    cov_probs = A.coverage_problems(ctx["coverage"], market, w0, ctx["as_of"])
    coverage_note = ";".join(cov_probs) or None
    if cov_probs and ctx["cov_scope_note"]:
        # 覆蓋表 scope 篩選說明：只附在覆蓋有問題的列（其他列看 ScreenResult.coverage_scope_note）
        base["data_note"] = "；".join(x for x in (base["data_note"], ctx["cov_scope_note"]) if x)
    if cov_probs and p.coverage_policy == "strict":
        return X("EXCL_ADJ_COVERAGE_GAP", f"視窗起日 {w0.date()}；{coverage_note}", coverage_note=coverage_note, **extra)

    events = A.resolve_events(valid, acts, w0, p.adj_prev_close_tolerance, p.derive_share_factor)
    for e in events:
        if e.code:
            return X(e.code, e.detail, coverage_note=coverage_note, **extra)
    F, S = A.cumulative_factors(W["date"], events)
    adj_close = W["close"].to_numpy() * F
    adj_high = W["high"].to_numpy() * F
    adj_vol = W["volume"].to_numpy() * S   # 量比的前 N 日均量套用股數因子；成交金額不還原

    if p.check_unexplained_gap:
        gaps_found = A.unexplained_gaps(list(W["date"]), adj_close, p.max_daily_move, p.legacy_max_daily_move,
                                        listing_idx0, tpos, p.new_listing_no_limit_days)
        if gaps_found:
            i, mv, lim = gaps_found[0]
            return X("EXCL_UNEXPLAINED_GAP",
                     f"{W['date'].iloc[i].date()} 還原後漲跌 {mv * 100:.2f}% 超過 ±{lim * 100:.2f}%，corporate_actions 無對應事件",
                     coverage_note=coverage_note, **extra)

    close, volume, turnover = float(t.close), float(t.volume), float(t.turnover)
    ma_s = float(A.rolling_mean(adj_close, p.ma_short)[-1])
    ma_l = float(A.rolling_mean(adj_close, p.ma_long)[-1])
    prior_high = float(pd.Series(adj_high).shift(1).rolling(p.breakout_lookback).max().iloc[-1])
    prior_avg = float(pd.Series(adj_vol).shift(1).rolling(p.volume_lookback).mean().iloc[-1])
    vol_ratio = volume / prior_avg
    cbase, is_ev = A.change_base(float(W["close"].iloc[-2]), W["date"].iloc[-2], as_of, events)
    change_pct = (close / cbase - 1.0) * 100.0
    chk, src_chg, chk_diff, _ = A.check_change(close, cbase, is_ev, t.change_twd, t.change_sign)
    prev_day = trade[tpos[as_of] - 1] if tpos[as_of] > 0 else None
    prev_row = rows_by_date.get(prev_day) if prev_day is not None else None
    if not is_ev and prev_row is not None and A.row_kind(prev_row.close, prev_row.volume) == "no_trade":
        chk, src_chg, chk_diff, _, chk_note = A.check_change_after_no_trade(
            close, cbase, t.change_twd, t.change_sign, t.change_note, prev_row.next_ref_twd)
        base["data_note"] = "；".join(x for x in (chk_note, base["data_note"]) if x)

    c1 = (close > ma_s) and (ma_s > ma_l)
    c2 = close > prior_high
    c3 = volume >= prior_avg * p.volume_multiplier
    c4 = turnover >= p.min_turnover
    flags = {"C1_TREND": c1, "C2_BREAKOUT": c2, "C3_VOLUME": c3, "C4_TURNOVER": c4}
    all_pass = all(flags[k] for k in flags if en[k])

    s = dict(base)
    s.update(extra)
    s.update(status="PASS" if all_pass else "FAIL", close_twd=close, change_pct=change_pct,
             change_base_twd=cbase, source_change_twd=src_chg, change_check=chk, change_check_diff_twd=chk_diff,
             volume_shares=volume, amount_twd=turnover, ma_short=ma_s, ma_long=ma_l,
             prior_high_twd=prior_high, prior_avg_volume_shares=prior_avg, volume_ratio=vol_ratio,
             pass_trend=c1, pass_breakout=c2, pass_volume=c3, pass_turnover=c4,
             n_adj_events=len(events), adj_events="；".join(e.desc for e in events) or None,
             coverage_note=coverage_note, price_source=A.clean_str(t.source), price_fetched_at=A.clean_str(t.fetched_at),
             price_version=(int(t.version) if pd.notna(getattr(t, "version", math.nan)) else None))

    ms, ml, bl, vl, vm = p.ma_short, p.ma_long, p.breakout_lookback, p.volume_lookback, p.volume_multiplier
    spec = {
        "C1_TREND": (close, f"收盤 {_fp(close)}；MA{ms} {_fp(ma_s)}；MA{ml} {_fp(ma_l)}", ma_s,
                     f"收盤 > MA{ms} 且 MA{ms} > MA{ml}",
                     f"收盤 {_fp(close)} {'>' if close > ma_s else '≤'} MA{ms} {_fp(ma_s)}，"
                     f"MA{ms} {_fp(ma_s)} {'>' if ma_s > ma_l else '≤'} MA{ml} {_fp(ma_l)}"),
        "C2_BREAKOUT": (close, f"收盤 {_fp(close)}；前 {bl} 日最高價（還原）{_fp(prior_high)}", prior_high,
                        f"收盤 > {_fp(prior_high)}",
                        f"收盤 {_fp(close)} {'>' if c2 else '≤'} 前 {bl} 日最高 {_fp(prior_high)}"),
        "C3_VOLUME": (vol_ratio, f"量比 {vol_ratio:.2f}（當日 {_fi(volume)} 股／前 {vl} 日均量 {_fi(prior_avg)} 股）",
                      vm, f"量比 ≥ {vm:g}",
                      f"當日 {_fi(volume)} 股 {'≥' if c3 else '<'} 均量 {_fi(prior_avg)} 股 × {vm:g} = {_fi(prior_avg * vm)} 股"),
        "C4_TURNOVER": (turnover, f"成交金額 {_fi(turnover)} 元", p.min_turnover, f"≥ {_fi(p.min_turnover)} 元",
                        f"成交金額 {_fi(turnover)} 元 {'≥' if c4 else '<'} {_fi(p.min_turnover)} 元"),
    }
    conds = []
    for cid, (av, at, tv, tt, why) in spec.items():
        if en[cid]:
            passed: Optional[bool] = bool(flags[cid])
            reason = ("成立：" if passed else "不成立：") + why
        else:
            passed, reason = None, f"未啟用（不列入判斷）；參考：{why}"
        conds.append({"symbol": sid, "price_date": as_of, "condition_id": cid,
                      "condition_name": CONDITION_NAMES[cid], "enabled": en[cid],
                      "actual_value": float(av), "actual_text": at, "threshold_value": float(tv),
                      "threshold_text": tt, "passed": passed, "reason": reason, "exclusion_code": None})
    return s, conds
