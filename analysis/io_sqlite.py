"""薄的 SQLite 讀取層：把 data/twstock.db（docs/schema.md 定稿）讀成 screener.screen() 需要的 DataFrame。

- 只讀（以 `mode=ro` URI 開啟），不寫入、不建表、不修改資料。
- 只取 is_current = 1 的現行列；量（volume_shares）單位為股、金額（amount_twd）為新台幣元，不做換算。
- 計算邏輯一律在 screener.py；本檔不做任何指標計算。

用法見 analysis/USAGE.md：
    inputs = load_screen_inputs("data/twstock.db", as_of="2026-11-09")
    result = screener.screen(**inputs.screen_kwargs(), params=ScreenParams())
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

import adjust as A

__all__ = ["tracked_members", "ScreenInputs", "connect_readonly", "default_as_of", "load_screen_inputs", "load_coverage", "load_coverage_scoped",
           "load_halts", "load_symbol_inputs", "max_fetched_at",
           "COVERAGE_TABLE", "HALTS_TABLE", "COVERAGE_SCOPES"]

# 覆蓋起日表（負責人 2026-11-10 決定欄位為 market, action_kind, coverage_start；由資料工程建立）
COVERAGE_TABLE = "corporate_actions_coverage"
COVERAGE_COLUMNS = ("market", "action_kind", "coverage_start")
# docs/schema.md DDL 另有 coverage_end（NOT NULL）, source, fetched_at（同一 market+kind 可多段）；有則讀
COVERAGE_OPTIONAL = ("coverage_end", "source", "fetched_at")
# 覆蓋表 scope（管線 3b 新增欄；2026-11-17 19:41 核可）：評估範圍 → 接受的 scope 值。
# "all"＝全市場（或不確定時的預設，守保）：只接受 scope='all'；all_pending_ratio、sample30、subset、NULL 及其他值都不算齊全。
# "tracked"＝追蹤清單（例如 sample30，須同時指定 symbols）：接受 sample30 或 all。資料庫沒有 scope 欄時不篩選（維持現狀）。
COVERAGE_SCOPES = {"all": ("all",), "tracked": ("sample30", "all")}
COVERAGE_SCOPE_LABELS = {"all": "全市場", "tracked": "追蹤清單"}

_PRICE_SQL_COLS = ("symbol, market, price_date, open_twd, high_twd, low_twd, close_twd, change_twd, change_sign, "
                   "volume_shares, amount_twd, source, fetched_at, version, is_current")
_ACTION_OPTIONAL = ("cash_subscription_ratio", "share_exchange_ratio", "announced_at_note")
_ACTION_SQL_COLS = ("symbol, market, action_date, action_type, ex_type_raw, prev_close_twd, reference_twd, "
                    "dividend_value_twd, cash_dividend_twd, rights_dividend_sum_twd, stock_dividend_per_thousand, "
                    "capital_reduction_ratio, refund_per_share_twd, source, fetched_at, version, is_current")


@dataclass(frozen=True)
class ScreenInputs:
    as_of: str
    window_start: str
    prices: pd.DataFrame
    calendar: pd.DataFrame
    actions: pd.DataFrame
    securities: pd.DataFrame
    market_days: pd.DataFrame
    actions_coverage: Optional[pd.DataFrame]   # None＝表不存在；空表＝表為空
    coverage_status: str                        # coverage_table_missing／coverage_table_empty／ok
    halts: Optional[pd.DataFrame] = None        # trading_halts；None＝表不存在
    halts_status: str = "halts_table_missing"   # halts_table_missing／ok
    input_max_fetched_at: Optional[str] = None  # 本次讀入各表列的 fetched_at 最大值（寫入 *_results.input_max_fetched_at）
    coverage_scope: str = "all"                 # 本次評估範圍的覆蓋 scope 模式（all／tracked）
    coverage_scope_note: Optional[str] = None   # 覆蓋表 scope 篩選說明；資料庫沒有 scope 欄時為 None
    as_of_resolution: Any = None                # as_of 未傳入時的 adjust.AsOfResolution（規則、範圍檔數、落後代號）；有傳入為 None
    volume_basis_status: str = "ok"             # ok／volume_basis_column_missing（缺欄：研究照算，writer 拒寫；indicators.md §9.3）

    def screen_kwargs(self) -> dict[str, Any]:
        """直接展開給 screener.screen()。"""
        return dict(prices=self.prices, calendar=self.calendar, actions=self.actions,
                    securities=self.securities, as_of=self.as_of, market_days=self.market_days,
                    actions_coverage=self.actions_coverage, halts=self.halts,
                    coverage_scope_note=self.coverage_scope_note)


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"找不到資料庫：{p}")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def default_as_of(con_or_path: "sqlite3.Connection | str | Path", symbols: Optional[Iterable[str]] = None):
    """as_of 未傳入時的預設評估日（規則見 adjust.resolve_as_of／AS_OF_RULE；全套件唯一入口）。
    追蹤範圍：symbols；None＝securities 現行普通股（is_current=1 AND is_common=1）。
    只看範圍內代號的現行行情列，且行情日必須是 trading_calendar 的交易日。回傳 adjust.AsOfResolution。"""
    if not isinstance(con_or_path, sqlite3.Connection):
        with closing(connect_readonly(con_or_path)) as con:
            return default_as_of(con, symbols)
    con = con_or_path
    if symbols is None:
        scope = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM securities WHERE is_current = 1 AND is_common = 1 ORDER BY symbol")]
    else:
        scope = list(dict.fromkeys(str(x).strip() for x in symbols))
    if not scope:
        raise ValueError("追蹤範圍是空的，無法決定 as_of")
    trade = [r[0] for r in con.execute("SELECT calendar_date FROM trading_calendar WHERE is_trade_day = 1")]
    latest: dict[str, str] = {}
    for i in range(0, len(scope), 500):
        chunk = scope[i:i + 500]
        latest.update(dict(con.execute(
            "SELECT p.symbol, MAX(p.price_date) FROM daily_prices p JOIN trading_calendar c "
            "ON c.calendar_date = p.price_date AND c.is_trade_day = 1 "
            f"WHERE p.is_current = 1 AND p.symbol IN ({','.join('?' * len(chunk))}) GROUP BY p.symbol", chunk).fetchall()))
    return A.resolve_as_of(latest, trade, scope)


def load_screen_inputs(
    db_path: str | Path,
    as_of: Optional[str] = None,
    symbols: Optional[Iterable[str]] = None,
    lookback_trade_days: int = 250,
    coverage_scope: str = "all",
) -> ScreenInputs:
    """讀取評估日 as_of（YYYY-MM-DD；None＝依 `default_as_of()`：追蹤範圍內有現行行情的最新交易日）往前 lookback_trade_days 個交易日的資料。

    symbols：限定代號（例如第二階段 30 檔）；None＝securities 全部普通股。
    lookback_trade_days 需大於 ScreenParams.required_valid_days + max_gap_days（預設 60+3），預設 250。
    coverage_scope：除權息覆蓋表 scope 模式（docs/indicators.md 第 10.5 節；資料庫有 scope 欄才生效）。
      "all"（預設，守保）＝只接受 scope='all'；"tracked"＝評估範圍是追蹤清單（必須同時給 symbols），接受 sample30 或 all。
      不接受的覆蓋列一律當作無覆蓋，走既有 strict 排除碼（EXCL_ADJ_COVERAGE_GAP）。
    """
    if coverage_scope not in COVERAGE_SCOPES:
        raise ValueError(f"coverage_scope 只能是 {tuple(COVERAGE_SCOPES)}，收到 {coverage_scope!r}")
    if coverage_scope == "tracked" and symbols is None:
        raise ValueError("coverage_scope='tracked'（追蹤清單）必須同時指定 symbols；全市場評估只能用 'all'")
    if lookback_trade_days < 2:
        raise ValueError("lookback_trade_days 至少 2")
    with closing(connect_readonly(db_path)) as con:
        ccols = {r[1] for r in con.execute("PRAGMA table_info(trading_calendar)")}
        cal = pd.read_sql_query("SELECT calendar_date, is_trade_day, name, source_kind"
                                + (", fetched_at" if "fetched_at" in ccols else "")
                                + " FROM trading_calendar ORDER BY calendar_date", con)
        trade = cal.loc[cal["is_trade_day"] == 1, "calendar_date"].tolist()
        if not trade:
            raise ValueError("trading_calendar 沒有交易日")
        as_of_res = None
        if as_of is None:
            as_of_res = default_as_of(con, symbols)
            as_of = as_of_res.as_of.strftime("%Y-%m-%d")
        if as_of not in set(trade):
            raise ValueError(f"as_of {as_of} 不是 trading_calendar 的交易日")
        idx = trade.index(as_of)
        start = trade[max(0, idx - lookback_trade_days + 1)]
        cal = cal[cal["calendar_date"] >= start].reset_index(drop=True)
        cal = cal[cal["calendar_date"] <= as_of].reset_index(drop=True)

        sym_list = None if symbols is None else [str(s).strip() for s in symbols]
        sym_sql, sym_args = "", []
        if sym_list is not None:
            sym_sql = f" AND symbol IN ({','.join('?' * len(sym_list))})"
            sym_args = sym_list

        pcols = {r[1] for r in con.execute("PRAGMA table_info(daily_prices)")}
        extra = ', "change"' if "change" in pcols else ""  # schema §7.3：change（含正負號）與 change_twd 同義
        if "is_halted" in pcols:                            # 資料庫已遷移的停牌旗標（schema.md DDL 尚未列）
            extra += ", is_halted"
        for c in ("is_no_trade", "change_note", A.NEXT_REF_COLUMN):   # 選用欄（核可 ②③）：存在才讀，只供核對與說明
            if c in pcols:
                extra += f", {c}"
        # 量額口徑（§9）：只讀 volume_basis，不讀 block_shares／block_amount_twd／block_source
        vb_status = "ok" if "volume_basis" in pcols else A.VOLUME_BASIS_COLUMN_MISSING
        if vb_status == "ok":
            extra += ", volume_basis"
        prices = pd.read_sql_query(
            f"SELECT {_PRICE_SQL_COLS}{extra} FROM daily_prices WHERE is_current = 1 AND price_date BETWEEN ? AND ?"
            f"{sym_sql} ORDER BY symbol, price_date", con, params=[start, as_of, *sym_args])
        acols = {r[1] for r in con.execute("PRAGMA table_info(corporate_actions)")}
        aextra = "".join(f", {c}" for c in _ACTION_OPTIONAL if c in acols)   # 待遷移欄位，存在才讀
        actions = pd.read_sql_query(
            f"SELECT {_ACTION_SQL_COLS}{aextra} FROM corporate_actions WHERE is_current = 1 AND action_date BETWEEN ? AND ?"
            f"{sym_sql} ORDER BY symbol, action_date", con, params=[start, as_of, *sym_args])
        # 減資／變更面額的恢復買賣日可能晚於 as_of（評估日尚在停止買賣期間），仍須讀入供停止買賣判定。
        # 還原因子只用 action_date <= as_of（screener 再篩）；此處不改既有窗口列，只補窗口外的這兩類事件。
        susp_types = ("capital_reduction", "par_value_change")
        later = pd.read_sql_query(
            f"SELECT {_ACTION_SQL_COLS}{aextra} FROM corporate_actions WHERE is_current = 1"
            f" AND action_type IN ({','.join('?' * len(susp_types))})"
            f" AND (action_date < ? OR action_date > ?)"
            f"{sym_sql} ORDER BY symbol, action_date",
            con, params=[*susp_types, start, as_of, *sym_args])
        if len(later):
            actions = pd.concat([actions, later], ignore_index=True)
        securities = pd.read_sql_query(
            f"SELECT symbol, market, name, listed_date, is_common, data_date, fetched_at FROM securities "
            f"WHERE is_current = 1 AND is_common = 1{sym_sql} ORDER BY symbol", con, params=sym_args)
        # 市場層級「當日有無行情」以全表計算，不受 symbols 限制，用於區分推定停牌與市場無資料
        # n_rows：該市場該日現行列數（< adjust.FULL_MARKET_MIN_ROWS＝整個市場都缺行情，停牌 insufficient 規則用）
        market_days = pd.read_sql_query(
            "SELECT market, price_date, COUNT(*) AS n_rows FROM daily_prices WHERE is_current = 1 AND price_date BETWEEN ? AND ? "
            "GROUP BY market, price_date",
            con, params=[start, as_of])
        coverage, cov_status, cov_note = load_coverage_scoped(con, coverage_scope, symbols)
        halts, halts_status = load_halts(con, start, as_of, sym_list)
    return ScreenInputs(as_of=as_of, window_start=start, prices=prices, calendar=cal, actions=actions,
                        securities=securities, market_days=market_days, actions_coverage=coverage,
                        coverage_status=cov_status, halts=halts, halts_status=halts_status,
                        input_max_fetched_at=max_fetched_at(prices, actions, cal, securities, coverage, halts),
                        coverage_scope=coverage_scope, coverage_scope_note=cov_note,
                        as_of_resolution=as_of_res, volume_basis_status=vb_status)


def max_fetched_at(*frames: Optional[pd.DataFrame]) -> Optional[str]:
    """各輸入表 fetched_at 的最大值（依實際時間比較，回傳原字串）；全無則 None。
    無時區的字串視為 Asia/Taipei。"""
    best, best_ts = None, None
    for df in frames:
        if df is None or df.empty or "fetched_at" not in df.columns:
            continue
        for v in df["fetched_at"].dropna().astype(str).unique():
            ts = pd.to_datetime(v, errors="coerce")
            if ts is pd.NaT or pd.isna(ts):
                continue
            ts = ts.tz_localize("Asia/Taipei") if ts.tzinfo is None else ts
            if best_ts is None or ts > best_ts:
                best, best_ts = v, ts
    return best


HALTS_TABLE = "trading_halts"


def load_halts(con: sqlite3.Connection, start: str, as_of: str, symbols: Optional[list[str]] = None
               ) -> tuple[Optional[pd.DataFrame], str]:
    """讀 trading_halts（pipeline/migrate.sql：market, symbol, halt_date, halt_time, resume_date, resume_time,
    halt_type, source, …, version, is_current）。只取與 [start, as_of] 重疊的停牌（halt_date ≤ as_of 且
    resume_date 為 NULL 或 > start）。表不存在 → (None, 'halts_table_missing')，不報錯。"""
    if not _table_exists(con, HALTS_TABLE):
        return None, "halts_table_missing"
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({HALTS_TABLE})")}
    for c in ("symbol", "halt_date"):
        if c not in cols:
            raise ValueError(f"{HALTS_TABLE} 缺少欄位 {c}")
    sel = [c for c in ("market", "symbol", "halt_date", "halt_time", "resume_date", "resume_time", "halt_type",
                       "source", "fetched_at") + A.HALT_XCHECK_COLUMNS if c in cols]   # 管線 3b 欄只作交叉檢查
    where = "h.halt_date <= ?" + (" AND (h.resume_date IS NULL OR h.resume_date > ?)" if "resume_date" in cols else "")
    args: list[Any] = [as_of] + ([start] if "resume_date" in cols else [])
    if "is_current" in cols:
        where += " AND h.is_current = 1"
    if symbols is not None:
        where += f" AND h.symbol IN ({','.join('?' * len(symbols))})"
        args += symbols
    # 停牌規則（2026-11-17 18:31 核可）：無官方恢復日時，取停牌日之後、評估日（含）以前第一個有官方成交
    # （有收盤價且成交量 > 0、且為交易日）的日子作為「依成交推定」的恢復日；不看評估日之後（不前視）。
    # 在資料庫端算，避免停牌日早於讀取窗口時看不到中間的成交。
    trade_resume = ("(SELECT MIN(p.price_date) FROM daily_prices p JOIN trading_calendar c "
                    "ON c.calendar_date = p.price_date AND c.is_trade_day = 1 "
                    "WHERE p.symbol = h.symbol AND p.is_current = 1 AND p.price_date > h.halt_date AND p.price_date <= ? "
                    "AND p.close_twd IS NOT NULL AND p.volume_shares > 0)")
    if "resume_date" in cols:
        trade_resume = f"CASE WHEN h.resume_date IS NULL THEN {trade_resume} END"
    df = pd.read_sql_query(f"SELECT {', '.join('h.' + c for c in sel)}, {trade_resume} AS {A.HALT_TRADE_RESUME_COLUMN} "
                           f"FROM {HALTS_TABLE} h WHERE {where}", con, params=[as_of] + args)
    _add_halt_holes(con, df, as_of)
    return df, "ok"


def _add_halt_holes(con: sqlite3.Connection, df: pd.DataFrame, as_of: str) -> None:
    """insufficient 規則（2026-11-17 19:41 核可）：對無官方恢復日的停牌列，算推定區間
    （停牌日之後、評估日（含）以前、依成交推定的恢復日之前）內「整個市場都缺行情」的交易日數與起訖，
    寫入 adjust.HALT_HOLE_COLUMNS。「整個市場都缺行情」＝該市場當日現行 daily_prices 列數 < adjust.FULL_MARKET_MIN_ROWS（分市場）；
    停牌列沒有市場別時，任一市場都不足才算。只用評估日（含）以前的資料（不前視）。"""
    n_col, f_col, l_col = A.HALT_HOLE_COLUMNS
    df[n_col], df[f_col], df[l_col] = pd.Series(dtype="float64"), None, None
    if df.empty:
        return
    trade = [r[0] for r in con.execute(
        "SELECT calendar_date FROM trading_calendar WHERE is_trade_day = 1 AND calendar_date <= ? ORDER BY calendar_date", (as_of,))]
    full: dict[str, set] = {}
    for m, d in con.execute("SELECT market, price_date FROM daily_prices WHERE is_current = 1 AND price_date <= ? "
                            "GROUP BY market, price_date HAVING COUNT(*) >= ?", (as_of, A.FULL_MARKET_MIN_ROWS)):
        full.setdefault(str(m), set()).add(d)
    mkts = list(full) or [""]
    import bisect as _b
    rd = df["resume_date"] if "resume_date" in df.columns else pd.Series(None, index=df.index)
    for i in df.index:
        if pd.notna(rd.loc[i]) and str(rd.loc[i]) != "":
            continue
        hd = str(df.at[i, "halt_date"])[:10]
        tr = df.at[i, A.HALT_TRADE_RESUME_COLUMN]
        end = str(tr)[:10] if pd.notna(tr) and tr else None
        m = str(df.at[i, "market"]) if "market" in df.columns and pd.notna(df.at[i, "market"]) else None
        lo = _b.bisect_right(trade, hd)
        span = trade[lo:] if end is None else trade[lo:_b.bisect_left(trade, end)]
        if m is not None:
            fs = full.get(m, set())
            holes = [d for d in span if d not in fs]
        else:
            holes = [d for d in span if all(d not in full.get(k, set()) for k in mkts)]
        df.at[i, n_col] = len(holes)
        if holes:
            df.at[i, f_col], df.at[i, l_col] = holes[0], holes[-1]


def load_coverage(con: sqlite3.Connection, coverage_scope: str = "all") -> tuple[Optional[pd.DataFrame], str]:
    """讀 corporate_actions_coverage(market, action_kind, coverage_start［, coverage_end, source, fetched_at］)。
    回傳 (None, 'coverage_table_missing')／(空表, 'coverage_table_empty'／'coverage_scope_none_accepted')／(表, 'ok')。
    表存在但缺必要欄位 → 拋 ValueError（schema 不一致，不靜默當成無覆蓋）。
    有 scope 欄時依 coverage_scope 篩選（見 load_coverage_scoped）。"""
    df, status, _note = load_coverage_scoped(con, coverage_scope)
    return df, status


TRACKED_UNIVERSE_TABLE = "universe_members"
TRACKED_UNIVERSE_ID = "sample30"


def tracked_members(con: sqlite3.Connection) -> Optional[frozenset[str]]:
    """追蹤清單代號（權威來源 pipeline/config.py SAMPLE30；DB 鏡像 universe_members.universe_id='sample30'，
    docs/schema.md §3.30）。不看 included（清單成員即算）。表不存在或沒有 sample30 列 → None（呼叫端保守只認 all）。"""
    if not _table_exists(con, TRACKED_UNIVERSE_TABLE):
        return None
    rows = con.execute(f"SELECT symbol FROM {TRACKED_UNIVERSE_TABLE} WHERE universe_id=?", (TRACKED_UNIVERSE_ID,)).fetchall()
    return frozenset(str(r[0]).strip() for r in rows) or None


def load_coverage_scoped(con: sqlite3.Connection, coverage_scope: str = "all", symbols: Optional[Iterable[str]] = None
                         ) -> tuple[Optional[pd.DataFrame], str, Optional[str]]:
    """同 load_coverage，另回傳 scope 篩選說明（資料庫沒有 scope 欄 → None，維持現狀、不篩選）。
    有 scope 欄時：coverage_scope='all' 只接受 scope='all'；'tracked' 接受 sample30 或 all，
    但前提是 symbols 全部屬於追蹤清單（universe_members.universe_id='sample30'）；表不存在、或有代號不在清單內 → 保守只接受 all。
    其他值（all_pending_ratio、subset、unknown、NULL 等）一律不採用＝當作無覆蓋。同一區間可有多列（管線 fix1 主鍵含 scope），
    不採用的列直接略過、採用的列照常合併。"""
    if coverage_scope not in COVERAGE_SCOPES:
        raise ValueError(f"coverage_scope 只能是 {tuple(COVERAGE_SCOPES)}，收到 {coverage_scope!r}")
    if not _table_exists(con, COVERAGE_TABLE):
        return None, "coverage_table_missing", None
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({COVERAGE_TABLE})")}
    missing = [c for c in COVERAGE_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"{COVERAGE_TABLE} 缺少欄位 {missing}（應為 {COVERAGE_COLUMNS}）")
    where = " WHERE is_current = 1" if "is_current" in cols else ""
    has_scope = "scope" in cols
    sel = list(COVERAGE_COLUMNS) + [c for c in COVERAGE_OPTIONAL if c in cols] + (["scope"] if has_scope else [])
    df = pd.read_sql_query(f"SELECT {', '.join(sel)} FROM {COVERAGE_TABLE}{where}", con)
    if not has_scope:
        return df, ("coverage_table_empty" if df.empty else "ok"), None
    accept = COVERAGE_SCOPES[coverage_scope]
    member_note = ""
    if coverage_scope == "tracked":
        mem = tracked_members(con)
        syms = sorted({str(x).strip() for x in (symbols or [])})
        outside = [x for x in syms if mem is None or x not in mem]
        if mem is None:
            accept, member_note = ("all",), "（universe_members 無追蹤清單 sample30，保守只接受 all）"
        elif outside or not syms:
            accept = ("all",)
            member_note = (f"（{len(outside)} 檔不在追蹤清單 universe_members sample30："
                           + "、".join(outside[:10]) + ("…" if len(outside) > 10 else "") + "，保守只接受 all）")
    sc = df["scope"].map(lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip())
    keep = sc.isin(accept)
    dropped = sc[~keep].fillna("NULL").value_counts().sort_index()
    note = (f"除權息覆蓋表 scope：評估範圍＝{COVERAGE_SCOPE_LABELS[coverage_scope]}，只接受 {'／'.join(accept)}；"
            f"{member_note}採用 {int(keep.sum())} 列"
            + (f"，不採用 {int((~keep).sum())} 列（" + "、".join(f"{k} {int(v)}" for k, v in dropped.items()) + "，視為無覆蓋）"
               if len(dropped) else ""))
    out = df[keep].reset_index(drop=True)
    if df.empty:
        status = "coverage_table_empty"
    elif out.empty:
        status = "coverage_scope_none_accepted"
    else:
        status = "ok"
    return out, status, note


def load_symbol_inputs(db_path: str | Path, symbol: str, as_of: Optional[str] = None,
                       lookback_trade_days: int = 250, coverage_scope: str = "all") -> dict[str, Any]:
    """個股圖用：回傳可直接展開給 series.ma_series()／series.change_series() 的參數（不含 symbol 以外的行情）。"""
    inp = load_screen_inputs(db_path, as_of=as_of, symbols=[symbol], lookback_trade_days=lookback_trade_days,
                             coverage_scope=coverage_scope)
    return dict(prices=inp.prices, actions=inp.actions, calendar=inp.calendar, symbol=str(symbol),
                as_of=inp.as_of, actions_coverage=inp.actions_coverage, securities=inp.securities)
