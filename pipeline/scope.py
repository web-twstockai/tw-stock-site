"""資料範圍判定：月營收、財報、除權息覆蓋表共用（fix1：改嚴）。

scope 值（寫入各 *_status／coverage 表）：
- ``all``      ：不過濾（symbols 為空），或過濾清單涵蓋「全部現行普通股」（securities is_current=1 且 COALESCE(is_common,1)=1；
                  指定 market 時只比該市場）。其他情況一律不是 all——不再用「>100 檔」判定。
- ``sample30`` ：過濾清單完全落在追蹤清單（config.SAMPLE30＝universe_members.universe_id='sample30'）內。
- ``subset``   ：其他任何過濾清單（symbol_count 記實際代號數）。
- ``unknown``  ：舊資料無法判定（只用於 migration）。
"""
from __future__ import annotations

import sqlite3

SCOPE_ALL = "all"
SCOPE_SAMPLE30 = "sample30"
SCOPE_SUBSET = "subset"
SCOPE_UNKNOWN = "unknown"


def current_common_symbols(conn: sqlite3.Connection, market: str | None = None) -> set[str]:
    sql = "SELECT symbol FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1"
    args: tuple = ()
    if market:
        sql += " AND market=?"
        args = (market,)
    return {r[0] for r in conn.execute(sql, args)}


def scope_info(conn: sqlite3.Connection | None, symbols, market: str | None = None) -> tuple[str, int | None]:
    """回傳 (scope, symbol_count)。symbol_count：過濾清單的代號數；不過濾時為當時現行普通股數（無 conn 時 None）。"""
    from pipeline import config

    if not symbols:
        return SCOPE_ALL, (len(current_common_symbols(conn, market)) if conn is not None else None)
    sset = set(symbols)
    if conn is not None:
        uni = current_common_symbols(conn, market)
        if uni and uni <= sset:
            return SCOPE_ALL, len(uni)
    if sset <= set(config.SAMPLE30):
        return SCOPE_SAMPLE30, len(sset)
    return SCOPE_SUBSET, len(sset)


def scope_of(symbols, conn: sqlite3.Connection | None = None, market: str | None = None) -> str:
    """見模組說明。沒給 conn 時無法驗證「涵蓋全部現行普通股」，非空清單一律不是 all（寧嚴勿鬆）。"""
    return scope_info(conn, symbols, market)[0]
