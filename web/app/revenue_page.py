"""F-001 月營收追蹤表（/revenue、/revenue.csv、首頁摘要）。

規格：docs/rd/proposals/F-001.md（負責人 04:10 審核的 6 項條件＋04:20 補充）。
- 「近 12 個月新高」與年增率一律呼叫 analysis/revenue.py evaluate_revenue()（rev_12m_high-v1／rev_yoy-v1）；網站不重算。
- 月增率、累計營收、累計年增率、remark 照 monthly_revenues 原值顯示（來源值，不重算）。
- 當月沒有列的公司：標題「本站資料庫尚無此月營收」，附 MAX(fetched_at)、「不代表公司尚未公告」與公開資訊觀測站連結；
  不放法定期限、全頁不出現「尚未公告」。
- 某市場該月彙總頁最近一次抓取失敗（source_status `mops_nas_revenue_{market}_{月份}` ok=0）→ 該市場整體顯示
  「來源故障，顯示上次成功版本（時間）」，該市場個股不列入「本站資料庫尚無此月營收」名單。
- 預設月份：台北日期的上一個月；當月 0 列顯示 0 檔，不跳回前一個月。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from . import revenue_rules as RR
from .data_source import DataSource, DataUnavailable, source_href
from .models import (REVENUE_MISSING_LABEL, REVENUE_NEGATIVE_DETAIL, REVENUE_NEGATIVE_NOTE, STATUS_ERROR, STATUS_INSUFFICIENT, STATUS_NOT_APPLICABLE, STATUS_OK,
                     STATUS_REVENUE_MISSING, STATUS_SOURCE_DOWN, TEST_LABEL, Val)

PAGE_SIZE = 100
MARKETS = {"TWSE": "上市", "TPEx": "上櫃"}
MOPS_URL = "https://mops.twse.com.tw/"   # 公開資訊觀測站首頁（各市場彙總表另附連結）
MOPS_SUMMARY_URL = "https://mopsov.twse.com.tw/nas/t21/{mk}/t21sc03_{roc}_{m}_0.html"
FIXED_NOTE = ("近 12 個月新高＝當月營收高於前 12 個月（不含當月）的最高值；相等不算。只代表數字比較，不代表利多或買進訊號。"
              "年增率可能因去年同月基期低而偏高，請一併看去年同月金額。")
NOT_ADVICE = "本頁不是投資建議。"
NOT_ANNOUNCE_NOTE = "不代表公司尚未公告"
# 金融業註記（負責人 04:20：只引用 t21sc03 金融保險業註記與 mopsfin 對照表原文，附網址；文字只說申報口徑不同）
FIN_INDUSTRIES = ("金融保險業", "金融保險")
FIN_NOTE = {
    "summary": "申報口徑不同，不宜跨產業比較。",
    "quotes": [
        {"text": "產業別：金融保險業（其中金控公司係控股公司，其申報之「營業收入」係認列所有子公司損益之合計數）",
         "source": "公開資訊觀測站 上市公司月營收彙總表（t21sc03）",
         "url": "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_115_9_0.html", "checked": "2026-11-10"},
        {"text": "營業收入：為利各業別比較，本項目對應至各業別之會計項目名稱列示如下。"
                 "一般行業：營業收入｜金融業：淨收益｜保險業：營業收入｜證券期貨業：收益｜金控：淨收益｜異業合併：收入",
         "source": "公開資訊觀測站 財務比較E點通（mopsfin）使用說明及應注意事項",
         "url": "https://mopsfin.twse.com.tw/terms", "checked": "2026-11-10"},
    ],
}
SORT_KEYS = {"symbol": "代號", "market": "市場", "revenue": "營收", "prev_year": "去年同月營收", "mom": "月增率",
             "yoy": "年增率", "cum": "累計營收", "cum_yoy": "累計年增率", "high": "近 12 個月新高"}
YOY_TEXT = {"REV_YOY_PREV_YEAR_ZERO": "不適用（去年同月為 0）",
            "REV_YOY_PREV_YEAR_NEGATIVE": "不適用（去年同月為負值）",
            "REV_YOY_PREV_YEAR_NULL": "資料不足（去年同月無資料）",
            "REV_YOY_CURRENT_NULL": "資料不足（當月營收為空值）"}
CSV_COLUMNS = ["revenue_month", "symbol", "name", "market", "revenue_twd", "revenue_prev_year_twd", "mom_pct",
               "yoy_pct", "yoy_status", "yoy_reason", "yoy_note", "cumulative_twd", "cumulative_yoy_pct", "rev_12m_high_status",
               "rev_12m_high_reason", "prior_12m_max_twd", "rev_12m_high_version", "yoy_version", "industry",
               "remark", "source", "fetched_at"]

_TODAY_OVERRIDE: Optional[dt.date] = None   # 測試用固定時鐘


def taipei_today(src: Optional[DataSource] = None) -> dt.date:
    if _TODAY_OVERRIDE is not None:
        return _TODAY_OVERRIDE
    if src is not None and src.is_test:
        from .data_source import FAKE_DATA_DATE
        return FAKE_DATA_DATE
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("Asia/Taipei")).date()


def default_month(today: dt.date) -> str:
    """台北日期的上一個月（11/01～11/30 → 2026-10；12/01 → 2026-11）。資料 0 列也不往回跳。"""
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{y}-{m:02d}"


def valid_month(s: Any) -> Optional[str]:
    if isinstance(s, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s.strip()):
        return s.strip()
    return None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def summary_url(market: str, month: str) -> str:
    y, m = month.split("-")
    return MOPS_SUMMARY_URL.format(mk="sii" if market == "TWSE" else "otc", roc=int(y) - 1911, m=int(m))


def _market_status(src: DataSource, market: str, month: str, rows: list[dict]) -> dict:
    """該市場×月份：MAX(fetched_at)（本站資料列）＋逐月抓取狀態（source_status，資料工程已補；沒有列＝無紀錄）。"""
    sid = f"mops_nas_revenue_{market}_{month}"
    fetched = max((_str(r.get("fetched_at")) for r in rows), default="")
    out = {"market": market, "label": MARKETS.get(market, market), "source_id": sid, "status": STATUS_OK,
           "max_fetched_at": fetched, "last_success": "", "last_attempt": "", "error": "", "has_record": False,
           "status_href": f"/status#src-{sid}", "row_href": source_href("source_status", source_id=sid),
           "url": summary_url(market, month), "rows": len(rows)}
    try:
        st = src.get_source_rows("source_status", {"source_id": sid}, limit=1)
    except (DataUnavailable, KeyError):
        st = []
    except Exception as e:
        out.update(status=STATUS_ERROR, error=f"source_status 讀取失敗：{type(e).__name__}: {e}")
        return out
    if st:
        r = st[0]
        out.update(has_record=True, last_success=_str(r.get("last_success_at")),
                   last_attempt=_str(r.get("last_attempt_at")), error=_str(r.get("last_error")),
                   url=_str(r.get("source_url")) or out["url"])
        if not r.get("ok") and r.get("last_attempt_at"):
            out["status"] = STATUS_SOURCE_DOWN
    return out


_CORE_CACHE: dict = {}   # (id(src), 資料版本, 範圍, today, month) → (src, core view)；只快取有資料版本的來源（SqliteSource）
_CORE_MAX = 16


def build(src: DataSource, month: Optional[str] = None, market: str = "all", high_only: bool = False,
          sort: str = "symbol", desc: bool = False, page: int = 1, today: Optional[dt.date] = None) -> dict:
    """組 /revenue 頁的資料。任何失敗都回 status=error（附原因），不拋例外。
    讀表＋analysis 評估（與市場／排序／分頁無關的部分）依資料版本＋月份快取；篩選、排序、分頁每次重做。"""
    today = today or taipei_today(src)
    dmonth = default_month(today)
    month = valid_month(month) or dmonth
    market = market if market in MARKETS else "all"
    sort = sort if sort in SORT_KEYS else "symbol"
    ver = src.data_version() if callable(getattr(src, "data_version", None)) else None
    key = None
    if ver is not None:
        u = src.universe
        key = (id(src), ver, u.name, u.text, tuple(sorted(src.universe_symbols() or ())),
               today.isoformat(), month)
        hit = _CORE_CACHE.get(key)
        if hit is not None and hit[0] is src:
            return _select(dict(hit[1]), market, high_only, sort, desc, page)
    core = _core(src, month, dmonth, today)
    if key is not None and core["status"] != STATUS_ERROR:
        if len(_CORE_CACHE) >= _CORE_MAX:
            _CORE_CACHE.pop(next(iter(_CORE_CACHE)))
        _CORE_CACHE[key] = (src, core)
    return _select(dict(core), market, high_only, sort, desc, page)


def _core(src: DataSource, month: str, dmonth: str, today: dt.date) -> dict:
    market, high_only, sort, desc = "all", False, "symbol", False
    u = src.universe
    uni = src.universe_symbols()
    view: dict[str, Any] = {
        "month": month, "default_month": dmonth, "today": today.isoformat(), "market": market, "high_only": high_only,
        "sort": sort, "desc": desc, "page": 1, "pages": 1, "page_size": PAGE_SIZE, "rows": [], "total": 0,
        "missing": [], "markets": [], "status": STATUS_OK, "error": "", "universe_text": u.text,
        "universe_label": u.label, "universe_all": u.symbols is None, "universe_count": None, "have": 0, "highs": 0, "months": [],
        "is_test": src.is_test, "fixed_note": FIXED_NOTE, "not_advice": NOT_ADVICE, "fin_note": FIN_NOTE,
        "not_announce_note": NOT_ANNOUNCE_NOTE, "missing_label": REVENUE_MISSING_LABEL, "mops_url": MOPS_URL,
        "sort_keys": SORT_KEYS, "versions": {}, "engine": "analysis/revenue.py evaluate_revenue()",
        "has_fin": False, "insufficient": 0, "filtered": [], "source_down": [], "missing_by_market": {},
        "have_href": source_href("monthly_revenues", agg="count", revenue_month=month, universe=1),
    }
    try:
        all_rows = src.get_source_rows("monthly_revenues", {}, limit=10_000_000)
        stocks = {s["code"]: s for s in src.list_stocks()}
    except Exception as e:
        view.update(status=STATUS_ERROR, error=f"monthly_revenues 讀取失敗：{type(e).__name__}: {e}")
        return view
    targets = sorted(uni) if uni is not None else sorted(stocks)
    view["universe_count"] = len(targets)
    rows = [r for r in all_rows if r.get("symbol") in set(targets)]
    view["months"] = sorted({r["revenue_month"] for r in rows} | {dmonth, month}, reverse=True)
    try:
        from .screening import analysis_module
        R = analysis_module("revenue")
        ev = R.evaluate_revenue(rows, month=month, symbols=targets)
        view["versions"] = {"rev_12m_high": R.REV_12M_HIGH_VERSION, "yoy": R.REV_YOY_VERSION}
        reasons = dict(R.REASON_CODES)
    except Exception as e:   # 載入失敗、重複列（ValueError）等 → 錯誤狀態
        view.update(status=STATUS_ERROR, error=f"analysis/revenue.py 無法評估（{type(e).__name__}：{e}）")
        return view
    raw = {r["symbol"]: r for r in rows if r.get("revenue_month") == month}
    by_mk: dict[str, list] = {}
    for r in raw.values():
        by_mk.setdefault(r.get("market") or "", []).append(r)
    mks = {m: _market_status(src, m, month, by_mk.get(m, [])) for m in MARKETS}
    view["markets"] = list(mks.values())
    test = src.is_test
    srcname = TEST_LABEL if test else "monthly_revenues"

    def V(v, unit, key, digits=0, status=STATUS_OK, note="", href=None):
        k = f"symbol={key}, revenue_month={month}"
        h = source_href("monthly_revenues", symbol=key, revenue_month=month) if href is None else href
        return Val(v, unit, srcname, month, is_test=test, digits=digits, href=h if v is not None else "",
                   ref={"table": "monthly_revenues", "key": k}, status=status if v is not None or status != STATUS_OK
                   else STATUS_INSUFFICIENT, note=note)

    out_rows, missing = [], []
    for e in ev.to_dict("records"):
        sym = e["symbol"]
        s = stocks.get(sym, {})
        r0 = raw.get(sym)
        mk = _str(e.get("market")) or s.get("market_code", "")
        if r0 is None:
            if mks.get(mk, {}).get("status") == STATUS_SOURCE_DOWN:
                continue   # 來源故障的市場：不把個股列入「本站資料庫尚無此月營收」
            missing.append({"symbol": sym, "name": s.get("name", ""), "market": mk,
                            "market_label": MARKETS.get(mk, mk), "href": f"/stock/{sym}#revenue"})
            continue
        hs = _str(e.get("rev_12m_high_status"))
        hreason = _str(e.get("rev_12m_high_reason"))
        ys, yr = _str(e.get("yoy_status")), _str(e.get("yoy_reason"))
        yoy = _num(e.get("yoy_pct"))
        if ys == "ok" and yoy is not None:
            yoy_v = V(yoy, "%", sym, 2, note=f"analysis {e.get('yoy_version')}；來源值 {_num(e.get('source_yoy_pct'))}")
        else:
            st = STATUS_NOT_APPLICABLE if yr in ("REV_YOY_PREV_YEAR_ZERO", "REV_YOY_PREV_YEAR_NEGATIVE") else STATUS_INSUFFICIENT
            yoy_v = Val(None, "%", srcname, month, is_test=test, status=st,
                        note=f"{yr}：{reasons.get(yr, '')}" if yr else "analysis 未提供年增率")
        neg = _num(e.get("revenue_twd")) is not None and _num(e.get("revenue_twd")) < 0
        if neg:   # 當月營收為負：年增率照 analysis 數字，只加註記
            yoy_v.note = REVENUE_NEGATIVE_DETAIL + (f"｜{yoy_v.note}" if yoy_v.note else "")
        industry = _str(r0.get("industry")) or s.get("industry", "")
        is_fin = industry in FIN_INDUSTRIES
        view["has_fin"] |= is_fin
        cna = RR.cum_na_reason(r0.get("cumulative_prev_year_twd"))
        cum_yoy = None if cna else _num(r0.get("cumulative_yoy_pct"))
        mom = _num(r0.get("mom_pct"))
        rec = {
            "symbol": sym, "name": s.get("name", ""), "market": mk, "market_label": MARKETS.get(mk, mk),
            "href": f"/stock/{sym}#revenue",
            "revenue": V(_num(e.get("revenue_twd")), "新台幣元", sym),
            "prev_year": V(_num(e.get("revenue_prev_year_twd")), "新台幣元", sym,
                           note="" if _num(e.get("revenue_prev_year_twd")) is not None else "去年同月無資料"),
            "mom": V(mom, "%", sym, 2, note="來源值（monthly_revenues.mom_pct）") if mom is not None else
                   Val(None, "%", srcname, month, is_test=test, status=STATUS_INSUFFICIENT, note="來源未提供月增率"),
            "yoy": yoy_v, "yoy_neg_note": REVENUE_NEGATIVE_NOTE if neg else "", "yoy_status": ys, "yoy_reason": yr, "yoy_text": YOY_TEXT.get(yr, ""),
            "cum": V(_num(r0.get("cumulative_twd")), "新台幣元", sym) if _num(r0.get("cumulative_twd")) is not None else
                   Val(None, "新台幣元", srcname, month, is_test=test, status=STATUS_INSUFFICIENT, note="來源未提供累計營收"),
            "cum_yoy": (Val(None, "%", srcname, month, is_test=test, status=STATUS_NOT_APPLICABLE,
                            note=f"{RR.CUM_NA_TEXT[cna]}｜{cna}：{reasons.get(cna, '')}") if cna else
                        V(cum_yoy, "%", sym, 2, note="來源值（monthly_revenues.cumulative_yoy_pct）") if cum_yoy is not None
                        else Val(None, "%", srcname, month, is_test=test, status=STATUS_INSUFFICIENT, note="來源未提供累計年增率")),
            "cum_yoy_text": RR.CUM_NA_TEXT.get(cna, ""),
            "high_status": hs, "high_reason": hreason, "high_reason_text": reasons.get(hreason, ""),
            "prior_max": _num(e.get("prior_12m_max_twd")), "prior_window": f"{_str(e.get('prior_window_start'))}～{_str(e.get('prior_window_end'))}",
            "remark": _str(r0.get("remark")).strip() if _str(r0.get("remark")).strip() not in ("-", "－") else "",
            "industry": industry, "is_fin": is_fin, "source": _str(r0.get("source")), "fetched_at": _str(r0.get("fetched_at")),
            "csv": {"revenue_month": month, "symbol": sym, "name": s.get("name", ""), "market": mk,
                    "revenue_twd": _num(e.get("revenue_twd")), "revenue_prev_year_twd": _num(e.get("revenue_prev_year_twd")),
                    "mom_pct": mom, "yoy_pct": yoy if ys == "ok" else None, "yoy_status": ys, "yoy_reason": yr,
                    "yoy_note": REVENUE_NEGATIVE_NOTE if neg else "",
                    "cumulative_twd": _num(r0.get("cumulative_twd")), "cumulative_yoy_pct": cum_yoy,
                    "rev_12m_high_status": hs, "rev_12m_high_reason": hreason,
                    "prior_12m_max_twd": _num(e.get("prior_12m_max_twd")),
                    "rev_12m_high_version": _str(e.get("rev_12m_high_version")), "yoy_version": _str(e.get("yoy_version")),
                    "industry": industry, "remark": _str(r0.get("remark")), "source": _str(r0.get("source")),
                    "fetched_at": _str(r0.get("fetched_at"))},
        }
        out_rows.append(rec)
    view["all_rows"] = out_rows   # 篩選、排序前（analysis 輸出順序）；靜態匯出 v2 前端篩排用
    view["have"] = len(out_rows)
    view["highs"] = sum(1 for r in out_rows if r["high_status"] == "true")
    view["insufficient"] = sum(1 for r in out_rows if r["high_status"] == "insufficient")
    view["missing"] = missing
    view["missing_by_market"] = {m: [x for x in missing if x["market"] == m] for m in MARKETS}
    view["source_down"] = [m for m in view["markets"] if m["status"] == STATUS_SOURCE_DOWN]
    if view["have"] == 0 and not view["source_down"]:
        view["status"] = STATUS_REVENUE_MISSING   # 該月 0 列：顯示 0 檔，不跳回前一個月
    view["have_href"] = source_href("monthly_revenues", agg="count", revenue_month=month, universe=1)
    return view


def _select(view: dict, market: str, high_only: bool, sort: str, desc: bool, page: int) -> dict:
    """篩選 → 排序 → 分頁（CSV 用同一份篩選＋排序，不分頁）。view 是 core 的淺拷貝，只換掉這裡設定的鍵。"""
    view.update(market=market, high_only=high_only, sort=sort, desc=desc)
    if "all_rows" not in view:   # 讀取／評估失敗的 core
        return view
    out_rows = view["all_rows"]
    sel = [r for r in out_rows if (market == "all" or r["market"] == market) and (not high_only or r["high_status"] == "true")]

    def key(r):
        if sort in ("symbol", "market"):
            return (0, r[sort])
        if sort == "high":
            return (0, {"true": 0, "false": 1, "insufficient": 2}.get(r["high_status"], 3))
        v = r[sort].value
        return (1, 0) if v is None else (0, v)
    nonnull = [r for r in sel if not (sort not in ("symbol", "market", "high") and r[sort].value is None)]
    keep = {id(r) for r in nonnull}
    nulls = [r for r in sel if id(r) not in keep]   # 各列是不同物件；以 id 判斷（原本 list 成員比對在全市場是 O(n²)）
    nonnull.sort(key=key, reverse=desc)
    sel = nonnull + nulls   # 缺值固定排最後（不論升降冪）
    view["filtered"] = sel
    view["total"] = len(sel)
    view["pages"] = max(1, -(-len(sel) // PAGE_SIZE))
    view["page"] = min(max(1, int(page or 1)), view["pages"])
    view["rows"] = sel[(view["page"] - 1) * PAGE_SIZE: view["page"] * PAGE_SIZE]
    return view


def home_summary(src: DataSource, today: Optional[dt.date] = None) -> dict:
    """首頁月營收一行：「〈月份〉已有營收 N／M｜近 12 個月新高 X｜看全部 →」（不放未見檔數）。"""
    v = build(src, today=today)
    return {"month": v["month"], "status": v["status"], "error": v["error"], "have": v["have"],
            "count": v["universe_count"], "highs": v["highs"], "more_href": f"/revenue?month={v['month']}",
            "high_href": f"/revenue?month={v['month']}&high=1", "have_href": v["have_href"],
            "source_down": v["source_down"], "insufficient": v["insufficient"]}
