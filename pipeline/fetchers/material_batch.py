"""全市場重大訊息：openapi 當日／近期快照（非 30 天完整史；避免逐檔 t05st01）。"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

from pipeline.coverage import record_summary
from pipeline.dates import now_iso, roc_ymd_to_iso
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.material_batch")

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"


def _time_norm(raw: str | None) -> str:
    s = re.sub(r"\D", "", raw or "")
    if len(s) >= 6:
        return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"
    if len(s) == 5:
        return f"0{s[0]}:{s[1:3]}:{s[3:5]}"
    if len(s) == 4:
        return f"{s[0:2]}:{s[2:4]}:00"
    return (raw or "").strip()


def _title(rec: dict) -> str:
    for k in rec:
        if "主旨" in k:
            return str(rec[k] or "").strip()
    return ""


def _ingest(conn, recs: list[dict], market: str, universe: set[str], source: str) -> dict:
    n = 0
    skipped = 0
    dates: set[str] = set()
    now = now_iso()
    for rec in recs:
        sym = str(rec.get("公司代號") or rec.get("SecuritiesCompanyCode") or rec.get("Code") or "").strip()
        if not sym or (universe and sym not in universe):
            skipped += 1
            continue
        ad = roc_ymd_to_iso(str(rec.get("發言日期") or rec.get("Date") or ""))
        atime = _time_norm(str(rec.get("發言時間") or rec.get("Time") or ""))
        title = _title(rec)
        if not ad:
            skipped += 1
            continue
        dates.add(ad)
        h = hashlib.sha256(f"{sym}|{ad}|{atime}|{title}".encode()).hexdigest()[:40]
        announced = f"{ad}T{atime}+08:00" if re.match(r"\d{1,2}:\d{2}", atime) else None
        row = {
            "symbol": sym,
            "market": market,
            "announce_date": ad,
            "announce_time": atime,
            "announced_at": announced,
            "announced_at_note": "來源快照無完整時區；已存發言日期／時間",
            "title": title,
            "clause": rec.get("符合條款") or rec.get("Clause"),
            "seq_no": None,
            "detail_text": rec.get("說明") or rec.get("Description"),
            "url": None,
            "content_hash": h,
            "source": source,
            "fetched_at": now,
            "unit": "n/a",
            "source_unit": None,
            "unit_conversion": None,
        }
        upsert_versioned(
            conn,
            "material_infos",
            {"symbol": sym, "announce_date": ad, "announce_time": atime, "content_hash": h},
            row,
            ["title"],
        )
        n += 1
        if n % 200 == 0:
            conn.commit()
    conn.commit()
    return {"rows": n, "skipped": skipped, "dates": sorted(dates)}


def fetch_snapshots(conn: sqlite3.Connection, symbols: list[str] | None = None, *, run_id: int | None = None) -> dict:
    universe = set(symbols or [])
    if not universe:
        universe = {
            r[0]
            for r in conn.execute(
                "SELECT symbol FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1"
            )
        }
    stats = {"rows": 0, "failed": [], "markets": {}, "note": "snapshot_only_not_30d_history"}
    for market, url, client in (
        ("TWSE", TWSE_URL, twse),
        ("TPEx", TPEX_URL, tpex),
    ):
        try:
            resp = client.request("GET", url)
            data = resp.json()
            if not isinstance(data, list):
                raise FetchError(f"unexpected body type={type(data)}", status=resp.status_code)
            st = _ingest(conn, data, market, universe, url)
            stats["markets"][market] = {"http": resp.status_code, "page": len(data), **st}
            stats["rows"] += st["rows"]
            touch_source(conn, f"mops_material_snapshot_{market}", url=url, ok=True, http_status=200, data_date=(st["dates"][-1] if st["dates"] else None))
            record_summary(
                conn, run_id=run_id, universe="all", data_kind="material", market=market,
                expected=len(data), success=st["rows"], failed=0, excluded=st["skipped"],
                date_start=st["dates"][0] if st["dates"] else None,
                date_end=st["dates"][-1] if st["dates"] else None,
                notes="openapi snapshot only; not a 30-day complete history; per-symbol t05st01 skipped for universe=all",
            )
        except FetchError as e:
            stats["failed"].append(f"{market}:{e}")
            touch_source(conn, f"mops_material_snapshot_{market}", url=url, ok=False, http_status=e.status, error=str(e))
            log.error("material snapshot %s %s", market, e)
    return stats
