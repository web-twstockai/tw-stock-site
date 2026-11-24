"""價格調整與共用計算（純函式）——screener.py、series.py 共用，避免重複邏輯。

內容：輸入正規化（對齊 docs/schema.md）、corporate_actions 事件解析（價格因子 f、股數因子 s）、
前復權累積因子、無法解釋跳空偵測（依日期分段門檻）、覆蓋起日檢查、漲跌幅基準價與來源 change 交叉檢查。
定義見 docs/indicators.md 第 1、3 節（2026-11-10 負責人核可）。
"""

from __future__ import annotations

import bisect
import re
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence

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
        # 2026-11-10 12:05 核可 ③：is_no_trade 只做交叉檢查（說明欄），不參與判定；缺欄或 NULL＝未知
        "is_no_trade": _numeric(df, "is_no_trade") if "is_no_trade" in df.columns else math.nan,
        "change_note": df["change_note"].map(clean_str) if "change_note" in df.columns else None,
        # 量額口徑（docs/indicators.md §9，2026-11-17 16:09 核可）：incl_block／excl_block／NULL；缺欄＝None（研究模式）
        "volume_basis": df["volume_basis"].map(clean_str) if "volume_basis" in df.columns else None,
        # 官方次日參考價（TPEx dailyQuotes「次日參考價」，記在該日列，供下一交易日比對）；缺欄＝無官方參考價
        "next_ref_twd": _numeric(df, NEXT_REF_COLUMN) if NEXT_REF_COLUMN in df.columns else math.nan,
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
    if "announced_at_note" in df.columns:
        out["announced_at_note"] = df["announced_at_note"].where(pd.notna(df["announced_at_note"]), None).values
    return out.reset_index(drop=True)


# 減資／變更面額「停止買賣日～恢復買賣日前一日」（indicators.md §8.7；H AO 核可 2026-11-24 13:32）
SUSPENSION_ACTION_TYPES = frozenset({"capital_reduction", "par_value_change"})
STOP_TRADE_DATE_RE = re.compile(r"停止買賣日=(\d{4}-\d{2}-\d{2})")
MAX_INFERRED_SUSPENSION_DAYS = 15  # 無停止買賣日時，只向前連續推算無列日的上限


def corp_suspension_dates(
    actions: Optional[pd.DataFrame],
    symbol: str,
    trade_days: Sequence,
    has_row: Iterable,
    as_of: Any,
) -> set[pd.Timestamp]:
    """該股因減資或變更面額而官方停止買賣的交易日（恢復買賣日不含）。

    區間來源：`announced_at_note` 的「停止買賣日=YYYY-MM-DD」（上櫃 bulletin 有此欄）。
    若只有恢復買賣日（上市 TWTAUU 註明 action_date＝恢復買賣日、非停止買賣日），則把恢復日前、連續且本股無行情列的交易日視為停止買賣期間，最多 ``MAX_INFERRED_SUSPENSION_DAYS`` 日，遇到有列的日子即停。
    推算只涵蓋無列日，不會把「官方有列但本庫缺漏」標成停止買賣。
    """
    out: set[pd.Timestamp] = set()
    if actions is None or len(actions) == 0:
        return out
    as_of_ts = pd.Timestamp(as_of).normalize()
    trade = [pd.Timestamp(d).normalize() for d in trade_days if pd.notna(d) and pd.Timestamp(d).normalize() <= as_of_ts]
    if not trade:
        return out
    rows = {pd.Timestamp(d).normalize() for d in has_row if pd.notna(d)}
    df = actions
    if "symbol" in df.columns:
        df = df[df["symbol"].astype(str).str.strip() == str(symbol).strip()]
    if "action_type" not in df.columns or "action_date" not in df.columns:
        return out
    has_note = "announced_at_note" in df.columns
    for _, r in df.iterrows():
        typ = canonical_action_type(r.get("action_type"))
        if typ not in SUSPENSION_ACTION_TYPES:
            continue
        resume = r.get("action_date")
        if pd.isna(resume):
            continue
        resume = pd.Timestamp(resume).normalize()
        stop = None
        if has_note and pd.notna(r.get("announced_at_note")):
            m = STOP_TRADE_DATE_RE.search(str(r.get("announced_at_note")))
            if m:
                try:
                    stop = pd.Timestamp(m.group(1)).normalize()
                except (ValueError, TypeError):
                    stop = None
        if stop is not None and stop < resume:
            for d in trade:
                if stop <= d < resume:
                    out.add(d)
            continue
        i = bisect.bisect_left(trade, resume) - 1
        n = 0
        while i >= 0 and n < MAX_INFERRED_SUSPENSION_DAYS:
            d = trade[i]
            if d in rows:
                break
            out.add(d)
            i -= 1
            n += 1
    return out



HALT_REQUIRED = ("symbol", "halt_date")
# 選用欄：io_sqlite.load_halts 由資料庫算出「停牌日之後、評估日（含）以前第一個有官方成交的交易日」（只在 resume_date 為 NULL 時）
HALT_TRADE_RESUME_COLUMN = "trade_resume_date"
HALT_SRC_OFFICIAL = "官方恢復日"
HALT_SRC_TRADE = "依成交推定"
HALT_SRC_ONGOING = "仍停牌"
# 0.9.1（2026-11-17 19:41 核可）：推定區間（依成交推定／仍停牌）內有「整個市場都缺行情」的交易日 → 來源標記加註此字樣；
# 這些日子本股無資料列時不算停牌（停牌日當天仍算停牌），改計缺漏（評估日則為 EXCL_NO_ROW_SUSPENDED_OR_MISSING）
HALT_SRC_INSUFFICIENT = "資料不足（insufficient）"
HALT_SOURCES = (HALT_SRC_OFFICIAL, HALT_SRC_TRADE, HALT_SRC_ONGOING, HALT_SRC_INSUFFICIENT)
# 「整個市場都缺行情」：某市場某交易日的現行 daily_prices 列數 < 此值（與管線 halt_fix.FULL_MARKET_MIN_ROWS 同值，分市場判定）
FULL_MARKET_MIN_ROWS = 100
# 選用欄：io_sqlite.load_halts 以「評估日（含）以前」的資料預先算好的推定區間缺行情日數與起訖（讀取窗口以前的部分也算進去）
HALT_HOLE_COLUMNS = ("halt_hole_days", "halt_hole_first", "halt_hole_last")
# 選用欄：管線 3b 的推定結果，只寫進 data_note 當交叉檢查，不用於判定
HALT_XCHECK_COLUMNS = ("resume_basis", "resume_date_inferred")


class HaltSpan(NamedTuple):
    """解析後的停牌區間：停牌日 halt_date（含）～ resume（不含；None＝至評估日仍停牌）。
    source：官方恢復日／依成交推定／仍停牌。
    n_holes／hole_first／hole_last：推定區間（停牌日之後、評估日（含）以前、恢復日之前）內「整個市場都缺行情」的交易日數與起訖；
    > 0 即為「資料不足（insufficient）」。holes：其中落在本次可判定範圍（讀取窗口）內的日期，供逐日判定。
    xcheck：管線 resume_basis／resume_date_inferred 交叉檢查文字（只標註）。"""
    halt_date: pd.Timestamp
    resume: Optional[pd.Timestamp]
    source: str
    n_holes: int = 0
    hole_first: Optional[pd.Timestamp] = None
    hole_last: Optional[pd.Timestamp] = None
    holes: frozenset = frozenset()
    xcheck: Optional[str] = None

    @property
    def insufficient(self) -> bool:
        return self.n_holes > 0


def normalize_halts(halts: Optional[pd.DataFrame]) -> dict[str, list[tuple]]:
    """trading_halts → {symbol: [(halt_date, resume_date 或 None, trade_resume_date 或 None,
    缺行情日數 或 None, 缺行情首日 或 None, 缺行情末日 或 None, 管線交叉檢查文字 或 None)]}。None／缺表＝無停牌資訊。
    只取 is_current=1。停牌日判定要再經 resolve_halts()（docs/indicators.md 第 10 節）。
    resume_basis／resume_date_inferred（管線 3b 欄）只組成交叉檢查文字，不參與判定。"""
    if halts is None or len(halts) == 0:
        return {}
    require(halts, HALT_REQUIRED, "halts（trading_halts）")
    df = _current(halts)
    hd = _dates(df["halt_date"])
    rd = _dates(df["resume_date"]) if "resume_date" in df.columns else pd.Series(pd.NaT, index=df.index)
    td = (_dates(df[HALT_TRADE_RESUME_COLUMN]) if HALT_TRADE_RESUME_COLUMN in df.columns
          else pd.Series(pd.NaT, index=df.index))
    nh = (pd.to_numeric(df[HALT_HOLE_COLUMNS[0]], errors="coerce") if HALT_HOLE_COLUMNS[0] in df.columns
          else pd.Series(math.nan, index=df.index))
    hf = _dates(df[HALT_HOLE_COLUMNS[1]]) if HALT_HOLE_COLUMNS[1] in df.columns else pd.Series(pd.NaT, index=df.index)
    hl = _dates(df[HALT_HOLE_COLUMNS[2]]) if HALT_HOLE_COLUMNS[2] in df.columns else pd.Series(pd.NaT, index=df.index)
    has_x = any(c in df.columns for c in HALT_XCHECK_COLUMNS)
    xb = df[HALT_XCHECK_COLUMNS[0]] if HALT_XCHECK_COLUMNS[0] in df.columns else pd.Series(None, index=df.index, dtype=object)
    xi = df[HALT_XCHECK_COLUMNS[1]] if HALT_XCHECK_COLUMNS[1] in df.columns else pd.Series(None, index=df.index, dtype=object)
    out: dict[str, list] = {}
    for sym, a, b, c, n, f, l, x1, x2 in zip(df["symbol"].astype(str).str.strip(), hd, rd, td, nh, hf, hl, xb, xi):
        if pd.isna(a):
            continue
        xc = (f"管線交叉檢查：resume_basis＝{clean_str(x1) or '（空）'}，resume_date_inferred＝{clean_str(x2) or '（空）'}"
              if has_x else None)
        out.setdefault(sym, []).append((a, None if pd.isna(b) else b, None if pd.isna(c) else c,
                                        None if pd.isna(n) else int(n), None if pd.isna(f) else f,
                                        None if pd.isna(l) else l, xc))
    return out


def resolve_halts(records: Sequence[tuple], valid_dates: Iterable[pd.Timestamp], as_of: pd.Timestamp,
                  trade_days: Optional[Iterable[pd.Timestamp]] = None,
                  is_hole: Optional[Any] = None) -> list[HaltSpan]:
    """停牌規則（docs/indicators.md 第 10 節；18:31 核可、19:04 確認、19:41 insufficient 規則）：停牌從停牌日開始算。
    - 有官方恢復日：停到恢復日前一天（source＝官方恢復日），一律用官方日。
    - 沒有恢復日：停到停牌日之後第一個有官方成交列（有收盤價且成交量 > 0）的前一天（source＝依成交推定）；
      停牌日之後到評估日都沒有成交 → 仍在停牌（source＝仍停牌）。
    - 資料不足（insufficient）：推定區間（停牌日之後、評估日（含）以前、恢復日之前）內「整個市場都缺行情」的交易日
      （is_hole(d) 為真）記入 holes；這些日子本股無資料列時不算停牌（見 halt_span_on 的 has_row）。停牌日當天不受影響。
      records 第 4～6 欄（io_sqlite 以評估日以前資料預算的缺行情日數／首日／末日，含讀取窗口以前）有值時用來標示，
      否則以 trade_days 範圍內自行計算。
    不前視：只用評估日（含）以前的成交；valid_dates 與 records 第 3 欄（資料庫推得的成交恢復日）晚於 as_of 一律不用。
    停牌日晚於 as_of 的紀錄不採用。"""
    vd = sorted(d for d in valid_dates if d <= as_of)
    td = sorted(d for d in (trade_days or ()) if d <= as_of)
    out: list[HaltSpan] = []
    for rec in records:
        a, b = rec[0], rec[1]
        pre = rec[2] if len(rec) > 2 else None
        pn, pf, pl = (rec[3], rec[4], rec[5]) if len(rec) > 5 else (None, None, None)
        xc = rec[6] if len(rec) > 6 else None
        if a is None or a > as_of:
            continue
        if b is not None:
            out.append(HaltSpan(a, b, HALT_SRC_OFFICIAL, xcheck=xc))
            continue
        i = bisect.bisect_right(vd, a)
        first = vd[i] if i < len(vd) else None
        if pre is not None and a < pre <= as_of and (first is None or pre < first):
            first = pre
        src = HALT_SRC_TRADE if first is not None else HALT_SRC_ONGOING
        holes = frozenset(d for d in td if d > a and (first is None or d < first) and is_hole is not None and is_hole(d))
        if pn is not None:
            n, hf, hl = int(pn), pf, pl
        else:
            n = len(holes)
            hf, hl = (min(holes), max(holes)) if holes else (None, None)
        out.append(HaltSpan(a, first, src, n, hf, hl, holes, xc))
    return out


def halt_span_on(spans: Sequence[tuple], d: pd.Timestamp, has_row: bool = True) -> Optional[tuple]:
    """d 落在哪個停牌區間（halt_date ≤ d 且（resume 為 None 或 d < resume））；沒有則 None。
    has_row=False（本股當日無資料列）且 d 是該區間的「整個市場都缺行情」日（HaltSpan.holes）→ 不算停牌（insufficient 規則）。"""
    for x in spans:
        a, b = x[0], x[1]
        if a <= d and (b is None or d < b):
            if not has_row and d in getattr(x, "holes", ()):
                continue
            return x
    return None


def halt_note(span: HaltSpan) -> str:
    """停牌區間的來源說明（寫入 data_note／排除明細）。"""
    a = span.halt_date.date()
    if span.source == HALT_SRC_OFFICIAL:
        s = f"停牌日 {a}，官方恢復日 {span.resume.date()}"
    elif span.source == HALT_SRC_TRADE:
        s = f"停牌日 {a}，無官方恢復日，恢復日 {span.resume.date()}（依成交推定）"
    else:
        s = f"停牌日 {a}，無官方恢復日，停牌日後至評估日無官方成交，視為仍停牌"
    if getattr(span, "insufficient", False):
        rng = (f"{span.hole_first.date()}～{span.hole_last.date()}" if span.hole_first is not None and span.hole_last is not None
               else "日期見讀取窗口")
        s += (f"；推定區間內 {span.n_holes} 個交易日整個市場都缺行情（{rng}），這些日子本股無資料列時不算停牌"
              f"（{HALT_SRC_INSUFFICIENT}）")
    if getattr(span, "xcheck", None):
        s += f"；{span.xcheck}"
    return s


def is_halt_day(intervals: Sequence[tuple], d: pd.Timestamp, has_row: bool = True) -> bool:
    """intervals 為 HaltSpan 或 (halt_date, resume) 序列；resume 為 None＝未結束。has_row 見 halt_span_on。"""
    return halt_span_on(intervals, d, has_row) is not None


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


# ---------------- 量額口徑（docs/indicators.md §9；2026-11-17 16:09 核可，方案 A）----------------

VOLUME_BASIS_TOTAL = "incl_block"           # 選股採用的總量口徑（含鉅額）
VOLUME_BASIS_VALUES = ("incl_block", "excl_block")
VOLUME_BASIS_COLUMN_MISSING = "volume_basis_column_missing"


def is_total_basis(v: Any) -> bool:
    """只有 incl_block 算總量口徑；excl_block、NULL、其他值一律否（不推定、不以 block 欄補算）。"""
    return clean_str(v) == VOLUME_BASIS_TOTAL


def basis_label(v: Any) -> str:
    s = clean_str(v)
    if s == "excl_block":
        return "excl_block（不含鉅額，tradingStock）"
    if s is None:
        return "NULL（口徑不明）"
    return s if s == VOLUME_BASIS_TOTAL else f"{s}（非允許值，視為口徑不明）"


# ---------------- 整股無成交日相關（DECISIONS 2026-11-10 12:05 核可三項）----------------

NEXT_REF_COLUMN = "next_ref_price_twd"   # daily_prices 選用欄：官方次日參考價（TPEx dailyQuotes「次日參考價」；資料工程 2026-11-10 12:08 新增）
NO_COMPARE_NOTE = "無比價"              # TWSE 漲跌符號 X（不比價）的顯示文字，與 daily_prices.change_note 同字
CHANGE_CHECK_STATUSES = ("ok", "mismatch", "no_source", "source_not_comparable", "source_sign_conflict",
                         "after_no_trade")


def row_kind(close: Any, volume: Any) -> str:
    """單列行情分類（只看價量，不看 is_no_trade／is_halted）：
    valid＝收盤非 NULL 且成交量 > 0；bad_volume＝有收盤價但成交量 NULL（核可 ①：一律資料錯誤）；
    no_trade＝其餘（收盤 NULL，或成交量 0；含只有零股成交而收盤 NULL 的日子）。"""
    c_na = close is None or pd.isna(close)
    v_na = volume is None or pd.isna(volume)
    if not c_na and v_na:
        return "bad_volume"
    if not c_na and not v_na and float(volume) > 0:
        return "valid"
    return "no_trade"


def no_trade_flag_conflict(close: Any, volume: Any, is_no_trade: Any) -> Optional[str]:
    """is_no_trade 交叉檢查（核可 ③：只標註，不改結果碼）。旗標 NULL／缺欄或該列為 bad_volume 時不比對。
    回傳衝突說明或 None。"""
    if is_no_trade is None or pd.isna(is_no_trade):
        return None
    kind = row_kind(close, volume)
    if kind == "bad_volume":
        return None
    flag = int(float(is_no_trade)) == 1
    if flag and kind == "valid":
        return "is_no_trade＝1 但價量顯示有成交"
    if not flag and kind == "no_trade":
        return "is_no_trade＝0 但價量顯示整股無成交（收盤 NULL 或量 0）"
    return None


def check_change_after_no_trade(close: float, last_close: float, change_twd: Any, change_sign: Any,
                                change_note: Any = None, official_ref: Any = None
                                ) -> tuple[str, Optional[float], Optional[float], Optional[float], str]:
    """整股無成交日的次一有效交易日（前一交易日有列但整股無成交）之漲跌核對（核可 ②）。
    官方漲跌以參考價計（TWSE 標 X 不比價；TPEx 以次日參考價為基準），自算仍以前一有效收盤為基準（只顯示，不覆蓋）。
    回傳 (狀態, 來源帶號漲跌, 差異, 容許值, 說明)：
    - 來源 X 或 change_note＝「無比價」→ after_no_trade，說明「無比價」；
    - 有官方參考價 official_ref → 以 (收盤 − 參考價) 與來源漲跌比對：一致 after_no_trade，超過容許值 mismatch；
    - 無官方參考價 → after_no_trade（不比對、不算 mismatch），說明列出來源隱含的參考價；
    - 來源正負號矛盾 → source_sign_conflict（資料問題優先）；來源無漲跌 → after_no_trade。"""
    src, st = signed_source_change(change_twd, change_sign)
    head = "前一交易日整股無成交"
    sign = clean_str(change_sign)
    is_x = sign is not None and sign.upper() == "X"            # 來源 X 時 change_twd 多為 NULL，先判 X
    if is_x or st == "source_not_comparable" or (src is None and clean_str(change_note) == NO_COMPARE_NOTE):
        return "after_no_trade", None, None, None, f"{NO_COMPARE_NOTE}（{head}，官方漲跌標 X）"
    if st == "source_sign_conflict":
        return st, src, None, None, f"{head}；來源漲跌正負號矛盾"
    ref = None if official_ref is None or pd.isna(official_ref) or float(official_ref) <= 0 else float(official_ref)
    if st == "no_source":
        extra = f"；官方參考價 {ref:.2f}" if ref is not None else ""
        return "after_no_trade", None, None, None, f"{head}；官方未提供漲跌{extra}"
    if ref is not None:
        tol = NON_EVENT_TOLERANCE_TWD
        diff = (close - ref) - src
        if abs(diff) <= tol + 1e-9:
            return "after_no_trade", src, diff, tol, f"{head}；依官方參考價 {ref:.2f} 比對一致"
        return "mismatch", src, diff, tol, f"{head}；依官方參考價 {ref:.2f} 比對不一致（差 {diff:+.3f} 元）"
    return ("after_no_trade", src, None, None,
            f"{head}；官方漲跌以參考價計（隱含參考價 {close - src:.2f} 元），自算以前一有效收盤 {last_close:.2f} 元為基準，不比對")


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
