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

__all__ = ["ScreenInputs", "connect_readonly", "default_as_of", "load_screen_inputs", "load_coverage", "load_halts", "load_symbol_inputs",
           "max_fetched_at",
           "COVERAGE_TABLE", "HALTS_TABLE"]

# 覆蓋起日表（負責人 2026-11-10 決定欄位為 market, action_kind, coverage_start；由資料工程建立）
COVERAGE_TABLE = "corporate_actions_coverage"
COVERAGE_COLUMNS = ("market", "action_kind", "coverage_start")
# docs/schema.md DDL 另有 coverage_end（NOT NULL）, source, fetched_at（同一 market+kind 可多段）；有則讀
COVERAGE_OPTIONAL = ("coverage_end", "source", "fetched_at")

_PRICE_SQL_COLS = ("symbol, market, price_date, open_twd, high_twd, low_twd, close_twd, change_twd, change_sign, "
                   "volume_shares, amount_twd, source, fetched_at, version, is_current")
_ACTION_OPTIONAL = ("cash_subscription_ratio", "share_exchange_ratio")
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
    as_of_resolution: Any = None                # as_of 未傳入時的 adjust.AsOfResolution（規則、範圍檔數、落後代號）；有傳入為 None

    def screen_kwargs(self) -> dict[str, Any]:
        """直接展開給 screener.screen()。"""
        return dict(prices=self.prices, calendar=self.calendar, actions=self.actions,
                    securities=self.securities, as_of=self.as_of, market_days=self.market_days,
                    actions_coverage=self.actions_coverage, halts=self.halts)


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
) -> ScreenInputs:
    """讀取評估日 as_of（YYYY-MM-DD；None＝依 `default_as_of()`：追蹤範圍內有現行行情的最新交易日）往前 lookback_trade_days 個交易日的資料。

    symbols：限定代號（例如第二階段 30 檔）；None＝securities 全部普通股。
    lookback_trade_days 需大於 ScreenParams.required_valid_days + max_gap_days（預設 60+3），預設 250。
    """
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
        prices = pd.read_sql_query(
            f"SELECT {_PRICE_SQL_COLS}{extra} FROM daily_prices WHERE is_current = 1 AND price_date BETWEEN ? AND ?"
            f"{sym_sql} ORDER BY symbol, price_date", con, params=[start, as_of, *sym_args])
        acols = {r[1] for r in con.execute("PRAGMA table_info(corporate_actions)")}
        aextra = "".join(f", {c}" for c in _ACTION_OPTIONAL if c in acols)   # 待遷移欄位，存在才讀
        actions = pd.read_sql_query(
            f"SELECT {_ACTION_SQL_COLS}{aextra} FROM corporate_actions WHERE is_current = 1 AND action_date BETWEEN ? AND ?"
            f"{sym_sql} ORDER BY symbol, action_date", con, params=[start, as_of, *sym_args])
        securities = pd.read_sql_query(
            f"SELECT symbol, market, name, listed_date, is_common, data_date, fetched_at FROM securities "
            f"WHERE is_current = 1 AND is_common = 1{sym_sql} ORDER BY symbol", con, params=sym_args)
        # 市場層級「當日有無行情」以全表計算，不受 symbols 限制，用於區分推定停牌與市場無資料
        market_days = pd.read_sql_query(
            "SELECT DISTINCT market, price_date FROM daily_prices WHERE is_current = 1 AND price_date BETWEEN ? AND ?",
            con, params=[start, as_of])
        coverage, cov_status = load_coverage(con)
        halts, halts_status = load_halts(con, start, as_of, sym_list)
    return ScreenInputs(as_of=as_of, window_start=start, prices=prices, calendar=cal, actions=actions,
                        securities=securities, market_days=market_days, actions_coverage=coverage,
                        coverage_status=cov_status, halts=halts, halts_status=halts_status,
                        input_max_fetched_at=max_fetched_at(prices, actions, cal, securities, coverage, halts),
                        as_of_resolution=as_of_res)


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
                       "source", "fetched_at") if c in cols]
    where = "halt_date <= ?" + (" AND (resume_date IS NULL OR resume_date > ?)" if "resume_date" in cols else "")
    args: list[Any] = [as_of] + ([start] if "resume_date" in cols else [])
    if "is_current" in cols:
        where += " AND is_current = 1"
    if symbols is not None:
        where += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += symbols
    df = pd.read_sql_query(f"SELECT {', '.join(sel)} FROM {HALTS_TABLE} WHERE {where}", con, params=args)
    return df, "ok"


def load_coverage(con: sqlite3.Connection) -> tuple[Optional[pd.DataFrame], str]:
    """讀 corporate_actions_coverage(market, action_kind, coverage_start［, coverage_end, source, fetched_at］)。
    回傳 (None, 'coverage_table_missing')／(空表, 'coverage_table_empty')／(表, 'ok')。
    表存在但缺必要欄位 → 拋 ValueError（schema 不一致，不靜默當成無覆蓋）。"""
    if not _table_exists(con, COVERAGE_TABLE):
        return None, "coverage_table_missing"
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({COVERAGE_TABLE})")}
    missing = [c for c in COVERAGE_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"{COVERAGE_TABLE} 缺少欄位 {missing}（應為 {COVERAGE_COLUMNS}）")
    where = " WHERE is_current = 1" if "is_current" in cols else ""
    sel = list(COVERAGE_COLUMNS) + [c for c in COVERAGE_OPTIONAL if c in cols]
    df = pd.read_sql_query(f"SELECT {', '.join(sel)} FROM {COVERAGE_TABLE}{where}", con)
    return df, ("coverage_table_empty" if df.empty else "ok")


def load_symbol_inputs(db_path: str | Path, symbol: str, as_of: Optional[str] = None,
                       lookback_trade_days: int = 250) -> dict[str, Any]:
    """個股圖用：回傳可直接展開給 series.ma_series()／series.change_series() 的參數（不含 symbol 以外的行情）。"""
    inp = load_screen_inputs(db_path, as_of=as_of, symbols=[symbol], lookback_trade_days=lookback_trade_days)
    return dict(prices=inp.prices, actions=inp.actions, calendar=inp.calendar, symbol=str(symbol),
                as_of=inp.as_of, actions_coverage=inp.actions_coverage, securities=inp.securities)
