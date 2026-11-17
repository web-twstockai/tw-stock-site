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
    """粗估：11 月起可能有 Q3；保守取上一完整季。"""
    t = today()
    # 2026-11 → 仍可能 Q3 申報中；用 Q2 2026 = 115/2 作為最新完整季起點
    y = t.year
    if t.month <= 3:
        return y - 1, 3
    if t.month <= 5:
        return y - 1, 4
    if t.month <= 8:
        return y, 1
    if t.month <= 11:
        return y, 2
    return y, 3


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


def fetch_all(conn: sqlite3.Connection, symbols: list[str]) -> dict:
    ey, eq = _latest_filed_quarter()
    quarters = quarter_iter(ey, eq, config.FINANCIAL_QUARTERS)
    symset = set(symbols)
    stats = {"cf": 0, "income": 0, "failed": []}
    for y, q in quarters:
        for typek in ("sii", "otc"):
            try:
                stats["cf"] += fetch_cashflow_season(conn, y, q, symset, typek)
            except FetchError as e:
                stats["failed"].append(f"cf:{typek}:{y}Q{q}:{e}")
            try:
                stats["income"] += fetch_income_season(conn, y, q, symset, typek)
            except FetchError as e:
                stats["failed"].append(f"inc:{typek}:{y}Q{q}:{e}")
    return stats
