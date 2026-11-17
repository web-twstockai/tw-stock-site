"""全期間重算 daily_prices 停牌／無成交，並依官方表補回無比價符號。

不續跑全市場行情回補；不改檢查點。
用法：.venv/bin/python -m pipeline.reclassify_prices
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.dates import ad_slash, ad_yyyymmdd, now_iso
from pipeline.db import connect, init_db
from pipeline.fetchers.market_prices import (
    DAILY_QUOTES_URL,
    MI_INDEX_URL,
    _payload_from_old,
    _upsert_price,
    parse_daily_quotes,
    parse_mi_index,
)
from pipeline.fetchers.price_fields import load_halt_coverage, trade_flags
from pipeline.httputil import FetchError, tpex, twse

log = logging.getLogger("pipeline.reclassify_prices")
TZ = ZoneInfo("Asia/Taipei")


def _row_stats(conn: sqlite3.Connection) -> dict:
    by_mkt = {}
    for m, hr, hs, nr, ns, cr, ntr, nts, xr, xs in conn.execute(
        """
        SELECT market,
          SUM(CASE WHEN is_halted=1 THEN 1 ELSE 0 END),
          COUNT(DISTINCT CASE WHEN is_halted=1 THEN symbol END),
          SUM(CASE WHEN close_twd IS NULL THEN 1 ELSE 0 END),
          COUNT(DISTINCT CASE WHEN close_twd IS NULL THEN symbol END),
          COUNT(*),
          SUM(CASE WHEN is_no_trade=1 THEN 1 ELSE 0 END),
          COUNT(DISTINCT CASE WHEN is_no_trade=1 THEN symbol END),
          SUM(CASE WHEN change_note='無比價' THEN 1 ELSE 0 END),
          COUNT(DISTINCT CASE WHEN change_note='無比價' THEN symbol END)
        FROM daily_prices WHERE is_current=1 GROUP BY market
        """
    ):
        by_mkt[m] = {
            "halted_rows": hr, "halted_syms": hs,
            "close_null_rows": nr, "close_null_syms": ns,
            "current_rows": cr,
            "no_trade_rows": ntr, "no_trade_syms": nts,
            "no_ref_rows": xr, "no_ref_syms": xs,
        }
    return by_mkt


def _dates_for_refetch(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """需重抓官方表的日期：全市場已入庫日＋樣本股仍缺符號（change 為 NULL）的日子。"""
    mi = {r[0] for r in conn.execute(
        "SELECT DISTINCT price_date FROM daily_prices WHERE is_current=1 AND source_table='MI_INDEX'"
    )}
    dq = {r[0] for r in conn.execute(
        "SELECT DISTINCT price_date FROM daily_prices WHERE is_current=1 AND source_table='dailyQuotes'"
    )}
    twse_null = {r[0] for r in conn.execute(
        """SELECT DISTINCT price_date FROM daily_prices
           WHERE is_current=1 AND market='TWSE' AND close_twd IS NOT NULL AND change_twd IS NULL"""
    )}
    tpex_null = {r[0] for r in conn.execute(
        """SELECT DISTINCT price_date FROM daily_prices
           WHERE is_current=1 AND market='TPEx' AND close_twd IS NOT NULL AND change_twd IS NULL"""
    )}
    twse_dates = sorted(mi | twse_null)
    tpex_dates = sorted(dq | tpex_null)
    return twse_dates, tpex_dates


def reclassify_halt_no_trade(conn: sqlite3.Connection) -> Counter:
    """不抓網：依 close 與 official trading_halts 重算全列。"""
    exact, intervals = load_halt_coverage(conn)
    counts: Counter = Counter()
    # 正常成交列就地補 is_no_trade=0，不升版，避免全表被 content_keys 變動誤升版
    cur = conn.execute(
        """UPDATE daily_prices SET is_no_trade=0
           WHERE is_current=1 AND close_twd IS NOT NULL
             AND COALESCE(is_halted,0)=0 AND is_no_trade IS NULL"""
    )
    counts["in_place_default"] = cur.rowcount
    conn.commit()
    rows = conn.execute(
        """SELECT * FROM daily_prices
           WHERE is_current=1 AND (close_twd IS NULL OR COALESCE(is_halted,0)=1)"""
    ).fetchall()
    for i, old in enumerate(rows, 1):
        close = old["close_twd"]
        no_trade, halted = trade_flags(close, old["symbol"], old["price_date"], exact, intervals)
        old_halt = int(old["is_halted"] or 0)
        old_nt = old["is_no_trade"]
        if old_halt == halted and int(old_nt or 0) == no_trade:
            counts["unchanged"] += 1
            continue
        payload = _payload_from_old(old, {
            "is_halted": halted,
            "is_no_trade": no_trade,
            "fetched_at": now_iso(),
        })
        st = _upsert_price(conn, payload)
        counts[st] += 1
        if i % 500 == 0:
            conn.commit()
            log.info("halt/no-trade progress %s/%s %s", i, len(rows), dict(counts))
    conn.commit()
    return counts


def _apply_official_rows(conn: sqlite3.Connection, official: list[dict]) -> Counter:
    counts: Counter = Counter()
    for row in official:
        old = conn.execute(
            "SELECT * FROM daily_prices WHERE symbol=? AND price_date=? AND is_current=1",
            (row["symbol"], row["price_date"]),
        ).fetchone()
        if old is None:
            counts["skipped_absent"] += 1
            continue
        updates = {
            "change": row.get("change"),
            "change_twd": row.get("change_twd"),
            "change_sign": row.get("change_sign"),
            "change_note": row.get("change_note"),
            "is_halted": row.get("is_halted"),
            "is_no_trade": row.get("is_no_trade"),
            "next_ref_price_twd": row.get("next_ref_price_twd"),
        }
        # 若只有次日參考價是新填的，就地寫入不升版
        class_keys = ["change", "change_twd", "change_sign", "change_note", "is_halted", "is_no_trade"]
        class_same = True
        for k in class_keys:
            ov = old[k] if k in old.keys() else None
            nv = updates[k]
            if k in ("is_halted", "is_no_trade"):
                if int(ov or 0) != int(nv or 0):
                    class_same = False
                    break
            elif ov != nv:
                class_same = False
                break
        old_ref = old["next_ref_price_twd"] if "next_ref_price_twd" in old.keys() else None
        new_ref = updates["next_ref_price_twd"]
        if class_same and old_ref == new_ref:
            counts["unchanged"] += 1
            continue
        if class_same:
            from pipeline.db import checksum
            from pipeline.fetchers.prices import CONTENT_KEYS
            payload = _payload_from_old(old, {"next_ref_price_twd": new_ref})
            ck = checksum(payload, CONTENT_KEYS)
            conn.execute(
                "UPDATE daily_prices SET next_ref_price_twd=?, checksum=? WHERE symbol=? AND price_date=? AND is_current=1",
                (new_ref, ck, old["symbol"], old["price_date"]),
            )
            counts["in_place_next_ref"] += 1
            continue
        payload = _payload_from_old(old, updates)
        # 不改來源／量額／OHLC
        st = _upsert_price(conn, payload)
        counts[st] += 1
    return counts


def refetch_change_signs(conn: sqlite3.Connection) -> dict:
    universe = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol, market FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1"
    )}
    exact, intervals = load_halt_coverage(conn)
    twse_dates, tpex_dates = _dates_for_refetch(conn)
    log.info("refetch dates twse=%s tpex=%s", len(twse_dates), len(tpex_dates))
    http_failed: list[str] = []
    tot = Counter()
    from datetime import date as date_cls

    def _dt(iso: str):
        y, m, d = map(int, iso.split("-"))
        return date_cls(y, m, d)

    for iso in twse_dates:
        url = MI_INDEX_URL.format(yyyymmdd=ad_yyyymmdd(_dt(iso)))
        try:
            resp = twse.request("GET", url)
            data = resp.json()
            if data.get("stat") != "OK":
                http_failed.append(f"TWSE:{iso}:{data.get('stat')}")
                log.warning("MI_INDEX %s %s", iso, data.get("stat"))
                continue
            rows = parse_mi_index(data, universe, iso, url, exact, intervals)
            c = _apply_official_rows(conn, rows)
            tot.update({f"twse_{k}": v for k, v in c.items()})
            conn.commit()
            log.info("MI_INDEX %s %s", iso, dict(c))
        except FetchError as e:
            http_failed.append(f"TWSE:{iso}:{e}")
            log.error("MI_INDEX %s %s", iso, e)

    for iso in tpex_dates:
        url = DAILY_QUOTES_URL.format(slash=ad_slash(_dt(iso)))
        try:
            resp = tpex.request("GET", url)
            if resp.status_code != 200:
                raise FetchError(f"http={resp.status_code}", status=resp.status_code)
            data = resp.json()
            stat = (data.get("stat") or "").lower()
            if stat not in ("ok", ""):
                http_failed.append(f"TPEx:{iso}:{data.get('stat')}")
                continue
            rows = parse_daily_quotes(data, universe, iso, url, exact, intervals)
            c = _apply_official_rows(conn, rows)
            tot.update({f"tpex_{k}": v for k, v in c.items()})
            conn.commit()
            log.info("dailyQuotes %s %s", iso, dict(c))
        except FetchError as e:
            http_failed.append(f"TPEx:{iso}:{e}")
            log.error("dailyQuotes %s %s", iso, e)

    return {"counts": dict(tot), "failed": http_failed, "twse_dates": twse_dates, "tpex_dates": tpex_dates}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    conn = connect()
    init_db(conn)
    before = _row_stats(conn)
    log.info("before %s", before)
    halt_counts = reclassify_halt_no_trade(conn)
    mid = _row_stats(conn)
    log.info("after halt/no-trade %s counts=%s", mid, dict(halt_counts))
    sign_st = refetch_change_signs(conn)
    after = _row_stats(conn)
    examples = [dict(r) for r in conn.execute(
        """SELECT symbol,market,price_date,close_twd,change_twd,change_sign,change_note,is_halted,is_no_trade,version,source_table
           FROM daily_prices WHERE is_current=1 AND symbol IN ('1538','5906') AND price_date='2026-11-09'"""
    )]
    report = {
        "finished_at": datetime.now(TZ).isoformat(),
        "before": before,
        "after_halt_no_trade": mid,
        "after": after,
        "halt_upsert": dict(halt_counts),
        "sign_refetch": sign_st["counts"],
        "http_failed": sign_st["failed"],
        "twse_dates": sign_st["twse_dates"],
        "tpex_dates": sign_st["tpex_dates"],
        "examples_1538_5906": examples,
    }
    out = Path("logs/reclassify_prices.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s after=%s failed=%s", out, after, sign_st["failed"])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not sign_st["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
