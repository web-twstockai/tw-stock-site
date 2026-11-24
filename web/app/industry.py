"""產業分類（/industry、/industry/{產業名稱}）：HUB_HANDOFF §3、§4.2、§5.5；設計原型 A 的產業分類頁。

資料：securities.industry（唯讀 DB 的官方產業類別名稱；上市、上櫃同名合併為一類）＋評估日 daily_prices。
- 產業代碼：資料庫 securities.industry_code 目前全為空，所以網址用產業名稱本身（穩定、不另編代碼）。
- 只列追蹤範圍（TWSITE_UNIVERSE）內的普通股；範圍不是全市場時畫面寫明。
- 漲跌幅取 analysis/screener.py screen() 的 change_pct（網站不自算百分比）；收盤、漲跌（元）、成交金額取 daily_prices 原值。
- 產業別對照：只有現行 securities 一份（securities_history 沒有生效區間），畫面寫出對照的資料日期，不假裝是歷史對照。
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .data_source import DataSource, DataUnavailable, source_href
from .models import STATUS_INSUFFICIENT, TEST_LABEL, Val

COLS = [  # key, 欄名, 數字欄?, 預設方向
    ("code", "代號", False, "asc"), ("name", "名稱", False, "asc"), ("market", "市場", False, "asc"),
    ("close", "收盤（元）", True, "desc"), ("change", "漲跌（元）", True, "desc"), ("change_pct", "漲跌幅", True, "desc"),
    ("trade_value", "成交金額（億元）", True, "desc"),
]
COL_KEYS = {c[0]: c for c in COLS}
MISSING_INDUSTRY = "未分類"   # securities.industry 為空


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _stocks(src: DataSource) -> list[dict]:
    return src.universe_stocks()


def _mapping_date(src: DataSource) -> Optional[str]:
    try:
        rows = src.get_source_rows("securities", {}, limit=1_000_000)
    except (DataUnavailable, KeyError):
        return None
    ds = sorted({str(r.get("data_date")) for r in rows if r.get("data_date")})
    return ds[-1] if ds else None


def _ind_name(s: dict) -> str:
    v = (s.get("industry") or "").strip()
    return v if v and v != "—" else MISSING_INDUSTRY


def index(src: DataSource, heat: bool = True) -> dict:
    view: dict[str, Any] = dict(industries=[], total=0, error="", mapping_date=None, is_test=src.is_test,
                                universe_all=True, universe_label="", missing_count=0, heat_ready=False,
                                heat_note="", heat_as_of=None)
    try:
        u = src.universe
        view.update(universe_all=u.symbols is None, universe_label=u.label)
        stocks = _stocks(src)
    except Exception as e:
        view["error"] = f"讀不到股票清單（{type(e).__name__}：{e}）"
        return view
    view["mapping_date"] = _mapping_date(src)
    groups: dict[str, dict] = {}
    for s in stocks:
        n = _ind_name(s)
        g = groups.setdefault(n, dict(industry_id=n, name=n, stock_count=0, listed_count=0, otc_count=0))
        g["stock_count"] += 1
        if s.get("market") == "上櫃":
            g["otc_count"] += 1
        else:
            g["listed_count"] += 1
    # 全市場的產業類別（含追蹤範圍內 0 檔的）也列出，0 檔照實顯示
    try:
        for s in src.list_stocks():
            n = _ind_name(s)
            groups.setdefault(n, dict(industry_id=n, name=n, stock_count=0, listed_count=0, otc_count=0))
    except Exception:
        pass
    inds = sorted(groups.values(), key=lambda g: (g["name"] == MISSING_INDUSTRY, g["name"]))
    view["industries"] = inds
    view["total"] = sum(g["stock_count"] for g in inds)
    view["missing_count"] = groups.get(MISSING_INDUSTRY, {}).get("stock_count", 0)
    if heat and inds and not view.get("error"):
        _attach_heat(src, view)
    return view


HEAT_SCALE = 3.0  # 加權漲跌幅達 ±3% 時顏色最深；實際數字仍照實顯示
HEAT_DISC = "這是依官方產業別做的當日統計，只供瀏覽比較，不是買進或賣出建議。產業分類較粗，檔數少的產業波動會比較大。"


def _attach_heat(src: DataSource, view: dict) -> None:
    """在產業總覽各格加上成交金額加權漲跌幅與成交金額。漲跌幅只取 analysis screen() 的 change_pct。"""
    as_of = (src.meta() or {}).get("data_date") or ""
    view["heat_as_of"] = as_of
    view["heat_disc"] = HEAT_DISC
    if not as_of:
        view["heat_note"] = "沒有資料日期，無法計算各產業的加權漲跌幅與成交金額。"
        view["heat_ready"] = False
        for g in view["industries"]:
            g.update(_empty_heat("沒有資料日期"))
        return
    px = _price_rows(src, as_of)
    pct, td = _change_pct(src)
    pct_ok = bool(td) and td == as_of and bool(pct)
    if not px:
        view["heat_note"] = f"資料日 {as_of} 沒有行情，無法計算加權漲跌幅與成交金額。"
        view["heat_ready"] = False
        for g in view["industries"]:
            g.update(_empty_heat(view["heat_note"]))
        return
    if not pct_ok:
        view["heat_note"] = "選股引擎的評估日與資料日期不同或無法執行，加權漲跌幅暫不顯示（本站不自算漲跌幅）。"
    stocks = _stocks(src)
    by: dict[str, list] = {}
    for s in stocks:
        by.setdefault(_ind_name(s), []).append(s)
    amounts = []
    for g in view["industries"]:
        g.update(_heat_group(by.get(g["industry_id"]) or [], px, pct if pct_ok else {}, as_of, src.is_test))
        if g.get("trade_value"):
            amounts.append(g["trade_value"])
    max_tv = max(amounts) if amounts else 0
    for g in view["industries"]:
        tv = g.get("trade_value") or 0
        g["flex"] = max(1, int(round((tv / max_tv) * 12))) if max_tv > 0 and tv > 0 else 1
        g["bar_pct"] = int(round((tv / max_tv) * 100)) if max_tv > 0 and tv > 0 else 0
    view["heat_ready"] = True


def _empty_heat(reason: str) -> dict:
    return dict(heat_status="insufficient", heat_reason=reason, weighted_pct=None, trade_value=None,
                trade_value_yi=None, up_n=0, down_n=0, flat_n=0, rated_n=0, kind="na", intensity=0,
                flex=1, bar_pct=0, aria="")


def _heat_group(members: list[dict], px: dict, pct: dict, as_of: str, is_test: bool) -> dict:
    if not members:
        d = _empty_heat("追蹤範圍內目前沒有股票")
        d["heat_status"] = "empty"
        d["aria"] = "目前沒有股票"
        return d
    w_sum = 0.0
    w_amt = 0.0
    tv = 0.0
    tv_ok = False
    up_n = down_n = flat_n = rated_n = 0
    priced = 0
    for s in members:
        r = px.get(s["code"])
        close = _num(r.get("close_twd")) if r else None
        if r is None or close is None:
            continue
        priced += 1
        amt = _num(r.get("amount_twd"))
        if amt is not None:
            tv += amt
            tv_ok = True
        cp = pct.get(s["code"])
        if cp is None:
            continue
        rated_n += 1
        if cp > 0:
            up_n += 1
        elif cp < 0:
            down_n += 1
        else:
            flat_n += 1
        if amt is not None and amt > 0:
            w_sum += cp * amt
            w_amt += amt
    yi = (tv / 1e8) if tv_ok else None
    if w_amt > 0:
        wp = w_sum / w_amt
        status, reason = "ok", ""
    else:
        wp = None
        status, reason = "insufficient", "這個產業沒有可計的成交金額加權漲跌幅（缺行情或分析引擎未提供漲跌幅，本站不自算）"
    kind, intensity, bg = _heat_style(wp)
    src_label = TEST_LABEL if is_test else "daily_prices"
    wp_val = Val(wp, "%", "analysis/screener.py screen()" if not is_test else TEST_LABEL, as_of,
                 status=STATUS_INSUFFICIENT if wp is None else "ok",
                 note=reason if wp is None else "成交金額加權漲跌幅（權重＝當日成交金額）", digits=2)
    amt_val = Val(yi, "億元", src_label, as_of,
                  status=STATUS_INSUFFICIENT if yi is None else "ok",
                  note="" if yi is not None else "沒有可計的成交金額", digits=2)
    parts = []
    if wp is not None:
        side = "上漲" if wp > 0 else "下跌" if wp < 0 else "平盤"
        parts.append(f"加權漲跌幅{side} {abs(wp):.2f}%")
    else:
        parts.append("加權漲跌幅資料不足")
    if yi is not None:
        parts.append(f"成交金額 {yi:,.2f} 億元")
    parts.append(f"{len(members)} 檔")
    if rated_n:
        parts.append(f"可計漲跌幅 {rated_n} 檔中 {up_n} 檔上漲")
    aria = "，".join(parts)
    return dict(heat_status=status, heat_reason=reason, weighted_pct=wp_val, trade_value=(tv if tv_ok else None),
                trade_value_yi=amt_val, up_n=up_n, down_n=down_n, flat_n=flat_n, rated_n=rated_n,
                priced_n=priced, kind=kind, intensity=intensity, bg=bg, flex=1, bar_pct=0, aria=aria)


def _heat_style(pct: Optional[float]) -> tuple[str, float, str]:
    """回傳 (漲跌類別, 0～1 深淺, 背景色)。顏色只做輔助，格子上另有數字與▲▼■。"""
    if pct is None:
        return "na", 0.0, ""
    if pct == 0:
        return "flat", 0.0, "#f3f1ec"
    intensity = min(1.0, abs(pct) / HEAT_SCALE)
    alpha = 0.14 + 0.32 * intensity
    if pct > 0:
        return "up", intensity, f"rgba(196,38,29,{alpha:.3f})"
    return "down", intensity, f"rgba(22,122,70,{alpha:.3f})"


def _price_rows(src: DataSource, date: str) -> dict:
    try:
        rows = src.get_source_rows("daily_prices", {"price_date": date}, limit=1_000_000)
    except (DataUnavailable, KeyError):
        return {}
    return {r["symbol"]: r for r in rows}


def _change_pct(src: DataSource) -> tuple[dict, str]:
    """analysis screen()（預設參數）的 change_pct；與首頁、篩選中心共用同一份快取結果。"""
    from .screening import run_screen
    from .models import ScreenParams
    try:
        r = run_screen(src, ScreenParams())
        out = r.extra.get("analysis_result")
        if r.error or out is None:
            return {}, r.trade_date
        return {str(x["symbol"]): _num(x.get("change_pct")) for x in out.summary.to_dict("records")}, r.trade_date
    except Exception:
        return {}, "—"


def detail(src: DataSource, industry_id: str, sort: Optional[str] = None, direction: Optional[str] = None) -> dict:
    view = index(src, heat=False)
    ind = next((g for g in view["industries"] if g["industry_id"] == industry_id), None)
    view.update(industry=ind, rows=[], na=[], sort="trade_value", dir="desc", cols=COLS, as_of=src.meta().get("data_date"))
    if ind is None or view.get("error"):
        return view
    sort = sort if sort in COL_KEYS else "trade_value"
    direction = direction if direction in ("asc", "desc") else COL_KEYS[sort][3]
    view.update(sort=sort, dir=direction)
    test = src.is_test
    dd = view["as_of"]
    px = _price_rows(src, dd) if dd else {}
    pct, td = _change_pct(src)
    pct_ok = td == dd
    src_label = TEST_LABEL if test else "daily_prices"
    rows = []
    for s in _stocks(src):
        if _ind_name(s) != industry_id:
            continue
        r = px.get(s["code"])
        href = source_href("daily_prices", symbol=s["code"], price_date=dd)
        ref = {"table": "daily_prices", "key": f"symbol={s['code']}, price_date={dd}"}

        def V(v, unit, digits=2, source=src_label, link=True):
            if v is None:
                return Val(None, unit, source, dd, status=STATUS_INSUFFICIENT, is_test=test, digits=digits,
                           note="" if link else "分析引擎排除此股時不提供漲跌幅，本站不自算")
            return Val(v, unit, source, dd, is_test=test, digits=digits, href=href if link else "",
                       ref=ref if link else {"table": "analysis/screener.py 輸出", "key": f"symbol={s['code']}, as_of={dd}"})
        close = _num(r.get("close_twd")) if r else None
        if r is None:
            st, why = "no_data", "今日行情尚未入庫"
        elif r.get("is_halted") in (1, True) and close is None:
            st, why = "halted", "今日無收盤價，不以 0 代替"
        elif close is None:
            st, why = "no_trade", "今日無成交，不以 0 代替"
        else:
            st, why = "ok", ""
        chg = None
        if r is not None and close is not None:
            if r.get("change") is not None:
                chg = _num(r.get("change"))
            elif r.get("change_twd") is not None:
                v = _num(r.get("change_twd"))
                chg = -abs(v) if v is not None and (r.get("change_sign") or "").strip() == "-" else v
            if (r.get("change_sign") or "") == "X":
                chg = None   # 無比價：不是平盤
        cp = pct.get(s["code"]) if (pct_ok and st == "ok") else None
        amt = _num(r.get("amount_twd")) if r is not None and st == "ok" else None
        rows.append(dict(code=s["code"], name=s["name"], market=s["market"], price_status=st, reason=why,
                         close=V(close, "新台幣元"), change=V(chg, "新台幣元"),
                         change_pct=V(cp, "%", source="analysis/screener.py screen()" if not test else TEST_LABEL, link=False),
                         amount=V(amt / 1e8 if amt is not None else None, "億元"),
                         _v=dict(code=s["code"], name=s["name"], market=s["market"], close=close, change=chg,
                                 change_pct=cp, trade_value=amt)))
    ok = [r for r in rows if r["price_status"] == "ok"]
    na = [r for r in rows if r["price_status"] != "ok"]
    numeric = COL_KEYS[sort][2]
    rev = direction == "desc"
    if numeric:
        have = [r for r in ok if r["_v"][sort] is not None]
        none = [r for r in ok if r["_v"][sort] is None]
        have.sort(key=lambda r: r["_v"]["code"])
        have.sort(key=lambda r: r["_v"][sort], reverse=rev)
        rows = have + none + sorted(na, key=lambda r: r["code"])   # 沒有收盤（或該欄無值）的一律排最後
    else:
        rows = sorted(rows, key=lambda r: (r["_v"][sort], r["code"]), reverse=rev)
    view["rows"] = rows
    view["na"] = na
    view["listed_count"] = sum(1 for r in rows if r["market"] != "上櫃")
    view["otc_count"] = sum(1 for r in rows if r["market"] == "上櫃")
    view["pct_note"] = "" if pct_ok else "漲跌幅：選股引擎的評估日與資料日期不同或無法執行，暫不顯示"
    return view


def sort_href(industry_id: str, view: dict, key: str) -> str:
    from urllib.parse import quote, urlencode
    if view.get("sort") == key:
        d = "asc" if view.get("dir") == "desc" else "desc"
    else:
        d = COL_KEYS[key][3]
    base = "/industry/" + quote(industry_id)
    if key == "trade_value" and d == "desc":
        return base
    return base + "?" + urlencode({"sort": key, "dir": d})
