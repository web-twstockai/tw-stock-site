"""終止上市／終止上櫃（下市）清單。
上市：openapi `company/suspendListingCsvAndHtml`（1 個請求，含 2001 年起全部；欄位 DelistingDate／Company／Code，無原因）。
上櫃：網頁版 `www/zh-tw/company/deListed?response=json&date=西元年`（每年 1 個請求；欄位含終止上櫃日期與原因）。
注意：「終止上櫃」包含轉上市，代號可能再出現在現行清單（以 delistings 與 securities 交叉比對，不自行推斷原因）。"""
from __future__ import annotations

import logging
import re
import sqlite3

from pipeline.dates import now_iso
from pipeline.db import touch_source
from pipeline.httputil import single_shot, tpex, twse

log = logging.getLogger("pipeline.delistings")
TWSE_URL = "https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/company/deListed?response=json&date={year}"


def roc_to_iso(s: str) -> str | None:
    """'115/09/01'、'115-10-16'、'1150901' → '2026-09-01'。"""
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", s) or re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _upsert(conn: sqlite3.Connection, market: str, sym: str, name: str, d: str, reason: str | None, source: str) -> str:
    old = conn.execute("SELECT name, reason FROM delistings WHERE market=? AND symbol=? AND delist_date=?", (market, sym, d)).fetchone()
    if old and (old[0], old[1]) == (name, reason):
        return "unchanged"
    conn.execute(
        """INSERT INTO delistings(market,symbol,name,delist_date,reason,source,fetched_at) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(market,symbol,delist_date) DO UPDATE SET name=excluded.name, reason=excluded.reason, source=excluded.source, fetched_at=excluded.fetched_at""",
        (market, sym, name, d, reason, source, now_iso()))
    return "updated" if old else "inserted"


def fetch_twse(conn: sqlite3.Connection) -> dict:
    with single_shot():
        resp = twse.request("GET", TWSE_URL)
    data = resp.json()
    c: dict = {}
    for r in data or []:
        sym, d = str(r.get("Code") or "").strip(), roc_to_iso(str(r.get("DelistingDate") or ""))
        if not sym or not d:
            continue
        st = _upsert(conn, "TWSE", sym, str(r.get("Company") or "").strip(), d, None, TWSE_URL)
        c[st] = c.get(st, 0) + 1
    touch_source(conn, "twse_delisting", url=TWSE_URL, ok=True, http_status=200, notes=f"rows={len(data or [])}")
    conn.commit()
    return c


def fetch_tpex(conn: sqlite3.Connection, years: list[int]) -> dict:
    c: dict = {"by_year": {}}
    for y in sorted(years):
        url = TPEX_URL.format(year=y)
        with single_shot():
            resp = tpex.request("GET", url)
        data = resp.json()
        tables = data.get("tables") or []
        rows = (tables[0].get("data") if tables else None) or []
        k = 0
        for r in rows:
            sym, d = str(r[0]).strip(), roc_to_iso(str(r[2]))
            if not sym or not d:
                continue
            st = _upsert(conn, "TPEx", sym, str(r[1]).strip(), d, str(r[3]).strip() if len(r) > 3 else None, url)
            c[st] = c.get(st, 0) + 1
            k += 1
        c["by_year"][y] = k
        touch_source(conn, f"tpex_delisted_{y}", url=url, ok=True, http_status=200, notes=f"rows={k}")
        conn.commit()
    return c


LAST_TRADE_BASIS = "daily_prices:last_traded_row"
HISTORY_VERSION_BASE = 9000        # 下市補寫列的 version 從 9000 起，避免與 securities 日後更新時封存的 version（小數字）撞 PK
HISTORY_BASIS = "delistings"


def fill_last_trade_dates(conn: sqlite3.Connection) -> dict:
    """delistings.last_trade_date／last_trade_basis（0 請求、可重跑、就地更新，不產生版本）。

    官方下市清單只有終止日、沒有最後交易日 → **不推估**；只取 DB 內該代號（同市場）≤ delist_date 的最後一筆「有成交」
    （volume_shares>0 或 transactions>0）官方行情日。為避免行情缺漏造成低估：last_trade_date 之後到 delist_date 之間的每個
    交易日（trading_calendar）都必須是該市場的「全市場日」（現行列數 ≥ FULL_MARKET_MIN_ROWS），且 calendar 要涵蓋 last_trade_date；
    否則（沒有行情列、期間缺行情、日曆不足）留空（NULL），basis 也留空。"""
    from pipeline.halt_fix import FULL_MARKET_MIN_ROWS

    out = {"filled": 0, "changed": 0, "blank": 0}
    cal_min = conn.execute("SELECT MIN(calendar_date) FROM trading_calendar").fetchone()[0]
    full_days: dict[str, set[str]] = {}
    for mkt, d in conn.execute(
            "SELECT market, price_date FROM daily_prices WHERE is_current=1 GROUP BY market, price_date HAVING COUNT(*)>=?",
            (FULL_MARKET_MIN_ROWS,)):
        full_days.setdefault(mkt, set()).add(d)
    rows = conn.execute("SELECT market, symbol, delist_date, last_trade_date, last_trade_basis FROM delistings").fetchall()
    for r in rows:
        mkt, sym, dd = r[0], r[1], r[2]
        last = conn.execute(
            """SELECT MAX(price_date) FROM daily_prices WHERE symbol=? AND market=? AND is_current=1 AND price_date<=?
               AND (COALESCE(volume_shares,0)>0 OR COALESCE(transactions,0)>0)""", (sym, mkt, dd)).fetchone()[0]
        val = basis = None
        if last and cal_min and cal_min <= last:
            between = [x[0] for x in conn.execute(
                "SELECT calendar_date FROM trading_calendar WHERE is_trade_day=1 AND calendar_date>? AND calendar_date<=?", (last, dd))]
            if all(d in full_days.get(mkt, ()) for d in between):
                val, basis = last, LAST_TRADE_BASIS
        if (val, basis) != (r[3], r[4]):
            conn.execute("UPDATE delistings SET last_trade_date=?, last_trade_basis=? WHERE market=? AND symbol=? AND delist_date=?",
                         (val, basis, mkt, sym, dd))
            out["changed"] += 1
        out["filled" if val else "blank"] += 1
    conn.commit()
    return out


def sync_securities_history(conn: sqlite3.Connection) -> dict:
    """把已下市代號寫進 securities_history（symbol 已不在現行 securities 才寫；代號被沿用／轉板者以現行清單為準，不寫）。
    valid_to＝終止日（官方）、valid_from＝NULL（官方下市清單沒有上市日；不推估）、valid_basis='delistings'。
    只收一般股票代號（4 位數字、首位 1–9；排除 0 開頭的 ETF）。冪等：同代號同 valid_to 的列就地更新；不動既有封存列。"""
    out = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped_current": 0}
    now = now_iso()
    for mkt, sym, name, dd, src, fetched in conn.execute(
            """SELECT market, symbol, name, delist_date, source, fetched_at FROM delistings
               WHERE symbol GLOB '[1-9][0-9][0-9][0-9]' ORDER BY delist_date""").fetchall():
        if conn.execute("SELECT 1 FROM securities WHERE symbol=? AND is_current=1", (sym,)).fetchone():
            out["skipped_current"] += 1
            continue
        old = conn.execute(
            "SELECT version, market, name, source FROM securities_history WHERE symbol=? AND valid_basis=? AND valid_to=?",
            (sym, HISTORY_BASIS, dd)).fetchone()
        if old:
            if (old[1], old[2], old[3]) == (mkt, name or sym, src):
                out["unchanged"] += 1
                continue
            conn.execute("UPDATE securities_history SET market=?, name=?, source=?, fetched_at=? WHERE symbol=? AND version=?",
                         (mkt, name or sym, src, fetched, sym, old[0]))
            out["updated"] += 1
            continue
        ver = conn.execute("SELECT COALESCE(MAX(version),?)+1 FROM securities_history WHERE symbol=? AND version>=?", (HISTORY_VERSION_BASE - 1, sym, HISTORY_VERSION_BASE)).fetchone()[0]
        conn.execute(
            """INSERT INTO securities_history(symbol,version,market,name,cfi_code,is_common,data_date,source,announced_at_note,fetched_at,
                                             unit,archived_at,valid_from,valid_to,valid_basis)
               VALUES(?,?,?,?,?,1,?,?,?,?,?,?,NULL,?,?)""",
            (sym, ver, mkt, name or sym, "n/a", str(fetched)[:10], src, "來源無公告時間（終止日見 valid_to）", fetched, "n/a", now, dd, HISTORY_BASIS))
        out["inserted"] += 1
    conn.commit()
    return out
