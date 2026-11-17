"""價格調整與共用計算（純函式）——screener.py、series.py 共用，避免重複邏輯。

內容：輸入正規化（對齊 docs/schema.md）、corporate_actions 事件解析（價格因子 f、股數因子 s）、
前復權累積因子、無法解釋跳空偵測（依日期分段門檻）、覆蓋起日檢查、漲跌幅基準價與來源 change 交叉檢查。
定義見 docs/indicators.md 第 1、3 節（2026-11-10 負責人核可）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

# 2015-06-01 起台股漲跌幅限制由 7% 放寬為 10%
LIMIT_CHANGE_DATE = pd.Timestamp("2015-06-01")
# 事件類型（2026-11-10 負責人核可完整清單）；左側為 schema 現行值的相容別名
ACTION_TYPE_ALIASES = MappingProxyType({
    "cash_dividend": "cash_dividend", "ex_dividend": "cash_dividend",
    "stock_dividend": "stock_dividend", "ex_rights": "stock_dividend",
    "ex_both": "ex_both",
    "capital_reduction": "capital_reduction",
    "par_value_change": "par_value_change",
    "stock_split": "stock_split",
})
DIVIDEND_TYPES = frozenset({"cash_dividend", "stock_dividend", "ex_both"})       # 正規化後的類型
EXCHANGE_TYPES = frozenset({"par_value_change", "stock_split"})
# 同日重複檢查的分組：同一天兩筆除權息類（不論息、權、權息）即不一致
DUP_GROUP = MappingProxyType({"cash_dividend": "dividend", "stock_dividend": "dividend", "ex_both": "dividend",
                              "capital_reduction": "capital_reduction", "par_value_change": "par_value_change",
                              "stock_split": "stock_split"})
ACTION_KINDS = DUP_GROUP  # 舊名相容
# strict 覆蓋檢查必須涵蓋的類別（action_kind 完整清單）
COVERAGE_KINDS = ("cash_dividend", "stock_dividend", "ex_both", "capital_reduction", "par_value_change", "stock_split")
# 面額變更／分割的換股比例欄位（每 1 舊股換得新股數）：schema §3.5 `share_exchange_ratio`
# （上市 TWTB7U／TWTB8U、上櫃 pvChgRslt）。capital_reduction_ratio 只屬減資，不作為面額變更的替代欄位。
EXCHANGE_RATIO_COLUMNS = ("share_exchange_ratio",)
# 覆蓋「由他類涵蓋」：台股分割依變更面額辦理（DECISIONS 2026-11-10 03:45），
# stock_split 的覆蓋＝自身覆蓋列 ∪ par_value_change 覆蓋列；兩種寫法（只有 par_value_change 列、或另寫 stock_split 列）皆可。
COVERAGE_COVERED_BY = MappingProxyType({"stock_split": ("par_value_change",)})


def canonical_action_type(t: Any) -> str:
    """正規化事件類型；不認得者原樣回傳（解析時判 EXCL_ADJ_FACTOR_MISSING）。"""
    return ACTION_TYPE_ALIASES.get(str(t).strip(), str(t).strip())


PRICE_REQUIRED = ("symbol", "price_date", "open_twd", "high_twd", "low_twd", "close_twd",
                  "volume_shares", "amount_twd")
CAL_REQUIRED = ("calendar_date", "is_trade_day")
ACT_REQUIRED = ("symbol", "action_date", "action_type")
ACT_NUMERIC = ("prev_close_twd", "reference_twd", "dividend_value_twd", "cash_dividend_twd",
               "stock_dividend_per_thousand", "capital_reduction_ratio", "refund_per_share_twd",
               "cash_subscription_ratio", "share_exchange_ratio")   # 後兩欄可不存在（schema 待遷移），缺欄＝NULL
COVERAGE_COLUMNS = ("market", "action_kind", "coverage_start")


# ------------------------------------------------------------------
# 基本工具
# ------------------------------------------------------------------
def to_ts(x: Any) -> pd.Timestamp:
    ts = pd.Timestamp(x)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()


def _dates(s: pd.Series) -> pd.Series:
    d = pd.to_datetime(s.replace("", np.nan))
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    return d.dt.normalize()


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    raw = df[col].replace("", np.nan)
    out = pd.to_numeric(raw, errors="coerce").astype(float)
    bad = out.isna() & raw.notna()
    if bad.any():
        raise ValueError(f"欄位 {col} 含非數值內容，例如 {raw[bad].iloc[0]!r}")
    return out


def require(df: Any, cols: Sequence[str], name: str) -> None:
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"{name} 必須是 pandas DataFrame")
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{name} 缺少必要欄位：{missing}")


def _current(df: pd.DataFrame) -> pd.DataFrame:
    if "is_current" in df.columns:
        return df[pd.to_numeric(df["is_current"], errors="coerce") == 1]
    return df


def clean_str(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    t = str(v).strip()
    return t or None


# ------------------------------------------------------------------
# 正規化（schema 欄位 → 內部欄位）
# ------------------------------------------------------------------
def normalize_calendar(cal: pd.DataFrame) -> pd.DataFrame:
    require(cal, CAL_REQUIRED, "calendar（trading_calendar）")
    out = pd.DataFrame({"calendar_date": _dates(cal["calendar_date"]),
                        "is_trade_day": pd.to_numeric(cal["is_trade_day"], errors="raise").astype(int) == 1})
    if out["calendar_date"].duplicated().any():
        raise ValueError("trading_calendar 有重複日期")
    return out


def normalize_prices(prices: pd.DataFrame) -> pd.DataFrame:
    require(prices, PRICE_REQUIRED, "prices（daily_prices）")
    df = _current(prices)
    sign = df["change_sign"].map(clean_str) if "change_sign" in df.columns else None
    out = pd.DataFrame({
        "symbol": df["symbol"].astype(str).str.strip(),
        "market": df["market"].map(lambda v: clean_str(v) or "") if "market" in df.columns else "",
        "date": _dates(df["price_date"]),
        "open": _numeric(df, "open_twd"), "high": _numeric(df, "high_twd"),
        "low": _numeric(df, "low_twd"), "close": _numeric(df, "close_twd"),
        "volume": _numeric(df, "volume_shares"), "turnover": _numeric(df, "amount_twd"),
        # schema §7.3：change（含正負號）與 change_twd 同義；兩者都有時以 change 為準，缺值再退回 change_twd
        "change_twd": (_numeric(df, "change").combine_first(_numeric(df, "change_twd"))
                       if "change" in df.columns else _numeric(df, "change_twd")),
        "change_sign": sign if sign is not None else None,
        # daily_prices.is_halted（資料庫已遷移、schema.md DDL 尚未列）：1＝來源標示停牌；缺欄或 NULL＝未知
        "is_halted": (_numeric(df, "is_halted").fillna(0) == 1) if "is_halted" in df.columns else False,
        "source": df["source"] if "source" in df.columns else None,
        "fetched_at": df["fetched_at"] if "fetched_at" in df.columns else None,
        "version": _numeric(df, "version") if "version" in df.columns else math.nan,
    })
    return out.reset_index(drop=True)


def normalize_actions(actions: Optional[pd.DataFrame]) -> pd.DataFrame:
    cols = ["symbol", "action_date", "action_type", *ACT_NUMERIC]
    if actions is None:
        return pd.DataFrame({c: pd.Series(dtype="datetime64[ns]" if c == "action_date" else object) for c in cols})
    require(actions, ACT_REQUIRED, "actions（corporate_actions）")
    df = _current(actions)
    out = pd.DataFrame({
        "symbol": df["symbol"].astype(str).str.strip(),
        "action_date": _dates(df["action_date"]),
        "action_type": df["action_type"].map(canonical_action_type),   # ex_dividend→cash_dividend 等
    })
    for c in ACT_NUMERIC:
        out[c] = _numeric(df, c)
    return out.reset_index(drop=True)


HALT_REQUIRED = ("symbol", "halt_date")


def normalize_halts(halts: Optional[pd.DataFrame]) -> dict[str, list[tuple[pd.Timestamp, Optional[pd.Timestamp]]]]:
    """trading_halts → {symbol: [(halt_date, resume_date 或 None)]}。None／缺表＝無停牌資訊。
    停牌日判定：halt_date ≤ d 且（resume_date 為 NULL 或 d < resume_date）。只取 is_current=1。"""
    if halts is None or len(halts) == 0:
        return {}
    require(halts, HALT_REQUIRED, "halts（trading_halts）")
    df = _current(halts)
    hd = _dates(df["halt_date"])
    rd = _dates(df["resume_date"]) if "resume_date" in df.columns else pd.Series(pd.NaT, index=df.index)
    out: dict[str, list] = {}
    for sym, a, b in zip(df["symbol"].astype(str).str.strip(), hd, rd):
        if pd.isna(a):
            continue
        out.setdefault(sym, []).append((a, None if pd.isna(b) else b))
    return out


def is_halt_day(intervals: Sequence[tuple[pd.Timestamp, Optional[pd.Timestamp]]], d: pd.Timestamp) -> bool:
    return any(a <= d and (b is None or d < b) for a, b in intervals)


def normalize_securities(secs: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if secs is None:
        return None
    require(secs, ("symbol",), "securities")
    df = _current(secs)
    out = pd.DataFrame({
        "symbol": df["symbol"].astype(str).str.strip(),
        "market": df["market"].map(clean_str) if "market" in df.columns else None,
        "name": df["name"] if "name" in df.columns else None,
        "listed_date": _dates(df["listed_date"]) if "listed_date" in df.columns else pd.NaT,
    })
    if out["symbol"].duplicated().any():
        raise ValueError("securities 代號重複")
    return out.set_index("symbol")


# ------------------------------------------------------------------
# 覆蓋區間（corporate_actions_coverage）
# 負責人 2026-11-10 決議最少欄位 (market, action_kind, coverage_start)；docs/schema.md DDL 另有
# coverage_end NOT NULL, source, fetched_at, notes，且「只有已成功完整取得的區間可記錄，中間失敗須切段」→
# 同一 (market, kind) 可有多段、多來源，合併後判定。coverage_end 若為 NULL（schema 不允許）視為至今。
# action_kind：完整清單 cash_dividend／stock_dividend／ex_both／capital_reduction／par_value_change／stock_split
# （2026-11-10 負責人核可），strict 須六類皆涵蓋。相容 schema §10 現行值 ex_right_dividend（＝息、權、權息三類）、
# capital_reduction_restore 等；對映表以外的值不計入覆蓋（例如減資預告），記在 Coverage.ignored_kinds。
# ------------------------------------------------------------------
_DIV3 = ("cash_dividend", "stock_dividend", "ex_both")
COVERAGE_KIND_ALIASES = MappingProxyType({
    # 完整清單（2026-11-10 負責人核可）
    "cash_dividend": ("cash_dividend",), "stock_dividend": ("stock_dividend",), "ex_both": ("ex_both",),
    "capital_reduction": ("capital_reduction",), "par_value_change": ("par_value_change",),
    "stock_split": ("stock_split",),
    # 相容：schema §10 現行值與舊值（同一來源表涵蓋息、權、權息三類）
    "ex_right_dividend": _DIV3, "dividend": _DIV3,
    "ex_dividend": ("cash_dividend",), "ex_rights": ("stock_dividend",),
    "capital_reduction_restore": ("capital_reduction",),
})
_OPEN_END = pd.Timestamp("2262-04-11")


@dataclass(frozen=True)
class Coverage:
    status: str                       # "ok"／"coverage_table_missing"／"coverage_table_empty"
    segments: Mapping[tuple[str, str], tuple[tuple[pd.Timestamp, pd.Timestamp], ...]]  # 已合併、已排序
    ignored_kinds: tuple[str, ...] = ()


def _isnull(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, float) and math.isnan(v)) or (v is pd.NaT)


def normalize_coverage(cov: Any) -> Coverage:
    """None＝表不存在；空 DataFrame／空 dict＝表為空；其餘解析並合併成各 (market, kind) 的覆蓋區間。
    DataFrame 欄位：market, action_kind, coverage_start［, coverage_end］。
    dict 形式：{(market, kind): 起日} 或 {(market, kind): (起日, 迄日)}。"""
    if cov is None:
        return Coverage("coverage_table_missing", {})
    if isinstance(cov, Coverage):
        return cov
    items: list[tuple[Any, Any, Any, Any]]
    if isinstance(cov, pd.DataFrame):
        require(cov, COVERAGE_COLUMNS, "actions_coverage（corporate_actions_coverage）")
        c = cov
        if "is_current" in c.columns:
            c = c[pd.to_numeric(c["is_current"], errors="coerce").fillna(1) == 1]
        ends = c["coverage_end"] if "coverage_end" in c.columns else [None] * len(c)
        items = list(zip(c["market"], c["action_kind"], c["coverage_start"], ends))
    elif isinstance(cov, Mapping):
        items = []
        for key, d in cov.items():
            if not (isinstance(key, tuple) and len(key) == 2):
                raise ValueError("actions_coverage 的鍵必須是 (market, action_kind)")
            st, en = (d if isinstance(d, tuple) else (d, None))
            items.append((key[0], key[1], st, en))
    else:
        raise TypeError("actions_coverage 必須是 DataFrame、dict 或 None")
    if not items:
        return Coverage("coverage_table_empty", {})
    raw: dict[tuple[str, str], list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    ignored = set()
    for m, k, st, en in items:
        kinds = COVERAGE_KIND_ALIASES.get(str(k).strip())
        if kinds is None:
            ignored.add(str(k))
            continue
        if _isnull(st):
            continue
        a = to_ts(st)
        b = _OPEN_END if _isnull(en) else to_ts(en)
        if b < a:
            raise ValueError(f"actions_coverage 區間迄日早於起日：{m} {k} {a.date()}～{b.date()}")
        for kind in kinds:
            raw.setdefault((str(m), kind), []).append((a, b))
    for (m, kind), lst in list(raw.items()):
        for target, sources in COVERAGE_COVERED_BY.items():
            if kind in sources:
                raw.setdefault((m, target), []).extend(lst)
    segs = {}
    for key, lst in raw.items():
        lst.sort()
        merged = [list(lst[0])]
        for a, b in lst[1:]:
            if a <= merged[-1][1] + pd.Timedelta(days=1):
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        segs[key] = tuple((a, b) for a, b in merged)
    return Coverage("ok", segs, tuple(sorted(ignored)))


def _segment_for(cov: Coverage, market: Optional[str], kind: str, as_of: pd.Timestamp
                 ) -> tuple[Optional[pd.Timestamp], Optional[str]]:
    """回傳 (含 as_of 那一段的起日, None) 或 (None, 問題代碼)。"""
    m = market or "未知市場"
    segs = cov.segments.get((market or "", kind))
    if not segs:
        return None, f"coverage_missing:{m}:{kind}"
    for a, b in segs:
        if a <= as_of <= b:
            return a, None
    if as_of < segs[0][0]:
        return None, f"window_before_coverage_start:{m}:{kind}:{segs[0][0].date()}"
    last_end = max(b for _, b in segs)
    if as_of > last_end:
        return None, f"coverage_end_before_as_of:{m}:{kind}:{last_end.date()}"
    return None, f"coverage_hole:{m}:{kind}:{as_of.date()}"


def coverage_problems(cov: Coverage, market: Optional[str], need_from: pd.Timestamp,
                      as_of: Optional[pd.Timestamp] = None) -> list[str]:
    """回傳覆蓋問題代碼清單（空＝已覆蓋）。[need_from, as_of] 必須落在同一連續覆蓋區間內。"""
    if cov.status != "ok":
        return [cov.status]
    as_of = need_from if as_of is None else as_of
    probs = []
    for kind in COVERAGE_KINDS:
        start, prob = _segment_for(cov, market, kind, as_of)
        if prob:
            probs.append(prob)
        elif start > need_from:
            probs.append(f"window_before_coverage_start:{market}:{kind}:{start.date()}")
    return probs


def coverage_boundary(cov: Coverage, market: Optional[str], as_of: pd.Timestamp
                      ) -> tuple[Optional[pd.Timestamp], list[str], Optional[str]]:
    """序列用：回傳 (起日, 問題, 起日說明)。日期 >= 起日的列才有完整覆蓋；問題非空表示整段不可用。"""
    if cov.status != "ok":
        return None, [cov.status], None
    starts, probs = [], []
    for kind in COVERAGE_KINDS:
        start, prob = _segment_for(cov, market, kind, as_of)
        if prob:
            probs.append(prob)
        else:
            starts.append((start, kind))
    if probs:
        return None, probs, None
    d, kind = max(starts)
    return d, [], f"window_before_coverage_start:{market}:{kind}:{d.date()}"


# ------------------------------------------------------------------
# 事件解析與累積因子
# ------------------------------------------------------------------
@dataclass(frozen=True)
class EventResolution:
    date: pd.Timestamp
    action_type: str
    code: Optional[str]        # None＝成功；否則為排除代碼
    detail: str
    f: float
    s: float
    desc: str


def _fail(d, t, code, detail):
    return EventResolution(d, t, code, detail, math.nan, math.nan, "")


def resolve_action(a: Any, our_prev: float, tolerance: float, derive_share_factor: bool) -> EventResolution:
    """解析單一 corporate_actions 事件。our_prev＝事件日前最後有效交易日收盤（行情）。"""
    d, t = a.action_date, a.action_type
    ds = d.date()
    src_prev = a.prev_close_twd
    if pd.notna(src_prev):
        if not src_prev > 0 or abs(src_prev / our_prev - 1.0) > tolerance:
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT",
                         f"{ds} {t} 來源前收 {src_prev} 與行情前收 {our_prev} 差異超過 {tolerance:.2%}")
        base = float(src_prev)
    else:
        base = our_prev
    ref = a.reference_twd
    if t in DIVIDEND_TYPES:
        if pd.isna(ref):
            return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} {t} 缺除權息參考價")
        if not ref > 0:
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} {t} 參考價不合理 {ref}")
        f = float(ref) / base
        csr = getattr(a, "cash_subscription_ratio", math.nan)   # 現金增資配股率（每股）；欄位不存在＝NULL
        if pd.notna(csr) and not csr >= 0:
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} {t} 現增配股率 {csr} 為負")
        sub = float(csr) if pd.notna(csr) else 0.0
        sub_note = f"＋現增配股率 {sub:g}（來源）" if pd.notna(csr) else ""
        note = ""
        if t == "cash_dividend":
            s = 1.0 + sub
            note = sub_note
        elif pd.notna(a.stock_dividend_per_thousand):
            s = 1.0 + float(a.stock_dividend_per_thousand) / 1000.0 + sub
            note = sub_note or "（假設該次無現金增資）"
        else:
            if sub > 0:
                return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING",
                             f"{ds} {t} 缺每仟股無償配股且有現金增資（現增配股率 {sub:g}），無法推算股數因子")
            div = 0.0 if t == "stock_dividend" else a.dividend_value_twd
            if not derive_share_factor or pd.isna(div):
                return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} {t} 缺每仟股無償配股，無法得出股數因子")
            s = (base - float(div)) / float(ref)
            note = "（股數因子推算；來源現增配股率 0）" if pd.notna(csr) else "（股數因子推算，假設該次無現金增資）"
        if not (s >= 1.0 - 1e-9):
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} {t} 股數因子 {s:.6f} < 1 不合理")
        return EventResolution(d, t, None, "", f, s, f"{ds} {t} f={f:.6f} s={s:.6f}{note}")
    if t == "capital_reduction":
        r = a.capital_reduction_ratio
        if pd.isna(r):
            return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} 減資缺換股率")
        if not (0 < r <= 1):
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} 減資換股率 {r} 不在 (0, 1]")
        note = ""
        if pd.isna(ref):
            # schema §9：refund_per_share_twd 為 NULL 表示來源未提供，不可當作零
            if pd.isna(a.refund_per_share_twd):
                return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} 減資缺參考價且退還股款未知（NULL 不當作零）")
            ref = (base - float(a.refund_per_share_twd)) / float(r)
            note = "（參考價依公式推算）"
        if not ref > 0:
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} 減資參考價不合理 {ref}")
        f = float(ref) / base
        return EventResolution(d, t, None, "", f, float(r), f"{ds} capital_reduction f={f:.6f} s={float(r):.6f}{note}")
    if t in EXCHANGE_TYPES:
        # 面額變更／分割：s＝換股率（每 1 舊股換發新股數），價值不變 → 參考價＝前收 ÷ 換股率
        r, col = math.nan, None
        for c in EXCHANGE_RATIO_COLUMNS:
            v = getattr(a, c, math.nan)
            if pd.notna(v):
                r, col = float(v), c
                break
        if pd.isna(r):
            return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} {t} 缺換股比例")
        if not r > 0:
            return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT", f"{ds} {t} 換股比例 {r} 不合理")
        if pd.notna(ref):
            if not ref > 0 or abs(float(ref) * r / base - 1.0) > tolerance:
                return _fail(d, t, "EXCL_ADJ_FACTOR_INCONSISTENT",
                             f"{ds} {t} 參考價 {ref} 與前收 {base}÷換股比例 {r:g} 差異超過 {tolerance:.2%}")
            f, note = float(ref) / base, ""
        else:
            f, note = 1.0 / r, "（參考價＝前收÷換股比例）"
        return EventResolution(d, t, None, "", f, r, f"{ds} {t} f={f:.6f} s={r:.6f}（{col}）{note}")
    return _fail(d, t, "EXCL_ADJ_FACTOR_MISSING", f"{ds} 事件類型 {t} 無法換算調整因子")


def resolve_events(valid: pd.DataFrame, acts: pd.DataFrame, after: Optional[pd.Timestamp],
                   tolerance: float, derive_share_factor: bool) -> list[EventResolution]:
    """解析 action_date > after（None＝全部）的事件，依日期排序。

    valid：該股有效交易日（欄位 date, close，已排序）。事件日前沒有任何有效交易日的事件不影響任何價格，略過。
    同日同類（dividend／capital_reduction）重複事件 → 皆判 EXCL_ADJ_FACTOR_INCONSISTENT。
    """
    ev = acts if after is None else acts[acts["action_date"] > after]
    ev = ev.sort_values("action_date", kind="mergesort")
    keys = [(DUP_GROUP.get(t, t), d) for t, d in zip(ev["action_type"], ev["action_date"])]
    dup = {k for k in keys if keys.count(k) > 1}
    vdates = valid["date"].to_numpy()
    vclose = valid["close"].to_numpy()
    out = []
    for a, k in zip(ev.itertuples(index=False), keys):
        i = np.searchsorted(vdates, np.datetime64(a.action_date), side="left")
        if i == 0:
            continue
        if k in dup:
            out.append(_fail(a.action_date, a.action_type, "EXCL_ADJ_FACTOR_INCONSISTENT",
                             f"{a.action_date.date()} 同一日有重複的同類調整事件"))
            continue
        out.append(resolve_action(a, float(vclose[i - 1]), tolerance, derive_share_factor))
    return out


def cumulative_factors(dates: Sequence[pd.Timestamp] | np.ndarray,
                       events: Sequence[EventResolution]) -> tuple[np.ndarray, np.ndarray]:
    """前復權累積因子：F(d)=Π f（事件日 > d），S(d)=Π s。只接受已成功解析的事件。"""
    d = np.asarray(pd.to_datetime(pd.Series(dates)).to_numpy())
    F = np.ones(len(d))
    S = np.ones(len(d))
    for e in events:
        if e.code is not None:
            raise ValueError("cumulative_factors 只接受成功解析的事件")
        before = d < np.datetime64(e.date)
        F[before] *= e.f
        S[before] *= e.s
    return F, S


def daily_move_limit(d: pd.Timestamp, max_daily_move: float, legacy_max_daily_move: float) -> float:
    """依日期分段的跳空門檻：2015-06-01 前用 legacy（預設 7%），之後用 max_daily_move（預設 11%）。"""
    return legacy_max_daily_move if d < LIMIT_CHANGE_DATE else max_daily_move


def unexplained_gaps(dates: Sequence[pd.Timestamp], adj_close: Sequence[float], max_daily_move: float,
                     legacy_max_daily_move: float, listing_idx0: Optional[int],
                     tpos: Mapping[pd.Timestamp, int], no_limit_days: int) -> list[tuple[int, float, float]]:
    """回傳 (索引 i, 還原漲跌, 門檻) 清單：第 i 列相對第 i-1 列的還原收盤漲跌超過門檻。上市初期免檢。"""
    out = []
    ac = pd.Series(np.asarray(adj_close, dtype=float))
    moves = (ac / ac.shift(1) - 1.0).to_numpy()
    for i in range(1, len(ac)):
        d = dates[i]
        lim = daily_move_limit(d, max_daily_move, legacy_max_daily_move)
        if abs(moves[i]) > lim:
            if listing_idx0 is not None and tpos[d] - listing_idx0 + 1 <= no_limit_days:
                continue
            out.append((i, float(moves[i]), lim))
    return out


def rolling_mean(values: Sequence[float], n: int) -> np.ndarray:
    return pd.Series(np.asarray(values, dtype=float)).rolling(n).mean().to_numpy()


# ------------------------------------------------------------------
# 漲跌幅與來源 change 交叉檢查
# ------------------------------------------------------------------
def tick_size(price: float) -> float:
    """台股普通股升降單位（2005 年起現制）。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def change_base(prev_close: float, prev_date: pd.Timestamp, date: pd.Timestamp,
                events: Sequence[EventResolution]) -> tuple[float, bool]:
    """漲跌幅基準價＝前一有效交易日收盤 × (prev_date, date] 內事件價格因子之積；回傳 (基準價, 是否事件日)。"""
    base, ev = prev_close, False
    for e in events:
        if prev_date < e.date <= date:
            base *= e.f
            ev = True
    return base, ev


NON_EVENT_TOLERANCE_TWD = 0.005  # 非事件日：價格兩位小數，容許 0.005 元


def change_tolerance(base: float, is_event_day: bool) -> float:
    """事件日：來源以開盤競價基準（參考價進位到最近檔位）計算，容許半檔＋0.005 元。"""
    return (tick_size(base) / 2.0 + NON_EVENT_TOLERANCE_TWD) if is_event_day else NON_EVENT_TOLERANCE_TWD


def signed_source_change(change_twd: Any, change_sign: Any) -> tuple[Optional[float], str]:
    """把來源 change_twd／change_sign 轉成帶號數值；回傳 (值, 狀態)。狀態：ok／no_source／source_not_comparable。"""
    if change_twd is None or (isinstance(change_twd, float) and math.isnan(change_twd)):
        return None, "no_source"
    sign = clean_str(change_sign)
    if sign is not None and sign.upper() == "X":
        return None, "source_not_comparable"
    v = float(change_twd)
    if sign == "-" and v > 0:
        v = -v
    if sign == "+" and v < 0:
        return None, "source_sign_conflict"
    return v, "ok"


def check_change(close: float, base: float, is_event_day: bool, change_twd: Any, change_sign: Any
                 ) -> tuple[str, Optional[float], Optional[float], float]:
    """回傳 (狀態, 來源帶號漲跌, 差異, 容許值)。狀態：ok／mismatch／no_source／source_not_comparable／source_sign_conflict。
    不一致只標記，不覆蓋自算值。"""
    src, st = signed_source_change(change_twd, change_sign)
    tol = change_tolerance(base, is_event_day)
    if st != "ok":
        return st, src, None, tol
    diff = (close - base) - src
    return ("ok" if abs(diff) <= tol + 1e-9 else "mismatch"), src, diff, tol


# ---------------- 預設評估日（as_of 未傳入時）——全套件唯一規則 ----------------

AS_OF_RULE = "scope_latest_trade_day-v1"


@dataclass(frozen=True)
class AsOfResolution:
    """as_of 未傳入時的決定結果。
    as_of：追蹤範圍內「有現行行情列」的最新交易日（trading_calendar.is_trade_day=1）。
    n_scope：追蹤範圍代號數；n_with_row：其中在 as_of 有行情列的檔數；
    lagging：範圍內最新行情日早於 as_of（或完全沒有行情）的代號，這些股票在該 as_of 會依既有規則被排除。"""
    as_of: pd.Timestamp
    rule: str
    n_scope: int
    n_with_row: int
    lagging: tuple[str, ...]


def resolve_as_of(latest_by_symbol: Mapping[Any, Any], trade_days: Sequence[Any] | Any,
                  scope: Optional[Sequence[Any]] = None) -> AsOfResolution:
    """預設 as_of 規則（`AS_OF_RULE`）：
    1. 只看追蹤範圍 scope 內的代號（None＝latest_by_symbol 全部鍵）；範圍外股票的日期一律不看。
    2. 每檔只取「是交易日」的行情日期（latest_by_symbol 的值必須已是該檔 ≤ 交易日曆最後一天、且為交易日的最新現行行情日；
       非交易日的值視為呼叫端錯誤，拋 ValueError）。
    3. as_of＝範圍內各檔上述日期的最大值。交易日曆是交易日但範圍內沒有任何行情列的日子（例如 2026-10-23）不會被選中。
    4. 範圍內各檔最新日期不一致時仍取最大值，較舊的代號列在 lagging（評估時依既有排除規則處理，不改 as_of）。
    5. 範圍內完全沒有行情 → ValueError。"""
    tds = {to_ts(d) for d in trade_days}
    if not tds:
        raise ValueError("交易日曆沒有任何交易日")
    keys = list(dict.fromkeys(str(k).strip() for k in (latest_by_symbol.keys() if scope is None else scope)))
    latest: dict[str, pd.Timestamp] = {}
    for k, v in latest_by_symbol.items():
        k = str(k).strip()
        if k not in keys or _isnull(v):
            continue
        d = to_ts(v)
        if d not in tds:
            raise ValueError(f"{k} 的最新行情日 {d.date()} 不是交易日（呼叫端須先限定交易日）")
        latest[k] = d
    if not latest:
        raise ValueError("追蹤範圍內沒有任何交易日的現行行情")
    as_of = max(latest.values())
    lag = tuple(sorted(k for k in keys if latest.get(k) != as_of))
    return AsOfResolution(as_of, AS_OF_RULE, len(keys), len(keys) - len(lag), lag)


def latest_trade_day_by_symbol(symbols: Sequence[Any], dates: Sequence[Any],
                               trade_days: Sequence[Any] | Any) -> dict[str, pd.Timestamp]:
    """由 (代號, 行情日) 序列算出每檔「是交易日」的最新行情日（給記憶體內呼叫端用，與 io_sqlite 的 SQL 等價）。"""
    tds = {to_ts(d) for d in trade_days}
    out: dict[str, pd.Timestamp] = {}
    for s, d in zip(symbols, dates):
        if _isnull(d):
            continue
        d = to_ts(d)
        if d in tds:
            s = str(s).strip()
            if s not in out or d > out[s]:
                out[s] = d
    return out
