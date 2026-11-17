"""除權息／減資（含 TWTAUU 恢復買賣日）與市場級 coverage。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from urllib.parse import urlencode

from pipeline.dates import ad_yyyymmdd, now_iso, parse_number, roc_ymd_to_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import tpex, twse
from pipeline.scope import scope_info

log = logging.getLogger("pipeline.corp")

# 除權息／減資／面額變更共用內容鍵（含 MOPS 補強欄；不含 fetched_at／source URL）
CORP_CONTENT_KEYS = [
    "prev_close_twd", "reference_twd", "rights_value_twd", "dividend_value_twd",
    "rights_dividend_sum_twd", "ex_type_raw", "cash_dividend_twd",
    "stock_dividend_per_thousand", "capital_reduction_ratio", "refund_per_share_twd",
    "cash_subscription_ratio", "share_exchange_ratio",
    "limit_up_twd", "limit_down_twd", "auction_base_twd",
]


def _action_type(raw: str | None) -> str:
    if not raw:
        return "unknown"
    if "權" in raw and "息" in raw:
        return "ex_both"
    if "息" in raw:
        return "ex_dividend"
    if "權" in raw:
        return "ex_rights"
    return "unknown"



def _mirror_stock_split_coverage(conn, market: str, start, end, source: str) -> None:
    """DECISIONS 2026-11-10 03:45：stock_split 視為由 par_value_change 覆蓋。"""
    _write_coverage(
        conn,
        market,
        "stock_split",
        start.isoformat() if hasattr(start, "isoformat") else str(start),
        end.isoformat() if hasattr(end, "isoformat") else str(end),
        source,
        f"covered_by=par_value_change; source_url={source}; DECISIONS.md 2026-11-10 03:45",
    )

# 目前這個區間查詢的範圍（各 fetch_* 開頭設定；單執行緒）。覆蓋表只能宣稱「實際入庫的範圍」：
# 以 30 檔過濾入庫的查詢，覆蓋範圍就是 sample30，不是全市場（2026-11-17 研究分析發現：表內只有 29 檔 38 列，覆蓋表卻寫成全市場）。
_COV: dict = {"symbols": None}


def _set_scope(symbols) -> None:
    """記下本次區間查詢的過濾清單；scope 在 _write_coverage 依市場與現行普通股清單判定（fix1：不再用 >100 檔）。"""
    _COV["symbols"] = set(symbols) if symbols else None


def _write_coverage(conn: sqlite3.Connection, market: str, kind: str, start: str, end: str, source: str, notes: str | None = None) -> None:
    """覆蓋表主鍵含 scope：sample30／subset／all 對同一區間各自一列，不互相覆蓋。
    'all' 與 'all_pending_ratio' 視為同一列的兩種狀態（只更新原列，不會並存）。"""
    scope, nsym = scope_info(conn, _COV["symbols"], market)
    now = now_iso()
    fam = ("all", "all_pending_ratio") if scope == "all" else (scope,)
    cur = conn.execute(
        f"""UPDATE corporate_actions_coverage SET fetched_at=?, notes=?, symbol_count=?
            WHERE market=? AND action_kind=? AND coverage_start=? AND coverage_end=? AND source=? AND scope IN ({','.join('?' * len(fam))})""",
        (now, notes, nsym, market, kind, start, end, source, *fam),
    )
    if cur.rowcount == 0:
        conn.execute(
            """INSERT INTO corporate_actions_coverage(market,action_kind,coverage_start,coverage_end,source,fetched_at,notes,scope,symbol_count)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (market, kind, start, end, source, now, notes, scope, nsym),
        )
    conn.commit()


def pending_ratio_rows(conn: sqlite3.Connection) -> int:
    """上市 權／權息 列中，配股比例仍是 NULL 且尚未用 MOPS 查過（沒有 corp_enrich 紀錄）的列數。"""
    return conn.execute(
        """SELECT COUNT(*) FROM corporate_actions a
           WHERE a.is_current=1 AND a.market='TWSE' AND a.action_type IN ('ex_both','ex_rights')
             AND a.stock_dividend_per_thousand IS NULL
             AND NOT EXISTS (SELECT 1 FROM backfill_state b WHERE b.job='corp_enrich' AND b.key=a.symbol AND b.status='done')"""
    ).fetchone()[0]


def refresh_enrich_scope(conn: sqlite3.Connection) -> dict:
    """上市「配股」與「權息」兩類覆蓋：配股比例還有未補的列 → scope 降為 'all_pending_ratio'（分析不得當全市場已齊）；補完 → 回 'all'。
    只調整原本是 'all' 或 'all_pending_ratio' 的列。"""
    n = pending_ratio_rows(conn)
    want = "all" if n == 0 else "all_pending_ratio"
    note = f"；配股比例待補 {n} 列（MOPS t05st09_2）" if n else ""
    cur = conn.execute(
        "UPDATE corporate_actions_coverage SET scope=? WHERE market='TWSE' AND action_kind IN ('stock_dividend','ex_both') AND scope IN ('all','all_pending_ratio')",
        (want,),
    )
    if note:
        conn.execute(
            "UPDATE corporate_actions_coverage SET notes=COALESCE(notes,'') || ? WHERE market='TWSE' AND action_kind IN ('stock_dividend','ex_both') AND scope='all_pending_ratio' AND COALESCE(notes,'') NOT LIKE '%配股比例待補%'",
            (note,))
    conn.commit()
    return {"pending_ratio_rows": n, "scope": want, "coverage_rows_updated": cur.rowcount}


def fetch_twse_exrights(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    _set_scope(symbols)
    url = (
        "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?"
        + urlencode({"response": "json", "startDate": ad_yyyymmdd(start), "endDate": ad_yyyymmdd(end), "stockNo": ""})
    )
    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_twt49u", url=url, ok=False, error=str(data.get("stat")))
        return 0
    n = 0
    for rec in data.get("data") or []:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(rec[0])
        if not ad:
            continue
        raw = rec[6]
        atype = _action_type(raw)
        # TWT49U 無每仟股配股／現金欄；保留既有 MOPS 等補強值，避免重抓洗掉
        prev = conn.execute(
            "SELECT cash_dividend_twd, stock_dividend_per_thousand, capital_reduction_ratio, "
            "refund_per_share_twd, cash_subscription_ratio, share_exchange_ratio, "
            "source, announced_at_note, unit, source_unit, unit_conversion "
            "FROM corporate_actions WHERE symbol=? AND action_date=? AND action_type=? AND is_current=1",
            (sym, ad, atype),
        ).fetchone()
        stock = prev["stock_dividend_per_thousand"] if prev else None
        enriched = bool(prev and stock is not None)
        row = {
            "symbol": sym,
            "market": "TWSE",
            "action_date": ad,
            "action_type": atype,
            "ex_type_raw": raw,
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": parse_number(rec[5]),
            "cash_dividend_twd": (prev["cash_dividend_twd"] if prev else None),
            "stock_dividend_per_thousand": stock,
            "capital_reduction_ratio": (prev["capital_reduction_ratio"] if prev else None),
            "refund_per_share_twd": (prev["refund_per_share_twd"] if prev else None),
            "cash_subscription_ratio": (prev["cash_subscription_ratio"] if prev else None),
            "share_exchange_ratio": (prev["share_exchange_ratio"] if prev else None),
            "limit_up_twd": parse_number(rec[7]),
            "limit_down_twd": parse_number(rec[8]),
            "auction_base_twd": parse_number(rec[9]),
            "source": (prev["source"] if enriched else url),
            "announced_at": None,
            "announced_at_note": (prev["announced_at_note"] if enriched else "來源無公告時間；action_date=除權息資料日期"),
            "fetched_at": now_iso(),
            "unit": (prev["unit"] if enriched else "amount=新台幣元"),
            "source_unit": (prev["source_unit"] if enriched else "元"),
            "unit_conversion": (prev["unit_conversion"] if enriched else "none"),
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": row["action_type"]},
            row,
            CORP_CONTENT_KEYS,
        )
        n += 1
    touch_source(conn, "twse_twt49u", url=url, ok=True, http_status=200)
    # TWT49U 一次涵蓋現金股利／股票股利／權息同日；coverage 分三種 action_kind 各記一列
    for kind in ("cash_dividend", "stock_dividend", "ex_both"):
        _write_coverage(conn, "TWSE", kind, start.isoformat(), end.isoformat(), url, "TWT49U 區間查詢成功；同源涵蓋三種除權息")
    refresh_enrich_scope(conn)      # TWT49U 沒有配股比例：待 MOPS 補強前，配股／權息兩類不算全市場已齊
    conn.commit()
    return n


def fetch_twse_twtauu(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    """減資恢復買賣參考價；action_date = 恢復買賣日期。"""
    _set_scope(symbols)
    url = (
        "https://www.twse.com.tw/exchangeReport/TWTAUU?"
        + urlencode({"response": "json", "startDate": ad_yyyymmdd(start), "endDate": ad_yyyymmdd(end)})
    )
    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_twtauu", url=url, ok=False, error=str(data.get("stat")))
        return 0
    # 驗證標題期間
    title = data.get("title") or ""
    n = 0
    for rec in data.get("data") or []:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(rec[0])  # 恢復買賣日期
        if not ad:
            continue
        row = {
            "symbol": sym,
            "market": "TWSE",
            "action_date": ad,
            "action_type": "capital_reduction",
            "ex_type_raw": rec[9] if len(rec) > 9 else None,
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": None,
            "cash_dividend_twd": None,
            "stock_dividend_per_thousand": None,
            "capital_reduction_ratio": None,  # TWTAUU 無換股率欄；不臆測
            "refund_per_share_twd": None,
            "cash_subscription_ratio": None,
            "limit_up_twd": parse_number(rec[5]),
            "limit_down_twd": parse_number(rec[6]),
            "auction_base_twd": parse_number(rec[7]),
            "source": url,
            "announced_at": None,
            "announced_at_note": "action_date=恢復買賣日（TWTAUU）；非停止買賣日",
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元",
            "source_unit": "元",
            "unit_conversion": "none",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": "capital_reduction"},
            row,
            ["prev_close_twd", "reference_twd", "ex_type_raw", "limit_up_twd"],
        )
        n += 1
    touch_source(conn, "twse_twtauu", url=url, ok=True, http_status=200, notes=title)
    _write_coverage(
        conn,
        "TWSE",
        "capital_reduction",
        start.isoformat(),
        end.isoformat(),
        url,
        f"TWTAUU 成功；title={title}；action_date=恢復買賣日",
    )
    conn.commit()
    return n


def fetch_tpex_exrights(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    _set_scope(symbols)
    d = f"{start.year-1911}/{start.month:02d}/{start.day:02d}"
    ed = f"{end.year-1911}/{end.month:02d}/{end.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/exright/dailyquo/exDailyQ_result.php?l=zh-tw&d={d}&ed={ed}&o=json"
    resp = tpex.request("GET", url)
    data = resp.json()
    tables = data.get("tables") or []
    # 驗證回傳日期區間字串
    top_date = str(data.get("date") or "")
    n = 0
    for rec in (tables[0].get("data") if tables else []) or []:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(rec[0])
        if not ad:
            continue
        raw = rec[8] if len(rec) > 8 else ""
        row = {
            "symbol": sym,
            "market": "TPEx",
            "action_date": ad,
            "action_type": _action_type(str(raw)),
            "ex_type_raw": str(raw),
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": parse_number(rec[5]),
            "dividend_value_twd": parse_number(rec[6]),
            "rights_dividend_sum_twd": parse_number(rec[7]),
            "cash_dividend_twd": parse_number(rec[13]) if len(rec) > 13 else None,
            "stock_dividend_per_thousand": parse_number(rec[14]) if len(rec) > 14 else None,
            "capital_reduction_ratio": None,
            "refund_per_share_twd": None,
            "cash_subscription_ratio": (
                (lambda v: (v / 1000.0) if v is not None else None)(
                    parse_number(rec[20]) if len(rec) > 20 else None
                )
            ),  # 按持股比例仟股認購 → 每1股可認購股數
            "limit_up_twd": parse_number(rec[9]) if len(rec) > 9 else None,
            "limit_down_twd": parse_number(rec[10]) if len(rec) > 10 else None,
            "auction_base_twd": parse_number(rec[11]) if len(rec) > 11 else None,
            "source": url,
            "announced_at": None,
            "announced_at_note": "來源無公告時間；action_date=除權息日期；cash_subscription_ratio=按持股比例仟股認購/1000",
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元;cash_subscription_ratio=股/股",
            "source_unit": "元;每仟股無償配股;按持股比例仟股認購",
            "unit_conversion": "cash_subscription_ratio=per_thousand/1000",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": row["action_type"]},
            row,
            CORP_CONTENT_KEYS,
        )
        n += 1
    ok = "202" in top_date or "~" in top_date or len((tables[0].get("data") if tables else []) or []) >= 0
    touch_source(conn, "tpex_exright", url=url, ok=True, http_status=200, notes=f"date={top_date}")
    if ok:
        for kind in ("cash_dividend", "stock_dividend", "ex_both"):
            _write_coverage(conn, "TPEx", kind, start.isoformat(), end.isoformat(), url, f"exDailyQ date={top_date}；同源涵蓋三種除權息")
    conn.commit()
    return n


def _parse_revivt_detail(html: str) -> dict:
    """解析 revivt 詳細資料 HTML：換股（每仟股→比率）、退還股款、現增配股率。"""
    import re
    from html import unescape

    out: dict = {
        "capital_reduction_ratio": None,
        "refund_per_share_twd": None,
        "cash_subscription_ratio": None,
        "stop_trade_date": None,
    }
    if not html:
        return out
    for m in re.finditer(r"<th>([^<]+)</th><td>([^<]*)</td>", html):
        k = m.group(1).strip().rstrip(":")
        v = unescape(m.group(2)).replace("\xa0", " ").replace("&nbsp", " ").strip()
        v = re.sub(r"\s+", " ", v)
        if "每壹仟股換發新股票" in k or "每一仟股換發新股票" in k:
            num = parse_number(v.split()[0] if v else None)
            if num is not None:
                out["capital_reduction_ratio"] = num / 1000.0  # 換股率（例 550股/仟股 → 0.55）
        elif "每股退還股款" in k:
            out["refund_per_share_twd"] = parse_number(v.split()[0] if v else None)
        elif "現金增資配股率" in k:
            out["cash_subscription_ratio"] = parse_number(v)
        elif "停止買賣日期" in k:
            out["stop_trade_date"] = roc_ymd_to_iso(v)
    return out


def fetch_tpex_revivt(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    """上櫃減資恢復買賣參考價；action_date = 恢復買賣日期。
    頁面：/zh-tw/announce/market/reduction/reference.html → API bulletin/revivt
    """
    _set_scope(symbols)
    payload = {
        "startDate": f"{start.year}/{start.month:02d}/{start.day:02d}",
        "endDate": f"{end.year}/{end.month:02d}/{end.day:02d}",
        "response": "json",
    }
    url = "https://www.tpex.org.tw/www/zh-tw/bulletin/revivt"
    resp = tpex.request("POST", url, data=payload)
    data = resp.json()
    top_date = str(data.get("date") or "")
    tables = data.get("tables") or []
    rows = (tables[0].get("data") if tables else []) or []
    # 驗證回傳區間字串含請求起迄（不可把錯誤頁當無事件）
    date_compact = top_date.replace("/", "")
    date_ok = ad_yyyymmdd(start) in date_compact and ad_yyyymmdd(end) in date_compact
    n = 0
    for rec in rows:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(str(rec[0]))
        if not ad:
            continue
        detail = _parse_revivt_detail(str(rec[10]) if len(rec) > 10 else "")
        row = {
            "symbol": sym,
            "market": "TPEx",
            "action_date": ad,
            "action_type": "capital_reduction",
            "ex_type_raw": rec[9] if len(rec) > 9 else None,
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": None,
            "cash_dividend_twd": None,
            "stock_dividend_per_thousand": None,
            "capital_reduction_ratio": detail["capital_reduction_ratio"],
            "refund_per_share_twd": detail["refund_per_share_twd"],
            "cash_subscription_ratio": detail["cash_subscription_ratio"],
            "limit_up_twd": parse_number(rec[5]),
            "limit_down_twd": parse_number(rec[6]),
            "auction_base_twd": parse_number(rec[7]),
            "source": url,
            "announced_at": None,
            "announced_at_note": (
                "action_date=恢復買賣日（bulletin/revivt）；"
                + (f"停止買賣日={detail['stop_trade_date']}；" if detail.get("stop_trade_date") else "")
                + "換股率=每仟股換發新股/1000"
            ),
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元;capital_reduction_ratio=換股率(新/舊)",
            "source_unit": "元;每仟股換發股數",
            "unit_conversion": "capital_reduction_ratio=per_thousand/1000",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": "capital_reduction"},
            row,
            CORP_CONTENT_KEYS,
        )
        n += 1
    touch_source(conn, "tpex_revivt", url=url, ok=True, http_status=200, notes=f"date={top_date};rows={len(rows)}")
    if date_ok:
        _write_coverage(
            conn,
            "TPEx",
            "capital_reduction",
            start.isoformat(),
            end.isoformat(),
            url,
            f"bulletin/revivt 成功；date={top_date}；action_date=恢復買賣日；自民國102年1月起",
        )
    conn.commit()
    return n



def _parse_pv_detail(html: str) -> dict:
    """解析上櫃面額變更詳細 HTML：換股率、變更前後面額。"""
    import re
    from html import unescape
    out = {"share_exchange_ratio": None, "par_before": None, "par_after": None, "stop_trade_date": None}
    if not html:
        return out
    for m in re.finditer(r"<th>([^<]+)</th><td>([^<]*)</td>", html):
        k = m.group(1).strip().rstrip(":")
        v = unescape(m.group(2)).replace("\xa0", " ").replace("&nbsp", " ").strip()
        v = re.sub(r"\s+", " ", v)
        if "變更股票面額換股率" in k:
            out["share_exchange_ratio"] = parse_number(v)  # 每1舊股換得新股數（來源已為此口徑）
        elif "變更前" in k and "面額" in k:
            out["par_before"] = parse_number(v)
        elif "變更後" in k and "面額" in k:
            out["par_after"] = parse_number(v)
        elif "停止買賣日期" in k:
            out["stop_trade_date"] = roc_ymd_to_iso(v)
    return out


def fetch_twse_twtb8u(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    """變更股票面額恢復買賣參考價；action_date=恢復買賣日。同源無獨立「分割」表。"""
    _set_scope(symbols)
    url = (
        "https://www.twse.com.tw/exchangeReport/TWTB8U?"
        + urlencode({"response": "json", "startDate": ad_yyyymmdd(start), "endDate": ad_yyyymmdd(end)})
    )
    # 預告表帶換股率，供對照（可能無完整歷史）
    url7 = (
        "https://www.twse.com.tw/exchangeReport/TWTB7U?"
        + urlencode({"response": "json", "startDate": ad_yyyymmdd(start), "endDate": ad_yyyymmdd(end)})
    )
    ratio_by_sym_resume: dict[tuple[str, str], float] = {}
    try:
        d7 = twse.request("GET", url7).json()
        if d7.get("stat") == "OK":
            for rec in d7.get("data") or []:
                sym = str(rec[1]).strip()
                resume = roc_ymd_to_iso(str(rec[3]))
                ratio = parse_number(rec[4])  # 變更股票面額換股率 = 每1舊股換得新股數
                if sym and resume and ratio is not None:
                    ratio_by_sym_resume[(sym, resume)] = ratio
    except Exception as e:
        log.warning("TWTB7U ratio prefetch failed: %s", e)

    resp = twse.request("GET", url)
    data = resp.json()
    if data.get("stat") != "OK":
        touch_source(conn, "twse_twtb8u", url=url, ok=False, error=str(data.get("stat")))
        return 0
    title = data.get("title") or ""
    n = 0
    for rec in data.get("data") or []:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(str(rec[0]))
        if not ad:
            continue
        ratio = ratio_by_sym_resume.get((sym, ad))
        row = {
            "symbol": sym,
            "market": "TWSE",
            "action_date": ad,
            "action_type": "par_value_change",
            "ex_type_raw": "變更股票面額",
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": None,
            "cash_dividend_twd": None,
            "stock_dividend_per_thousand": None,
            "capital_reduction_ratio": None,
            "refund_per_share_twd": None,
            "cash_subscription_ratio": None,
            "share_exchange_ratio": ratio,
            "limit_up_twd": parse_number(rec[5]),
            "limit_down_twd": parse_number(rec[6]),
            "auction_base_twd": parse_number(rec[7]),
            "source": url,
            "announced_at": None,
            "announced_at_note": "action_date=恢復買賣日（TWTB8U）；share_exchange_ratio 來自 TWTB7U 同代號恢復日對照（每1舊股換得新股數）；非獨立分割表",
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元;share_exchange_ratio=新股/舊股",
            "source_unit": "元;換股率(每1舊股換得新股數)",
            "unit_conversion": "none",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": "par_value_change"},
            row,
            CORP_CONTENT_KEYS,
        )
        n += 1
    touch_source(conn, "twse_twtb8u", url=url, ok=True, http_status=200, notes=title)
    _write_coverage(
        conn, "TWSE", "par_value_change", start.isoformat(), end.isoformat(), url,
        f"TWTB8U 成功；title={title}；官方表名為變更股票面額；stock_split 見 covered_by 列",
    )
    _mirror_stock_split_coverage(conn, "TWSE", start, end, url)
    conn.commit()
    return n


def fetch_tpex_pv_chg(conn: sqlite3.Connection, symbols: set[str], start, end) -> int:
    """上櫃變更股票面額恢復買賣參考價；API bulletin/pvChgRslt。"""
    _set_scope(symbols)
    payload = {
        "startDate": f"{start.year}/{start.month:02d}/{start.day:02d}",
        "endDate": f"{end.year}/{end.month:02d}/{end.day:02d}",
        "response": "json",
    }
    url = "https://www.tpex.org.tw/www/zh-tw/bulletin/pvChgRslt"
    resp = tpex.request("POST", url, data=payload)
    data = resp.json()
    top_date = str(data.get("date") or "")
    tables = data.get("tables") or []
    rows = (tables[0].get("data") if tables else []) or []
    date_compact = top_date.replace("/", "")
    date_ok = ad_yyyymmdd(start) in date_compact and ad_yyyymmdd(end) in date_compact
    n = 0
    for rec in rows:
        sym = str(rec[1]).strip()
        if symbols and sym not in symbols:
            continue
        ad = roc_ymd_to_iso(str(rec[0]))
        if not ad:
            continue
        detail = _parse_pv_detail(str(rec[8]) if len(rec) > 8 else "")
        row = {
            "symbol": sym,
            "market": "TPEx",
            "action_date": ad,
            "action_type": "par_value_change",
            "ex_type_raw": "變更股票面額",
            "prev_close_twd": parse_number(rec[3]),
            "reference_twd": parse_number(rec[4]),
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": None,
            "cash_dividend_twd": None,
            "stock_dividend_per_thousand": None,
            "capital_reduction_ratio": None,
            "refund_per_share_twd": None,
            "cash_subscription_ratio": None,
            "share_exchange_ratio": detail["share_exchange_ratio"],
            "limit_up_twd": parse_number(rec[5]),
            "limit_down_twd": parse_number(rec[6]),
            "auction_base_twd": parse_number(rec[7]),
            "source": url,
            "announced_at": None,
            "announced_at_note": (
                "action_date=恢復買賣日（bulletin/pvChgRslt）；"
                + (f"停止買賣日={detail['stop_trade_date']}；" if detail.get("stop_trade_date") else "")
                + "share_exchange_ratio=詳細資料「變更股票面額換股率」（每1舊股換得新股數）；非獨立分割表"
            ),
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元;share_exchange_ratio=新股/舊股",
            "source_unit": "元;換股率(每1舊股換得新股數)",
            "unit_conversion": "none",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": ad, "action_type": "par_value_change"},
            row,
            CORP_CONTENT_KEYS,
        )
        n += 1
    touch_source(conn, "tpex_pvChgRslt", url=url, ok=True, http_status=200, notes=f"date={top_date};rows={len(rows)}")
    if date_ok:
        _write_coverage(
            conn, "TPEx", "par_value_change", start.isoformat(), end.isoformat(), url,
            f"bulletin/pvChgRslt 成功；date={top_date}；官方表名變更股票面額；stock_split 見 covered_by 列；頁面自 2019-09-09",
        )
        _mirror_stock_split_coverage(conn, "TPEx", start, end, url)
    conn.commit()
    return n


def enrich_twse_stock_dividends_from_mops(conn: sqlite3.Connection, symbols: set[str], max_items: int | None = None) -> dict:
    """對缺 stock_dividend_per_thousand 的上市 權／權息 列，以 MOPS t05st09_2 補強。
    單位：盈餘轉增資配股(元/股) ÷ 面額 ×1000 → stock_dividend_per_thousand；不倒推參考價。
    """
    from bs4 import BeautifulSoup
    from pipeline.httputil import mops, warm_mops, FetchError
    import re as _re

    if symbols:
        need = conn.execute(
            """SELECT symbol, action_date, action_type, ex_type_raw, prev_close_twd, reference_twd,
                      rights_dividend_sum_twd, source, limit_up_twd, limit_down_twd, auction_base_twd
               FROM corporate_actions
               WHERE is_current=1 AND market='TWSE'
                 AND action_type IN ('ex_both','ex_rights')
                 AND stock_dividend_per_thousand IS NULL
                 AND symbol IN ({})
            """.format(",".join("?" * len(symbols))),
            tuple(symbols),
        ).fetchall()
    else:
        need = conn.execute(
            """SELECT symbol, action_date, action_type, ex_type_raw, prev_close_twd, reference_twd,
                      rights_dividend_sum_twd, source, limit_up_twd, limit_down_twd, auction_base_twd
               FROM corporate_actions
               WHERE is_current=1 AND market='TWSE'
                 AND action_type IN ('ex_both','ex_rights')
                 AND stock_dividend_per_thousand IS NULL"""
        ).fetchall()

    out: dict = {"filled": 0, "failed": [], "skipped": 0}
    out["pending"] = len(need)
    attempted: list[str] = []
    if max_items is not None:
        # 以「代號」為單位限量（同代號共用一次 MOPS 請求）；嘗試次數少的先做，查不到的往後排，避免每次都卡在同一批
        from pipeline.runstats import state_get
        syms_sorted = sorted({r["symbol"] for r in need},
                             key=lambda x: ((state_get(conn, "corp_enrich", x) or (None, 0))[1], x))
        keep = set(syms_sorted[:max_items])
        need = [r for r in need if r["symbol"] in keep]
        out["symbols_this_run"] = len(keep)
    if not need:
        out["coverage"] = refresh_enrich_scope(conn)
        return out
    try:
        warm_mops("https://mopsov.twse.com.tw/mops/web/t05st09_2")
    except Exception as e:
        out["failed"].append(f"warm:{e}")

    from pipeline import config as _cfg
    cache: dict[str, dict | None] = {}
    err_syms: set[str] = set()
    streak = 0
    for row in need:
        sym = row["symbol"]
        if sym not in cache:
            if streak >= _cfg.REDIRECT_STREAK_STOP:
                out["stopped"] = f"consecutive_errors:{streak}"
                break
            errs: list = []
            cache[sym] = _fetch_mops_t05st09(sym, errs)
            attempted.append(sym)
            if errs:
                err_syms.add(sym)
                streak += 1
            else:
                streak = 0
        info = cache[sym]
        if not info:
            out["failed"].append(f"{sym}:no_mops_dividend")
            continue
        stock_yuan = info.get("stock_yuan_per_share")
        par = info.get("par") or 10.0
        cash = info.get("cash_dividend_twd")
        if stock_yuan is None:
            out["skipped"] += 1
            continue
        per_thousand = (float(stock_yuan) / float(par)) * 1000.0
        src = info["source"]
        rec = {
            "symbol": sym,
            "market": "TWSE",
            "action_date": row["action_date"],
            "action_type": row["action_type"],
            "ex_type_raw": row["ex_type_raw"],
            "prev_close_twd": row["prev_close_twd"],
            "reference_twd": row["reference_twd"],
            "rights_value_twd": None,
            "dividend_value_twd": None,
            "rights_dividend_sum_twd": row["rights_dividend_sum_twd"],
            "cash_dividend_twd": cash,
            "stock_dividend_per_thousand": per_thousand,
            "capital_reduction_ratio": None,
            "refund_per_share_twd": None,
            "cash_subscription_ratio": None,
            "limit_up_twd": row["limit_up_twd"],
            "limit_down_twd": row["limit_down_twd"],
            "auction_base_twd": row["auction_base_twd"],
            "source": f"{src} | primary_ex={row['source']}",
            "announced_at": None,
            "announced_at_note": (
                f"stock/cash from MOPS t05st09_2：盈餘轉增資配股{stock_yuan}元/股÷面額{par}×1000={per_thousand:g}股/千股"
                + (f"；現金股利{cash}元/股" if cash is not None else "")
                + f"。除權息日對照 TWT49U {row['action_date']}。"
            ),
            "fetched_at": now_iso(),
            "unit": "stock_dividend=股/千股; cash_dividend=元/股",
            "source_unit": "MOPS 盈餘轉增資配股=元/股; 現金股利=元/股",
            "unit_conversion": "stock_per_thousand=(stock_yuan_per_share/par)*1000",
        }
        upsert_versioned(
            conn,
            "corporate_actions",
            {"symbol": sym, "action_date": row["action_date"], "action_type": row["action_type"]},
            rec,
            CORP_CONTENT_KEYS,
        )
        out["filled"] += 1
        touch_source(conn, f"mops_t05st09_{sym}", url=src, ok=True, http_status=200, data_date=row["action_date"])
    if attempted:
        from pipeline.runstats import state_set
        for sym in attempted:
            if sym not in err_syms:      # 請求失敗／擋頁的代號不記嘗試次數（下次仍優先）
                state_set(conn, "corp_enrich", sym, "done", None, bump=True)
    conn.commit()
    out["coverage"] = refresh_enrich_scope(conn)
    return out


def _fetch_mops_t05st09(symbol: str, errors: list | None = None) -> dict | None:
    """errors（可選）：請求失敗／擋頁時附加原因（與「查無配股資料」區分，供連續失敗停止判斷）。"""
    from bs4 import BeautifulSoup
    from pipeline.httputil import mops, FetchError
    import re as _re

    url = "https://mopsov.twse.com.tw/mops/web/ajax_t05st09_2"
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "keyword4": "",
        "code1": "",
        "TYPEK2": "",
        "checkbtn": "",
        "queryName": "co_id",
        "inpuType": "co_id",
        "TYPEK": "all",
        "isnew": "true",
        "co_id": symbol,
        "year": "115",
    }
    try:
        resp = mops.request("POST", url, data=payload, retries=3)
    except FetchError as e:
        log.warning("mops t05st09 %s: %s", symbol, e)
        if errors is not None:
            errors.append(str(e)[:200])
        return None
    text = resp.content.decode("utf-8", errors="replace")
    if "安全性考量" in text or "無法呈現" in text:
        if errors is not None:
            errors.append("blocked_page")
        return None
    soup = BeautifulSoup(text, "lxml")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 18:
            continue
        if cells[0] not in ("股東會確認", "董事會通過", "董事會決議", "決議通過", "股東會決議"):
            continue
        par = 10.0
        for c in cells:
            if "新台幣" in c and "元" in c:
                m = _re.search(r"([0-9]+(?:\.[0-9]+)?)", c.replace(",", ""))
                if m:
                    par = float(m.group(1))
        cash = parse_number(cells[10])
        stock_yuan = parse_number(cells[14])
        if stock_yuan is None:
            continue
        return {
            "cash_dividend_twd": cash,
            "stock_yuan_per_share": stock_yuan,
            "par": par,
            "source": f"{url}?co_id={symbol}&year=115",
        }
    return None



def fetch_all(conn: sqlite3.Connection, symbols: list[str], *, max_enrich: int | None = None, start=None) -> dict:
    end = today()
    start = start or end - timedelta(days=400)
    s = set(symbols)
    out = {
        "twse_ex": fetch_twse_exrights(conn, s, start, end),
        "twse_cr_restore": fetch_twse_twtauu(conn, s, start, end),
        "twse_par_value": fetch_twse_twtb8u(conn, s, start, end),
        "tpex_ex": fetch_tpex_exrights(conn, s, start, end),
        "tpex_cr_restore": fetch_tpex_revivt(conn, s, start, end),
        "tpex_par_value": fetch_tpex_pv_chg(conn, s, start, end),
    }
    try:
        out["mops_div_enrich"] = enrich_twse_stock_dividends_from_mops(conn, s, max_items=max_enrich)
    except Exception as e:
        log.exception("mops dividend enrich")
        out["mops_div_enrich"] = {"failed": [str(e)]}
    return out
