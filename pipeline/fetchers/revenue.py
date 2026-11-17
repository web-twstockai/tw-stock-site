"""月營收：mopsov NAS 靜態頁（千元→元）。"""
from __future__ import annotations

import logging
import re
import sqlite3

from bs4 import BeautifulSoup

from pipeline import config
from pipeline.dates import iso_to_roc_ym, month_iter, now_iso, parse_number, revenue_month_phase, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import FetchError, mops

log = logging.getLogger("pipeline.revenue")


def _thousand_to_twd(v: float | None) -> int | None:
    if v is None:
        return None
    return int(round(v * 1000))


def _parse_nas(html: bytes, symbols: set[str] | None = None) -> list[dict]:
    """解析 NAS 月營收表。symbols 為 None 或空集合時回傳全表列（供頁面列數判斷）。"""
    text = html.decode("cp950", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    out = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if not cells or not re.fullmatch(r"\d{4}", cells[0] or ""):
            continue
        sym = cells[0]
        if symbols and sym not in symbols:
            continue
        remark = cells[10] if len(cells) > 10 else None
        out.append(
            {
                "symbol": sym,
                "name": cells[1],
                "revenue": parse_number(cells[2]),
                "prev_month": parse_number(cells[3]),
                "prev_year": parse_number(cells[4]),
                "mom": parse_number(cells[5]),
                "yoy": parse_number(cells[6]),
                "cum": parse_number(cells[7]),
                "cum_prev": parse_number(cells[8]),
                "cum_yoy": parse_number(cells[9]) if len(cells) > 9 else None,
                "remark": remark,
            }
        )
    return out


# 公告期內頁面列數少於此值視為 partial_pending（官方尚在陸續公告）
_PARTIAL_PENDING_PAGE_ROWS = 50


def _classify_empty_or_sparse(*, page_rows: int, phase: str) -> tuple[str, bool, str | None]:
    """回傳 (status, ok_flag, error_note)。
    not_yet_due／partial_pending → ok=True（不算來源故障）；
    source_down／fail → ok=False；ok 成功列 → ok=True。
    """
    if page_rows <= 0:
        if phase in ("too_early", "pending"):
            return "not_yet_due", True, "0 rows; before/within announce deadline (M+1 day 10)"
        return "source_down", False, "0 rows after announce deadline"
    if phase == "pending" and page_rows < _PARTIAL_PENDING_PAGE_ROWS:
        return "partial_pending", True, f"only {page_rows} page rows within announce window"
    return "ok", True, None


def _mark_not_yet_due(
    conn: sqlite3.Connection,
    *,
    market: str,
    ym: str,
    url: str | None = None,
    reason: str,
) -> None:
    """資料月未結束或不應抓取／公告期內 0 列：記 not_yet_due，ok=1（首頁不算故障）。"""
    sid = f"mops_nas_revenue_{market}_{ym}"
    touch_source(
        conn,
        sid,
        url=url,
        ok=True,
        http_status=None,
        data_date=None,  # 不覆寫成功期別
        error=None,
        notes="not_yet_due",
    )
    # 明確清掉 last_error（touch_source 會寫 error=None）
    conn.execute(
        "UPDATE source_status SET last_error=NULL, notes=?, ok=1 WHERE source_id=?",
        (f"not_yet_due: {reason}", sid),
    )
    _write_fetch_status(
        conn,
        market=market,
        ym=ym,
        status="not_yet_due",
        http_status=None,
        row_count=0,
        source=url or "",
        error=reason,
    )


def _looks_like_block_page(resp) -> str | None:
    """200 但內容為擋頁／非 NAS 表 → 視為失敗。回傳錯誤字串或 None。"""
    head = (resp.text or "")[:500]
    if "安全性考量" in head or "FOR SECURITY REASONS" in head.upper() or "無法呈現" in head:
        return f"block_page/http={resp.status_code}"
    if "text/html" in (resp.headers.get("content-type") or "") and "t21sc03" not in head and "公司代號" not in (resp.text or "")[:2000]:
        # NAS 成功頁為 cp950 HTML 含表格；明顯導向／error 頁當失敗
        if any(k in head for k in ("404", "error", "Redirect", "errors", "Access Denied")):
            return f"html_error_page/http={resp.status_code}"
    return None


def _write_fetch_status(
    conn: sqlite3.Connection,
    *,
    market: str,
    ym: str,
    status: str,
    http_status: int | None,
    row_count: int,
    source: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO revenue_fetch_status(market,revenue_month,status,http_status,row_count,source,fetched_at,error)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(market,revenue_month) DO UPDATE SET
             status=excluded.status,
             http_status=excluded.http_status,
             row_count=excluded.row_count,
             source=excluded.source,
             fetched_at=excluded.fetched_at,
             error=excluded.error
        """,
        (market, ym, status, http_status, row_count, source, now_iso(), error),
    )


def fetch_month(conn: sqlite3.Connection, ym: str, symbols: set[str], market: str) -> int:
    roc_y, roc_m = iso_to_roc_ym(ym)
    path = "sii" if market == "TWSE" else "otc"
    url = f"https://mopsov.twse.com.tw/nas/t21/{path}/t21sc03_{roc_y}_{roc_m}.html"
    try:
        resp = mops.request("GET", url)
    except FetchError as e:
        touch_source(conn, f"mops_nas_revenue_{market}_{ym}", url=url, ok=False, http_status=e.status, error=str(e))
        _write_fetch_status(conn, market=market, ym=ym, status="fail", http_status=e.status, row_count=0, source=url, error=str(e))
        conn.commit()
        raise
    block = _looks_like_block_page(resp)
    if block:
        touch_source(conn, f"mops_nas_revenue_{market}_{ym}", url=url, ok=False, http_status=resp.status_code, error=block)
        _write_fetch_status(conn, market=market, ym=ym, status="fail", http_status=resp.status_code, row_count=0, source=url, error=block)
        conn.commit()
        raise FetchError(block, blocked=True, status=resp.status_code)
    if resp.status_code == 404 or len(resp.content) < 500:
        phase = revenue_month_phase(ym)
        status, ok_flag, err_note = _classify_empty_or_sparse(page_rows=0, phase=phase)
        if status == "not_yet_due":
            err_note = "empty/404 before announce deadline"
        touch_source(
            conn,
            f"mops_nas_revenue_{market}_{ym}",
            url=url,
            ok=ok_flag,
            http_status=resp.status_code,
            data_date=None,
            error=None if ok_flag else (err_note or "empty/404"),
            notes=status,
        )
        if ok_flag:
            conn.execute(
                "UPDATE source_status SET last_error=NULL, notes=?, ok=1 WHERE source_id=?",
                (f"{status}: {err_note}", f"mops_nas_revenue_{market}_{ym}"),
            )
        _write_fetch_status(
            conn, market=market, ym=ym, status=status, http_status=resp.status_code,
            row_count=0, source=url, error=err_note or "empty/404",
        )
        conn.commit()
        return 0
    page_rows_all = _parse_nas(resp.content, symbols=None)
    page_n = len(page_rows_all)
    rows = [r for r in page_rows_all if (not symbols) or r["symbol"] in symbols]
    mkt_map = {r[0]: r[1] for r in conn.execute("SELECT symbol, market FROM securities")}
    n = 0
    for r in rows:
        rec = {
            "symbol": r["symbol"],
            "market": mkt_map.get(r["symbol"], market),
            "revenue_month": ym,
            "revenue_twd": _thousand_to_twd(r["revenue"]),
            "revenue_prev_month_twd": _thousand_to_twd(r["prev_month"]),
            "revenue_prev_year_twd": _thousand_to_twd(r["prev_year"]),
            "mom_pct": r["mom"],
            "yoy_pct": r["yoy"],
            "cumulative_twd": _thousand_to_twd(r["cum"]),
            "cumulative_prev_year_twd": _thousand_to_twd(r["cum_prev"]),
            "cumulative_yoy_pct": r["cum_yoy"],
            "industry": None,
            "remark": r["remark"],
            "source": url,
            "announced_at": None,
            "announced_at_note": "來源無精確公告時間；頁面為月營收彙總",
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元",
            "source_unit": "新台幣仟元",
            "unit_conversion": "*1000",
        }
        upsert_versioned(
            conn,
            "monthly_revenues",
            {"symbol": r["symbol"], "revenue_month": ym},
            rec,
            ["revenue_twd", "revenue_prev_month_twd", "cumulative_twd", "mom_pct", "yoy_pct"],
        )
        n += 1
    phase = revenue_month_phase(ym)
    status, ok_flag, err_note = _classify_empty_or_sparse(page_rows=page_n, phase=phase)
    # 也抓了但資料月未結束／公告期內 0 列：強制 not_yet_due，絕不可 source_down
    if page_n == 0 and phase in ("too_early", "pending"):
        status, ok_flag, err_note = "not_yet_due", True, err_note or "0 rows parsed before deadline"
    notes = status if status != "ok" else None
    touch_source(
        conn,
        f"mops_nas_revenue_{market}_{ym}",
        url=url,
        ok=ok_flag,
        http_status=200,
        data_date=ym if n > 0 else None,
        error=None if ok_flag else (err_note or "0 rows parsed"),
        notes=notes,
    )
    if status == "not_yet_due":
        conn.execute(
            "UPDATE source_status SET last_error=NULL, notes=?, ok=1 WHERE source_id=?",
            (f"not_yet_due: {err_note}", f"mops_nas_revenue_{market}_{ym}"),
        )
    # 市場級彙總鍵：僅在真正成功（有列）或期限後故障時更新，避免 not_yet_due 污染
    if status == "ok" or status == "partial_pending":
        touch_source(
            conn,
            f"mops_nas_revenue_{market}",
            url=url,
            ok=True,
            http_status=200,
            data_date=ym if n > 0 else None,
            error=None,
            notes=status if status != "ok" else None,
        )
    elif status == "source_down":
        touch_source(
            conn,
            f"mops_nas_revenue_{market}",
            url=url,
            ok=False,
            http_status=200,
            data_date=None,
            error=err_note or "0 rows after deadline",
            notes="source_down",
        )
    _write_fetch_status(
        conn,
        market=market,
        ym=ym,
        status=status,
        http_status=200,
        row_count=n,
        source=url,
        error=err_note,
    )
    conn.commit()
    log.info("revenue %s %s rows=%s page_rows=%s status=%s phase=%s", market, ym, n, page_n, status, phase)
    return n


def fetch_all(conn: sqlite3.Connection, symbols: list[str], *, max_months: int | None = None) -> dict:
    end = today()
    end_ym = f"{end.year:04d}-{end.month:02d}"
    target = max_months if max_months is not None else config.REVENUE_MONTHS
    # 多取幾個月以跳過 too_early／pending 空月後仍湊滿 target 個「有資料」月
    months = month_iter(end_ym, target + 4)
    symset = set(symbols)
    ok_months = []
    failed = []
    skipped = []
    for ym in months:
        if len(ok_months) >= target:
            break
        phase = revenue_month_phase(ym, end)
        if phase == "too_early":
            # 資料月尚未結束：不發 HTTP；記 not_yet_due（不算故障）
            for market in ("TWSE", "TPEx"):
                path = "sii" if market == "TWSE" else "otc"
                roc_y, roc_m = iso_to_roc_ym(ym)
                url = f"https://mopsov.twse.com.tw/nas/t21/{path}/t21sc03_{roc_y}_{roc_m}.html"
                _mark_not_yet_due(
                    conn, market=market, ym=ym, url=url,
                    reason=f"data month {ym} not ended as_of={end.isoformat()}; announce deadline M+1-10",
                )
            conn.commit()
            skipped.append(ym)
            log.info("revenue skip too_early %s", ym)
            continue
        got = 0
        for market in ("TWSE", "TPEx"):
            try:
                got += fetch_month(conn, ym, symset, market)
            except FetchError as e:
                failed.append(f"{market}:{ym}:{e}")
                log.error("revenue fail %s %s %s", market, ym, e)
        if got > 0:
            ok_months.append(ym)
        elif phase == "pending":
            # 公告期內兩市場皆 0 列：已由 fetch_month 記 not_yet_due；不計入 ok_months
            skipped.append(ym)
    return {"months": ok_months, "failed": failed, "skipped": skipped}
