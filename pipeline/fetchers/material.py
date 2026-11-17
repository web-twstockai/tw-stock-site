"""重大訊息：mopsov ajax_t05st01（近 30 天，按月查）。"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import timedelta

from bs4 import BeautifulSoup

from pipeline import config
from pipeline.dates import now_iso, roc_ymd_to_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import FetchError, mops, warm_mops

log = logging.getLogger("pipeline.material")


def _months_for_window(days: int) -> list[tuple[int, int]]:
    end = today()
    start = end - timedelta(days=days)
    cur = date_ym(start)
    last = date_ym(end)
    out = []
    y, m = cur
    while (y, m) <= last:
        out.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def date_ym(d):
    return d.year, d.month


def fetch_symbol_month(conn: sqlite3.Connection, symbol: str, market: str, year: int, month: int) -> int:
    typek = "sii" if market == "TWSE" else "otc"
    roc_y = year - 1911
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t05st01"
    data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": typek,
        "co_id": symbol,
        "year": str(roc_y),
        "month": f"{month:02d}",
        "b_date": "",
        "e_date": "",
    }
    # Referer + session cookie；307 時 httputil 會重試
    resp = mops.request(
        "POST",
        url,
        data=data,
        headers={"Referer": "https://mopsov.twse.com.tw/mops/web/t05st01"},
    )
    soup = BeautifulSoup(resp.content.decode("utf-8", errors="replace"), "lxml")
    n = 0
    cutoff = (today() - timedelta(days=config.MATERIAL_DAYS)).isoformat()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        # 公司代號,名稱,發言日期,發言時間,主旨
        if not re.search(r"\d{4}", cells[0].replace("\xa0", "").strip()):
            continue
        sym = re.sub(r"\D", "", cells[0])[:4] or symbol
        ad = roc_ymd_to_iso(cells[2])
        if not ad or ad < cutoff:
            continue
        title = cells[4]
        atime = cells[3]
        h = hashlib.sha256(f"{sym}|{ad}|{atime}|{title}".encode()).hexdigest()[:40]
        announced = None
        note = "來源無完整時區；已存發言日期／時間"
        if ad and atime:
            # 115/11/03 + 15:55:14
            announced = f"{ad}T{atime}+08:00" if re.match(r"\d{1,2}:\d{2}", atime) else None
        rec = {
            "symbol": sym,
            "market": market,
            "announce_date": ad,
            "announce_time": atime,
            "announced_at": announced,
            "announced_at_note": note,
            "title": title,
            "clause": None,
            "seq_no": None,
            "detail_text": None,
            "url": None,  # 列表頁無個別公告 URL；詳文頁未抓取時為 NULL
            "content_hash": h,
            "source": f"{url}?co_id={symbol}&year={roc_y}&month={month}",
            "fetched_at": now_iso(),
            "unit": "n/a",
            "source_unit": None,
            "unit_conversion": None,
        }
        upsert_versioned(
            conn,
            "material_infos",
            {"symbol": sym, "announce_date": ad, "announce_time": atime, "content_hash": h},
            rec,
            ["title"],
        )
        n += 1
    touch_source(conn, "mops_material", url=url, ok=True, http_status=200)
    conn.commit()
    return n


def fetch_all(conn: sqlite3.Connection, symbols: list[str], *, days: int | None = None) -> dict:
    mkt = {r[0]: r[1] for r in conn.execute("SELECT symbol, market FROM securities")}
    for s in config.SAMPLE30_TWSE:
        mkt.setdefault(s, "TWSE")
    for s in config.SAMPLE30_TPEX:
        mkt.setdefault(s, "TPEx")
    window_days = days if days is not None else config.MATERIAL_DAYS
    months = _months_for_window(window_days)
    stats = {"rows": 0, "failed": [], "ok_symbols": [], "empty_symbols": []}
    warm_mops()
    for sym in symbols:
        before = stats["rows"]
        sym_fail = False
        for y, m in months:
            try:
                stats["rows"] += fetch_symbol_month(conn, sym, mkt.get(sym, "TWSE"), y, m)
            except FetchError as e:
                stats["failed"].append(f"{sym}:{y}-{m}:{e}")
                log.error("material fail %s %s", sym, e)
                sym_fail = True
                # 307 後重新暖機再繼續下一月
                warm_mops()
        if not sym_fail:
            # 即使 0 列（近 30 天無公告）也算來源成功可查
            stats["ok_symbols"].append(sym)
            if stats["rows"] == before:
                stats["empty_symbols"].append(sym)
        touch_source(
            conn,
            f"mops_material_{sym}",
            url="https://mopsov.twse.com.tw/mops/web/ajax_t05st01",
            ok=not sym_fail,
            http_status=None if sym_fail else 200,
            error=";".join([x for x in stats["failed"] if x.startswith(sym + ":")][:3]) or None,
            notes="empty_30d" if (not sym_fail and stats["rows"] == before) else None,
        )
    return stats
