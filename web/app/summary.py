"""每日摘要的數值組裝：數字一律由程式從資料表讀取／計算，並附可點開的來源連結。

每個 fact 的 Val.href 指向：
- /source?table=…&主鍵…   來源資料表的那一列（或同一日期的多列＋彙總算式）
- /stock/{代號}#…          個股頁對應區段
- /screener#row-{代號}     選股結果列（analysis/screener.py 計算的值）
- /status#src-{source_id}  資料狀態頁錨點
解釋文字由 analysis/narrative.py summary_narrative()（規則模板）產生；失敗時顯示原因，不自行撰寫。
"""
from __future__ import annotations

import json
from typing import Optional

from .data_source import DataSource, DataUnavailable, price_class, price_direction, source_href
from .models import (FLAT_MAYBE_NOCOMPARE_NOTE, INDEX_CHANGE_PCT_MISSING_NOTE, NO_COMPARE_ALL_NULL_NOTE, NO_COMPARE_LABEL,
                     NO_COMPARE_NO_COLUMN_NOTE, NO_COMPARE_NOT_STORED_NOTE, NO_COMPARE_SIGN, STATUS_ERROR,
                     STATUS_INSUFFICIENT, STATUS_SOURCE_DOWN, TEST_LABEL, TURNOVER_TOTAL_NOTE, ScreenParams, Val,
                     basis_counts, basis_counts_text)


def _fact(label: str, v: Val, sub: str = "", links: Optional[list] = None, unit_label: str = "") -> dict:
    return {"label": label, "val": v, "sub": sub, "links": links or [], "unit_label": unit_label}


def build_daily_summary(src: DataSource) -> dict:
    from .screening import run_screen

    test = src.is_test
    srcname = TEST_LABEL if test else ""
    meta = src.meta()
    dd = meta["data_date"]
    sections: list[dict] = []

    def V(value, unit, as_of, href, table, key, digits=0, source=None):
        return Val(value, unit, source or srcname or table, as_of, is_test=test, digits=digits, href=href,
                   ref={"table": table, "key": key})

    def missing(unit, note, as_of=dd):
        return Val(None, unit, srcname or "—", as_of, status=STATUS_INSUFFICIENT, note=note, is_test=test)

    # ---- 1. 市場概況（追蹤範圍）：daily_prices，只算 TWSITE_UNIVERSE 範圍內的股票 ----
    facts = []
    uni = src.universe_symbols()
    try:
        rows = src.get_source_rows("daily_prices", {"price_date": dd}, limit=1_000_000)
    except (DataUnavailable, KeyError):
        rows = []
    if uni is not None:   # 範圍外股票（例如全市場回補寫入的列）不算入
        rows = [r for r in rows if r.get("symbol") in uni]
    ux = {"universe": 1} if uni is not None else {}   # 來源頁同一算式（/source?…&universe=1）
    if rows:
        traded = [r for r in rows if r.get("close_twd") is not None]
        k = f"price_date={dd}" + ("，symbol∈追蹤範圍" if uni is not None else "")
        cls = [price_class(r) for r in traded]
        # 無比價（官方 X）：欄位不存在／當日全空／資料庫從未存過 X → 標「無法判斷」，不顯示 0
        nc_note = ""
        if "change_sign" not in rows[0]:
            nc_note = NO_COMPARE_NO_COLUMN_NOTE
        elif all(r.get("change_sign") is None for r in traded):
            nc_note = NO_COMPARE_ALL_NULL_NOTE
        elif "nocompare" not in cls:
            try:
                ever = src.get_source_rows("daily_prices", {"change_sign": NO_COMPARE_SIGN}, limit=1)
            except (DataUnavailable, KeyError):
                ever = []
            if not ever:
                nc_note = NO_COMPARE_NOT_STORED_NOTE
        if nc_note:
            nc_val = Val(None, "檔", srcname or "daily_prices", dd, status=STATUS_INSUFFICIENT, note=nc_note, is_test=test)
        else:
            nc_val = V(cls.count("nocompare"), "檔", dd,
                       source_href("daily_prices", agg="count", price_date=dd, change_sign=NO_COMPARE_SIGN, **ux),
                       "daily_prices", k + f"，change_sign='{NO_COMPARE_SIGN}'")
        flat_val = V(cls.count("flat"), "檔", dd, source_href("daily_prices", agg="count", price_date=dd, dir="flat", **ux),
                     "daily_prices", k + "，有收盤、漲跌=0、非無比價")
        if nc_note:
            flat_val.note = FLAT_MAYBE_NOCOMPARE_NOTE
        facts += [
            _fact("行情檔數", V(len(traded), "檔", dd, source_href("daily_prices", agg="count", price_date=dd, **ux),
                               "daily_prices", k), unit_label="檔"),
            _fact("上漲家數", V(sum(1 for r in rows if price_direction(r) > 0), "檔", dd,
                               source_href("daily_prices", agg="count", price_date=dd, dir="up", **ux),
                               "daily_prices", k + "，漲跌>0"), unit_label="檔"),
            _fact("下跌家數", V(sum(1 for r in rows if price_direction(r) < 0), "檔", dd,
                               source_href("daily_prices", agg="count", price_date=dd, dir="down", **ux),
                               "daily_prices", k + "，漲跌<0"), unit_label="檔"),
            _fact("平盤家數", flat_val, sub=flat_val.note, unit_label="檔"),
            _fact(f"{NO_COMPARE_LABEL}家數", nc_val, sub=nc_note, unit_label="檔"),
            _fact("成交金額合計", V(sum(r.get("amount_twd") or 0 for r in rows), "新台幣元", dd,
                                 source_href("daily_prices", agg="sum:amount_twd", price_date=dd, **ux),
                                 "daily_prices", k), sub=TURNOVER_TOTAL_NOTE + "；" + basis_counts_text(basis_counts(traded))),
        ]
    else:
        facts.append(_fact("行情檔數", missing("檔", "daily_prices 沒有此日期的現行列")))
    idx = {r.get("index_id"): r for r in src.index_quotes(dd)}
    for iid, label in (("TAIEX", "加權指數"), ("TPEX", "櫃買指數")):
        r = idx.get(iid)
        if r and r.get("close") is not None:
            key = f"index_id={iid}, price_date={dd}"
            href = source_href("index_daily", index_id=iid, price_date=dd)
            facts.append(_fact(label, V(r["close"], "點", dd, href, "index_daily", key, 2,
                                        source=r.get("source") if not test else None)))
            if r.get("change") is not None:   # 來源給的漲跌點數
                facts.append(_fact(f"{label}漲跌", V(r["change"], "點", dd, href, "index_daily", key, 2,
                                                  source=r.get("source") if not test else None)))
            if r.get("change_pct") is not None:
                facts.append(_fact(f"{label}漲跌幅", V(r["change_pct"], "%", dd, href, "index_daily", key, 2,
                                                   source=r.get("source") if not test else None)))
            else:   # 不自算百分比
                facts.append(_fact(f"{label}漲跌幅", missing("%", INDEX_CHANGE_PCT_MISSING_NOTE)))
        else:
            facts.append(_fact(label, missing("點", "index_daily 表或當日列不存在")))
    # HANDOFF ⑥：指數近 20 個交易日收盤序列（每點附來源列連結）
    series = {iid: src.index_series(iid, 20) for iid in ("TAIEX", "TPEX")}
    try:
        uni_label = src.universe.label
    except Exception as e:
        uni_label = f"追蹤範圍設定錯誤：{e}"
    sections.append({"id": "market", "title": f"市場概況（{dd}，{uni_label}）", "universe_label": uni_label,
                     "facts": facts, "index_series": series})

    # ---- 2. 技術篩選：analysis/screener.py ----
    res = run_screen(src, ScreenParams())
    facts = []
    if res.error:
        facts.append(_fact("入選檔數", missing("檔", res.error)))
    else:
        facts.append(_fact("入選檔數", V(len(res.rows), "檔", res.trade_date, "/screener#results",
                                       "analysis/screener.py 輸出", f"as_of={res.trade_date}, status=PASS"),
                           unit_label="檔"))
        facts.append(_fact("排除檔數", V(len(res.excluded), "檔", res.trade_date, "/screener#excluded",
                                       "analysis/screener.py 輸出", f"as_of={res.trade_date}, status=EXCLUDED"),
                           unit_label="檔"))
    sections.append({"id": "screen", "title": "技術篩選（預設條件）", "facts": facts,
                     "more": ("看完整「今日入選」表 ↓", "#today"),
                     "zero": (not res.error and not res.rows), "zero_kind": res.extra.get("zero_kind", ""),
                     "trade_date": res.trade_date})

    # ---- 3. 月營收：F-001 首頁摘要（〈月份〉已有營收 N／M｜近 12 個月新高 X｜看全部 →；不放未見檔數）----
    from . import revenue_page as RP
    facts = []
    hs = RP.home_summary(src)   # 預設月份＝台北日期上一個月；0 列顯示 0 檔，不跳回
    month = hs["month"]
    uni = src.revenue_universe(month)
    high_fact = None
    if hs["status"] == STATUS_ERROR:
        facts.append(_fact(f"{month} 已有營收", Val(None, "檔", srcname or "monthly_revenues", month,
                                                     status=STATUS_ERROR, note=hs["error"], is_test=test)))
    else:
        facts.append(_fact(f"{month} 已有營收", V(hs["have"], "檔", month, hs["have_href"], "monthly_revenues",
                                                  f"revenue_month={month}, symbol∈{uni['label']}"), unit_label="檔"))
        high_fact = _fact(f"{month} 近 12 個月新高",
                          V(hs["highs"], "檔", month, hs["high_href"], "analysis/revenue.py evaluate_revenue()",
                            f"revenue_month={month}, rev_12m_high_status=true",
                            source=srcname or "analysis/revenue.py"), unit_label="檔")
        facts.append(high_fact)
    sections.append({"id": "revenue", "title": "月營收", "facts": facts, "month": month,
                     "high_fact": high_fact, "more_href": hs["more_href"], "revenue_status": hs["status"],
                     "source_down": hs["source_down"],
                     "revenue_universe": hs["count"], "revenue_universe_note": uni.get("note", "")})

    # ---- 4. 重大訊息：material_infos ----
    facts = []
    try:
        mi = src.get_source_rows("material_infos", {"announce_date": dd}, limit=1_000_000)
        if uni is not None:   # 只算追蹤範圍內公司的公告
            mi = [r for r in mi if r.get("symbol") in uni]
        facts.append(_fact(f"{dd} 重大訊息則數", V(len(mi), "則", dd,
                                                 source_href("material_infos", agg="count", announce_date=dd, **ux),
                                                 "material_infos", f"announce_date={dd}"
                                                 + ("，symbol∈追蹤範圍" if uni is not None else "")), unit_label="則"))
    except (DataUnavailable, KeyError):
        facts.append(_fact("重大訊息則數", missing("則", "material_infos 無法讀取")))
    sections.append({"id": "events", "title": "重大訊息（官方）", "facts": facts})

    # ---- 5. 資料狀態：source_status ----
    facts = []
    try:
        st = src.source_status()
        down = [s for s in st if s["status_code"] == STATUS_SOURCE_DOWN]
        facts.append(_fact("來源故障數", V(len(down), "個", dd, "/status#sources", "source_status", "ok=0"),
                           unit_label="個",
                           links=[(f"{s['name']}（上次成功 {s['last_success_short']}）", f"/status#src-{s['id']}")
                                  for s in down]))
    except DataUnavailable as e:
        facts.append(_fact("來源故障數", missing("個", str(e))))
    sections.append({"id": "status", "title": "資料狀態", "facts": facts})

    narrative = build_narrative(src, res, dd, [r for r in (idx.get("TAIEX"), idx.get("TPEX")) if r])
    try:   # 首頁「目前為○○，不代表全市場」依實際追蹤範圍（全市場不顯示）
        uni_all = src.universe_symbols() is None
    except Exception:
        uni_all = False
    return {"date": dd, "sections": sections, "universe_label": uni_label, "universe_all": uni_all,
            "narrative": narrative, "is_test": test,
            "disclaimer": "技術條件僅為篩選結果，非投資建議；只有技術訊號時不推論漲跌原因。"}


def _fact_href(f) -> str:
    """facts_json 的一筆（narrative.Fact 或 dict）→ 網站可點開的來源位置。"""
    g = (lambda k: f.get(k)) if isinstance(f, dict) else (lambda k: getattr(f, k, None))
    key = dict(g("source_key") or {})
    t = g("source_table")
    if t == "screen_results":   # 表尚未建立：指向選股頁（同一份 analysis/screener.py 結果）
        if key.get("symbol"):
            return f"/screener#row-{key['symbol']}"
        if key.get("exclusion_code"):
            return "/screener#exclusion-stats"
        return "/screener#results"
    if t in ("daily_prices", "index_daily", "securities"):
        k = {kk: vv for kk, vv in key.items() if kk in ("symbol", "price_date", "index_id")}
        return source_href(t, **k) if k else ""
    up = g("upstream") or {}
    if isinstance(up, dict) and up.get("table") == "daily_prices":
        return source_href("daily_prices", **(up.get("key") or {}))
    return ""


def _fact_view(f) -> dict:
    g = (lambda k: f.get(k)) if isinstance(f, dict) else (lambda k: getattr(f, k, None))
    return {"id": g("fact_id") or "", "label": g("label") or "", "display": g("display") or "",
            "unit": g("unit") or "", "table": g("source_table") or "", "field": g("source_field") or "",
            "key": ", ".join(f"{k}={v}" for k, v in (g("source_key") or {}).items()),
            "date": g("data_date") or "", "href": _fact_href(f)}


def live_narrative(src: DataSource, res, dd: str, index_rows: list):
    """即時呼叫 analysis/narrative.py summary_narrative()；回傳 (Narrative 或 None, 錯誤訊息, 狀態代碼)。
    狀態代碼（HANDOFF §15.1，皆在 STATUS_LABELS 內）：""＝無錯誤；insufficient＝資料不夠產生解釋；
    error＝narrative 載入／呼叫失敗、例外或回傳格式錯誤。"""
    from .screening import analysis_module
    ar = res.extra.get("analysis_result")
    if res.error or ar is None:
        data_gap = (not res.error) or res.error.startswith(("無行情資料", "追蹤範圍"))
        return None, f"沒有篩選結果可產生摘要：{res.error or '—'}", (STATUS_INSUFFICIENT if data_gap else STATUS_ERROR)
    try:
        nar = analysis_module("narrative")
    except Exception as e:
        return None, f"analysis/narrative.py 載入失敗：{type(e).__name__}: {e}", STATUS_ERROR
    extra = []
    for r in index_rows:
        if r.get("close") is None:
            continue
        key = {"index_id": r.get("index_id"), "price_date": dd}
        extra.append(nar.Fact(f"X{len(extra) + 1:03d}", r.get("index_name") or r.get("index_id"), r["close"],
                              f"{float(r['close']):,.2f}", "點", "index_daily", "close", key, dd))
    try:
        n = nar.summary_narrative(ar, extra_facts=extra, news_events=())   # news_events 未定稿 → 不傳
    except (ValueError, TypeError) as e:
        return None, f"summary_narrative() 拒絕產出（{type(e).__name__}）：{e}", STATUS_ERROR
    except Exception as e:   # 不讓首頁 500
        return None, f"summary_narrative() 執行失敗：{type(e).__name__}: {e}", STATUS_ERROR
    if not isinstance(getattr(n, "text", None), str) or not hasattr(n, "facts"):
        return None, f"summary_narrative() 回傳格式錯誤：{type(n).__name__}", STATUS_ERROR
    return n, "", ""


def archived_narrative(src: DataSource, dd: str) -> dict | None:
    """唯讀讀取 daily_summaries 存檔（pipeline/schema.sql：summary_date, generated_at, provider, text, facts_json, review_status）。
    表不存在、欄位不足或當日無列 → None。網站不寫入此表。"""
    rows = []
    for key in ("summary_date", "data_date"):   # pipeline/schema.sql：summary_date；data_date 為後補同義欄
        try:
            rows += src.get_source_rows("daily_summaries", {key: dd}, limit=50)
        except (DataUnavailable, KeyError):
            pass
    # 防禦：欄不存在時 get_source_rows 不會套用過濾 → 這裡再比對一次；審核退回（rejected）的不顯示
    rows = [r for r in rows if dd in (str(r.get("summary_date") or "")[:10], str(r.get("data_date") or "")[:10])
            and (r.get("text") or "").strip() and r.get("review_status") != "rejected"]
    if not rows:
        return None
    r = sorted(rows, key=lambda x: str(x.get("generated_at") or ""))[-1]
    out = {"mode": "archived", "engine": "daily_summaries 存檔", "is_test": src.is_test,
           "provider": r.get("provider") or "—", "generated_at": r.get("generated_at") or "—",
           "generation_method": r.get("generation_method") or "", "text": r["text"],
           "lines": str(r["text"]).split("\n"), "facts": [], "facts_note": "", "error": "", "error_status": "",
           "review_status": r.get("review_status") or "",
           "href": source_href("daily_summaries", summary_date=dd) if r.get("summary_date")
           else source_href("daily_summaries", data_date=dd)}
    if "facts_json" not in r:
        out["facts_note"] = "存檔沒有 facts_json 欄位，無法列出數字來源"
        return out
    try:
        facts = json.loads(r["facts_json"] or "[]")
        if not isinstance(facts, list):
            raise ValueError("facts_json 不是陣列")
        out["facts"] = [_fact_view(f) for f in facts if isinstance(f, dict)]
        if not out["facts"]:
            out["facts_note"] = "facts_json 為空"
    except (ValueError, TypeError) as e:
        out["facts_note"] = f"facts_json 無法解析：{e}"
    return out


def build_narrative(src: DataSource, res, dd: str, index_rows: list) -> dict:
    """有 daily_summaries 存檔就讀存檔；沒有才即時呼叫 narrative.py（標「尚未存檔，即時計算」）。"""
    arch = archived_narrative(src, dd)
    if arch:
        return arch
    out = {"mode": "live", "engine": "analysis/narrative.py summary_narrative()", "is_test": src.is_test,
           "provider": "", "generated_at": "", "text": "", "lines": [], "facts": [], "facts_note": "", "error": "",
           "error_status": "", "generation_method": "", "href": ""}
    n, err, code = live_narrative(src, res, dd, index_rows)
    if err:
        out["error"], out["error_status"] = err, code or STATUS_ERROR
        return out
    out.update(text=n.text, lines=n.text.split("\n"), generation_method=n.generation_method,
               facts=[_fact_view(f) for f in n.facts])
    return out
