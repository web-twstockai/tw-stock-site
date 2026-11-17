"""交易日曆：證交所 holidaySchedule 為主，FMTQIK／櫃買 st41 交叉驗證臨時休市。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from pipeline.dates import now_iso, roc_ymd_to_iso, today
from pipeline.db import touch_source
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.calendar")

TWSE_HOLIDAY_OPENAPI = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TWSE_HOLIDAY_RWD = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json"
FMTQIK_URL = "https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={yyyymmdd}"
ST41_URL = "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_index/st41_result.php?l=zh-tw&d={roc_ym}&o=json"

# 已知臨時休市公告（補強說明；判定仍以行情交叉驗證為準）
KNOWN_SPECIAL_CLOSURES = {
    "2026-07-10": {
        "name": "臨時休市（巴威颱風）",
        "description": "臺北市政府宣布公教停止上班；證交所／櫃買全日休市。公告：https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69ef76943019f46cb86ae0110.pdf",
        "source": "https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69ef76943019f46cb86ae0110.pdf",
    },
    "2026-10-23": {
        "name": "臨時休市（薔琵颱風）",
        "description": "臺北市政府宣布公教停止上班；證交所／櫃買全日休市。公告：https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d6a0fa351b01a1638507940202.pdf",
        "source": "https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d6a0fa351b01a1638507940202.pdf",
    },
}


def _parse_holiday_rows(data) -> dict[str, tuple[str, str, bool]]:
    """回傳 iso → (name, desc, is_trade)。年曆列預設非交易；僅「開始／最後交易」等例外為交易。"""
    rows = data
    if isinstance(data, dict):
        rows = data.get("data") or data.get("result") or []
    holiday_map: dict[str, tuple[str, str, bool]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row.get("Date") or row.get("date") or ""
        iso = roc_ymd_to_iso(str(raw))
        if not iso:
            continue
        name = row.get("Name") or row.get("name") or ""
        desc = row.get("Description") or row.get("description") or ""
        is_trade = False
        if "開始交易" in name or "最後交易" in name:
            is_trade = True
        if "無交易" in name or "放假" in name or "休息" in desc or "無交易" in desc:
            is_trade = False
        if "僅辦理結算" in name or "僅辦理結算" in desc:
            is_trade = False
        holiday_map[iso] = (name, desc, is_trade)
    return holiday_map


def _fetch_holiday_map() -> tuple[dict[str, tuple[str, str, bool]], str]:
    """優先 openapi；失敗則 rwd。"""
    last_err: Exception | None = None
    for url in (TWSE_HOLIDAY_OPENAPI, TWSE_HOLIDAY_RWD):
        try:
            resp = twse.request("GET", url)
            data = resp.json()
            m = _parse_holiday_rows(data)
            if m:
                return m, url
        except Exception as e:  # noqa: BLE001 — 嘗試備援來源
            last_err = e
            log.warning("holidaySchedule fail %s: %s", url, e)
    raise FetchError(f"holidaySchedule unavailable: {last_err}")


def _fmtqik_dates(year: int) -> set[str]:
    """證交所 FMTQIK 月表實際有列的日期＝上市集中市場有行情日。"""
    out: set[str] = set()
    end = today()
    for month in range(1, 13):
        d0 = date(year, month, 1)
        if d0 > end:
            break
        url = FMTQIK_URL.format(yyyymmdd=f"{year}{month:02d}01")
        try:
            resp = twse.request("GET", url)
            data = resp.json()
            if data.get("stat") not in (None, "OK", "ok"):
                # 無資料月份
                continue
            for row in data.get("data") or []:
                iso = roc_ymd_to_iso(str(row[0]))
                if iso:
                    out.add(iso)
        except FetchError as e:
            log.warning("FMTQIK %s: %s", url, e)
    return out


def _st41_dates(year: int) -> set[str]:
    """櫃買 st41 月表實際有列的日期＝櫃買有行情日。"""
    out: set[str] = set()
    end = today()
    for month in range(1, 13):
        d0 = date(year, month, 1)
        if d0 > end:
            break
        roc_ym = f"{year - 1911}/{month:02d}"
        url = ST41_URL.format(roc_ym=roc_ym)
        try:
            resp = tpex.request("GET", url)
            data = resp.json()
            tables = data.get("tables") or []
            rows = (tables[0].get("data") if tables else None) or data.get("aaData") or []
            for rec in rows:
                iso = roc_ymd_to_iso(str(rec[0]))
                if iso:
                    out.add(iso)
        except FetchError as e:
            log.warning("st41 %s: %s", url, e)
    return out


def _upsert_calendar_row(
    conn: sqlite3.Connection,
    *,
    iso: str,
    is_trade: bool,
    name: str | None,
    desc: str | None,
    kind: str,
    source: str,
) -> str:
    """回傳 inserted／unchanged／updated；內容變動時 version+1，並更新 source／fetched_at。"""
    now = now_iso()
    old = conn.execute(
        "SELECT is_trade_day, name, description, source_kind, source, version FROM trading_calendar WHERE calendar_date=?",
        (iso,),
    ).fetchone()
    if old is None:
        conn.execute(
            """INSERT INTO trading_calendar(
                 calendar_date,is_trade_day,name,description,source_kind,source,
                 announced_at,announced_at_note,fetched_at,unit,version)
               VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
            (iso, 1 if is_trade else 0, name, desc, kind, source, None, "來源無公告時間", now, "n/a"),
        )
        return "inserted"
    same = (
        int(old[0]) == (1 if is_trade else 0)
        and (old[1] or None) == (name or None)
        and (old[2] or None) == (desc or None)
        and old[3] == kind
    )
    if same:
        conn.execute(
            "UPDATE trading_calendar SET fetched_at=?, source=? WHERE calendar_date=?",
            (now, source, iso),
        )
        return "unchanged"
    new_ver = int(old[5] or 1) + 1
    conn.execute(
        """UPDATE trading_calendar SET
             is_trade_day=?, name=?, description=?, source_kind=?, source=?,
             fetched_at=?, version=?
           WHERE calendar_date=?""",
        (1 if is_trade else 0, name, desc, kind, source, now, new_ver, iso),
    )
    return "updated"


def fetch_and_store(conn: sqlite3.Connection, year: int | None = None) -> int:
    year = year or today().year
    holiday_map, holiday_url = _fetch_holiday_map()
    touch_source(conn, "twse_holiday", url=holiday_url, ok=True, http_status=200, data_date=f"{year}-01-01")

    twse_bars = _fmtqik_dates(year)
    tpex_bars = _st41_dates(year)
    touch_source(
        conn,
        "twse_fmtqik_calendar_xref",
        url=FMTQIK_URL.format(yyyymmdd=f"{year}0101"),
        ok=True,
        http_status=200,
        data_date=max(twse_bars) if twse_bars else f"{year}-01-01",
        notes=f"fmtqik_days={len(twse_bars)}",
    )
    touch_source(
        conn,
        "tpex_st41_calendar_xref",
        url=ST41_URL.format(roc_ym=f"{year - 1911}/01"),
        ok=True,
        http_status=200,
        data_date=max(tpex_bars) if tpex_bars else f"{year}-01-01",
        notes=f"st41_days={len(tpex_bars)}",
    )

    as_of = today()
    n = 0
    updated = 0
    anomalies: list[dict] = []
    d = date(year, 1, 1)
    d1 = date(year, 12, 31)
    while d <= d1:
        iso = d.isoformat()
        # 未來日期：僅年曆＋週末，不做行情交叉（尚無 FMTQIK）
        future = d >= as_of  # 含今日：FMTQIK／st41 當日可能尚未入月表

        if iso in holiday_map:
            name, desc, is_trade = holiday_map[iso]
            kind = "twse_holiday"
            source = holiday_url
        elif d.weekday() >= 5:
            name, desc, is_trade = "週末", "", False
            kind = "weekend_rule"
            source = holiday_url
        else:
            name, desc, is_trade = None, None, True
            kind = "weekday_default"
            source = holiday_url

        if not future:
            has_twse = iso in twse_bars
            has_tpex = iso in tpex_bars
            has_any = has_twse or has_tpex
            has_both_missing = (not has_twse) and (not has_tpex)

            if is_trade and has_both_missing:
                # 年曆／平日預設為交易日，但兩市指數月表皆無列 → 臨時休市（颱風等）
                special = KNOWN_SPECIAL_CLOSURES.get(iso, {})
                name = special.get("name") or "臨時休市（無行情交叉驗證）"
                desc = special.get("description") or (
                    f"weekday_default/holiday 標交易日，但 FMTQIK 與 st41 皆無 {iso}；"
                    f"twse_bar={has_twse} tpex_bar={has_tpex}"
                )
                source = special.get("source") or (
                    f"xref:FMTQIK+st41; holiday={holiday_url}"
                )
                kind = "market_closed_xref"
                is_trade = False
                anomalies.append(
                    {
                        "date": iso,
                        "type": "marked_trade_no_bar",
                        "twse_bar": has_twse,
                        "tpex_bar": has_tpex,
                        "action": "set_non_trade",
                    }
                )
            elif (not is_trade) and has_any and kind != "weekend_rule":
                # 標休市但有行情（排除週末誤標）
                anomalies.append(
                    {
                        "date": iso,
                        "type": "marked_closed_has_bar",
                        "twse_bar": has_twse,
                        "tpex_bar": has_tpex,
                        "action": "set_trade",
                        "prev_kind": kind,
                        "prev_name": name,
                    }
                )
                name = (name or "") + "（行情交叉驗證改為交易日）"
                desc = (desc or "") + f" | FMTQIK={has_twse} st41={has_tpex}"
                kind = "market_open_xref"
                source = f"xref:FMTQIK+st41; holiday={holiday_url}"
                is_trade = True
            elif is_trade and (has_twse != has_tpex):
                anomalies.append(
                    {
                        "date": iso,
                        "type": "market_divergence",
                        "twse_bar": has_twse,
                        "tpex_bar": has_tpex,
                        "action": "keep_trade",
                    }
                )

        st = _upsert_calendar_row(
            conn,
            iso=iso,
            is_trade=is_trade,
            name=name,
            desc=desc,
            kind=kind,
            source=source,
        )
        if st == "updated":
            updated += 1
        n += 1
        d += timedelta(days=1)

    conn.commit()
    log.info(
        "calendar year=%s rows=%s updated=%s anomalies=%s twse_bars=%s tpex_bars=%s",
        year,
        n,
        updated,
        len(anomalies),
        len(twse_bars),
        len(tpex_bars),
    )
    try:
        import json
        from pathlib import Path
        Path("logs").mkdir(exist_ok=True)
        Path(f"logs/calendar_anomalies_{year}.json").write_text(
            json.dumps(
                {
                    "year": year,
                    "fetched_at": now_iso(),
                    "holiday_url": holiday_url,
                    "anomalies": anomalies,
                    "twse_bar_count": len(twse_bars),
                    "tpex_bar_count": len(tpex_bars),
                    "known_special": list(KNOWN_SPECIAL_CLOSURES),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("calendar anomaly log write failed: %s", e)

    if year == today().year:
        fetch_and_store(conn, year - 1)
    return n


def recent_trade_days(conn: sqlite3.Connection, n: int, end: date | None = None) -> list[str]:
    end = end or today()
    rows = conn.execute(
        """SELECT calendar_date FROM trading_calendar
           WHERE is_trade_day=1 AND calendar_date<=?
           ORDER BY calendar_date DESC LIMIT ?""",
        (end.isoformat(), n),
    ).fetchall()
    return sorted(r[0] for r in rows)
