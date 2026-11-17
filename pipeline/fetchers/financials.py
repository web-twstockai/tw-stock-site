"""綜合損益與現金流量（mopsov 季報 HTML，仟元→元）。"""
from __future__ import annotations

import logging
import re
import sqlite3

from bs4 import BeautifulSoup

from pipeline import config
from pipeline.dates import now_iso, parse_number, quarter_iter, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import FetchError, mops

log = logging.getLogger("pipeline.financials")


def _x1000(v: float | None) -> int | None:
    if v is None:
        return None
    return int(round(v * 1000))


def _latest_filed_quarter() -> tuple[int, int]:
    """最近一個「申報期限已過」的季別（全市場已齊）：Q1→5/15、Q2→8/14、Q3→11/14、Q4（年報）→次年 3/31。
    （3b 修正：舊版整個 11 月都回 Q2，Q3 要到 12 月才會抓。）期限當天還在申報，所以要「晚於期限」才算。"""
    t = today()
    md = (t.month, t.day)
    if md > (11, 14):
        return t.year, 3
    if md > (8, 14):
        return t.year, 2
    if md > (5, 15):
        return t.year, 1
    if md > (3, 31):
        return t.year - 1, 4
    return t.year - 1, 3


def _parse_tables_by_code(html: bytes) -> dict[str, list[str]]:
    text = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    out: dict[str, list[str]] = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if cells and re.fullmatch(r"\d{4}", cells[0] or ""):
            out[cells[0]] = cells
    return out


def fetch_cashflow_season(conn: sqlite3.Connection, year: int, season: int, symbols: set[str], typek: str) -> int:
    roc_y = year - 1911
    season_s = f"{season:02d}"
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb20"
    data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": typek,
        "year": str(roc_y),
        "season": season_s,
    }
    resp = mops.request("POST", url, data=data)
    rows = _parse_tables_by_code(resp.content)
    mkt = "TWSE" if typek == "sii" else "TPEx"
    n = 0
    for sym, cells in rows.items():
        if symbols and sym not in symbols:
            continue
        # 代號,名稱,營業,投資,籌資,匯率,本期增減,期初,期末
        if len(cells) < 9:
            continue
        rec = {
            "symbol": sym,
            "market": mkt,
            "fiscal_year": year,
            "fiscal_quarter": season,
            "operating_cf_twd": _x1000(parse_number(cells[2])),
            "investing_cf_twd": _x1000(parse_number(cells[3])),
            "financing_cf_twd": _x1000(parse_number(cells[4])),
            "fx_effect_twd": _x1000(parse_number(cells[5])),
            "net_increase_twd": _x1000(parse_number(cells[6])),
            "cash_begin_twd": _x1000(parse_number(cells[7])),
            "cash_end_twd": _x1000(parse_number(cells[8])),
            "report_basis": "合併",
            "period_type": "累計",
            "derived": 0,
            "derivation_method": None,
            "source": f"{url}?TYPEK={typek}&year={roc_y}&season={season_s}",
            "announced_at": None,
            "announced_at_note": "來源無公告時間；MOPS 季報彙總預設合併／累計（依頁面口徑）",
            "fetched_at": now_iso(),
            "unit": "amount=新台幣元",
            "source_unit": "新台幣仟元",
            "unit_conversion": "*1000",
        }
        upsert_versioned(
            conn,
            "cash_flow_statements",
            {"symbol": sym, "fiscal_year": year, "fiscal_quarter": season, "report_basis": "合併", "period_type": "累計"},
            rec,
            ["operating_cf_twd", "investing_cf_twd", "financing_cf_twd", "cash_end_twd", "report_basis", "period_type"],
        )
        n += 1
    touch_source(conn, f"mops_cf_{typek}", url=url, ok=True, http_status=200, data_date=f"{year}Q{season}")
    conn.commit()
    return n


def fetch_income_season(conn: sqlite3.Connection, year: int, season: int, symbols: set[str], typek: str) -> int:
    """綜合損益：不同產業表欄位不同，盡量抓營業收入／營業利益／本期淨利／EPS。"""
    roc_y = year - 1911
    season_s = f"{season:02d}"
    url = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04"
    data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": typek,
        "year": str(roc_y),
        "season": season_s,
    }
    resp = mops.request("POST", url, data=data)
    text = resp.content.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    mkt = "TWSE" if typek == "sii" else "TPEx"
    n = 0
    # process each table with its header
    for table in soup.find_all("table"):
        headers = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0] in ("公司代號", "公司\n代號") or (len(cells) > 1 and "公司代號" in "".join(cells[:2])):
                headers = cells
                continue
            if not re.fullmatch(r"\d{4}", cells[0] or ""):
                continue
            sym = cells[0]
            if symbols and sym not in symbols:
                continue
            # map by header names when possible
            hmap = {headers[i]: i for i in range(len(headers))} if headers else {}

            def col(*names: str) -> float | None:
                for name in names:
                    if name in hmap and hmap[name] < len(cells):
                        return parse_number(cells[hmap[name]])
                return None

            revenue = col("營業收入", "收入", "淨收益", "利息淨收益")
            op_inc = col("營業利益（損失）", "營業利益", "營業利益（損失）")
            net = col(
                "本期淨利（淨損）",
                "繼續營業單位本期淨利（淨損）",
                "繼續營業單位本期稅後淨利（淨損）",
                "本期稅後淨利（淨損）",
            )
            eps = col("基本每股盈餘（元）", "基本每股盈餘")
            # fallback positional for general industry (ci-like)
            if revenue is None and len(cells) > 2:
                revenue = parse_number(cells[2])
            if net is None and len(cells) > 15:
                # often near end before EPS
                for idx in range(len(cells) - 1, 2, -1):
                    # skip looking
                    pass
            form = "mixed"
            if "利息淨收益" in (headers or []):
                form = "basi"
            elif "營業收入" in (headers or []):
                form = "ci"
            rec = {
                "symbol": sym,
                "market": mkt,
                "fiscal_year": year,
                "fiscal_quarter": season,
                "industry_form": form,
                "revenue_twd": _x1000(revenue),
                "gross_profit_twd": _x1000(col("營業毛利（毛損）", "營業毛利（毛損）淨額")),
                "operating_income_twd": _x1000(op_inc),
                "non_operating_twd": _x1000(col("營業外收入及支出")),
                "pretax_income_twd": _x1000(col("稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）")),
                "net_income_twd": _x1000(net),
                "net_income_parent_twd": _x1000(col("淨利（淨損）歸屬於母公司業主", "淨利（損）歸屬於母公司業主")),
                "eps_twd": eps,
                "raw_json": None,
                "report_basis": "合併",
                "period_type": "累計",
                "derived": 0,
                "derivation_method": None,
                "source": f"{url}?TYPEK={typek}&year={roc_y}&season={season_s}",
                "announced_at": None,
                "announced_at_note": "來源無公告時間；MOPS 季報彙總預設合併／累計（依頁面口徑）",
                "fetched_at": now_iso(),
                "unit": "amount=新台幣元;eps=元",
                "source_unit": "新台幣仟元(金額);元(EPS)",
                "unit_conversion": "amount*1000; eps unchanged",
            }
            upsert_versioned(
                conn,
                "income_statements",
                {
                    "symbol": sym,
                    "fiscal_year": year,
                    "fiscal_quarter": season,
                    "industry_form": form,
                    "report_basis": "合併",
                    "period_type": "累計",
                },
                rec,
                ["revenue_twd", "operating_income_twd", "net_income_twd", "eps_twd", "report_basis", "period_type"],
            )
            n += 1
    touch_source(conn, f"mops_income_{typek}", url=url, ok=True, http_status=200, data_date=f"{year}Q{season}")
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 頁級抓取（市場 × 季 × 表）：financial_fetch_status、頁級重試、連續 3xx／擋頁就停（Phase 3）
# ---------------------------------------------------------------------------
KIND_FUNCS = {"income": fetch_income_season, "cashflow": fetch_cashflow_season}
_SRC = {"income": "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04", "cashflow": "https://mopsov.twse.com.tw/mops/web/ajax_t163sb20"}


def _scope_of(conn: sqlite3.Connection, symset: set[str], typek: str) -> str:
    from pipeline.scope import scope_of
    return scope_of(symset, conn, "TWSE" if typek == "sii" else "TPEx")


def _write_page_status(conn: sqlite3.Connection, *, typek: str, y: int, q: int, kind: str, status: str,
                       http_status: int | None, n: int, scope: str, error: str | None) -> None:
    mkt = "TWSE" if typek == "sii" else "TPEx"
    conn.execute(
        """INSERT INTO financial_fetch_status(market,fiscal_year,fiscal_quarter,table_kind,status,http_status,row_count,scope,attempts,source,fetched_at,error)
           VALUES(?,?,?,?,?,?,?,?,1,?,?,?)
           ON CONFLICT(market,fiscal_year,fiscal_quarter,table_kind) DO UPDATE SET
             status=excluded.status, http_status=excluded.http_status, row_count=excluded.row_count, scope=excluded.scope,
             attempts=financial_fetch_status.attempts+1, source=excluded.source, fetched_at=excluded.fetched_at, error=excluded.error""",
        (mkt, y, q, kind, status, http_status, n, scope, _SRC[kind], now_iso(), error),
    )
    conn.commit()


def page_done(conn: sqlite3.Connection, typek: str, y: int, q: int, kind: str, scope: str) -> bool:
    mkt = "TWSE" if typek == "sii" else "TPEx"
    r = conn.execute(
        "SELECT status, scope FROM financial_fetch_status WHERE market=? AND fiscal_year=? AND fiscal_quarter=? AND table_kind=?",
        (mkt, y, q, kind)).fetchone()
    return bool(r and r[0] == "ok" and r[1] == scope)


def fetch_page(conn: sqlite3.Connection, kind: str, y: int, q: int, symset: set[str], typek: str) -> dict:
    """抓一頁並寫 financial_fetch_status。回傳 {n, status, error, redirect}；不丟 FetchError（RateLimited 除外）。"""
    from pipeline.httputil import RateLimited

    scope = _scope_of(conn, symset, typek)
    try:
        n = KIND_FUNCS[kind](conn, y, q, symset, typek)
    except RateLimited:
        raise
    except FetchError as e:
        msg = str(e)
        redirect = "redirect/http=" in msg or e.blocked
        _write_page_status(conn, typek=typek, y=y, q=q, kind=kind, status="blocked" if e.blocked else "fail",
                           http_status=e.status, n=0, scope=scope, error=msg[:300])
        return {"n": 0, "status": "blocked" if e.blocked else "fail", "error": msg, "redirect": redirect}
    status = "ok" if n > 0 else "empty"
    _write_page_status(conn, typek=typek, y=y, q=q, kind=kind, status=status, http_status=200, n=n, scope=scope, error=None)
    return {"n": n, "status": status, "error": None, "redirect": False}


def fetch_pages(conn: sqlite3.Connection, pages: list[tuple[int, int, str, str]], symset: set[str], *,
                budget=None, max_pages: int | None = None, skip_done: bool = False, retry_failed: bool = True) -> dict:
    """依序抓 pages=[(年, 季, typek, kind)]。
    - skip_done：已是同範圍 ok 的頁不再抓。
    - 連續 REDIRECT_STREAK_STOP 頁 3xx／擋頁就停止（視為 MOPS 限流，留待下次）。
    - 頁級重試：主流程結束後，把本次失敗的頁再試一次（未停止且時間預算內）。
    - max_pages：本次最多送出幾頁請求。"""
    from pipeline.httputil import RateLimited

    out: dict = {"pages_total": len(pages), "fetched": 0, "skipped_done": 0, "rows": {"income": 0, "cashflow": 0},
                 "failed": [], "empty": [], "stopped": None, "retried": 0, "retry_ok": 0}
    streak = 0
    failed_pages: list[tuple[int, int, str, str]] = []

    def one(pg) -> bool:
        nonlocal streak
        y, q, typek, kind = pg
        try:
            r = fetch_page(conn, kind, y, q, symset, typek)
        except RateLimited as e:
            out["stopped"] = f"rate_limited:{e}"
            return False
        out["fetched"] += 1
        if r["status"] in ("fail", "blocked"):
            out["failed"].append(f"{kind}:{typek}:{y}Q{q}:{r['error']}")
            failed_pages.append(pg)
            streak = streak + 1 if r["redirect"] else 0
            if streak >= config.REDIRECT_STREAK_STOP:
                out["stopped"] = f"consecutive_redirects:{streak}"
                return False
        else:
            streak = 0
            out["rows"][kind] += r["n"]
            if r["status"] == "empty":
                out["empty"].append(f"{kind}:{typek}:{y}Q{q}")
        return True

    for pg in pages:
        y, q, typek, kind = pg
        if skip_done and page_done(conn, typek, y, q, kind, _scope_of(conn, symset, typek)):
            out["skipped_done"] += 1
            continue
        if max_pages is not None and out["fetched"] >= max_pages:
            out["stopped"] = "max_pages"
            break
        if budget is not None and budget.expired(reserve_s=60):
            out["stopped"] = "budget"
            break
        if not one(pg):
            break
    if retry_failed and failed_pages and not out["stopped"]:
        for pg in list(failed_pages):
            if (max_pages is not None and out["fetched"] >= max_pages) or (budget is not None and budget.expired(reserve_s=60)):
                break
            out["retried"] += 1
            before = len(out["failed"])
            if not one(pg):
                break
            if len(out["failed"]) == before:
                out["retry_ok"] += 1
                out["failed"] = [f for f in out["failed"] if not f.startswith(f"{pg[3]}:{pg[2]}:{pg[0]}Q{pg[1]}:")]
    return out


def build_pages(quarters: list[tuple[int, int]]) -> list[tuple[int, int, str, str]]:
    """每季 × 市場（sii／otc）× 表（income／cashflow），由新到舊。"""
    return [(y, q, typek, kind) for (y, q) in quarters for typek in ("sii", "otc") for kind in ("income", "cashflow")]


def fetch_all(conn: sqlite3.Connection, symbols: list[str], *, quarters: int | None = None) -> dict:
    """evening 用：抓最近 quarters 季（預設 config.FINANCIAL_QUARTERS；all 模式 evening 傳 2）。每頁都記 financial_fetch_status。"""
    ey, eq = _latest_filed_quarter()
    qs = quarter_iter(ey, eq, quarters or config.FINANCIAL_QUARTERS)
    symset = set(symbols)
    r = fetch_pages(conn, build_pages(qs), symset, skip_done=False, retry_failed=True)
    return {"cf": r["rows"]["cashflow"], "income": r["rows"]["income"], "failed": r["failed"], "pages": r["fetched"],
            "stopped": r["stopped"]}
