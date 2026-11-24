"""FastAPI 入口。啟動：web/run.sh start（127.0.0.1:8765）。"""
from __future__ import annotations

import os
import csv
import io
import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .data_source import AGGREGATES, SOURCE_TABLES, DataUnavailable, get_source, price_class, price_direction, source_href
from .models import (ADVANCED_BOOL_PARAMS, ADVANCED_NUM_PARAMS, ADVANCED_STR_PARAMS, BASIC_PARAMS, EVENT_CATEGORIES,
                     INT_PARAMS, PARAM_LABELS, STATUS_LABELS, TEST_LABEL, TOGGLE_PARAMS, ScreenParams, Val,
                     CHANGE_CHECK_LABELS, CHANGE_CHECK_NOTES, CHANGE_CHECK_STATE, VOLUME_BASIS_NOTE,
                     volume_basis_note, volume_basis_policy, source_basis_segments, VOLUME_BASIS_TITLE, VOLUME_BASIS_PENDING,
                     volume_basis_label, block_info, analysis_text_zh, zh_text, change_check_label, basis_segments,
                     basis_counts_text, SUSPEND_CODES)
from .screening import (SORT_KEYS, analysis_defaults, exclusion_label, exclusion_label_source, exclusion_labels,
                        run_screen)

BASE = Path(__file__).resolve().parent
from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app):
    _warm_hub()
    yield


app = FastAPI(title="台股盤後研究", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _warm_hub() -> None:
    """伺服器啟動後在背景先算篩選中心 M1～M21（含 analysis/modes.py 全市場約 1 分鐘），請求不用等第一次計算。
    TWSITE_MODES_WARM=0 關閉；測試資料、靜態匯出不預熱。"""
    if os.environ.get("TWSITE_MODES_WARM", "1") == "0" or os.environ.get("TWSITE_STATIC_EXPORT") == "1":
        return
    import threading

    def run():
        try:
            src = get_source()
            if not src.is_test:
                from . import hub as H
                H._core(src)
        except Exception:
            pass   # 預熱失敗不影響服務；請求時照常計算並顯示錯誤狀態
    threading.Thread(target=run, name="hub-warm", daemon=True).start()


def fmt_num(v, digits=2):
    if v is None:
        return ""
    if isinstance(v, (int,)) or digits == 0:
        return f"{v:,.0f}"
    return f"{v:,.{digits}f}"


def fmt_big(v):
    """新台幣元 → 億/萬 顯示（原值放 tooltip）。"""
    if v is None:
        return ""
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:,.2f} 億"
    if a >= 1e4:
        return f"{v / 1e4:,.1f} 萬"
    return f"{v:,.0f}"


templates.env.filters["num"] = fmt_num
templates.env.filters["big"] = fmt_big
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, TEST_LABEL=TEST_LABEL, EVENT_CATEGORIES=EVENT_CATEGORIES,
                             VOLUME_BASIS_NOTE=VOLUME_BASIS_NOTE, CHANGE_CHECK_LABELS=CHANGE_CHECK_LABELS,
                             volume_basis_note=volume_basis_note, volume_basis_policy=volume_basis_policy)


def ctx(request: Request, **kw):
    src = get_source()
    try:  # 頁頂資料狀態列用（設計 ROUND1 §5；只讀現有 source_status 介面）
        site_sources = src.source_status()
    except Exception:  # 狀態列非必要：任何讀取失敗都不影響頁面本身
        site_sources = []
    base = dict(request=request, meta=src.meta(), is_test=src.is_test, nav=kw.pop("nav", ""), site_sources=site_sources)
    base.update(kw)
    return base


# ---------------- 參數解析（不夾值、不修正；驗證交給 analysis） ----------------
def _coerce(raw: str, name: str):
    """字串 → 數字；解析不了就原字串交給 analysis（它會拋 TypeError 並指出參數名）。"""
    import re as _re
    t = raw.strip()
    if name in INT_PARAMS and _re.fullmatch(r"[+-]?\d+", t):
        return int(t)
    try:
        return float(t)  # 整數參數收到 20.5 → 原樣傳給 analysis（TypeError：必須是整數）
    except ValueError:
        return raw


def parse_params(request: Request) -> ScreenParams:
    qs = request.query_params
    submitted = qs.get("f") == "1"
    values: dict = {}
    raw: dict = {}
    for name in BASIC_PARAMS + ADVANCED_NUM_PARAMS:
        qn = "min_turnover_yi" if name == "min_turnover" else name
        if qn in qs:
            raw[qn] = qs[qn]
            v = _coerce(qs[qn], name)
            # USAGE.md：成交金額以「億元」輸入後 ×1e8（單位換算，不是修正）；非數字原樣交給 analysis
            values[name] = v * 100_000_000 if name == "min_turnover" and isinstance(v, (int, float)) else v
    for name in ADVANCED_STR_PARAMS:
        if name in qs:
            raw[name] = values[name] = qs[name]
    if submitted:
        for name in TOGGLE_PARAMS:
            values[name] = qs.get(name) == "1"
        if qs.get("adv") == "1":
            for name in ADVANCED_BOOL_PARAMS:
                values[name] = qs.get(name) == "1"
    sort = qs.get("sort") if qs.get("sort") in SORT_KEYS else "turnover"
    return ScreenParams(values=values, raw=raw, sort=sort, order="asc" if qs.get("order") == "asc" else "desc")


def params_query(p: ScreenParams, **override) -> str:
    d: dict = {"f": "1", **p.raw, "sort": p.sort, "order": p.order}
    for name in TOGGLE_PARAMS:
        if p.values.get(name, True):
            d[name] = "1"
    if any(n in p.values for n in ADVANCED_BOOL_PARAMS):
        d["adv"] = "1"
        for name in ADVANCED_BOOL_PARAMS:
            if p.values.get(name, True):
                d[name] = "1"
    d.update(override)
    return urlencode(d)


def form_value(p: ScreenParams, name: str, defaults: dict) -> str:
    """表單回填：使用者原輸入優先，否則顯示 analysis 預設值。"""
    qn = "min_turnover_yi" if name == "min_turnover" else name
    if qn in p.raw:
        return p.raw[qn]
    v = defaults.get(name, "")
    if name == "min_turnover" and isinstance(v, (int, float)):
        return f"{v / 1e8:g}"
    return f"{v:g}" if isinstance(v, float) else str(v)


# ---------------- 頁面 ----------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    src = get_source()
    result = run_screen(src, ScreenParams())
    return templates.TemplateResponse(request, "home.html", ctx(
        request, nav="home", result=result, summary=src.get_daily_summary(),
        events=[e for e in src.events(days=30) if e["category"] in ("official", "media")][:8], sources=src.source_status()))


@app.get("/screener", response_class=HTMLResponse)
def screener(request: Request):
    src = get_source()
    p = parse_params(request)
    result = run_screen(src, p)
    defaults = analysis_defaults()
    from .screening import explanations
    explain = explanations(result.extra.get("params") or None)
    pg, page_result = screener_page(result, p, request.query_params.get("page"))
    return templates.TemplateResponse(request, "screener.html", ctx(
        request, nav="screener", result=result, p=p, sort_keys=SORT_KEYS, defaults=defaults, explain=explain,
        pg=pg, page_result=page_result,
        fv=lambda n: form_value(p, n, defaults), labels=PARAM_LABELS, adv_num=ADVANCED_NUM_PARAMS,
        adv_bool=ADVANCED_BOOL_PARAMS, qs=params_query(p), params_query=params_query))


@app.get("/hub", response_class=HTMLResponse)
def hub(request: Request):
    """篩選中心（HUB_HANDOFF §1–§8）：模式判斷全部呼叫 analysis（app/hub.py）。"""
    from . import hub as H
    qs = request.query_params
    mode = (qs.get("mode") or "").upper()
    view = H.build(get_source(), mode if mode in H.MODE_IDS else None, qs.get("sort"))
    return templates.TemplateResponse(request, "hub.html", ctx(request, nav="hub", hub=view))


@app.get("/industry", response_class=HTMLResponse)
def industry_index(request: Request):
    from . import industry as I
    return templates.TemplateResponse(request, "industry.html", ctx(request, nav="industry", ind=I.index(get_source()),
                                                                      industry_mod=I))


@app.get("/industry/{industry_id}", response_class=HTMLResponse)
def industry_detail(request: Request, industry_id: str):
    from . import industry as I
    qs = request.query_params
    view = I.detail(get_source(), industry_id, qs.get("sort"), qs.get("dir"))
    status = 200 if view.get("industry") or view.get("error") else 404
    return templates.TemplateResponse(request, "industry.html", ctx(request, nav="industry", ind=view, industry_mod=I,
                                                                      industry_id=industry_id), status_code=status)


SCREENER_PAGE_SIZE = 100


def screener_page(result, p, page) -> tuple:
    """選股結果分頁：每頁 100 檔（排序已由 run_screen 依 p.sort／p.order 決定；CSV 不分頁）。"""
    import dataclasses
    total = len(result.rows)
    pages = max(1, -(-total // SCREENER_PAGE_SIZE))
    try:
        n = int(page or 1)
    except (TypeError, ValueError):
        n = 1
    n = min(max(1, n), pages)
    pr = result if pages == 1 else dataclasses.replace(
        result, rows=result.rows[(n - 1) * SCREENER_PAGE_SIZE: n * SCREENER_PAGE_SIZE])
    pg = {"page": n, "pages": pages, "total": total, "size": SCREENER_PAGE_SIZE,
          "href": lambda i: "/screener?" + params_query(p, page=str(i)) + "#results"}
    return pg, pr


CSV_HEADER = ["代號", "名稱", "市場", "交易日期", "收盤(新台幣元)", "漲跌幅(%)", "成交金額(新台幣元)",
              "MA短", "MA長", "量比(倍)", "條件1_趨勢", "條件2_突破", "條件3_量能", "條件4_成交金額",
              "符合原因", "漲跌交叉檢查", "評估日量額口徑", "均量窗口非總量日數", "觀察窗資料異常日數", "資料說明",
              "資料來源", "資料標記"]


def _csv_cell(c):
    if not c.enabled:
        return "未啟用"
    return "通過" if c.passed else ("資料不足" if c.passed is None else "未通過")


def screener_csv_parts(r) -> tuple[list, list, list]:
    """選股 CSV 的（表頭列, 資料列, 結尾列）；/screener.csv 與靜態匯出 v2 共用。"""
    p = r.params
    ap = r.extra.get("params") or p.values
    ap = {k: v for k, v in ap.items()}
    head = [[f"# 參數：{', '.join(f'{k}={v}' for k, v in ap.items())}；交易日 {r.trade_date}；引擎：{r.engine}"],
            [f"# {VOLUME_BASIS_NOTE}"]]
    if r.extra.get("basis_counts_text"):
        head.append([f"# {r.extra['basis_counts_text']}"])
    if r.extra.get("universe_text"):
        head.append([f"# {r.extra['universe_text']}（清單來源 {r.extra.get('universe_source', '')}；範圍外不計入排除）"])
    for e in r.extra.get("exclusion_stats") or []:
        head.append([f"# 排除 {e['code']}：{e['count']} 檔｜{e['label']}"])
    if r.extra.get("volume_basis_status_text"):
        head.append([f"# {r.extra['volume_basis_status_text']}"])
    head.append(CSV_HEADER)
    rows = [screener_csv_row(row, r.is_test) for row in r.rows]
    tail = []
    if r.error:
        tail.append([f"（無法篩選）{r.error}"])
    elif not r.rows and r.extra.get("zero_kind") == "coverage":
        tail.append(["（零檔）0 檔是因為資料覆蓋不足，不是沒有股票符合條件；見資料狀態頁覆蓋起日"])
    elif not r.rows:
        tail.append(["（零檔）條件下無符合標的"])
    return head, rows, tail


def _extra_cell(extra: list, key: str) -> str:
    for e in extra or []:
        if e["key"] == key:
            return e["text"]
    return "" if key == "data_note" else "—"


def screener_csv_row(row, is_test: bool) -> list:
    reasons = "；".join(f"{c.label}：{c.actual}" for c in row.conditions if c.enabled)
    return [row.code, row.name, row.market, row.trade_date, row.close.value, row.change_pct.value,
            int(row.turnover.value), row.ma_short.value, row.ma_long.value, row.vol_ratio.value,
            *[_csv_cell(c) for c in row.conditions], reasons,
            change_check_label(row.change_check),
            *[_extra_cell(row.extra, k) for k in ("volume_basis", "window_volume_basis_days", "window_bad_data_days",
                                                  "data_note")],
            row.close.source,
            TEST_LABEL if is_test else "正式"]


def screener_csv_name(r) -> str:
    tag = "_TEST" if r.is_test else ""
    return f"screener_{r.trade_date.replace('-', '') if r.trade_date != '—' else 'NA'}{tag}.csv"


@app.get("/screener.csv")
def screener_csv(request: Request):
    src = get_source()
    p = parse_params(request)
    r = run_screen(src, p)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    head, rows, tail = screener_csv_parts(r)
    for line in head + rows + tail:
        w.writerow(line)
    body = "\ufeff" + buf.getvalue()  # UTF-8 BOM 讓 Excel 正確辨識中文
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{screener_csv_name(r)}"'})


# ---------------- F-001 月營收追蹤表 ----------------
def _revenue_args(request: Request) -> dict:
    qs = request.query_params
    try:
        page = int(qs.get("page") or 1)
    except ValueError:
        page = 1
    return dict(month=qs.get("month"), market=qs.get("market") or "all", high_only=qs.get("high") == "1",
                sort=qs.get("sort") or "symbol", desc=qs.get("desc") == "1", page=page)


@app.get("/revenue", response_class=HTMLResponse)
def revenue(request: Request):
    from . import revenue_page as RP
    src = get_source()
    a = _revenue_args(request)
    v = RP.build(src, **a)

    def qs(**over):
        from urllib.parse import urlencode
        q = {"month": v["month"], "market": v["market"], "high": "1" if v["high_only"] else "",
             "sort": v["sort"], "desc": "1" if v["desc"] else "", "page": ""}
        q.update({k: ("" if x is None else x) for k, x in over.items()})
        return urlencode({k: x for k, x in q.items() if x not in ("", None)})
    return templates.TemplateResponse(request, "revenue.html", ctx(request, nav="revenue", v=v, rqs=qs))


@app.get("/revenue.csv")
def revenue_csv(request: Request):
    from . import revenue_page as RP
    src = get_source()
    a = _revenue_args(request)
    v = RP.build(src, **a)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([f"# 營收月份 {v['month']}｜{v['universe_text']}｜市場 {RP.MARKETS.get(v['market'], '全部')}"
                f"｜只看近 12 個月新高 {'是' if v['high_only'] else '否'}｜排序 {RP.SORT_KEYS[v['sort']]}{'（降冪）' if v['desc'] else ''}"])
    w.writerow([f"# 金額單位：新台幣元（來源仟元×1000）；百分比單位：%；定義版本 {v['versions'].get('rev_12m_high', '')}／{v['versions'].get('yoy', '')}"])
    w.writerow([f"# {RP.FIXED_NOTE}{RP.NOT_ADVICE}"])
    w.writerow(["# yoy_note：當月營收為負時標「當月營收為負」；年增率數字照 analysis 定義，不改為不適用"])
    if v["status"] == "error":
        w.writerow([f"# 讀取失敗：{v['error']}"])
    for m in v.get("source_down") or []:
        w.writerow([f"# {m['label']} 來源故障，顯示上次成功版本（{m['last_success'] or '從未成功'}）"])
    if src.is_test:
        w.writerow([f"# {TEST_LABEL}"])
    w.writerow(RP.CSV_COLUMNS)
    for r in v.get("filtered") or []:
        w.writerow(["" if r["csv"][c] is None else r["csv"][c] for c in RP.CSV_COLUMNS])
    tag = "_TEST" if src.is_test else ""
    return Response(("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="revenue_{v["month"].replace("-", "")}{tag}.csv"'})


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock(request: Request, code: str):
    src = get_source()
    c = stock_context(src, code, request.query_params)
    return templates.TemplateResponse(request, "stock.html", ctx(request, nav="", **c))


def stock_fin(src, code: str, period: str, scope: str, basis: str, basis_given: bool):
    """財報區塊：回傳 (實際口徑, fin)。未指定口徑且單季列不存在時，預設改顯示已入庫的累計（不推算單季）。"""
    fin = src.financials(code, period, scope, basis)
    if period == "quarter" and not basis_given and not fin.get("rows"):
        alt = src.financials(code, period, scope, "cumulative")
        if alt.get("rows"):
            basis, fin = "cumulative", alt
            fin["notes"] = list(fin.get("notes") or []) + ["單季列尚未入庫（資料層未提供單季值），預設改顯示累計；網站不自行推算單季。"]
    return basis, fin


def stock_screen_status(src, code: str):
    sr = run_screen(src, ScreenParams())
    st = (sr.extra.get("status_by_symbol") or {}).get(code)
    if sr.error:
        return {"status": "ERROR", "label": "無法篩選", "detail": sr.error, "code": ""}
    if st:
        lab = {"PASS": "入選", "FAIL": "未符合條件", "EXCLUDED": "排除"}.get(st["status"], st["status"])
        z = zh_text(st["reason"], st["code"])
        return {"status": st["status"], "label": lab, "code": st["code"], "detail": z["text"], "detail_title": z["title"],
                "extra": st.get("extra") or [],
                "code_label": exclusion_label(st["code"]) if st["code"] else "",
                "code_label_src": exclusion_label_source(st["code"]) if st["code"] else "",
                "trade_date": sr.trade_date}
    return None


def stock_modes(src, code: str) -> dict:
    """「今天符合的模式」：與篩選中心同一份計算（hub._core 快取）；失敗時回 error 狀態，不讓個股頁壞掉。"""
    from . import hub as H
    try:
        return H.today_modes(src, code)
    except Exception as e:
        return dict(state="error", hits=[], insufficient=0, as_of="—", error=f"{type(e).__name__}：{e}")


def stock_params(qs) -> tuple:
    """個股頁查詢參數 → (period, scope, basis, cat, price_mode)（不認得的值回預設）。"""
    period = "annual" if qs.get("period") == "annual" else "quarter"
    scope = "individual" if qs.get("scope") == "individual" else "consolidated"
    basis = "cumulative" if qs.get("basis") == "cumulative" else "single"
    cat = qs.get("cat") if qs.get("cat") in EVENT_CATEGORIES else None
    return period, scope, basis, cat, ("raw" if qs.get("price") == "raw" else "adj")


def stock_context(src, code: str, qs, parts=("quote", "fin", "events", "rev", "screen")) -> dict:
    """個股頁模板變數（/stock/{code} 與靜態匯出 v2 共用；qs 為查詢參數 mapping）。"""
    s = src.get_stock(code)
    if not s:
        raise HTTPException(404, f"查無股票代號 {code}")
    if not src.in_universe(code):
        raise HTTPException(404, f"{code} {s['name']} 不在本站追蹤範圍（{src.universe.label}），本站未提供此股頁面")
    period, scope, basis, cat, _pm = stock_params(qs)
    ph = src.price_history(code, 120)
    _overlay_analysis_series(src, code, ph)
    from . import analysis_hooks as hooks
    price_mode = "raw" if qs.get("price") == "raw" else "adj"
    kl = hooks.kline(src, code, ph["rows"], price_mode) if ph["rows"] else None
    if kl:
        ph["adjustment"] = KLINE_ADJ_TEXT[kl["mode"]]
    chart = {
        "mode": kl["mode"] if kl else price_mode,
        "candles": kl["candles"] if kl else [],
        "volume": kl["volume"] if kl else [],
        "markers": kl["markers"] if kl else [],
        "ma20": [{"time": r["date"], "value": m} for r, m in zip(ph["rows"], ph["ma20"]) if m is not None],
        "ma60": [{"time": r["date"], "value": m} for r, m in zip(ph["rows"], ph["ma60"]) if m is not None],
    }
    if kl:
        kl["ma_ranges"] = _ma_ranges(chart, ph)
    quote = None
    if ph["rows"]:
        last = ph["rows"][-1]
        d = last["date"]
        href = source_href("daily_prices", symbol=code, price_date=d)
        ref = {"table": "daily_prices", "key": f"symbol={code}, price_date={d}"}

        def QV(v, unit, digits=2, note=""):
            return Val(v, unit, ph["source"], d, is_test=ph["is_test"], digits=digits, href=href if v is not None else "",
                       ref=ref, status="ok" if v is not None else "insufficient", note=note)
        ma_note = ph.get("ma_note", "")
        quote = {"close": QV(last["close"], "新台幣元"), "volume": QV(last["volume"], "股", 0),
                 "change": QV(ph["change"][-1], "新台幣元", note=ph.get("change_note", "")),
                 "change_pct": QV(ph["change_pct"][-1], "%", note=ph.get("change_pct_note", "")),
                 "ma20": QV(ph["ma20"][-1], "新台幣元", note=ma_note or ph.get("ma20_note", "")),
                 "ma60": QV(ph["ma60"][-1], "新台幣元", note=ma_note or ph.get("ma60_note", "") or "交易日數不足 60 日")}
        lv = ph.get("last_vol") or {}
        if lv:
            quote["amount"] = QV(lv.get("amount"), "新台幣元", 0)
            quote["vol_basis"] = volume_basis_label(lv.get("volume_basis"), bool(lv.get("has_basis_col")))
            quote["block"] = block_info(lv.get("block_shares"), lv.get("block_amount_twd"), lv.get("block_source"),
                                        bool(lv.get("has_block_col")))
    chk = None
    if quote and ph.get("change_check"):
        c = ph["change_check"][-1] or {}
        if c.get("check"):
            chk = dict(c, label=change_check_label(c["check"]),
                       note=CHANGE_CHECK_NOTES.get(c["check"], ""), state=CHANGE_CHECK_STATE.get(c["check"], "na"))
    out = dict(s=s, ph=ph, chart=chart, chart_json=json.dumps(chart), quote=quote, chk=chk, kline=kl, price_mode=price_mode,
               check_labels=CHANGE_CHECK_LABELS, basis_segs=basis_segments([(x[0], x[1], x[2] if len(x) > 2 else None) for x in ph.get("row_sources") or []],
                                         bool(ph.get("has_basis_col"))),
               basis_title=VOLUME_BASIS_TITLE, cat=cat, period=period, scope=scope, basis=basis)
    if "fin" in parts:
        out["basis"], out["fin"] = stock_fin(src, code, period, scope, basis, "basis" in qs)
    if "screen" in parts:
        out["screen_status"] = stock_screen_status(src, code)
        out["stock_modes"] = stock_modes(src, code)
    if "rev" in parts:
        out["rev"] = src.monthly_revenue(code, 24)
    if "events" in parts:
        out["events"] = src.events(code, 30, cat)
    return out


def _ma_ranges(chart: dict, ph: dict) -> str:
    """均線實際畫出的起訖與點數（前復權均線；與 K 線模式無關）。沒有點時附中文原因。"""
    out = []
    for w in (20, 60):
        pts = chart.get(f"ma{w}") or []
        if pts:
            out.append(f"MA{w} {pts[0]['time']}～{pts[-1]['time']}（{len(pts)} 日）")
        else:
            why = ph.get(f"ma{w}_note") or ph.get("ma_note") or "沒有可畫的點"
            out.append(f"MA{w} 未畫（{why}）")
    return "；".join(out)


KLINE_ADJ_TEXT = {
    "adj": "K 線為前復權還原價（analysis ohlc_series()，以資料基準日為準；成交量同步還原）；均線為還原價",
    "raw": "K 線為未調整原始價（daily_prices）；均線為 analysis 還原價計算",
}


@app.get("/api/stock/{code}/ohlc")
def api_stock_ohlc(code: str, price: str = "adj", days: int = 120):
    """K 線資料 API：price=adj（預設，前復權）｜raw；欄位見 docs/design/HANDOFF.md §13。失敗回 status=error，不回 500。"""
    from . import analysis_hooks as hooks
    src = get_source()
    if not src.get_stock(code) or not src.in_universe(code):
        raise HTTPException(404, f"查無股票代號 {code}（或不在本站追蹤範圍）")
    days = max(1, min(int(days), 500))
    try:
        ph = src.price_history(code, days)
    except Exception as e:
        return JSONResponse({"symbol": code, "status": "error", "note": f"price_history 失敗：{type(e).__name__}: {e}",
                             "mode": price, "candles": [], "volume": [], "markers": [], "events": []})
    kl = hooks.kline(src, code, ph["rows"], price) if ph["rows"] else {
        "requested": price, "mode": price, "status": "insufficient", "note": "沒有行情列", "candles": [], "volume": [],
        "markers": [], "events": [], "fallback_note": "", "boundary_date": "", "all_unavailable": False,
        "engine": "", "skipped": 0}
    return JSONResponse(dict(kl, symbol=code, source=ph.get("source"), as_of=ph.get("as_of"), is_test=ph.get("is_test"),
                             adjustment=KLINE_ADJ_TEXT[kl["mode"]], price_unit="新台幣元", volume_unit="股"))


def _overlay_analysis_series(src, code: str, ph: dict) -> None:
    """均線、漲跌幅一律來自 analysis（analysis_hooks）；函式不存在時：正式資料顯示資料不足，測試資料保留假值。"""
    from . import analysis_hooks as hooks
    if not ph["rows"]:
        return
    s = hooks.stock_series(src, code)
    dates = [r["date"] for r in ph["rows"]]
    if s["has_ma"]:
        ph["ma20"] = [s["ma"].get(d, {}).get(20) for d in dates]
        ph["ma60"] = [s["ma"].get(d, {}).get(60) for d in dates]
        last20, last60 = s["ma_note"].get(20, ""), s["ma_note"].get(60, "")
        ph["ma_note"] = ""
        ph["ma20_note"], ph["ma60_note"] = last20, last60
        ph["ma_note_raw"] = dict(s.get("ma_note_raw") or {})
        ph["ma_unavailable_raw"] = s.get("ma_unavailable_raw", "")
        ph["ma_engine"] = s["engine"]
    elif not ph["is_test"]:
        ph["ma_note"] = hooks.describe(hooks.MA_FUNC)
    if s["has_change"]:
        ph["change_pct"] = [s["change_pct"].get(d) for d in dates]
        ph["change_pct_note"] = ""
        ph["change_check"] = [s["change_check"].get(d, {}) for d in dates]
        ph["change_mismatch"] = [dict(c, date=d) for d, c in zip(dates, ph["change_check"])
                                 if c.get("check") in ("mismatch", "source_sign_conflict")]
    elif not ph["is_test"]:
        ph["change_pct_note"] = hooks.describe(hooks.CHANGE_FUNC)
    ph["notes"] = ph["notes"] + [n for n in s["notes"] if "尚未提供" not in n]


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    src = get_source()
    unavailable = ""
    sources, jobs, excluded, coverage = [], [], [], None
    try:
        sources = src.source_status()
        jobs = src.job_runs(20)
        excluded = src.excluded_securities()
        coverage = src.coverage()
    except DataUnavailable as e:
        unavailable = str(e)
    sr = run_screen(src, ScreenParams())
    counts = sr.extra.get("exclusion_counts") or {}
    labels = exclusion_labels()
    excl_codes = [{"code": c, "label": lab, "count": (counts.get(c, 0) if not sr.error else None),
                   "src": exclusion_label_source(c), "suspend": c in SUSPEND_CODES}
                  for c, lab in labels.items()]
    static_export = os.environ.get("TWSITE_STATIC_EXPORT") == "1"   # 靜態匯出時：顯示本版資訊，不讀本機發布紀錄
    from . import pages_deploy
    pages = None if static_export else pages_deploy.view(src.meta().get("data_date"))
    return templates.TemplateResponse(request, "status.html", ctx(
        request, nav="status", sources=sources, jobs=jobs, excluded=excluded, coverage=coverage,
        pages=pages, static_export=static_export,
        unavailable=unavailable, excl_codes=excl_codes, excl_trade_date=sr.trade_date if not sr.error else ""))


UNIVERSE_TABLES = {"monthly_revenues", "daily_prices", "income_statements", "cash_flow_statements", "material_infos"}


@app.get("/source", response_class=HTMLResponse)
def source_panel(request: Request):
    """來源明細面板：顯示來源資料表名稱、主鍵、資料日期、來源與原始列（含原始單位）。"""
    c, status = source_context(get_source(), request.query_params)
    return templates.TemplateResponse(request, "source.html", ctx(request, **c), status_code=status)


def source_context(src, qs) -> tuple[dict, int]:
    """來源明細模板變數與 HTTP 狀態（/source 與靜態匯出 v2 共用；qs 為查詢參數 mapping）。"""
    table = qs.get("table", "")
    spec = SOURCE_TABLES.get(table)
    if not spec:
        raise HTTPException(404, f"不支援的來源資料表：{table or '（未指定）'}")
    allowed = spec["keys"] + spec["filters"]
    filters = {k: qs[k] for k in allowed if k in qs}
    if not filters:
        raise HTTPException(404, "需指定主鍵或過濾欄位")
    agg = qs.get("agg", "")
    if agg and agg not in AGGREGATES:
        raise HTTPException(404, f"不支援的彙總：{agg}")
    direction = qs.get("dir") if table == "daily_prices" and qs.get("dir") in ("up", "down", "flat") else ""

    in_universe = qs.get("universe") == "1" and table in UNIVERSE_TABLES
    uni_syms = src.universe_symbols() if in_universe else None

    query_filters = dict(filters)   # 之後 filters 會加入顯示用說明，查詢一律用這份

    def fetch(limit):
        rs = src.get_source_rows(table, query_filters, limit=limit if not (direction or uni_syms is not None) else 1_000_000)
        if uni_syms is not None:   # 追蹤範圍過濾（與首頁「已有營收 N／M」同一算式）
            rs = [r for r in rs if r.get("symbol") in uni_syms][:limit]
        if direction == "flat":   # 平盤＝有收盤、官方漲跌 0、不是 X 無比價（與首頁同一分類 price_class）
            rs = [r for r in rs if price_class(r) == "flat"][:limit]
        elif direction:
            want = 1 if direction == "up" else -1
            rs = [r for r in rs if price_direction(r) == want][:limit]
        return rs
    rows = fetch(500)
    full = spec["keys"] and all(k in filters for k in spec["keys"])
    agg_value = None
    if agg == "count":
        agg_value = len(fetch(1_000_000))
    elif agg == "sum:amount_twd":
        agg_value = sum(r.get("amount_twd") or 0 for r in fetch(1_000_000))
    if direction:
        filters = {**filters, "漲跌方向": {"up": "change>0（無 change 欄時 change_sign='+'）；不含 X 無比價",
                                         "down": "change<0（無 change 欄時 change_sign='-'）；不含 X 無比價",
                                         "flat": "有收盤價、change=0、change_sign≠'X'（不含無比價）"}[direction]}
    agg_expr = AGGREGATES.get(agg, "")
    if in_universe:
        u = src.universe
        filters = {**filters, "追蹤範圍": f"symbol ∈ {u.label}（{u.source}）" if u.symbols is not None else u.label}
        if agg == "count" and table == "material_infos":   # 公告是「則數」：一家公司可有多則
            agg_expr = f"count(*) WHERE symbol ∈ 追蹤範圍（{u.label}）"
            agg_value = len(fetch(1_000_000))
        elif agg == "count":
            agg_expr = f"count(DISTINCT symbol) WHERE symbol ∈ 追蹤範圍（{u.label}）"
            agg_value = len({r.get("symbol") for r in fetch(1_000_000)})
    symbol = filters.get("symbol")
    related = []
    if symbol:
        anchor = {"daily_prices": "quote", "monthly_revenues": "revenue", "income_statements": "fin",
                  "cash_flow_statements": "fin", "material_infos": "events"}.get(table, "")
        related.append((f"個股頁 {symbol}", f"/stock/{symbol}" + (f"#{anchor}" if anchor else "")))
        if table == "daily_prices":
            related.append(("選股結果列", f"/screener#row-{symbol}"))
    if table in ("source_status", "job_runs"):
        related.append(("資料狀態頁", "/status#" + ("src-" + filters.get("source_id", "") if table == "source_status" else "jobs")))
    status = 200 if (rows or agg) else 404
    return dict(table=table, spec=spec, filters=filters, rows=rows, full=full, agg=agg,
                agg_expr=agg_expr, agg_value=agg_value, related=related), status


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    src = get_source()
    res = src.search(q)
    if len(res) == 1 and q.strip():
        return RedirectResponse(f"/stock/{res[0]['code']}", status_code=302)
    return templates.TemplateResponse(request, "search.html", ctx(request, nav="", q=q, results=res, universe_label=src.universe.label))


@app.get("/api/stocks")
def api_stocks():
    """搜尋框自動完成用（代號＋名稱清單）。"""
    return [{"code": s["code"], "name": s["name"]} for s in get_source().universe_stocks()]   # 只列追蹤範圍內（可開頁）的股票


@app.get("/healthz")
def healthz():
    src = get_source()
    return {"ok": True, "source": src.name, "is_test": src.is_test}


@app.exception_handler(DataUnavailable)
async def data_unavailable(request: Request, exc: DataUnavailable):
    return templates.TemplateResponse(request, "error.html", ctx(
        request, message=f"{exc.label}：{exc.detail}", state=exc.state,
        hint="資料庫尚未建立或仍在入庫中；請稍後再試或到資料狀態頁查看。"), status_code=503)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    detail = getattr(exc, "detail", "找不到頁面")
    return templates.TemplateResponse(request, "error.html", ctx(request, message=detail), status_code=404)
