"""日行情漲跌符號與停牌／無成交分類（TWSE／TPEx 共用）。"""
from __future__ import annotations

import re
import sqlite3

from pipeline.dates import parse_number

_HTML = re.compile(r"<[^>]+>")
# 較長的詞在前，避免「除權息」先被切成「除權」
_EX_TOKENS = ("除權息", "除息", "除權", "減資")


def strip_cell(s: str | None) -> str:
    if s is None:
        return ""
    return _HTML.sub("", str(s)).replace("\xa0", " ").strip()


def parse_change(
    sign_or_combined: str | None,
    value: str | None = None,
) -> tuple[float | None, str | None, str | None]:
    """回傳 (change, change_sign, change_note)。

    - 官方 X 或上櫃除權／除息／減資等無比價：change=NULL、change_note='無比價'，
      change_sign 保留原始符號（'X' 或 '除息' 等）。
    - 空白漲跌且價差 0：平盤，change=0、change_sign=''、note=None。
    - +／-：帶正負號的數值。
    - ---／空： (None, None, None)。
    """
    sign_text = strip_cell(sign_or_combined)
    if value is None:
        compact = sign_text.replace(" ", "")
        upper = compact.upper()
        if upper.startswith("X"):
            return None, "X", "無比價"
        for tok in _EX_TOKENS:
            if tok in sign_text:
                return None, tok, "無比價"
        if compact in ("", "-", "--", "---", "N/A", "n/a", "無"):
            return None, None, None
        chg = parse_number(sign_text)
        if chg is None:
            if "X" in upper:
                return None, "X", "無比價"
            return None, None, None
        if chg > 0:
            return chg, "+", None
        if chg < 0:
            return chg, "-", None
        return 0.0, "", None

    val_text = strip_cell(value)
    combined_upper = (sign_text + val_text).upper()
    if "X" in combined_upper:
        return None, "X", "無比價"
    for tok in _EX_TOKENS:
        if tok in sign_text or tok in val_text:
            return None, tok, "無比價"
    val = parse_number(value)
    if "+" in sign_text:
        return val, "+", None
    if "-" in sign_text:
        if val is None:
            return None, "-", None
        return -abs(val), "-", None
    # 空白符號：平盤或僅有數值
    if val is None:
        return None, "" if sign_text == "" else (sign_text or None), None
    if val == 0:
        return 0.0, "", None
    if val > 0:
        return val, "+", None
    return val, "-", None


def load_halt_coverage(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], dict[str, list[tuple[str, str]]]]:
    """官方 trading_halts。

    resume_date 有值：涵蓋 [halt_date, resume_date)。
    resume_date 為空：只涵蓋 halt_date 當日。上櫃 tpex_spendi_history 常缺恢復日，
    但股票之後已恢復成交，不可把後續有收盤價的日子標成停牌。
    """
    exact: set[tuple[str, str]] = set()
    intervals: dict[str, list[tuple[str, str]]] = {}
    for sym, hd, rd in conn.execute(
        "SELECT symbol, halt_date, resume_date FROM trading_halts WHERE is_current=1"
    ):
        if not sym or not hd:
            continue
        if rd is None or rd == "" or rd == hd:
            exact.add((str(sym), str(hd)))
        else:
            intervals.setdefault(str(sym), []).append((str(hd), str(rd)))
    return exact, intervals


def is_official_halt(
    symbol: str,
    price_date: str,
    exact: set[tuple[str, str]],
    intervals: dict[str, list[tuple[str, str]]],
) -> bool:
    if (symbol, price_date) in exact:
        return True
    for hd, rd in intervals.get(symbol, []):
        if hd <= price_date < rd:
            return True
    return False


def trade_flags(
    close,
    symbol: str,
    price_date: str,
    exact: set[tuple[str, str]],
    intervals: dict[str, list[tuple[str, str]]],
) -> tuple[int, int]:
    """回傳 (is_no_trade, is_halted)。is_halted 只依官方 trading_halts，不以 close 空值推定。"""
    is_no_trade = 1 if close is None else 0
    is_halted = 1 if is_official_halt(symbol, price_date, exact, intervals) else 0
    return is_no_trade, is_halted
