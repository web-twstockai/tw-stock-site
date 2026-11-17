"""ISIN → securities / securities_excluded。"""
from __future__ import annotations

import logging
import re
import sqlite3
from html.parser import HTMLParser

from pipeline import config
from pipeline.dates import now_iso, roc_ymd_to_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import twse

log = logging.getLogger("pipeline.securities")

ISIN_TWSE = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
ISIN_TPEX = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"
COMMON_CFI = {"ESVUFR", "ESVTFR"}

# 雲端略過 isin 時，DB 內證券清單至少要有這麼多檔普通股，否則忽略略過旗標、照抓（2026-11-17 實際：上市 1058、上櫃 898）
MIN_COMMON_TO_SKIP = {"TWSE": 900, "TPEx": 700}


def should_skip_isin(conn: sqlite3.Connection) -> tuple[bool, dict]:
    """PIPELINE_SKIP_ISIN=1 時能否真的略過：DB 內現行普通股清單要達門檻。回傳 (可略過, 各市場檔數)。"""
    counts = {"TWSE": 0, "TPEx": 0}
    try:
        for m, n in conn.execute(
            "SELECT market, COUNT(*) FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1 GROUP BY market"
        ).fetchall():
            counts[m] = n
    except sqlite3.Error:
        pass
    ok = all(counts.get(m, 0) >= th for m, th in MIN_COMMON_TO_SKIP.items())
    return ok, counts


def note_isin_skipped(conn: sqlite3.Connection, reason: str) -> None:
    """略過 isin 時只更新 last_attempt_at 與 notes；ok、last_success_at、last_error 等一律不動（清單其實是舊的）。"""
    now = now_iso()
    for sid in ("isin_twse", "isin_tpex"):
        conn.execute(
            "UPDATE source_status SET last_attempt_at=?, notes=? WHERE source_id=?",
            (now, f"skipped {now[:10]}：{reason}（沿用舊清單，ok／last_success_at 未更新）", sid),
        )
    conn.commit()


def _exclude_reason(cfi: str, market: str, section: str) -> str | None:
    if "創新板" in market or "創新板" in section:
        return "創新板（第一版排除）"
    if cfi in COMMON_CFI and section.strip() in ("股票",):
        return None
    if cfi.startswith("RW"):
        return f"CFI={cfi} 權證／牛熊"
    if cfi.startswith(("EP", "EF")):
        return f"CFI={cfi} 特別股"
    if cfi.startswith("CEO") or cfi.startswith("CM"):
        return f"CFI={cfi} ETF／ETN／主動式"
    if cfi == "EDSDDR":
        return f"CFI={cfi} TDR"
    if cfi.startswith("CB"):
        return f"CFI={cfi} 受益證券"
    if cfi in COMMON_CFI and section.strip() != "股票":
        return f"區段={section}（非股票區）"
    return f"CFI={cfi} 非普通股"


def _parse_isin_html(raw: bytes, market_label: str) -> tuple[str | None, list[dict]]:
    text = raw.decode("cp950", errors="replace")
    m = re.search(r"最近更新日期:([0-9/]+)", text)
    data_date = roc_ymd_to_iso(m.group(1)) if m else today().isoformat()
    section = ""
    rows: list[dict] = []
    # section headers
    for sec in re.finditer(r"<td[^>]*colspan=7[^>]*><B>\s*([^<]+?)\s*<B>", text):
        pass
    # walk with simple regex rows + track last section by splitting
    parts = re.split(r"(<td[^>]*colspan=7[^>]*><B>\s*[^<]+?\s*<B>\s*</td>)", text)
    current_section = ""
    for part in parts:
        sm = re.search(r"<B>\s*([^<]+?)\s*<B>", part)
        if sm and "colspan" in part:
            current_section = sm.group(1).strip()
            continue
        for r in re.findall(
            r"<tr><td[^>]*>([^<]+)</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td>"
            r"<td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td><td[^>]*>([^<]*)</td></tr>",
            part,
        ):
            code_name, isin, listed, mkt, ind, cfi, note = r
            parts_cn = code_name.replace("\u3000", " ").split(None, 1)
            code = parts_cn[0].strip()
            name = parts_cn[1].strip() if len(parts_cn) > 1 else ""
            rows.append(
                {
                    "symbol": code,
                    "name": name,
                    "isin": isin.strip(),
                    "listed_date": roc_ymd_to_iso(listed.strip()),
                    "market_raw": mkt.strip(),
                    "industry": ind.strip() or None,
                    "cfi_code": cfi.strip(),
                    "section": current_section,
                    "note": note.strip(),
                }
            )
    return data_date, rows


def fetch_and_store(conn: sqlite3.Connection) -> dict:
    stats = {"twse_common": 0, "tpex_common": 0, "excluded": 0}
    for url, market in ((ISIN_TWSE, "TWSE"), (ISIN_TPEX, "TPEx")):
        # ISIN 檔很大（上市約 10 MB），慢速連線可能要 10 分鐘以上：總時間上限放寬到 900 秒（其他請求維持預設 150 秒）
        resp = twse.request("GET", url, deadline=900, timeout=(20, 120), retries=2)
        data_date, rows = _parse_isin_html(resp.content, market)
        touch_source(conn, f"isin_{market.lower()}", url=url, ok=True, http_status=resp.status_code, data_date=data_date)
        for row in rows:
            cfi = row["cfi_code"]
            reason = _exclude_reason(cfi, row["market_raw"], row["section"])
            if reason:
                conn.execute(
                    """INSERT INTO securities_excluded(symbol,market,name,cfi_code,section,exclude_reason,data_date,source,fetched_at,version)
                       VALUES(?,?,?,?,?,?,?,?,?,1)
                       ON CONFLICT(symbol,market,data_date,exclude_reason) DO UPDATE SET
                         name=excluded.name, cfi_code=excluded.cfi_code, section=excluded.section, fetched_at=excluded.fetched_at
                    """,
                    (
                        row["symbol"],
                        market,
                        row["name"],
                        cfi,
                        row["section"],
                        reason,
                        data_date,
                        url,
                        now_iso(),
                    ),
                )
                stats["excluded"] += 1
                continue
            rec = {
                "symbol": row["symbol"],
                "market": market,
                "name": row["name"],
                "name_full": None,
                "isin": row["isin"],
                "cfi_code": cfi,
                "industry": row["industry"],
                "industry_code": None,
                "listed_date": row["listed_date"],
                "is_common": 1,
                "exclude_reason": None,
                "data_date": data_date,
                "source": url,
                "announced_at": None,
                "announced_at_note": "來源無公告時間",
                "fetched_at": now_iso(),
                "unit": "n/a",
                "source_unit": None,
                "unit_conversion": None,
            }
            upsert_versioned(
                conn,
                "securities",
                {"symbol": row["symbol"]},
                rec,
                ["name", "isin", "cfi_code", "industry", "listed_date", "market", "data_date"],
                history_table="securities_history",
            )
            if market == "TWSE":
                stats["twse_common"] += 1
            else:
                stats["tpex_common"] += 1
        conn.execute(
            "UPDATE source_status SET notes=NULL WHERE source_id=? AND notes LIKE 'skipped%'", (f"isin_{market.lower()}",)
        )   # 這次真的抓成功了，清掉先前「略過」註記
        conn.commit()
        log.info("%s common=%s excluded_rows≈%s data_date=%s", market, stats, stats["excluded"], data_date)
    return stats
