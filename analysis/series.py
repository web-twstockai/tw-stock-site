"""個股序列（給個股頁圖表）：均線序列 ma_series() 與漲跌幅序列 change_series()——純函式。

與 screener.py 共用 analysis/adjust.py 的價格調整、跳空偵測、覆蓋檢查與漲跌幅基準，不另寫一套邏輯。
在同一評估日，ma_series 的最後一列 MA 與 screener 的 ma_short／ma_long 相同（測試與獨立核對皆驗證）。

可用範圍（trust boundary）：以下任一情形，邊界日之前的所有列標示「不可用」，調整後價格與 MA 為 NULL，不硬算：
  1. corporate_actions 覆蓋不足：表不存在／為空／缺某市場某類 → 整段不可用；覆蓋起日之前不可用。
  2. 事件無法解析（缺因子、不一致）→ 該事件日之前不可用。
  3. 無法解釋的跳空 → 跳空日之前不可用。
邊界取三者最晚者；邊界之後的列不受影響（其累積因子只含邊界後、已解析成功的事件）。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

import adjust as A
from screener import ScreenParams

__all__ = ["ma_series", "change_series", "ohlc_series", "OhlcSeries", "MA_SERIES_STATUS", "OHLC_COLUMNS", "EVENT_COLUMNS"]

MA_SERIES_STATUS = {
    "ok": "可用",
    "no_trade": "無成交或無資料列（停牌或資料缺漏，待確認）",
    "unavailable": "調整因子不可用（見 unavailable_code／unavailable_reason）",
}


def _prepare(prices, actions, calendar, symbol, as_of, securities, actions_coverage):
    cal = A.normalize_calendar(calendar)
    trade_all = sorted(cal.loc[cal["is_trade_day"], "calendar_date"])
    px = A.normalize_prices(prices)
    px = px[px["symbol"] == str(symbol)]
    if as_of is not None:
        as_of_ts = A.to_ts(as_of)
    else:   # 預設評估日：範圍＝本檔（adjust.resolve_as_of 同一規則）；個股頁應傳入與選股相同的 as_of
        as_of_ts = A.resolve_as_of(A.latest_trade_day_by_symbol(px["symbol"], px["date"], trade_all),
                                   trade_all, [str(symbol)]).as_of
    if as_of_ts not in set(trade_all):
        raise ValueError(f"as_of {as_of_ts.date()} 不是交易日（依 trading_calendar）")
    trade = [d for d in trade_all if d <= as_of_ts]
    tpos = {d: i for i, d in enumerate(trade)}
    px = px[(px["date"] <= as_of_ts) & px["date"].isin(tpos.keys())].sort_values("date", kind="mergesort")
    if px["date"].duplicated().any():
        raise ValueError(f"{symbol} 有重複行情列")
    acts = A.normalize_actions(actions)
    acts = acts[(acts["symbol"] == str(symbol)) & (acts["action_date"] <= as_of_ts)]
    secs = A.normalize_securities(securities)
    sec = secs.loc[str(symbol)] if secs is not None and str(symbol) in secs.index else None
    market = (A.clean_str(sec["market"]) if sec is not None else None) or (
        A.clean_str(px["market"].iloc[-1]) if len(px) else None)
    listing_idx0 = None
    if sec is not None and pd.notna(sec["listed_date"]) and pd.Timestamp(sec["listed_date"]) >= trade[0]:
        listing_idx0 = bisect.bisect_left(trade, pd.Timestamp(sec["listed_date"]))
    cov = A.normalize_coverage(actions_coverage)
    return as_of_ts, trade, tpos, px, acts, market, listing_idx0, cov


def _boundary(valid, acts, market, cov, p, listing_idx0, tpos, as_of):
    """回傳 (邊界日或 None＝全段可用, 'ALL'＝全段不可用, 代碼, 說明, 成功事件)。"""
    cands = []  # (日期, 代碼, 說明)
    cb, probs, cb_note = A.coverage_boundary(cov, market, as_of)
    if probs:
        return "ALL", "EXCL_ADJ_COVERAGE_GAP", ";".join(probs), []
    if cb is not None and len(valid) and valid["date"].iloc[0] < cb:
        cands.append((cb, "EXCL_ADJ_COVERAGE_GAP", cb_note))
    events = A.resolve_events(valid, acts, None, p.adj_prev_close_tolerance, p.derive_share_factor)
    for e in events:
        if e.code:
            cands.append((e.date, e.code, e.detail))
    ok_events = [e for e in events if e.code is None]
    lo = max([c[0] for c in cands], default=None)
    if p.check_unexplained_gap and len(valid) > 1:
        v = valid if lo is None else valid[valid["date"] >= lo]
        if len(v) > 1:
            F, _ = A.cumulative_factors(v["date"], ok_events)
            gl = A.unexplained_gaps(list(v["date"]), v["close"].to_numpy() * F, p.max_daily_move,
                                    p.legacy_max_daily_move, listing_idx0, tpos, p.new_listing_no_limit_days)
            if gl:
                i, mv, lim = gl[-1]
                d = v["date"].iloc[i]
                cands.append((d, "EXCL_UNEXPLAINED_GAP", f"{d.date()} 還原後漲跌 {mv * 100:.2f}% 超過 ±{lim * 100:.2f}%"))
    if not cands:
        return None, None, None, ok_events
    d, code, detail = max(cands, key=lambda c: c[0])
    return d, code, detail, [e for e in ok_events if e.date > d]


def ma_series(
    prices: pd.DataFrame,
    actions: Optional[pd.DataFrame],
    calendar: pd.DataFrame,
    symbol: str,
    windows: Sequence[int] = (20, 60),
    as_of: Any = None,
    actions_coverage: Any = None,
    securities: Optional[pd.DataFrame] = None,
    params: Optional[ScreenParams] = None,
) -> pd.DataFrame:
    """每個交易日一列（自該股第一筆行情起至 as_of），欄位：
    price_date, symbol, close_twd（原始）, adj_factor（價格累積因子 F）, share_factor（股數累積因子 S）,
    adj_close_twd, status（ok／no_trade／unavailable）, unavailable_code, unavailable_reason,
    以及每個 N：ma_N（NULL 時見 ma_N_note：INSUFFICIENT_HISTORY／ADJ_UNAVAILABLE／NO_TRADE）。
    MA＝最近 N 個有效交易日（含當日）的還原收盤簡單平均，與 screener 相同。"""
    p = ScreenParams() if params is None else params
    ws = [int(w) for w in windows]
    if not ws or any(w < 1 or w > 500 for w in ws) or len(set(ws)) != len(ws):
        raise ValueError("windows 必須是 1～500 且不重複的整數")
    as_of_ts, trade, tpos, px, acts, market, listing_idx0, cov = _prepare(
        prices, actions, calendar, symbol, as_of, securities, actions_coverage)
    cols = ["price_date", "symbol", "close_twd", "adj_factor", "share_factor", "adj_close_twd", "status",
            "unavailable_code", "unavailable_reason"] + [c for w in ws for c in (f"ma_{w}", f"ma_{w}_note")]
    if px.empty:
        return pd.DataFrame(columns=cols)
    valid = px[(px["volume"] > 0) & px["close"].notna()].reset_index(drop=True)
    bd, code, detail, events = _boundary(valid, acts, market, cov, p, listing_idx0, tpos, as_of_ts)

    usable = np.ones(len(valid), dtype=bool)
    if bd == "ALL":
        usable[:] = False
    elif bd is not None:
        usable = (valid["date"] >= bd).to_numpy()
    F = np.full(len(valid), np.nan)
    S = np.full(len(valid), np.nan)
    if usable.any():
        Fu, Su = A.cumulative_factors(valid.loc[usable, "date"], events)
        F[usable], S[usable] = Fu, Su
    adj = valid["close"].to_numpy() * F
    mas = {}
    for w in ws:
        m = A.rolling_mean(adj, w)          # 視窗內含 NaN（不可用）時 rolling 結果為 NaN
        note = np.where(np.arange(len(valid)) + 1 < w, "INSUFFICIENT_HISTORY", np.where(np.isnan(m), "ADJ_UNAVAILABLE", None))
        mas[w] = (m, note)

    vpos = {d: i for i, d in enumerate(valid["date"])}
    first = px["date"].iloc[0]
    rows = []
    for d in trade[tpos[first]:]:
        r: dict[str, Any] = {"price_date": d, "symbol": str(symbol)}
        if d in vpos:
            i = vpos[d]
            r.update(close_twd=float(valid["close"].iloc[i]))
            if usable[i]:
                r.update(adj_factor=float(F[i]), share_factor=float(S[i]), adj_close_twd=float(adj[i]), status="ok")
            else:
                r.update(adj_factor=None, share_factor=None, adj_close_twd=None, status="unavailable",
                         unavailable_code=code, unavailable_reason=detail)
            for w in ws:
                m, note = mas[w]
                val = None if np.isnan(m[i]) else float(m[i])
                r[f"ma_{w}"] = val
                r[f"ma_{w}_note"] = None if val is not None else (note[i] or "ADJ_UNAVAILABLE")
        else:
            r.update(close_twd=None, status="no_trade", unavailable_reason=MA_SERIES_STATUS["no_trade"])
            for w in ws:
                r[f"ma_{w}"], r[f"ma_{w}_note"] = None, "NO_TRADE"
        rows.append(r)
    return pd.DataFrame(rows, columns=cols)


def change_series(
    prices: pd.DataFrame,
    actions: Optional[pd.DataFrame],
    calendar: pd.DataFrame,
    symbol: str,
    as_of: Any = None,
    actions_coverage: Any = None,
    securities: Optional[pd.DataFrame] = None,
    params: Optional[ScreenParams] = None,
) -> pd.DataFrame:
    """每個有效交易日一列：漲跌幅＝當日收盤 ÷ 基準價 − 1；基準價＝前一有效交易日收盤，
    除權息／減資事件日（含跨停牌）改以參考價（前收 × 事件價格因子）為基準。
    並與來源 change_twd／change_sign 交叉檢查（容許值見 adjust.change_tolerance），不一致只標記 change_check=mismatch，不覆蓋。
    欄位：price_date, symbol, close_twd, prev_price_date, prev_close_twd, change_base_twd, is_event_day,
          change_twd_calc, change_pct, source_change_twd, change_check, change_check_diff_twd, change_tolerance_twd,
          status（ok／unavailable／first_row）, note。"""
    p = ScreenParams() if params is None else params
    as_of_ts, trade, tpos, px, acts, market, listing_idx0, cov = _prepare(
        prices, actions, calendar, symbol, as_of, securities, actions_coverage)
    cols = ["price_date", "symbol", "close_twd", "prev_price_date", "prev_close_twd", "change_base_twd",
            "is_event_day", "change_twd_calc", "change_pct", "source_change_twd", "change_check",
            "change_check_diff_twd", "change_tolerance_twd", "status", "note"]
    valid = px[(px["volume"] > 0) & px["close"].notna()].reset_index(drop=True)
    if valid.empty:
        return pd.DataFrame(columns=cols)
    events = A.resolve_events(valid, acts, None, p.adj_prev_close_tolerance, p.derive_share_factor)
    cb, probs, cb_note = A.coverage_boundary(cov, market, as_of_ts)
    rows = []
    for i in range(len(valid)):
        r = valid.iloc[i]
        out: dict[str, Any] = {"price_date": r["date"], "symbol": str(symbol), "close_twd": float(r["close"])}
        if i == 0:
            out.update(status="first_row", note="無前一有效交易日")
            rows.append(out)
            continue
        pv = valid.iloc[i - 1]
        between = [e for e in events if pv["date"] < e.date <= r["date"]]
        bad = [e for e in between if e.code]
        out.update(prev_price_date=pv["date"], prev_close_twd=float(pv["close"]))
        if bad:
            out.update(status="unavailable", note=f"{bad[0].code}：{bad[0].detail}", is_event_day=True)
            rows.append(out)
            continue
        base, is_ev = A.change_base(float(pv["close"]), pv["date"], r["date"], between)
        chk, src, diff, tol = A.check_change(float(r["close"]), base, is_ev, r["change_twd"], r["change_sign"])
        notes = []
        if probs:
            notes.append(";".join(probs))
        elif cb is not None and r["date"] < cb:
            notes.append(f"{cb_note}（未確認是否為除權息日）")
        out.update(change_base_twd=base, is_event_day=is_ev, change_twd_calc=float(r["close"]) - base,
                   change_pct=(float(r["close"]) / base - 1.0) * 100.0, source_change_twd=src,
                   change_check=chk, change_check_diff_twd=diff, change_tolerance_twd=tol, status="ok",
                   note="；".join(notes) or None)
        rows.append(out)
    return pd.DataFrame(rows, columns=cols)


OHLC_COLUMNS = ["price_date", "symbol", "open_twd", "high_twd", "low_twd", "close_twd", "volume_shares", "amount_twd",
                "adj_factor", "share_factor", "adj_open_twd", "adj_high_twd", "adj_low_twd", "adj_close_twd",
                "adj_volume_shares", "status", "unavailable_code", "unavailable_reason"]
EVENT_COLUMNS = ["action_date", "action_type", "action_kind", "status", "price_factor", "share_factor",
                 "code", "detail", "desc"]


@dataclass(frozen=True)
class OhlcSeries:
    """bars：每個交易日一列（OHLC_COLUMNS）；events：as_of 以前的除權息／減資等事件清單（EVENT_COLUMNS）。"""
    bars: pd.DataFrame
    events: pd.DataFrame
    boundary_date: Optional[pd.Timestamp]   # 可用範圍起日（之前不可用）；None＝全段可用或無資料
    all_unavailable: bool                   # True＝整段不可用（例如覆蓋表為空）


def ohlc_series(
    prices: pd.DataFrame,
    actions: Optional[pd.DataFrame],
    calendar: pd.DataFrame,
    symbol: str,
    as_of: Any = None,
    actions_coverage: Any = None,
    securities: Optional[pd.DataFrame] = None,
    params: Optional[ScreenParams] = None,
) -> OhlcSeries:
    """個股 K 線用的前復權 OHLC 序列（純函式），口徑與 screener／ma_series 相同：
    - 以 as_of 為基準前復權：還原價＝原始價 × F(d)，還原量＝原始量 × S(d)；成交金額不還原。
    - 可用範圍與 ma_series 相同（覆蓋不足、事件無法解析、無法解釋跳空 → 邊界前整段 status=unavailable，還原值為 NULL，原始值照列）。
    - 停牌、無成交或無資料列的交易日：status=no_trade，原始值照來源（可能為 NULL），還原值 NULL。
    events：as_of 以前該股全部事件，status＝applied（已用於還原）／before_boundary（解析成功但在可用範圍邊界前）／
    failed（無法解析，code 為排除代碼）；price_factor／share_factor 為單一事件因子 f／s（失敗時 NULL）。"""
    p = ScreenParams() if params is None else params
    as_of_ts, trade, tpos, px, acts, market, listing_idx0, cov = _prepare(
        prices, actions, calendar, symbol, as_of, securities, actions_coverage)
    if px.empty:
        return OhlcSeries(pd.DataFrame(columns=OHLC_COLUMNS), pd.DataFrame(columns=EVENT_COLUMNS), None, False)
    valid = px[(px["volume"] > 0) & px["close"].notna()].reset_index(drop=True)
    bd, code, detail, ok_events = _boundary(valid, acts, market, cov, p, listing_idx0, tpos, as_of_ts)
    usable = np.ones(len(valid), dtype=bool)
    if bd == "ALL":
        usable[:] = False
    elif bd is not None:
        usable = (valid["date"] >= bd).to_numpy()
    F = np.full(len(valid), np.nan)
    S = np.full(len(valid), np.nan)
    if usable.any():
        Fu, Su = A.cumulative_factors(valid.loc[usable, "date"], ok_events)
        F[usable], S[usable] = Fu, Su

    def _f(v):
        return None if v is None or pd.isna(v) else float(v)

    vpos = {d: i for i, d in enumerate(valid["date"])}
    raw = {d: r for d, r in zip(px["date"], px.to_dict("records"))}
    rows = []
    for d in trade[tpos[px["date"].iloc[0]]:]:
        r0 = raw.get(d, {})
        out: dict[str, Any] = {"price_date": d, "symbol": str(symbol), "open_twd": _f(r0.get("open")),
                               "high_twd": _f(r0.get("high")), "low_twd": _f(r0.get("low")),
                               "close_twd": _f(r0.get("close")), "volume_shares": _f(r0.get("volume")),
                               "amount_twd": _f(r0.get("turnover"))}
        if d not in vpos:
            out.update(status="no_trade", unavailable_reason=MA_SERIES_STATUS["no_trade"])
        else:
            i = vpos[d]
            if usable[i]:
                f, sh = float(F[i]), float(S[i])
                out.update(adj_factor=f, share_factor=sh, status="ok",
                           adj_open_twd=None if out["open_twd"] is None else out["open_twd"] * f,
                           adj_high_twd=None if out["high_twd"] is None else out["high_twd"] * f,
                           adj_low_twd=None if out["low_twd"] is None else out["low_twd"] * f,
                           adj_close_twd=out["close_twd"] * f,
                           adj_volume_shares=out["volume_shares"] * sh)
            else:
                out.update(status="unavailable", unavailable_code=code, unavailable_reason=detail)
        rows.append(out)
    bars = pd.DataFrame(rows, columns=OHLC_COLUMNS)
    for c in ("unavailable_code", "unavailable_reason"):
        bars[c] = bars[c].astype(object).where(bars[c].notna(), None)

    applied = {(e.date, e.action_type) for e in ok_events}
    ev_rows = []
    for e in A.resolve_events(valid, acts, None, p.adj_prev_close_tolerance, p.derive_share_factor):
        st = "failed" if e.code else ("applied" if (e.date, e.action_type) in applied else "before_boundary")
        ev_rows.append({"action_date": e.date, "action_type": e.action_type,
                        "action_kind": A.canonical_action_type(e.action_type), "status": st,
                        "price_factor": None if e.code else float(e.f), "share_factor": None if e.code else float(e.s),
                        "code": e.code, "detail": e.detail or None, "desc": e.desc or None})
    events = pd.DataFrame(ev_rows, columns=EVENT_COLUMNS)
    return OhlcSeries(bars, events, None if bd in (None, "ALL") else bd, bd == "ALL")
