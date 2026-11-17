"""資料存取 adapter 層 —— 網站讀資料的唯一入口。

- DataSource：網站需要的讀取介面（頁面只呼叫這些方法）。
- FakeSource：「測試資料」。內部用記憶體表模擬 docs/schema.md 的表與欄位，所有數值 is_test=True。
- SqliteSource：唯讀讀取 data/twstock.db（docs/schema.md 2026-11-10 定稿版），只讀 is_current=1 的列。

切換：環境變數 TWSITE_SOURCE=fake|sqlite（預設 fake）、TWSITE_DB=資料庫路徑。
注意：FakeSource 為了產生「看起來合理」的假資料會做簡單平均，那只是造假資料用；
正式指標與篩選一律由 analysis/screener.py 計算（見 screening.py）。
"""
from __future__ import annotations

import abc
import datetime as dt
import json
import os
import random
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .jobsummary import group_errors, summarize as summarize_errors
from .models import REVENUE_NEGATIVE_DETAIL, REVENUE_NEGATIVE_NOTE, source_note_label
from .models import (
    EVENT_CATEGORIES,
    STATUS_INSUFFICIENT,
    STATUS_ERROR,
    STATUS_FINANCIALS_MISSING,
    FINANCIALS_MISSING_LABEL,
    FINANCIALS_DEADLINE_NOTE,
    STATUS_NOT_APPLICABLE,
    STATUS_OK,
    STATUS_SOURCE_DOWN,
    STATUS_LABELS,
    STATUS_REVENUE_MISSING,
    REVENUE_DEADLINE_NOTE,
    TEST_LABEL,
    Val,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "data" / "twstock.db"
MARKET_LABEL = {"TWSE": "上市", "TPEx": "上櫃"}
JOB_STATUS_LABEL = {"success": "成功", "partial": "部分成功", "failed": "失敗", "running": "執行中"}


from . import freshness

class DataUnavailable(Exception):
    """資料庫不存在、表不存在或表是空的 —— 頁面顯示明確狀態，不回 500。"""

    LABELS = {"db_missing": "資料庫不存在", "table_missing": "資料表不存在", "table_empty": "資料表沒有資料",
              "db_error": "資料庫讀取錯誤"}

    def __init__(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        super().__init__(f"{self.LABELS.get(state, state)}：{detail}")

    @property
    def label(self) -> str:
        return self.LABELS.get(self.state, self.state)


# 來源明細面板可查的表與欄位（白名單；表名/欄位名不接受任意輸入）
SOURCE_TABLES: dict[str, dict[str, Any]] = {
    "daily_prices": {"label": "日行情", "keys": ["symbol", "price_date"], "filters": ["market", "change_sign"],
                     "date_col": "price_date", "current": True,
                     "units": "價格欄 *_twd＝新台幣元；volume_shares＝股；amount_twd＝新台幣元"},
    "monthly_revenues": {"label": "月營收", "keys": ["symbol", "revenue_month"], "filters": ["market"],
                         "date_col": "revenue_month", "current": True,
                         "units": "*_twd＝新台幣元（來源仟元×1000）；*_pct＝%（來源值）"},
    "income_statements": {"label": "綜合損益", "keys": ["symbol", "fiscal_year", "fiscal_quarter", "industry_form"],
                          "filters": [], "date_col": "fiscal_year", "current": True,
                          "units": "*_twd＝新台幣元（來源仟元×1000）；eps_twd＝元/股"},
    "cash_flow_statements": {"label": "現金流量", "keys": ["symbol", "fiscal_year", "fiscal_quarter"], "filters": [],
                             "date_col": "fiscal_year", "current": True, "units": "*_twd＝新台幣元（來源仟元×1000）"},
    "material_infos": {"label": "重大訊息", "keys": ["symbol", "announce_date", "announce_time", "content_hash"],
                       "filters": [], "date_col": "announce_date", "current": True, "units": "—"},
    "securities": {"label": "普通股清單", "keys": ["symbol"], "filters": ["market"], "date_col": "data_date",
                   "current": True, "units": "—"},
    "securities_excluded": {"label": "排除名單", "keys": ["symbol", "market", "data_date", "exclude_reason"],
                            "filters": [], "date_col": "data_date", "current": False, "units": "—"},
    "job_runs": {"label": "工作紀錄", "keys": ["run_id"], "filters": ["job_name", "status", "data_date"],
                 "date_col": "data_date", "current": False, "units": "—"},
    "source_status": {"label": "來源狀態", "keys": ["source_id"], "filters": ["ok"], "date_col": "last_data_date",
                      "current": False, "units": "—"},
}
# --- pipeline/schema.sql（＋ migrate.sql 補的 index_daily、trading_halts）的其他表；2026-11-10 04:2x 對照真實 DB ---
SOURCE_TABLES.update({
    "corporate_actions": {"label": "除權息／減資", "keys": ["symbol", "action_date", "action_type"], "filters": [],
                          "date_col": "action_date", "current": True,
                          "units": "*_twd＝新台幣元；stock_dividend_per_thousand＝每千股配股數；ratio＝比例"},
    "corporate_actions_coverage": {"label": "除權息／減資覆蓋起訖", "keys": ["market", "action_kind"],
                                   "filters": ["source"], "date_col": "coverage_start", "current": False, "units": "—"},
    "index_daily": {"label": "指數日行情", "keys": ["index_id", "price_date"], "filters": [],
                    "date_col": "price_date", "current": True, "units": "close／change＝點；change_pct＝%（來源有才存）"},
    "trading_halts": {"label": "暫停交易", "keys": ["market", "symbol", "halt_date"], "filters": ["halt_type"],
                      "date_col": "halt_date", "current": True, "units": "—"},
    "news_items": {"label": "新聞", "keys": ["news_id"], "filters": ["category", "event_key"],
                   "date_col": "published_at", "current": True, "units": "—"},
    "news_item_symbols": {"label": "新聞—公司關聯", "keys": ["news_id", "symbol"], "filters": [],
                          "date_col": "", "current": False, "units": "—"},
    "screen_results": {"label": "篩選結果存檔", "keys": ["as_of", "symbol", "params_hash"], "filters": ["status"],
                       "date_col": "as_of", "current": True, "units": "依 analysis 輸出（*_twd＝元、*_shares＝股）"},
    "daily_summaries": {"label": "每日摘要存檔", "keys": ["summary_date", "summary_kind", "params_hash"],
                        "filters": ["data_date"], "date_col": "summary_date", "current": True, "units": "—"},
})
# 網站讀取的欄位與真實結構（pipeline/schema.sql／data/twstock.db）的對照；不再有「網站假設」的欄位名
SCHEMA_NOTES = {
    "income_statements／cash_flow_statements.report_basis": "合併／個別（真實 DB 目前只有 合併）",
    "income_statements／cash_flow_statements.period_type": "單季／累計／年度（真實 DB 目前只有 累計）",
    "income_statements／cash_flow_statements.derived＋derivation_method": "1＝推算值",
    "daily_prices.change": "帶正負號漲跌價差（缺值退回 change_twd＋change_sign）；is_halted 由 analysis 讀",
    "material_infos.url": "原文連結（schema.sql 未列，pipeline/db.py 補欄；空值退回 source）",
    "index_daily": "index_id（TAIEX／TPEX）, index_name, price_date, close, change, change_pct（來源無則 NULL）",
    "news_items＋news_item_symbols": "news_items 不含代號；個股頁經 news_item_symbols(news_id, symbol, evidence) JOIN；category 為中文四類",
    "daily_summaries": "summary_date（＝data_date）, generated_at, provider, text, facts_json, review_status, is_current",
    "corporate_actions_coverage": "market, action_kind（類別以 analysis/adjust.py COVERAGE_KINDS 為準）, coverage_start, coverage_end",
}
ASSUMED = SCHEMA_NOTES   # 舊名相容
# schema.sql 已有、但網站目前不讀的表（第二階段再接）
PENDING_TABLES = ["news_events", "news_event_symbols"]
MATERIAL_URL_COLS = ["url", "original_url", "source_url"]
# news_items.category（pipeline/schema.sql 中文四類）→ 網站分類鍵
NEWS_CATEGORY_KEYS = {"官方已公告事實": "official", "媒體報導": "media", "社群未證實訊息": "social", "AI分析推論": "ai",
                      "官方": "official", "媒體": "media", "社群未證實": "social", "AI推論": "ai"}
BASIS_ALIASES = {"consolidated": ["consolidated", "合併", "C"], "individual": ["individual", "個別", "standalone", "I"]}
PERIOD_ALIASES = {"single": ["single", "quarter", "單季", "Q"], "cumulative": ["cumulative", "ytd", "YTD", "累計"],
                  "annual": ["annual", "年度", "cumulative", "ytd", "YTD", "累計"]}
INDEX_NAMES = {"TAIEX": "加權指數", "TPEX": "櫃買指數"}
AGGREGATES = {"count": "count(*)", "sum:amount_twd": "sum(amount_twd)"}


def _coverage_kinds() -> tuple:
    """strict 覆蓋必須涵蓋的類別：讀 analysis/adjust.py COVERAGE_KINDS（研究分析定義），讀不到用舊兩類。"""
    try:
        from .screening import analysis_module
        return tuple(analysis_module("adjust").COVERAGE_KINDS)
    except Exception:
        return ("dividend", "capital_reduction")


def source_href(table: str, agg: str = "", **key) -> str:
    from urllib.parse import urlencode
    q = {"table": table, **{k: v for k, v in key.items()}}
    if agg:
        q["agg"] = agg
    return "/source?" + urlencode(q)


def price_direction(r: dict) -> int:
    """漲跌方向：有 daily_prices.change（暫定，帶正負號）就用它，否則用 change_sign。
    官方「X 無比價」（change_sign='X'）不算漲、跌，也不是平盤（見 price_class）。"""
    if (r.get("change_sign") or "") == "X":
        return 0
    if "change" in r:
        c = r.get("change") or 0
        return (c > 0) - (c < 0)
    return {"+": 1, "-": -1}.get(r.get("change_sign") or "", 0)


def price_class(r: dict) -> str:
    """首頁家數分類：up／down／flat／nocompare（官方 X 無比價）／notrade（無收盤價）。只依官方漲跌與符號，不自算。"""
    if r.get("close_twd") is None and "close_twd" in r:
        return "notrade"
    if (r.get("change_sign") or "") == "X":
        return "nocompare"
    d = price_direction(r)
    return "up" if d > 0 else "down" if d < 0 else "flat"


def _key_text(key: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in key.items())


class DataSource(abc.ABC):
    """網站讀取介面。回傳的數字一律是 Val（含來源、資料日期、狀態）。"""

    is_test: bool = False
    name: str = ""

    @abc.abstractmethod
    def meta(self) -> dict: ...
    @abc.abstractmethod
    def list_stocks(self) -> list[dict]: ...
    @abc.abstractmethod
    def get_stock(self, code: str) -> Optional[dict]: ...
    @abc.abstractmethod
    def price_history(self, code: str, days: int = 120) -> dict: ...
    @abc.abstractmethod
    def monthly_revenue(self, code: str, months: int = 24) -> dict: ...
    @abc.abstractmethod
    def financials(self, code: str, period: str, scope: str, basis: str) -> dict: ...
    @abc.abstractmethod
    def events(self, code: Optional[str] = None, days: int = 30, category: Optional[str] = None) -> list[dict]: ...
    @abc.abstractmethod
    def source_status(self) -> list[dict]: ...
    @abc.abstractmethod
    def job_runs(self, limit: int = 20) -> list[dict]: ...
    @abc.abstractmethod
    def excluded_securities(self) -> list[dict]:
        """securities_excluded（非普通股）最新一份清單。"""
    @abc.abstractmethod
    def screener_frame(self) -> dict:
        """給 analysis/screener.py 的輸入：{"daily": DataFrame, "trading_dates": [...] | None, "as_of": str}。"""
    @abc.abstractmethod
    def get_source_rows(self, table: str, filters: dict, limit: int = 200) -> list[dict]:
        """來源明細面板：依白名單表與主鍵/過濾欄位取回原始列（只取現行列）。"""
    def index_quotes(self, data_date: str) -> list[dict]:
        """index_daily（暫定）當日各指數；表不存在則 []。"""
        try:
            return self.get_source_rows("index_daily", {"price_date": data_date}, limit=20)
        except (DataUnavailable, KeyError):
            return []

    def coverage(self) -> Optional[list[dict]]:
        """corporate_actions_coverage（暫定）；表不存在回 None。"""
        try:
            return self.get_source_rows("corporate_actions_coverage", {}, limit=100) or None
        except (DataUnavailable, KeyError):
            return None

    def get_daily_summary(self) -> dict:
        """每日摘要：數值由程式從資料表計算（app/summary.py），解釋文字由 summary_narrative() 提供。"""
        from .summary import build_daily_summary
        return build_daily_summary(self)

    def universe_stocks(self) -> list[dict]:
        """追蹤範圍內的股票（搜尋、自動完成清單、個股頁共用；範圍外的股票不列出、不開頁）。全市場＝list_stocks()。"""
        uni = self.universe_symbols()
        rows = self.list_stocks()
        return rows if uni is None else [s for s in rows if s["code"] in uni]

    def in_universe(self, code: str) -> bool:
        uni = self.universe_symbols()
        return uni is None or code in uni

    def search(self, q: str) -> list[dict]:
        q = (q or "").strip()
        if not q:
            return []
        ql = q.lower()
        return [s for s in self.universe_stocks() if s["code"].startswith(q) or ql in s["name"].lower()]

    # ---- 選股追蹤範圍（app/universe.py；TWSITE_UNIVERSE）----
    _universe = None

    @property
    def universe(self):
        if self._universe is None:
            from .universe import resolve
            self._universe = resolve()
        return self._universe

    @universe.setter
    def universe(self, u) -> None:
        self._universe = u

    # ---- HANDOFF ①②：來源分類與過期判斷（規則在 app/freshness.py）----
    def _fresh_now(self) -> dt.datetime:
        return dt.datetime.now(freshness.TZ)

    def _trade_days(self) -> list[str]:
        return []

    def _latest_for(self, category: str, market: Optional[str]) -> Optional[str]:
        """資料表中該類別（與市場）的最新資料日；子類別實作。"""
        return None

    @staticmethod
    def _source_market(source_id: str) -> Optional[str]:
        sid = (source_id or "").lower()
        if "twse" in sid or "_sii" in sid:
            return "TWSE"
        if "tpex" in sid or "_otc" in sid:
            return "TPEx"
        return None

    def _enrich_status(self, raw: list[dict]) -> list[dict]:
        now, days, out, cache = self._fresh_now(), None, [], {}
        for r in raw:
            v = self._status_view(r)
            cat = freshness.source_category(r["source_id"])
            if cat in ("quotes", "revenue", "financials") and days is None:
                days = self._trade_days()
            mk = self._source_market(r["source_id"])
            if (cat, mk) not in cache:
                try:
                    cache[(cat, mk)] = self._latest_for(cat, mk)
                except Exception:
                    cache[(cat, mk)] = None
            v.update(freshness.judge(cat, now, days or [], cache[(cat, mk)], r.get("last_data_date")))
            v["market"] = mk or ""
            v["last_success_short"] = freshness.fmt_time(r.get("last_success_at")) or "從未成功"
            out.append(v)
        return out

    # ---- HANDOFF ③：月營收分母 ----
    def universe_symbols(self) -> Optional[set]:
        """追蹤範圍代號集合；None＝全市場。"""
        syms = self.universe.symbols
        return None if syms is None else set(syms)

    def revenue_universe(self, month: str) -> dict:
        """首頁「已有營收 N／M」：M＝追蹤範圍檔數（app/universe.py；all＝現行普通股數），
        N＝追蹤範圍內有該月現行 monthly_revenues 列的公司數（與 /source?…&universe=1 的 count 算式一致）。"""
        u = self.universe
        uni = self.universe_symbols()
        try:
            rows = self.get_source_rows("monthly_revenues", {"revenue_month": month}, limit=1_000_000)
        except (DataUnavailable, KeyError):
            rows = None
        total = len(uni) if uni is not None else len(self.list_stocks())
        have = None if rows is None else len({r["symbol"] for r in rows if uni is None or r["symbol"] in uni})
        return {"count": total, "have": have, "label": u.label,
                "note": f"{u.text}（{u.source}）；分子＝範圍內有 {month} 現行月營收列的公司數"}

    # ---- HANDOFF ⑥：指數近 N 日序列 ----
    def index_series(self, index_id: str, days: int = 20) -> dict:
        """近 N 個交易日收盤序列（不補值、不內插）。status：ok／insufficient（點數不足或表不存在）／
        error（讀取失敗；附 note，不拋例外，HANDOFF §14.1）。"""
        base = {"index_id": index_id, "name": index_id, "points": []}
        try:
            rows = self.get_source_rows("index_daily", {"index_id": index_id}, limit=100_000)
        except (DataUnavailable, KeyError):
            return {**base, "status": STATUS_INSUFFICIENT, "note": "index_daily 表不存在"}
        except Exception as e:   # sqlite 錯誤、DB 鎖定等
            return {**base, "status": STATUS_ERROR, "note": f"讀取失敗：{type(e).__name__}: {e}"}
        try:
            rows = sorted((r for r in rows if r.get("close") is not None), key=lambda r: r["price_date"])[-days:]
            pts = [{"date": r["price_date"], "close": float(r["close"]),
                    "change": r.get("change"), "source": r.get("source") or "",
                    "fetched_at": r.get("fetched_at") or "",
                    "href": source_href("index_daily", index_id=index_id, price_date=r["price_date"])} for r in rows]
        except (TypeError, ValueError, KeyError) as e:
            return {**base, "status": STATUS_ERROR, "note": f"讀取失敗：資料格式錯誤（{type(e).__name__}: {e}）"}
        name = (rows[-1].get("index_name") if rows else None) or index_id
        st = STATUS_OK if len(pts) >= days else STATUS_INSUFFICIENT
        note = "" if st == STATUS_OK else (f"只有 {len(pts)} 個交易日（需要 {days}）" if pts
                                           else f"index_daily 沒有 {index_id} 的現行列")
        return {"index_id": index_id, "name": name, "points": pts, "status": st, "note": note}
    # ---- 共用轉換（Fake 與 Sqlite 都用 schema 欄位，畫面轉換集中在這裡）----
    @staticmethod
    def _status_view(r: dict) -> dict:
        # status_code（HANDOFF §15.2）：ok／insufficient／source_down；中文 status 保留供過渡期模板使用
        if r.get("ok"):
            code = STATUS_OK
        elif not r.get("last_attempt_at"):
            code = STATUS_INSUFFICIENT
        else:
            code = STATUS_SOURCE_DOWN
        st = STATUS_LABELS[code]
        return dict(id=r["source_id"], name=r.get("notes_name") or r["source_id"], url=r.get("source_url") or "",
                    status=st, status_code=code, last_success=r.get("last_success_at") or "從未成功",
                    last_attempt=r.get("last_attempt_at") or "—", http=r.get("last_http_status"),
                    data_date=r.get("last_data_date") or "—",
                    missing=source_note_label(r.get("notes"))["label"] or "—",   # 顯示層：代碼→中文（models）
                    notes_raw=r.get("notes") or "", notes_view=source_note_label(r.get("notes")),
                    errors=[r["last_error"]] if r.get("last_error") else [],
                    href=source_href("source_status", source_id=r["source_id"]))

    @staticmethod
    def _job_view(r: dict) -> dict:
        def arr(x):
            if not x:
                return []
            try:
                v = json.loads(x)
                return v if isinstance(v, list) else [str(v)]
            except (TypeError, ValueError):
                return [str(x)]
        return dict(run_id=r["run_id"], job_name=r["job_name"], started_at=r["started_at"],
                    finished_at=r.get("finished_at") or "—", status=r["status"],
                    status_label=JOB_STATUS_LABEL.get(r["status"], r["status"]),
                    data_date=r.get("data_date") or "—", scope=r.get("symbols_scope") or "—",
                    success_items=arr(r.get("success_items")), failed_items=arr(r.get("failed_items")),
                    error_summary=summarize_errors(r.get("error_summary") or ""),
                    error_summary_raw=r.get("error_summary") or "",
                    error_groups=group_errors(r.get("error_summary") or ""), retry_count=r.get("retry_count") or 0,
                    href=source_href("job_runs", run_id=r["run_id"]))

    def _meta_from(self, jobs: list[dict], data_date: Optional[str], completed: Optional[dict] = None) -> dict:
        """completed：最後一筆 finished_at 非空的 job（HANDOFF §16.3「上次完成時間」；工作執行中也能顯示）。"""
        last = jobs[0] if jobs else None
        if completed is None:
            completed = next((j for j in jobs if j.get("finished_at")), None)
        return {
            "last_completed_at": (completed.get("finished_at") or "") if completed else "",
            "last_completed_job": (completed.get("job_name") or "") if completed else "",
            "last_completed_status": (JOB_STATUS_LABEL.get(completed["status"], completed["status"])
                                      if completed else ""),
            "last_completed_run_id": completed.get("run_id") if completed else None,
            "update_detail_raw": (last.get("error_summary") or "") if last else "",
            "is_test": self.is_test, "source_name": self.name,
            "data_date": data_date or "資料不足",
            "update_status": JOB_STATUS_LABEL.get(last["status"], last["status"]) if last else "無工作紀錄",
            "update_detail": summarize_errors(last.get("error_summary") or "") if last else "job_runs 沒有紀錄",
            "last_run_started": last["started_at"] if last else "—",
            "last_run_finished": (last.get("finished_at") or "—") if last else "—",
            "timezone": "Asia/Taipei", "unavailable": "",
        }


# ----------------------------------------------------------------------------
# FakeSource：測試資料（表結構模擬 docs/schema.md）
# ----------------------------------------------------------------------------
_SAMPLE = [
    ("2330", "台積電", "TWSE", "半導體業"), ("2317", "鴻海", "TWSE", "其他電子業"),
    ("2454", "聯發科", "TWSE", "半導體業"), ("2308", "台達電", "TWSE", "電子零組件業"),
    ("2412", "中華電", "TWSE", "通信網路業"), ("2881", "富邦金", "TWSE", "金融保險業"),
    ("2882", "國泰金", "TWSE", "金融保險業"), ("2801", "彰銀", "TWSE", "金融保險業"),
    ("1301", "台塑", "TWSE", "塑膠工業"), ("6505", "台塑化", "TWSE", "油電燃氣業"),
    ("2002", "中鋼", "TWSE", "鋼鐵工業"), ("1101", "台泥", "TWSE", "水泥工業"),
    ("1216", "統一", "TWSE", "食品工業"), ("2912", "統一超", "TWSE", "貿易百貨業"),
    ("2207", "和泰車", "TWSE", "汽車工業"), ("2603", "長榮", "TWSE", "航運業"),
    ("3008", "大立光", "TWSE", "光電業"), ("2357", "華碩", "TWSE", "電腦及週邊設備業"),
    ("3711", "日月光投控", "TWSE", "半導體業"), ("9910", "豐泰", "TWSE", "其他業"),
    ("6488", "環球晶", "TPEx", "半導體業"), ("5347", "世界", "TPEx", "半導體業"),
    ("8299", "群聯", "TPEx", "半導體業"), ("3105", "穩懋", "TPEx", "半導體業"),
    ("3293", "鈊象", "TPEx", "文化創意業"), ("8069", "元太", "TPEx", "光電業"),
    ("5483", "中美晶", "TPEx", "半導體業"), ("5274", "信驊", "TPEx", "半導體業"),
    ("6547", "高端疫苗", "TPEx", "生技醫療業"), ("4105", "東洋", "TPEx", "生技醫療業"),
]
_BASE_PRICE = {
    "2330": 1050, "2317": 210, "2454": 1320, "2308": 420, "2412": 128, "2881": 92, "2882": 68,
    "2801": 21, "1301": 45, "6505": 38, "2002": 22, "1101": 31, "1216": 80, "2912": 255,
    "2207": 520, "2603": 205, "3008": 2450, "2357": 610, "3711": 160, "9910": 215, "6488": 430,
    "5347": 105, "8299": 980, "3105": 145, "3293": 1150, "8069": 245, "5483": 120, "5274": 4100,
    "6547": 55, "4105": 62,
}
_FORCED_PASS = {"2330", "2454", "3008", "8299", "3293"}
_SUSPENDED = {"6547"}
_HALTED_LAST = {"8069"}    # 最新交易日停牌且 daily_prices.is_halted=1 → analysis：EXCL_SUSPENDED（確認停牌）
_MISSING_LAST = {"5483"}   # 最新交易日沒有資料列 → analysis：EXCL_NO_ROW_SUSPENDED_OR_MISSING（停牌或資料缺漏，待確認）
_SRC_MISMATCH = {"2330": 5.0}
_MIXED_SOURCE = {"6488": 60}   # 測試資料：較早 60 日來自 dailyQuotes、其後 tradingStock（混合來源示範）
_UNKNOWN_SOURCE = {"4105"}     # 測試資料：來源不在對照表 → 口徑未知
# K 線示範（HANDOFF §18.3-1；只進個股 K 線／均線輸入，不進 screener_frame，不影響選股與 sqlite）
_KLINE_FAILED_EVENT = {"9910": 30}   # 倒數第 30 個交易日一筆缺因子的現金股利 → status=failed、之前不畫還原價
_KLINE_NO_ADJ = {"1301"}             # 此股覆蓋表缺漏 → 還原價整段不可用（all_unavailable）→ K 線改顯示原始價


def _fake_px_source(code: str, market: str, i: int) -> str:
    """測試資料的 daily_prices.source：依真實端點 URL 格式（標測試，非真實抓取）。"""
    if code in _UNKNOWN_SOURCE:
        return "測試資料：未登錄來源（非真實 URL）"
    if market == "TWSE":
        return f"測試資料 https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&stockNo={code}"
    if code in _MIXED_SOURCE and i < _MIXED_SOURCE[code]:
        return "測試資料 https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?response=json"
    return f"測試資料 https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={code}&response=json"

_SHORT_HISTORY = {"4105"}
_FINANCIAL = {"2881", "2882", "2801"}
_REV_NOT_YET = {"2207", "9910", "4105"}
_Q_NOT_YET = {"2317", "2603", "6488"}

FAKE_DATA_DATE = dt.date(2026, 11, 9)
FAKE_FETCHED = "2026-11-09T18:02:11+08:00"
FAKE_SRC = "測試資料（FakeSource，非真實 URL）"


def _trading_days(end: dt.date, n: int) -> list[dt.date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def _month_add(y: int, m: int, k: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1


class FakeSource(DataSource):
    is_test = True
    name = "FakeSource（測試資料）"
    SRC = TEST_LABEL

    def __init__(self) -> None:
        self._stocks = [dict(code=c, name=n, market=MARKET_LABEL[m], market_code=m, industry=i)
                        for c, n, m, i in _SAMPLE]
        self._by_code = {s["code"]: s for s in self._stocks}
        self._price_cache: dict[str, list[dict]] = {}
        self._tables_cache: Optional[dict[str, list[dict]]] = None

    def _v(self, value, unit="", as_of="", status=STATUS_OK, note="", last_success="", digits=2, href="",
           ref=None) -> Val:
        return Val(value=value, unit=unit, source=self.SRC, as_of=as_of, status=status, note=note,
                   last_success=last_success, is_test=True, digits=digits, href=href, ref=ref)

    # ---- 記憶體表（schema 欄位）----
    def _meta_cols(self, unit="n/a", source_unit=None, conv=None):
        return dict(source=FAKE_SRC, announced_at=None, announced_at_note="測試資料", fetched_at=FAKE_FETCHED,
                    unit=unit, source_unit=source_unit, unit_conversion=conv, version=1, is_current=1,
                    checksum=None)

    def _tables(self) -> dict[str, list[dict]]:
        if self._tables_cache is not None:
            return self._tables_cache
        t: dict[str, list[dict]] = {k: [] for k in SOURCE_TABLES}
        for s in self._stocks:
            code = s["code"]
            t["securities"].append(dict(symbol=code, market=s["market_code"], name=s["name"], industry=s["industry"],
                                        cfi_code="ESVUFR", is_common=1, data_date=FAKE_DATA_DATE.isoformat(),
                                        **self._meta_cols()))
            prev = None
            ser = self._series(code)
            for i_r, r in enumerate(ser):
                chg = None if prev is None or r["volume"] == 0 else round(r["close"] - prev, 2)
                if chg is not None and i_r == len(ser) - 1 and code in _SRC_MISMATCH:
                    chg = round(chg + _SRC_MISMATCH[code], 2)
                sign = "" if not chg else ("+" if chg > 0 else "-")
                t["daily_prices"].append(dict(
                    symbol=code, market=s["market_code"], price_date=r["date"], open_twd=r["open"],
                    high_twd=r["high"], low_twd=r["low"], close_twd=r["close"] if r["volume"] else None,
                    change_twd=abs(chg) if chg else 0.0, change_sign=sign, change=chg, volume_shares=r["volume"],
                    is_halted=1 if (code in _HALTED_LAST and i_r == len(ser) - 1) else 0,
                    px_source=_fake_px_source(code, s["market_code"], i_r),
                    amount_twd=round(r["close"] * r["volume"]),
                    **self._meta_cols("volume=股;amount=新台幣元",
                                      "股;元" if s["market_code"] == "TWSE" else "張;仟元",
                                      None if s["market_code"] == "TWSE" else "shares=lots*1000; amount=thousand_twd*1000")))
                dp = t["daily_prices"][-1]
                dp["source"] = dp.pop("px_source")
                prev = r["close"]
            revs, not_yet = self._rev_series(code)
            for (y, m), v in sorted(revs.items()):
                if not_yet and (y, m) == (2026, 10):
                    continue
                py, pm = _month_add(y, m, -1)
                t["monthly_revenues"].append(dict(
                    symbol=code, market=s["market_code"], revenue_month=f"{y}-{m:02d}", revenue_twd=v,
                    revenue_prev_month_twd=revs.get((py, pm)), revenue_prev_year_twd=revs.get((y - 1, m)),
                    mom_pct=round((v / revs[(py, pm)] - 1) * 100, 2) if (py, pm) in revs else None,
                    yoy_pct=round((v / revs[(y - 1, m)] - 1) * 100, 2) if (y - 1, m) in revs else None,
                    industry=s["industry"], remark=None,
                    **self._meta_cols("amount=新台幣元", "新台幣仟元", "*1000")))
        for e in self.events(days=30, category="official"):
            d, tm = e["published_at"].split(" ")
            t["material_infos"].append(dict(symbol=e["code"], market=self._by_code[e["code"]]["market_code"],
                                            announce_date=d, announce_time=tm + ":00", title=e["title"],
                                            clause="（測試資料）", content_hash=f"fake{e['id']}",
                                            url=f"/source?table=material_infos&symbol={e['code']}&announce_date={d}",
                                            **self._meta_cols()))
        for e in self.events(days=30):
            if e["category"] == "official":
                continue
            t["news_items"].append(dict(news_id=f"fake-{e['id']}", symbol=e["code"], title=e["title"],
                                        url="", publisher="（測試媒體）" if e["category"] == "media" else "（測試社群）",
                                        published_at=e["published_at"], category=e["category"], summary=e["summary"],
                                        impact=e["impact"], pending=e["pending"], evidence=e["evidence"],
                                        **self._meta_cols()))
        rnd = random.Random(42)
        for iid, base in (("TAIEX", 27500.0), ("TPEX", 265.0)):
            px = base
            for d in _trading_days(FAKE_DATA_DATE, 30):
                chg = round(px * rnd.gauss(0.001, 0.008), 2)
                t["index_daily"].append(dict(index_id=iid, index_name=INDEX_NAMES[iid], price_date=d.isoformat(),
                                             close=round(px + chg, 2), change=chg, change_pct=round(chg / px * 100, 2),
                                             **self._meta_cols("點")))
                px += chg
        # daily_summaries：存檔示範列在 get_source_rows() 第一次讀取時建立（需先有篩選結果）
        t["corporate_actions_coverage"] = [dict(market=m, action_kind=k, coverage_start="2000-01-01（測試資料）")
                                           for m in ("TWSE", "TPEx") for k in _coverage_kinds()]
        t["securities_excluded"] = [
            dict(symbol="0050", market="TWSE", name="元大台灣50", cfi_code="CEOGEU", section="ETF",
                 exclude_reason="ETF", data_date=FAKE_DATA_DATE.isoformat(), source=FAKE_SRC,
                 fetched_at=FAKE_FETCHED, version=1),
            dict(symbol="2881A", market="TWSE", name="富邦特", cfi_code="EPNRAR", section="特別股",
                 exclude_reason="特別股", data_date=FAKE_DATA_DATE.isoformat(), source=FAKE_SRC,
                 fetched_at=FAKE_FETCHED, version=1),
            dict(symbol="030001", market="TWSE", name="測試權證", cfi_code="RWSCCA", section="權證",
                 exclude_reason="CFI=RWSCCA 權證", data_date=FAKE_DATA_DATE.isoformat(), source=FAKE_SRC,
                 fetched_at=FAKE_FETCHED, version=1),
        ]
        t["job_runs"] = [
            dict(run_id=102, job_name="evening", started_at="2026-11-09T22:00:03+08:00",
                 finished_at="2026-11-09T22:09:10+08:00", status="partial", data_date="2026-11-09",
                 symbols_scope="sample30", success_items='["material_infos","income_statements"]',
                 failed_items='["monthly_revenues"]',
                 error_summary="測試資料：公開資訊觀測站 月營收 HTTP 503，重試 3 次失敗，保留 2026-11-08 版本",
                 retry_count=3, host_note="測試資料"),
            dict(run_id=101, job_name="daily", started_at="2026-11-09T18:00:05+08:00",
                 finished_at="2026-11-09T18:07:41+08:00", status="success", data_date="2026-11-09",
                 symbols_scope="sample30", success_items='["daily_prices","securities"]', failed_items="[]",
                 error_summary="", retry_count=0, host_note="測試資料"),
        ]
        def ss(sid, name, ok, succ, att, http, dd, err, notes):
            return dict(source_id=sid, notes_name=name, source_url="（測試資料）", last_success_at=succ,
                        last_attempt_at=att, last_http_status=http, last_data_date=dd, last_error=err, ok=ok,
                        notes=notes, updated_at=att or FAKE_FETCHED)
        t["source_status"] = [
            ss("twse_mi_index", "證交所 MI_INDEX（上市日行情）", 1, "2026-11-09T18:02:11+08:00",
               "2026-11-09T18:02:11+08:00", 200, "2026-11-09", None, "無缺漏"),
            ss("tpex_daily_quotes", "櫃買中心 上櫃日收盤行情", 1, "2026-11-09T18:03:40+08:00",
               "2026-11-09T18:03:40+08:00", 200, "2026-11-09", None, "無缺漏"),
            ss("mops_monthly_revenue", "公開資訊觀測站 月營收", 0, "2026-11-08T22:04:55+08:00",
               "2026-11-09T22:00:03+08:00", 503, "2026-10",
               "2026-11-09 22:00:03 HTTP 503（測試資料），已重試 3 次失敗，保留 2026-11-08 版本",
               "2026-10 月營收（部分公司）"),
            ss("mops_financials", "公開資訊觀測站 財報", 1, "2026-11-09T22:06:12+08:00",
               "2026-11-09T22:06:12+08:00", 200, "2026Q2", None, "2026Q3 多數公司財報尚未入庫（法定期限 11/14）"),
            ss("mops_material_info", "公開資訊觀測站 重大訊息", 1, "2026-11-09T22:07:30+08:00",
               "2026-11-09T22:07:30+08:00", 200, "2026-11-09", None, "無缺漏"),
            ss("news", "相關新聞", 0, None, None, None, None, None, "新聞來源尚未選定"),
        ]
        self._tables_cache = t
        return t

    def kline_demo_inputs(self, code: str, kw: dict) -> dict:
        """K 線示範案例（測試資料）：9910 一筆無法還原的事件、1301 沒有還原價。只供測試與截圖。"""
        import pandas as pd
        if code in _KLINE_FAILED_EVENT:
            days = sorted(kw["prices"]["price_date"])
            d = days[-_KLINE_FAILED_EVENT[code]]
            kw = dict(kw, actions=pd.DataFrame([{"symbol": code, "market": self._by_code[code]["market_code"],
                                                 "action_date": d, "action_type": "cash_dividend",
                                                 "is_current": 1}]))   # 數值欄全空 → analysis 判 failed
        if code in _KLINE_NO_ADJ:
            kw = dict(kw, actions_coverage={})   # 覆蓋表缺漏 → EXCL_ADJ_COVERAGE_GAP、全段不可用
        return kw

    archive_summary = True   # 測試可關閉，模擬「當日無存檔」

    def _ensure_summary_archive(self) -> None:
        """測試資料的 daily_summaries 存檔示範：以 analysis/narrative.py 產生一次並存在記憶體（模擬研究分析已寫入）。"""
        t = self._tables()
        if t["daily_summaries"] or not self.archive_summary or getattr(self, "_archiving", False):
            return
        self._archiving = True
        try:
            from .models import ScreenParams
            from .screening import run_screen
            from .summary import live_narrative
            dd = FAKE_DATA_DATE.isoformat()
            idx = [r for r in t["index_daily"] if r["price_date"] == dd]
            n, err, _code = live_narrative(self, run_screen(self, ScreenParams()), dd, idx)
            if n is not None:
                t["daily_summaries"].append(dict(
                    summary_date=dd, summary_kind="post_market", review_status="not_required", is_current=1,
                    data_date=dd, generated_at="2026-11-09T18:30:00+08:00", provider="研究分析（測試存檔示範）",
                    screen_run_id=101, generation_method=n.generation_method, text=n.text, facts_json=n.facts_json))
        finally:
            self._archiving = False

    def get_source_rows(self, table: str, filters: dict, limit: int = 200) -> list[dict]:
        if table == "daily_summaries":
            self._ensure_summary_archive()
        rows = self._tables().get(table, [])
        out = [r for r in rows if all(str(r.get(k) if r.get(k) is not None else "") == str(v) for k, v in filters.items())]
        return out[:limit]

    # ---- 基本資料 ----
    def meta(self) -> dict:
        m = self._meta_from(self._tables()["job_runs"], FAKE_DATA_DATE.isoformat())
        m["update_detail"] = "測試資料：示範「公開資訊觀測站 月營收」來源故障，其餘成功。"
        return m

    def list_stocks(self) -> list[dict]:
        return list(self._stocks)

    def get_stock(self, code: str) -> Optional[dict]:
        return self._by_code.get(code)

    # ---- 行情 ----
    def _series(self, code: str) -> list[dict]:
        if code in self._price_cache:
            return self._price_cache[code]
        rnd = random.Random(int(code) * 7919)
        n = 45 if code in _SHORT_HISTORY else 160
        days = _trading_days(FAKE_DATA_DATE, n)
        px = float(_BASE_PRICE.get(code, 100))
        base_vol = rnd.randint(3_000, 40_000) * 1000
        rows = []
        for i, d in enumerate(days):
            last = i == n - 1
            drift = 0.004 if (code in _FORCED_PASS and i > n - 40) else 0.0
            chg = 0.045 if (code in _FORCED_PASS and last) else rnd.gauss(drift, 0.015)
            o = px * (1 + rnd.gauss(0, 0.004))
            c = max(1.0, px * (1 + chg))
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.006)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.006)))
            vol = int(base_vol * max(0.3, rnd.gauss(1, 0.25)))
            if code in _FORCED_PASS and last:
                vol, h = int(base_vol * 2.4), c * 1.003
            if (code in _SUSPENDED and i >= n - 3) or (code in _HALTED_LAST and last):
                o = h = l = c = px
                vol = 0
            rows.append(dict(date=d.isoformat(), open=round(o, 2), high=round(h, 2), low=round(l, 2),
                             close=round(c, 2), volume=vol))
            px = c
        if code in _MISSING_LAST:
            rows = rows[:-1]
        self._price_cache[code] = rows
        return rows

    @staticmethod
    def _fake_ma(closes: list[float], n: int) -> list[Optional[float]]:
        # 僅供假資料圖表；正式均線由 analysis/ 提供
        return [round(sum(closes[i - n + 1:i + 1]) / n, 2) if i + 1 >= n else None for i in range(len(closes))]

    def price_history(self, code: str, days: int = 120) -> dict:
        rows = self._series(code)
        closes = [r["close"] for r in rows]
        ma20, ma60 = self._fake_ma(closes, 20), self._fake_ma(closes, 60)
        sl = slice(max(0, len(rows) - days), len(rows))
        notes = []
        if code in _SHORT_HISTORY:
            notes.append("資料不足：上市櫃交易日數少於 60 日，MA60 無法計算。")
        if code in _SUSPENDED:
            notes.append("最近 3 個交易日無成交（測試資料），不列入選股計算。")
        if code in _HALTED_LAST:
            notes.append("最新交易日停牌（測試資料：daily_prices.is_halted=1）。")
        if code in _MISSING_LAST:
            notes.append("最新交易日沒有資料列（測試資料）：停牌或資料缺漏，待確認。")
        chg = [None] + [round(b["close"] - a["close"], 2) if b["volume"] else None for a, b in zip(rows, rows[1:])]
        chg_pct = [None] + [round((b["close"] / a["close"] - 1) * 100, 2) if b["volume"] else None
                            for a, b in zip(rows, rows[1:])]
        if code in _SRC_MISMATCH and chg[-1] is not None:   # 與 daily_prices 假表一致（官方漲跌刻意偏差）
            chg[-1] = round(chg[-1] + _SRC_MISMATCH[code], 2)
        mk = next((x["market_code"] for x in self._stocks if x["code"] == code), "TWSE")
        srcs = [(r["date"], _fake_px_source(code, mk, i)) for i, r in enumerate(rows)][sl]
        return {"rows": rows[sl], "row_sources": srcs, "ma20": ma20[sl], "ma60": ma60[sl], "change": chg[sl], "change_pct": chg_pct[sl],
                "change_note": "", "change_pct_note": "", "source": self.SRC,
                "as_of": rows[-1]["date"], "is_test": True, "volume_unit": "股", "price_unit": "新台幣元",
                "adjustment": "未調整（價格調整方式見 docs/indicators.md）", "notes": notes,
                "ma_note": "測試資料的均線為假資料產生器計算"}

    def screener_frame(self) -> dict:
        """analysis/screener.screen() 的輸入（欄位即 schema 欄位）；只含追蹤範圍內的股票。"""
        import pandas as pd
        t = self._tables()
        uni = self.universe.symbols
        keep = (lambda r: True) if uni is None else (lambda r: r["symbol"] in uni)
        cols = ["symbol", "market", "price_date", "open_twd", "high_twd", "low_twd", "close_twd",
                "change_twd", "change_sign", "is_halted", "volume_shares", "amount_twd", "source", "fetched_at",
                "is_current"]
        prices = pd.DataFrame([{c: r[c] for c in cols} for r in t["daily_prices"] if keep(r)])
        days = sorted({r["price_date"] for r in t["daily_prices"]})
        calendar = pd.DataFrame({"calendar_date": days, "is_trade_day": [1] * len(days)})
        securities = pd.DataFrame([{"symbol": r["symbol"], "market": r["market"], "name": r["name"],
                                    "listed_date": None, "is_current": 1} for r in t["securities"] if keep(r)])
        # 測試資料沒有除權息事件；宣告覆蓋起日早於資料起點，讓 strict 政策可評估（僅限 FakeSource）
        coverage = {(m, k): "2000-01-01" for m in ("TWSE", "TPEx") for k in _coverage_kinds()}
        return {"kwargs": dict(prices=prices, calendar=calendar, actions=None, securities=securities,
                               as_of=FAKE_DATA_DATE.isoformat(), actions_coverage=coverage)}

    def excluded_securities(self) -> list[dict]:
        return list(self._tables()["securities_excluded"])

    # ---- 月營收 ----
    def _rev_series(self, code: str):
        rnd = random.Random(int(code) * 104729)
        total = 20 if code in _SHORT_HISTORY else 36
        base = _BASE_PRICE.get(code, 100) * rnd.randint(20, 200) * 1_000_000
        revs: dict[tuple[int, int], int] = {}
        for k in range(total):
            y, m = _month_add(2026, 10, -(total - 1 - k))
            base *= 1 + rnd.gauss(0.008, 0.06)
            revs[(y, m)] = round(base)
        return revs, code in _REV_NOT_YET

    def monthly_revenue(self, code: str, months: int = 24) -> dict:
        revs, not_yet = self._rev_series(code)
        latest_y, latest_m = 2026, 10
        rows = []
        for k in range(months):
            y, m = _month_add(latest_y, latest_m, -k)
            label = f"{y}-{m:02d}"
            href = source_href("monthly_revenues", symbol=code, revenue_month=label)
            ref = {"table": "monthly_revenues", "key": f"symbol={code}, revenue_month={label}"}
            if (y, m) == (latest_y, latest_m) and not_yet:
                na = self._v(None, "新台幣元", label, STATUS_REVENUE_MISSING, REVENUE_DEADLINE_NOTE)
                rows.append(dict(month=label, revenue=na, mom=na, yoy=na, cum=na, cum_yoy=na))
                continue
            if (y, m) not in revs:
                ins = self._v(None, "新台幣元", label, STATUS_REVENUE_MISSING, "此月份前無資料")
                rows.append(dict(month=label, revenue=ins, mom=ins, yoy=ins, cum=ins, cum_yoy=ins))
                continue
            r = revs[(y, m)]
            py, pm = _month_add(y, m, -1)
            ly = (y - 1, m)
            V = lambda v, u="%", d=2: self._v(v, u, label, digits=d, href=href, ref=ref)  # noqa: E731
            mom = V(round((r / revs[(py, pm)] - 1) * 100, 2)) if (py, pm) in revs else \
                self._v(None, "%", label, STATUS_INSUFFICIENT, "缺上月營收")
            yoy = V(round((r / revs[ly] - 1) * 100, 2)) if ly in revs else \
                self._v(None, "%", label, STATUS_INSUFFICIENT, "缺去年同月營收")
            ck = [(y, mm) for mm in range(1, m + 1)]
            lk = [(y - 1, mm) for mm in range(1, m + 1)]
            if all(k2 in revs for k2 in ck):
                cv = sum(revs[k2] for k2 in ck)
                cum = V(cv, "新台幣元", 0)
                cum_yoy = V(round((cv / sum(revs[k2] for k2 in lk) - 1) * 100, 2)) if all(k2 in revs for k2 in lk) \
                    else self._v(None, "%", label, STATUS_INSUFFICIENT, "缺去年累計")
            else:
                cum = self._v(None, "新台幣元", label, STATUS_INSUFFICIENT, "缺當年部分月份")
                cum_yoy = self._v(None, "%", label, STATUS_INSUFFICIENT, "缺當年部分月份")
            rows.append(dict(month=label, revenue=V(r, "新台幣元", 0), mom=mom, yoy=yoy, cum=cum, cum_yoy=cum_yoy))
        return {"rows": rows, "source": self.SRC, "unit": "新台幣元", "is_test": True,
                "source_down": True, "last_success": "2026-11-08",
                "source_note": "測試資料：示範月營收來源故障，顯示上次成功版本（2026-11-08）。"}

    # ---- 財報 ----
    def financials(self, code: str, period: str = "quarter", scope: str = "consolidated",
                   basis: str = "single") -> dict:
        rnd = random.Random(int(code) * 15485863 + (0 if scope == "consolidated" else 1))
        fin = code in _FINANCIAL
        scale = _BASE_PRICE.get(code, 100) * rnd.randint(5, 60) * 10_000_000 * (0.7 if scope == "individual" else 1)
        if period == "annual":
            periods, basis = ["2025", "2024", "2023"], "annual"
        else:
            periods = ["2026Q3", "2026Q2", "2026Q1", "2025Q4", "2025Q3", "2025Q2", "2025Q1", "2024Q4"]
        rows = []
        for p in periods:
            if p == "2026Q3" and code in _Q_NOT_YET:
                na = self._v(None, "", p, STATUS_FINANCIALS_MISSING, FINANCIALS_DEADLINE_NOTE)
                rows.append(dict(period=p, derived=False, derived_note="", revenue=na, gross_margin=na, op_margin=na,
                                 net_income=na, eps=na, ocf=na))
                continue
            q_mult = int(p[-1]) if (basis == "cumulative" and p[-2] == "Q") else (4 if period == "annual" else 1)
            rev = scale * q_mult * (1 + rnd.gauss(0, 0.08))
            ni = rev * rnd.uniform(0.05, 0.3)
            gm = self._v(None, "%", p, STATUS_NOT_APPLICABLE, "金融業無營業毛利結構，毛利率不適用") if fin else \
                self._v(round(rnd.uniform(15, 55), 2), "%", p)
            om = self._v(None, "%", p, STATUS_NOT_APPLICABLE, "金融業不適用營業利益率") if fin else \
                self._v(round(rnd.uniform(5, 40), 2), "%", p)
            ocf_note = "現金流量表僅公告累計數；單季值＝本期累計－上期累計（推算）" if basis == "single" else ""
            derived = basis == "single" and p.endswith("Q4")
            rows.append(dict(period=p, derived=derived, derived_note="Q4 單季＝全年累計－前三季累計（推算）" if derived else "",
                             revenue=self._v(round(rev), "新台幣元", p, digits=0), gross_margin=gm,
                             op_margin=om, net_income=self._v(round(ni), "新台幣元", p, digits=0),
                             eps=self._v(round(ni / (scale * 2.5) * 10, 2), "元/股", p),
                             ocf=self._v(round(ni * rnd.uniform(0.6, 1.6)), "新台幣元", p, note=ocf_note, digits=0)))
        return {"rows": rows, "period": period, "scope": scope, "basis": basis, "source": self.SRC,
                "is_test": True, "is_financial": fin, "version": "v1（測試資料，無修訂）", "notes": [],
                "has_basis": True, "has_period_type": True}

    # ---- 事件 ----
    def events(self, code: Optional[str] = None, days: int = 30, category: Optional[str] = None) -> list[dict]:
        evs = []
        cats = list(EVENT_CATEGORIES)
        targets = [self._by_code[code]] if code and code in self._by_code else self._stocks[:8]
        for s in targets:
            rnd = random.Random(int(s["code"]) * 31337)
            for k in range(4):
                cat = cats[k % 4]
                d = FAKE_DATA_DATE - dt.timedelta(days=0 if (k == 0 and s["code"] in ("2330", "2317")) else rnd.randint(0, days - 1))
                hh, mm = rnd.randint(8, 21), rnd.randint(0, 59)
                title = {"official": f"{s['name']}公告董事會決議（測試資料）",
                         "media": f"媒體報導：{s['name']}擴產計畫（測試資料）",
                         "social": f"社群傳聞：{s['name']}接獲大單（測試資料，未證實）",
                         "ai": f"AI 推論：{s['name']}量價變化可能與月營收公告有關（測試資料）"}[cat]
                evs.append(dict(
                    id=f"{s['code']}-{k}", code=s["code"], name=s["name"], category=cat,
                    category_label=EVENT_CATEGORIES[cat], title=title, published_at=f"{d.isoformat()} {hh:02d}:{mm:02d}",
                    url=(f"/source?table=material_infos&symbol={s['code']}&announce_date={d.isoformat()}"
                         if cat == "official" else "#測試資料無原文連結"), related=[s["code"]], summary="這是版型用的測試資料摘要，非真實事件。",
                    impact="（測試資料）可能影響：待分析。" if cat != "social" else "社群訊息只當線索，不作為依據。",
                    pending="來源與內容待資料工程接上公開資訊觀測站後核實。",
                    evidence="測試資料：無關聯證據" if cat in ("media", "social") else "", source=self.SRC, is_test=True))
        if category:
            evs = [e for e in evs if e["category"] == category]
        evs.sort(key=lambda e: e["published_at"], reverse=True)
        return evs

    # ---- 資料狀態 ----
    def source_status(self) -> list[dict]:
        return self._enrich_status(self._tables()["source_status"])

    def _fresh_now(self) -> dt.datetime:   # 測試資料固定在資料日當晚，結果可重現
        return dt.datetime.combine(FAKE_DATA_DATE, dt.time(23, 0), freshness.TZ)

    def _trade_days(self) -> list[str]:
        return [d.isoformat() for d in _trading_days(FAKE_DATA_DATE, 400)]

    def _latest_for(self, category, market):
        t = self._tables()
        tbl, col = {"quotes": ("daily_prices", "price_date"), "revenue": ("monthly_revenues", "revenue_month")}.get(
            category, (None, None))
        if not tbl:
            return None
        vals = [r[col] for r in t[tbl] if market is None or r.get("market") == market]
        return max(vals) if vals else None

    def job_runs(self, limit: int = 20) -> list[dict]:
        return [self._job_view(r) for r in self._tables()["job_runs"][:limit]]


# ----------------------------------------------------------------------------
# SqliteSource：data/twstock.db（唯讀，只讀 is_current=1）
# ----------------------------------------------------------------------------
class SqliteSource(DataSource):
    is_test = False

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        self.name = f"SqliteSource（{self.db_path.name}，唯讀）"
        self._cols: dict[str, tuple[float, set[str]]] = {}

    def data_version(self) -> tuple:
        """資料版本（快取鍵）：資料庫檔與 -wal 檔的 (mtime_ns, size)。pipeline 寫入後版本改變，快取自動失效。"""
        out = []
        for p in (self.db_path, Path(str(self.db_path) + "-wal")):
            try:
                st = p.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    # ---- 低階 ----
    def _conn(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise DataUnavailable("db_missing", str(self.db_path))
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as e:  # pragma: no cover
            raise DataUnavailable("db_error", str(e))
        con.row_factory = sqlite3.Row
        return con

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        con = self._conn()
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise DataUnavailable("table_missing", str(e))
            raise DataUnavailable("db_error", str(e))
        finally:
            con.close()

    def _columns(self, table: str) -> set[str]:
        """PRAGMA table_info 防禦式偵測；表不存在回空集合（不報錯）。快取 30 秒（入庫中可能新增表/欄位）。"""
        import time
        hit = self._cols.get(table)
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        cols = {r["name"] for r in self._q(f"PRAGMA table_info({table})")}
        self._cols[table] = (time.monotonic(), cols)
        return cols

    def _has(self, table: str, *cols: str) -> bool:
        c = self._columns(table)
        return bool(c) and all(x in c for x in cols)

    def _cur(self, table: str, alias: str = "") -> str:
        """現行列條件：表有 is_current 欄就加 is_current=1。"""
        return f" AND {alias}is_current = 1" if "is_current" in self._columns(table) else ""

    def _require_rows(self, table: str) -> None:
        if not self._q(f"SELECT 1 FROM {table} LIMIT 1"):
            raise DataUnavailable("table_empty", table)

    def _uni_sql(self, col: str = "symbol") -> tuple[str, tuple]:
        """追蹤範圍條件（TWSITE_UNIVERSE）：全市場＝不加條件。所有跨股票的彙總查詢都要套用（範圍外股票不得算入）。"""
        try:
            syms = self.universe_symbols()
        except Exception:   # 範圍設定錯誤：各頁另外顯示錯誤訊息
            return "", ()
        if syms is None:
            return "", ()
        syms = sorted(syms)
        if not syms:
            return f" AND {col} IN (NULL)", ()
        return f" AND {col} IN ({','.join('?' * len(syms))})", tuple(syms)

    def _latest_price_date(self) -> Optional[str]:
        """資料日期＝追蹤範圍內最新的現行行情日（全市場回補寫入範圍外股票的較新日期時，不會跳日）。"""
        u, ua = self._uni_sql()
        r = self._q("SELECT max(price_date) AS d FROM daily_prices WHERE 1=1" + self._cur("daily_prices") + u, ua)
        return r[0]["d"] if r else None

    def _val(self, v, unit, as_of, source, digits=2, href="", ref=None, status=STATUS_OK, note="") -> Val:
        if v is None and status == STATUS_OK:
            status, note = STATUS_INSUFFICIENT, note or "來源列此欄為空"
        return Val(v, unit, source, as_of, status=status, note=note, digits=digits, href=href, ref=ref)

    # ---- 介面 ----
    def meta(self) -> dict:
        try:
            jobs = self._q("SELECT * FROM job_runs ORDER BY run_id DESC LIMIT 1")
            done = self._q("SELECT * FROM job_runs WHERE finished_at IS NOT NULL AND finished_at <> ''"
                           " ORDER BY run_id DESC LIMIT 1")
            dd = self._latest_price_date()
            return self._meta_from(jobs, dd, done[0] if done else {})
        except DataUnavailable as e:
            m = self._meta_from([], None)
            m.update(update_status=e.label, update_detail=str(e), unavailable=e.state)
            return m

    def list_stocks(self) -> list[dict]:
        rows = self._q("SELECT symbol, name, market, industry FROM securities WHERE 1=1" + self._cur("securities")
                       + " ORDER BY symbol")
        return [dict(code=r["symbol"], name=r["name"], market=MARKET_LABEL.get(r["market"], r["market"]),
                     market_code=r["market"], industry=r["industry"] or "—") for r in rows]

    def get_stock(self, code: str) -> Optional[dict]:
        r = self._q("SELECT symbol, name, market, industry FROM securities WHERE symbol=?" + self._cur("securities"),
                    (code,))
        if not r:
            return None
        r = r[0]
        return dict(code=r["symbol"], name=r["name"], market=MARKET_LABEL.get(r["market"], r["market"]),
                    market_code=r["market"], industry=r["industry"] or "—")

    def price_history(self, code: str, days: int = 120) -> dict:
        rows = self._q("SELECT * FROM daily_prices WHERE symbol=?" + self._cur("daily_prices")
                       + " ORDER BY price_date DESC LIMIT ?", (code, days))
        rows.reverse()
        has_change = self._has("daily_prices", "change")
        has_twd = self._has("daily_prices", "change_twd")
        traded = [r for r in rows if r["close_twd"] is not None]

        def signed(r):   # schema §7.3：change（含正負）優先，缺值退回 change_twd＋change_sign（同 indicators.md 1.1）
            if has_change and r.get("change") is not None:
                return r["change"]
            if has_twd and r.get("change_twd") is not None:
                v = float(r["change_twd"])
                return -abs(v) if (r.get("change_sign") or "").strip() == "-" else v
            return None
        out = [dict(date=r["price_date"], open=r["open_twd"], high=r["high_twd"], low=r["low_twd"],
                    close=r["close_twd"], volume=r["volume_shares"] or 0) for r in traded]
        notes = []
        if len(traded) < len(rows):
            notes.append(f"{len(rows) - len(traded)} 個交易日無成交／停牌（無收盤價），圖上略過。")
        if not out:
            notes.append("資料不足：daily_prices 沒有此股的現行行情列。")
        src = rows[-1]["source"] if rows else "daily_prices"
        # 均線與漲跌幅由 main.py 透過 analysis_hooks 疊上；這裡不計算
        return {"rows": out, "ma20": [None] * len(out), "ma60": [None] * len(out),
                "change": [signed(r) for r in traded] if (has_change or has_twd) else [None] * len(out),
                "change_pct": [None] * len(out),
                "ma_note": "", "change_pct_note": "",
                "change_note": "" if (has_change or has_twd) else "daily_prices.change／change_twd 欄位尚未建立",
                "row_sources": [(r["price_date"], r.get("source")) for r in traded],
                "source": src, "as_of": out[-1]["date"] if out else "—", "is_test": False, "volume_unit": "股",
                "price_unit": "新台幣元", "adjustment": "K 線為未調整原始價；均線為 analysis 還原價計算",
                "notes": notes}

    def monthly_revenue(self, code: str, months: int = 24) -> dict:
        rows = self._q("SELECT * FROM monthly_revenues WHERE symbol=?" + self._cur("monthly_revenues")
                       + " ORDER BY revenue_month DESC LIMIT ?", (code, months))
        by_m = {r["revenue_month"]: r for r in rows}
        # 年增率：analysis evaluate_revenue()（去年同月 0／負 → 不適用，HANDOFF §16.3 (a)）；需要前一年列，另讀
        from . import revenue_rules as RR
        hist = self._q("SELECT * FROM monthly_revenues WHERE symbol=?" + self._cur("monthly_revenues"), (code,))
        yoy_info = RR.yoy_by_month(hist)
        try:
            codes = RR.reason_codes()
        except Exception:
            codes = {}
        # 期待的最新月份＝最新行情日的上個月
        dd = self._latest_price_date() or dt.date.today().isoformat()
        y, m = int(dd[:4]), int(dd[5:7])
        ey, em = _month_add(y, m, -1)
        out = []
        for k in range(months):
            yy, mm = _month_add(ey, em, -k)
            label = f"{yy}-{mm:02d}"
            r = by_m.get(label)
            if not r:
                st = STATUS_REVENUE_MISSING   # 全站月營收缺值一律此狀態（不判斷是否已公告）
                note = REVENUE_DEADLINE_NOTE if k == 0 else "monthly_revenues 無此月份現行列"
                na = Val(None, "新台幣元", "monthly_revenues", label, status=st, note=note)
                out.append(dict(month=label, revenue=na, mom=na, yoy=na, cum=na, cum_yoy=na))
                continue
            href = source_href("monthly_revenues", symbol=code, revenue_month=label)
            ref = {"table": "monthly_revenues", "key": f"symbol={code}, revenue_month={label}"}
            V = lambda v, u, d=2: self._val(v, u, label, r["source"], d, href, ref)  # noqa: E731
            if "__error__" in yoy_info:
                yoy = Val(None, "%", r["source"], label, status=STATUS_ERROR, note=yoy_info["__error__"])
            else:
                info = yoy_info.get(label)
                st, note = RR.yoy_status_for(info)
                yoy = V(info["yoy_pct"], "%") if st == "ok" else Val(None, "%", r["source"], label, status=st, note=note)
                if st == "ok":
                    yoy.note = f"analysis {info['version']}；來源值 {r.get('yoy_pct')}"
            cr = RR.cum_na_reason(r.get("cumulative_prev_year_twd"))
            cum_yoy = Val(None, "%", r["source"], label, status=STATUS_NOT_APPLICABLE,
                          note=f"{RR.CUM_NA_TEXT[cr]}｜{cr}：{codes.get(cr, '')}") if cr else V(r["cumulative_yoy_pct"], "%")
            neg = r.get("revenue_twd") is not None and r["revenue_twd"] < 0
            if neg:   # 當月營收為負：年增率照 analysis 數字，只加註記（第十三批）
                yoy.note = REVENUE_NEGATIVE_DETAIL + (f"｜{yoy.note}" if yoy.note else "")
            out.append(dict(month=label, revenue=V(r["revenue_twd"], "新台幣元", 0), mom=V(r["mom_pct"], "%"),
                            yoy=yoy, cum=V(r["cumulative_twd"], "新台幣元", 0), cum_yoy=cum_yoy,
                            yoy_neg_note=REVENUE_NEGATIVE_NOTE if neg else ""))
        failing = [s for s in self.source_status() if "revenue" in s["id"] and s["status_code"] == STATUS_SOURCE_DOWN]
        return {"rows": out, "source": rows[0]["source"] if rows else "monthly_revenues", "unit": "新台幣元",
                "is_test": False, "source_down": bool(failing),
                "last_success": failing[0]["last_success"] if failing else "",
                "source_note": f"月營收來源故障，顯示上次成功版本（{failing[0]['last_success']}）。" if failing else ""}

    FIN_FORMS = {"ci": "一般業", "basi": "銀行業", "bd": "證券業", "fh": "金控業", "ins": "保險業", "mim": "異業"}

    def financials(self, code: str, period: str = "quarter", scope: str = "consolidated",
                   basis: str = "single") -> dict:
        """依 report_basis（合併/個別）與 period_type（單季/累計）分頁籤；欄位不存在時顯示「資料不足」。"""
        has_basis = self._has("income_statements", "report_basis")
        has_ptype = self._has("income_statements", "period_type")
        dcol = "derived" if self._has("income_statements", "derived") else "is_derived"  # schema.md：derived
        has_derived = self._has("income_statements", dcol)
        notes = []
        if period == "annual":
            basis = "annual"
        base = {"period": period, "scope": scope, "basis": basis, "source": "income_statements", "is_test": False,
                "is_financial": False, "version": "—", "has_basis": has_basis, "has_period_type": has_ptype}
        where, args = ["symbol = ?"], [code]
        if has_basis:
            al = BASIS_ALIASES[scope]
            where.append(f"report_basis IN ({','.join('?' * len(al))})")
            args += al
        elif scope == "individual":
            return {**base, "rows": [], "notes": notes,
                    "empty_reason": f"{FINANCIALS_MISSING_LABEL}（income_statements 尚無 report_basis 欄位，無法區分個別報表）",
                    "empty_status": STATUS_FINANCIALS_MISSING}
        else:
            notes.append("income_statements 尚無 report_basis 欄位：以下未標示合併／個別，依來源原值顯示。")
        want = "cumulative" if basis in ("cumulative", "annual") else "single"
        if has_ptype:
            al = PERIOD_ALIASES["annual" if basis == "annual" else want]
            where.append(f"period_type IN ({','.join('?' * len(al))})")
            args += al
            if basis == "annual":
                where.append("fiscal_quarter = 4")  # 年度＝Q4 累計
        elif basis in ("cumulative", "annual"):
            return {**base, "rows": [], "notes": notes,
                    "empty_reason": f"{FINANCIALS_MISSING_LABEL}（income_statements 尚無 period_type 欄位，無法區分累計／年度）",
                    "empty_status": STATUS_FINANCIALS_MISSING}
        else:
            notes.append("income_statements 尚無 period_type 欄位：未標示單季／累計，依來源原值顯示。")
        inc = self._q("SELECT * FROM income_statements WHERE " + " AND ".join(where) + self._cur("income_statements")
                      + " ORDER BY fiscal_year DESC, fiscal_quarter DESC LIMIT ?", tuple(args) + (3 if basis == "annual" else 8,))
        cf_cols = self._columns("cash_flow_statements")
        cfs: dict = {}
        if cf_cols:
            for r in self._q("SELECT * FROM cash_flow_statements WHERE symbol=?" + self._cur("cash_flow_statements"), (code,)):
                if "report_basis" in cf_cols and r.get("report_basis") not in BASIS_ALIASES[scope]:
                    continue
                if "period_type" in cf_cols and r.get("period_type") not in PERIOD_ALIASES[want]:
                    continue
                cfs[(r["fiscal_year"], r["fiscal_quarter"])] = r
        rows, is_fin = [], False
        for r in inc:
            p = str(r["fiscal_year"]) if basis == "annual" else f"{r['fiscal_year']}Q{r['fiscal_quarter']}"
            derived = bool(has_derived and r.get(dcol))
            fin = r["industry_form"] not in ("ci", "mim")
            is_fin = is_fin or fin
            key = dict(symbol=code, fiscal_year=r["fiscal_year"], fiscal_quarter=r["fiscal_quarter"],
                       industry_form=r["industry_form"])
            href = source_href("income_statements", **key)
            ref = {"table": "income_statements", "key": _key_text(key)
                   + (f", report_basis={r['report_basis']}" if has_basis else "")
                   + (f", period_type={r['period_type']}" if has_ptype else "")}
            dnote = (f"推算值（{r.get('derivation_method') or 'Q4 單季＝全年累計－前三季'}）") if derived else ""
            V = lambda v, u, d=2, **kw: self._val(v, u, p, r["source"], d, href, ref, note=kw.pop("note", dnote), **kw)  # noqa: E731
            rev = r["revenue_twd"]
            if fin:
                gm = V(None, "%", status=STATUS_NOT_APPLICABLE, note=f"{self.FIN_FORMS.get(r['industry_form'], '金融業')}無營業毛利結構")
                om = V(None, "%", status=STATUS_NOT_APPLICABLE, note="金融業不適用營業利益率")
            else:
                # 比率＝來源兩欄相除（展示用），不是技術指標
                gm = V(round(r["gross_profit_twd"] / rev * 100, 2) if rev and r["gross_profit_twd"] is not None else None, "%")
                om = V(round(r["operating_income_twd"] / rev * 100, 2) if rev and r["operating_income_twd"] is not None else None, "%")
            cf = cfs.get((r["fiscal_year"], r["fiscal_quarter"]))
            if not cf:
                ocf = Val(None, "新台幣元", "cash_flow_statements", p, status=STATUS_INSUFFICIENT, note="無對應口徑的現金流量現行列")
            else:
                ck = dict(symbol=code, fiscal_year=r["fiscal_year"], fiscal_quarter=r["fiscal_quarter"])
                ocf = self._val(cf["operating_cf_twd"], "新台幣元", p, cf["source"], 0,
                                source_href("cash_flow_statements", **ck),
                                {"table": "cash_flow_statements", "key": _key_text(ck)},
                                note="推算值" if cf.get("is_derived") else "")
            rows.append(dict(period=p, derived=derived, derived_note=dnote, revenue=V(rev, "新台幣元", 0),
                             gross_margin=gm, op_margin=om, net_income=V(r["net_income_twd"], "新台幣元", 0),
                             eps=V(r["eps_twd"], "元/股"), ocf=ocf))
        return {**base, "rows": rows, "notes": notes, "is_financial": is_fin,
                "source": inc[0]["source"] if inc else "income_statements",
                "version": ", ".join(sorted({f"v{r['version']}" for r in inc})) or "—",
                "empty_reason": "" if rows else f"{FINANCIALS_MISSING_LABEL}（income_statements 沒有此口徑的現行列）",
                "empty_status": "" if rows else STATUS_FINANCIALS_MISSING}

    def events(self, code: Optional[str] = None, days: int = 30, category: Optional[str] = None) -> list[dict]:
        """material_infos（官方）＋ news_items（暫定，四類標籤）。表/欄位不存在時略過。"""
        dd = self._latest_price_date() or dt.date.today().isoformat()
        since = (dt.date.fromisoformat(dd) - dt.timedelta(days=days)).isoformat()
        evs: list[dict] = []
        names = {}
        if self._columns("securities"):
            names = {r["symbol"]: r["name"] for r in self._q("SELECT symbol, name FROM securities")}
        if (not category or category == "official") and self._columns("material_infos"):
            cols = self._columns("material_infos")
            url_col = next((c for c in MATERIAL_URL_COLS if c in cols), None)
            sql = "SELECT * FROM material_infos WHERE announce_date >= ?" + self._cur("material_infos")
            args: list = [since]
            if code:
                sql += " AND symbol = ?"
                args.append(code)
            else:   # 全站事件列表（首頁）：只列追蹤範圍內的公司
                u, ua = self._uni_sql()
                sql += u
                args += list(ua)
            for r in self._q(sql + " ORDER BY announce_date DESC, announce_time DESC LIMIT 100", tuple(args)):
                link = (r.get(url_col) if url_col else None) or r["source"]
                evs.append(dict(id=f"{r['symbol']}-{r['content_hash']}", code=r["symbol"], name=names.get(r["symbol"], r["symbol"]),
                                category="official", category_label=EVENT_CATEGORIES["official"], title=r["title"],
                                published_at=f"{r['announce_date']} {(r['announce_time'] or '')[:5]}".strip(), url=link,
                                url_kind="原文連結" if url_col and r.get(url_col) else (
                                    "來源 URL（此則原文連結尚未入庫）" if url_col else "來源 URL（無原文連結欄位）"),
                                related=[r["symbol"]], summary=(r.get("detail_text") or "")[:200] or "（詳文未抓取）",
                                impact="—（AI 分析尚未接上）", pending=r.get("clause") or "—", evidence="官方公告公司代號",
                                source=r["source"], is_test=False))
        ncols = self._columns("news_items")
        link_cols = self._columns("news_item_symbols")
        if ncols and {"title", "published_at"} <= ncols:
            # pipeline/schema.sql：news_items 不放代號，公司對應在 news_item_symbols（多對多）
            cur = " AND n.is_current = 1" if "is_current" in ncols else ""
            if code and not link_cols:
                rows = []   # 無關聯表 → 無法對應公司，不顯示（避免錯配）
            elif code:
                rows = self._q("SELECT n.*, s.symbol AS _sym, s.evidence AS _evidence FROM news_items n "
                               "JOIN news_item_symbols s ON s.news_id = n.news_id WHERE s.symbol = ? "
                               "AND substr(n.published_at,1,10) >= ?" + cur + " ORDER BY n.published_at DESC LIMIT 100",
                               (code, since))
            else:
                rows = self._q("SELECT n.* FROM news_items n WHERE substr(n.published_at,1,10) >= ?" + cur
                               + " ORDER BY n.published_at DESC LIMIT 100", (since,))
            for r in rows:
                cat = NEWS_CATEGORY_KEYS.get(r.get("category"), r.get("category"))
                if cat not in EVENT_CATEGORIES:
                    cat = "media"
                if category and cat != category:
                    continue
                sym = r.get("_sym") or ""
                evs.append(dict(id=f"news-{r.get('news_id')}", code=sym, name=names.get(sym, sym),
                                category=cat, category_label=EVENT_CATEGORIES[cat], title=r["title"],
                                published_at=str(r["published_at"] or "")[:16].replace("T", " "),
                                url=r.get("url") or "#", url_kind="原文連結" if r.get("url") else "無連結",
                                related=[sym] if sym else [], summary="—", impact="—（AI 分析尚未接上）",
                                pending="—", evidence=r.get("_evidence") or "", source=r.get("source") or "",
                                is_test=False))
        evs.sort(key=lambda e: e["published_at"], reverse=True)
        return evs

    def source_status(self) -> list[dict]:
        if not self._columns("source_status"):
            return []
        return self._enrich_status(self._q("SELECT * FROM source_status ORDER BY source_id"))

    def _trade_days(self) -> list[str]:
        if not self._has("trading_calendar", "calendar_date", "is_trade_day"):
            return []
        return [r["d"] for r in self._q("SELECT calendar_date AS d FROM trading_calendar WHERE is_trade_day=1")]

    def _latest_for(self, category, market):
        spec = {"quotes": ("daily_prices", "price_date"), "revenue": ("monthly_revenues", "revenue_month")}
        if category in spec:
            tbl, col = spec[category]
            if not self._has(tbl, col):
                return None
            mk = " AND market=?" if market and self._has(tbl, "market") else ""
            u, ua = self._uni_sql() if self._has(tbl, "symbol") else ("", ())
            r = self._q(f"SELECT max({col}) AS v FROM {tbl} WHERE 1=1{self._cur(tbl)}{mk}{u}",
                        ((market,) if mk else ()) + ua)
            return r[0]["v"] if r else None
        if category == "financials" and self._has("income_statements", "fiscal_year", "fiscal_quarter"):
            u, ua = self._uni_sql() if self._has("income_statements", "symbol") else ("", ())
            r = self._q("SELECT fiscal_year AS y, fiscal_quarter AS q FROM income_statements WHERE 1=1"
                        f"{self._cur('income_statements')}{u} ORDER BY fiscal_year DESC, fiscal_quarter DESC LIMIT 1", ua)
            return f"{r[0]['y']}Q{r[0]['q']}" if r else None
        return None

    def job_runs(self, limit: int = 20) -> list[dict]:
        if not self._columns("job_runs"):
            return []
        return [self._job_view(r) for r in self._q("SELECT * FROM job_runs ORDER BY run_id DESC LIMIT ?", (limit,))]

    def excluded_securities(self) -> list[dict]:
        if not self._columns("securities_excluded"):
            return []
        return self._q("SELECT * FROM securities_excluded WHERE data_date = (SELECT max(data_date) FROM securities_excluded)"
                       " ORDER BY exclude_reason, symbol")

    def screener_frame(self) -> dict:
        """用研究分析的 analysis/io_sqlite.py 讀取（唯讀、只取 is_current=1），不自行組 SQL。"""
        import importlib.util
        import sys
        if not self.db_path.exists():
            raise DataUnavailable("db_missing", str(self.db_path))
        path = PROJECT_ROOT / "analysis" / "io_sqlite.py"
        if str(path.parent) not in sys.path:
            sys.path.append(str(path.parent))
        if "analysis_io_sqlite" not in sys.modules:
            spec = importlib.util.spec_from_file_location("analysis_io_sqlite", path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules["analysis_io_sqlite"] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
        mod = sys.modules["analysis_io_sqlite"]
        try:
            uni = self.universe.symbols   # 追蹤範圍（app/universe.py）；None＝全市場
            # 評估日＝追蹤範圍內最新行情日（analysis 預設取全表最新日；全市場回補先寫入範圍外的較新日期時會跳日）
            as_of = self._latest_price_date() if uni is not None else None
            inputs = mod.load_screen_inputs(self.db_path, symbols=list(uni) if uni is not None else None,
                                            **({"as_of": as_of} if as_of else {}))
        except (ValueError, FileNotFoundError) as e:
            raise DataUnavailable("table_empty", str(e))
        except Exception as e:  # sqlite3.OperationalError（缺表）等
            if "no such table" in str(e):
                raise DataUnavailable("table_missing", str(e))
            raise DataUnavailable("db_error", str(e))
        return {"kwargs": inputs.screen_kwargs()}

    def get_source_rows(self, table: str, filters: dict, limit: int = 200) -> list[dict]:
        spec = SOURCE_TABLES[table]
        cols = self._columns(table)
        if not cols:
            return []  # 表尚未建立 → 呼叫端顯示「資料不足」
        where, args = ["1=1"], []
        for k, v in filters.items():
            if k in cols and k in spec["keys"] + spec["filters"]:
                where.append(f"{k} = ?")
                args.append(v)
        sql = f"SELECT * FROM {table} WHERE " + " AND ".join(where) + self._cur(table) + " LIMIT ?"
        return self._q(sql, tuple(args) + (limit,))


_SOURCE: Optional[DataSource] = None


def get_source() -> DataSource:
    global _SOURCE
    if _SOURCE is None:
        kind = os.environ.get("TWSITE_SOURCE", "fake").lower()
        _SOURCE = SqliteSource(os.environ.get("TWSITE_DB", DEFAULT_DB)) if kind == "sqlite" else FakeSource()
    return _SOURCE


def set_source(src: Optional[DataSource]) -> None:
    """測試用：替換目前資料來源（None＝下次依環境變數重建）。"""
    global _SOURCE
    _SOURCE = src
