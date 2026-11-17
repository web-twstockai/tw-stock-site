"""呼叫研究分析函式的接點（個股均線序列、漲跌幅序列）。網站不自行計算；函式不存在或失敗時 → 畫面顯示「資料不足」。

對照 analysis/series.py（analysis-0.3.0，2026-11-10 02:2x 出現）：
  ma_series(prices, actions, calendar, symbol, windows=(20,60), as_of, actions_coverage, securities, params)
      → DataFrame[price_date, ma_20, ma_20_note, ma_60, ma_60_note, status, …]
  change_series(prices, actions, calendar, symbol, as_of, actions_coverage, securities, params)
      → DataFrame[price_date, change_pct, status, note, …]
輸入：SqliteSource → analysis/io_sqlite.load_symbol_inputs(db, symbol)（USAGE.md §1）；FakeSource → screener_frame() 切出該股。
change_series 的 change_check（ok／mismatch／no_source／source_not_comparable／source_sign_conflict）只標記，不改值。
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from .data_source import PROJECT_ROOT, DataSource, DataUnavailable
from .models import ma_note_label, unavailable_label

CANDIDATE_MODULES = ["series", "indicators", "adjust", "screener"]
MA_FUNC = "ma_series"
CHANGE_FUNC = "change_series"

_OVERRIDE: dict[str, Optional[Callable]] = {}   # 測試用注入（值為 None＝模擬函式不存在）
_LOADED: dict[str, Any] = {}
_FRAME_CACHE: dict[int, tuple[Any, dict, Any]] = {}


def _load(name: str):
    if name in _LOADED:
        return _LOADED[name]
    from . import screening
    from .screening import analysis_module
    mod = None
    if (screening.ANALYSIS_DIR / f"{name}.py").exists():
        try:
            mod = analysis_module(name)   # 頂層名稱，與 screener／narrative 共用同一份模組
        except Exception:
            mod = None
    _LOADED[name] = mod
    return mod


def get_func(func: str) -> Optional[Callable]:
    if func in _OVERRIDE:
        return _OVERRIDE[func]
    for name in CANDIDATE_MODULES:
        mod = _load(name)
        fn = getattr(mod, func, None) if mod else None
        if callable(fn):
            return fn
    return None


def describe(func: str) -> str:
    return f"analysis 尚未提供 {func}()，網站不自行計算"


def clear_cache() -> None:
    _FRAME_CACHE.clear()
    _LOADED.clear()
    _BULK.clear()
    _RESULT_CACHE.clear()


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 4)


def data_version(source: DataSource):
    """資料版本（快取鍵）：SqliteSource＝資料庫檔與 -wal 的 (mtime_ns, size)；其他來源固定 0。"""
    fn = getattr(source, "data_version", None)
    try:
        return fn() if callable(fn) else 0
    except Exception:
        return time.monotonic()   # 讀不到版本：不共用快取


def _frame(source: DataSource) -> dict:
    """screener_frame()（追蹤範圍全部股票的 analysis 輸入）依資料版本快取；選股與個股頁共用同一份。"""
    ver = (data_version(source), _universe_key(source))
    hit = _FRAME_CACHE.get(id(source))
    if hit and hit[0] == ver and hit[2] is source:
        return hit[1]
    kw = source.screener_frame()["kwargs"]
    _FRAME_CACHE[id(source)] = (ver, kw, source)
    _BULK.pop(id(source), None)
    return kw


def _universe_key(source: DataSource):
    try:
        u = source.universe
        return (u.name, u.symbols)
    except Exception as e:
        return ("error", str(e))


_BULK: dict[int, tuple[Any, dict]] = {}
_RESULT_CACHE: dict[tuple, Any] = {}   # (來源, 資料版本, 函式, 代號) → 結果；只留最近 _RESULT_MAX 筆
_RESULT_MAX = 64


def _bulk_groups(source: DataSource) -> dict:
    """把 _frame() 依代號切好（prices／actions／securities），供 _symbol_inputs() 取單檔。"""
    kw = _frame(source)
    hit = _BULK.get(id(source))
    if hit and hit[1]["kw"] is kw:
        return hit[1]

    def split(df):
        if df is None or len(df) == 0 or "symbol" not in df.columns:
            return {}
        return {str(k): g.reset_index(drop=True) for k, g in df.groupby(df["symbol"].astype(str).str.strip(), sort=False)}
    g = {"prices": split(kw.get("prices")), "actions": split(kw.get("actions")),
         "securities": split(kw.get("securities")), "kw": kw}
    _BULK[id(source)] = (None, g)
    return g


def _memo(source: DataSource, func: str, code: str, compute: Callable[[], Any], deps: tuple = ()):
    """同一來源物件、同一資料版本、同一組 analysis 函式（deps）時重用結果（個股頁 adj／raw 兩種模式共用一次計算）。"""
    key = (id(source), data_version(source), _universe_key(source), BULK_INPUTS, func, code, tuple(id(d) for d in deps))
    hit = _RESULT_CACHE.get(key)
    if hit is not None and hit[0] is source:
        return hit[1]
    v = compute()
    if len(_RESULT_CACHE) >= _RESULT_MAX:
        _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
    _RESULT_CACHE[key] = (source, v)
    return v


def _bulk_inputs(source: DataSource, code: str) -> Optional[dict]:
    """追蹤範圍內的股票：由 screener_frame()（io_sqlite.load_screen_inputs，同一視窗）切出該股；
    內容與 io_sqlite.load_symbol_inputs(db, code) 相同（analysis 內部也是依 symbol 篩選），但不必每檔重讀全表。
    範圍外的股票回 None（改走 load_symbol_inputs）。"""
    try:
        uni = source.universe_symbols()
    except Exception:
        return None
    if uni is not None and code not in uni:
        return None
    g = _bulk_groups(source)
    kw = g["kw"]
    prices = kw.get("prices")
    if prices is None:
        return None
    empty = lambda df: df.iloc[0:0].reset_index(drop=True) if df is not None else None  # noqa: E731
    return dict(prices=g["prices"].get(code, empty(prices)), actions=g["actions"].get(code, empty(kw.get("actions"))),
                calendar=kw["calendar"], symbol=str(code), as_of=kw.get("as_of"),
                actions_coverage=kw.get("actions_coverage"),
                securities=g["securities"].get(code, empty(kw.get("securities"))))


BULK_INPUTS = True   # False＝每檔呼叫 io_sqlite.load_symbol_inputs()（舊行為；兩者輸出逐檔比對相同，見第十六批回報）


def _symbol_inputs(source: DataSource, code: str) -> dict:
    """USAGE.md：SQLite 用 io_sqlite.load_symbol_inputs(db, symbol)；FakeSource 由同一份 screen 輸入切出該股。
    SQLite 且在追蹤範圍內：改由已快取的 screener_frame() 切出（BULK_INPUTS；同一份 io_sqlite 輸出）。"""
    db = getattr(source, "db_path", None)
    if db is not None and BULK_INPUTS:
        if not db.exists():
            raise DataUnavailable("db_missing", str(db))
        try:
            b = _bulk_inputs(source, code)
        except DataUnavailable:
            b = None
        if b is not None:
            return b
    if db is not None:
        if not db.exists():
            raise DataUnavailable("db_missing", str(db))
        io = _load("io_sqlite")
        if io is not None and hasattr(io, "load_symbol_inputs"):
            try:
                # 評估日與選股相同：追蹤範圍內最新行情日（範圍外較新的列不讓個股頁跳日）
                lp = getattr(source, "_latest_price_date", None)
                as_of = lp() if callable(lp) and source.universe_symbols() is not None else None
                return io.load_symbol_inputs(db, code, **({"as_of": as_of} if as_of else {}))
            except Exception as e:
                raise DataUnavailable("db_error", f"io_sqlite.load_symbol_inputs：{type(e).__name__}: {e}")
    kw = _frame(source)
    prices = kw.get("prices")
    if prices is None or len(prices) == 0:
        return dict(kw, prices=prices, symbol=code)
    out = dict(prices=prices[prices["symbol"] == code], actions=kw.get("actions"), calendar=kw["calendar"],
               symbol=code, as_of=kw.get("as_of"), actions_coverage=kw.get("actions_coverage"),
               securities=kw.get("securities"))
    demo = getattr(source, "kline_demo_inputs", None)   # 只有 FakeSource 有：K 線示範案例（不影響 sqlite、不影響選股）
    return demo(code, out) if callable(demo) else out


def stock_series(source: DataSource, code: str, windows=(20, 60)) -> dict:
    import copy
    return copy.deepcopy(_memo(source, f"stock_series{tuple(windows)}", code, lambda: _stock_series(source, code, windows),
                               (get_func(MA_FUNC), get_func(CHANGE_FUNC))))


def _stock_series(source: DataSource, code: str, windows=(20, 60)) -> dict:
    """回傳 {"ma": {日期: {N: 值}}, "ma_note": {N: 說明}, "change_pct": {日期: 值}, "notes": [...],
    "has_ma": bool, "has_change": bool}；函式不存在 → has_* False。"""
    out: dict = {"ma": {}, "ma_note": {}, "ma_note_raw": {}, "ma_unavailable_raw": "", "change_pct": {}, "change_check": {}, "notes": [], "has_ma": False,
                 "has_change": False, "engine": ""}
    fn_ma, fn_chg = get_func(MA_FUNC), get_func(CHANGE_FUNC)
    if not fn_ma:
        out["notes"].append(describe(MA_FUNC))
    if not fn_chg:
        out["notes"].append(describe(CHANGE_FUNC))
    if not fn_ma and not fn_chg:
        return out
    try:
        common = _symbol_inputs(source, code)
    except DataUnavailable as e:
        out["notes"].append(f"無法取得行情輸入：{e}")
        return out
    prices = common.get("prices")
    if prices is None or len(prices) == 0:
        out["notes"].append("資料不足：daily_prices 沒有現行列")
        return out
    out["engine"] = "analysis/series.py"
    if fn_ma:
        try:
            df = fn_ma(windows=tuple(windows), **common)
            for r in df.to_dict("records"):
                d = str(r["price_date"])[:10]
                out["ma"][d] = {w: _num(r.get(f"ma_{w}")) for w in windows}
            if len(df):
                last = df.iloc[-1]
                for w in windows:
                    note = last.get(f"ma_{w}_note")
                    if isinstance(note, str) and note:
                        ml = ma_note_label(note)
                        out["ma_note"][w] = ml["label"]
                        out["ma_note_raw"][w] = ml["raw"]
                if last.get("status") == "unavailable" and isinstance(last.get("unavailable_reason"), str):
                    lab = unavailable_label(last.get("unavailable_code"), last["unavailable_reason"])
                    out["notes"].append(f"均線不可用：{lab['label']}")
                    out["ma_unavailable_raw"] = lab["raw"]
            out["has_ma"] = True
        except Exception as e:  # analysis 拋錯 → 顯示資料不足，不回 500
            out["notes"].append(f"ma_series() 執行失敗：{type(e).__name__}: {e}")
    if fn_chg:
        try:
            df = fn_chg(**common)
            for r in df.to_dict("records"):
                d = str(r["price_date"])[:10]
                out["change_pct"][d] = _num(r.get("change_pct"))
                chk = r.get("change_check")
                out["change_check"][d] = {
                    "check": chk if isinstance(chk, str) else "",
                    "calc_twd": _num(r.get("change_twd_calc")), "source_twd": _num(r.get("source_change_twd")),
                    "diff_twd": _num(r.get("change_check_diff_twd")), "tol_twd": _num(r.get("change_tolerance_twd")),
                    "status": r.get("status") if isinstance(r.get("status"), str) else "",
                    "note": r.get("note") if isinstance(r.get("note"), str) else ""}
            out["has_change"] = True
        except Exception as e:
            out["notes"].append(f"change_series() 執行失敗：{type(e).__name__}: {e}")
    return out


# ---- 個股 K 線（analysis-0.7.0 series.ohlc_series；前復權）----
OHLC_FUNC = "ohlc_series"
EVENT_SHORT = {"cash_dividend": "除息", "stock_dividend": "除權", "ex_both": "除權息", "capital_reduction": "減資",
               "par_value_change": "面額變更", "stock_split": "分割"}
EVENT_KIND_LABEL = {"cash_dividend": "現金股利", "stock_dividend": "股票股利", "ex_both": "現金及股票股利",
                    "capital_reduction": "減資", "par_value_change": "變更面額", "stock_split": "股票分割"}
_DESC_RE = __import__("re").compile(r"^\S+\s+\S+\s+f=([-\d.]+)\s+s=([-\d.]+)(.*)$")


def event_desc_zh(kind: str, status: str, price_factor, share_factor, code: str, detail: str, desc: str) -> str:
    """事件說明中文化（HANDOFF §18.3-4）：例「現金股利，調整因子 0.997335」。因子取 analysis desc 原字串（6 位小數），
    沒有時用 price_factor／share_factor；網站不重算。失敗事件：「現金股利，無法還原：〈排除代碼中文〉」。"""
    name = EVENT_KIND_LABEL.get(kind, kind or "事件")
    if code:
        try:
            from .screening import exclusion_label
            why = exclusion_label(code) or code
        except Exception:
            why = code
        detail = __import__("re").sub(r"^\d{4}-\d{2}-\d{2}\s+[a-z_]+\s*", "", detail or "").strip()
        return f"{name}，無法還原：{why}" + (f"（{detail}）" if detail and detail not in why else "")
    m = _DESC_RE.match(desc or "")
    f_s, s_s, tail = (m.group(1), m.group(2), m.group(3).strip()) if m else (
        None if price_factor is None else f"{price_factor:.6f}", None if share_factor is None else f"{share_factor:.6f}", "")
    tail = tail.lstrip("＋+").strip()
    if f_s is None:
        out = name
    elif s_s is None or abs(float(s_s) - 1.0) < 1e-12:
        out = f"{name}，調整因子 {f_s}"
    else:
        out = f"{name}，價格調整因子 {f_s}、股數調整因子 {s_s}"
    if not tail:
        return out
    return out + (tail if tail.startswith(("（", "(")) else f"；{tail}")


EVENT_STATUS_LABEL = {"applied": "已用於還原", "before_boundary": "在可用範圍起日之前（未用於還原）",
                      "failed": "無法解析（未用於還原）"}


def _date(v) -> str:
    return str(v)[:10] if v is not None and str(v) not in ("NaT", "None", "nan") else ""


def stock_ohlc(source: DataSource, code: str) -> dict:
    import copy
    return copy.deepcopy(_memo(source, "stock_ohlc", code, lambda: _stock_ohlc(source, code), (get_func(OHLC_FUNC),)))


def _stock_ohlc(source: DataSource, code: str) -> dict:
    """呼叫 series.ohlc_series(**load_symbol_inputs)；回傳
    {"status": ok|insufficient|error, "note", "bars": {日期: {...}}, "events": [...], "boundary_date", "all_unavailable",
     "engine"}。失敗不拋例外。網站不重算還原價。"""
    out = {"status": "ok", "note": "", "bars": {}, "events": [], "boundary_date": "", "all_unavailable": False,
           "engine": "analysis/series.py ohlc_series()"}
    fn = get_func(OHLC_FUNC)
    if not fn:
        out.update(status="error", note=describe(OHLC_FUNC))
        return out
    try:
        common = _symbol_inputs(source, code)
    except DataUnavailable as e:
        out.update(status="insufficient", note=f"無法取得行情輸入：{e}")
        return out
    if common.get("prices") is None or len(common["prices"]) == 0:
        out.update(status="insufficient", note="資料不足：daily_prices 沒有現行列")
        return out
    try:
        o = fn(**common)
        for r in o.bars.to_dict("records"):
            d = _date(r.get("price_date"))
            out["bars"][d] = {k: (_num(r.get(k)) if k not in ("status", "unavailable_code", "unavailable_reason")
                                  else (r.get(k) if isinstance(r.get(k), str) else ""))
                              for k in ("open_twd", "high_twd", "low_twd", "close_twd", "volume_shares", "amount_twd",
                                        "adj_factor", "share_factor", "adj_open_twd", "adj_high_twd", "adj_low_twd",
                                        "adj_close_twd", "adj_volume_shares", "status", "unavailable_code",
                                        "unavailable_reason")}
        for r in o.events.to_dict("records"):
            kind = r.get("action_kind") if isinstance(r.get("action_kind"), str) else ""
            st = r.get("status") if isinstance(r.get("status"), str) else ""
            S = lambda k: r.get(k) if isinstance(r.get(k), str) else ""  # noqa: E731
            pf = None if r.get("price_factor") is None or r.get("price_factor") != r.get("price_factor") else float(r["price_factor"])
            sf = None if r.get("share_factor") is None or r.get("share_factor") != r.get("share_factor") else float(r["share_factor"])
            out["events"].append({
                "desc_zh": event_desc_zh(kind, st, pf, sf, S("code"), S("detail"), S("desc")),
                "desc_raw": S("desc") or S("detail"),
                "code_label": unavailable_label(S("code"))["label"] if S("code") else "",
                "date": _date(r.get("action_date")), "action_type": r.get("action_type") if isinstance(r.get("action_type"), str) else "",
                "action_kind": kind, "short": EVENT_SHORT.get(kind, kind or "事件"), "status": st,
                "status_label": EVENT_STATUS_LABEL.get(st, st), "price_factor": _num(r.get("price_factor")),
                "share_factor": _num(r.get("share_factor")), "code": r.get("code") if isinstance(r.get("code"), str) else "",
                "detail": r.get("detail") if isinstance(r.get("detail"), str) else "",
                "desc": r.get("desc") if isinstance(r.get("desc"), str) else ""})
        out["events"].sort(key=lambda e: e["date"])
        ua = next((b for b in out["bars"].values() if b["status"] == "unavailable"), None)
        lab = unavailable_label(ua["unavailable_code"], ua["unavailable_reason"]) if ua else unavailable_label("", "")
        out["unavailable"] = lab["label"]            # 中文（models.unavailable_label）
        out["unavailable_code"] = lab["code"]
        out["unavailable_raw"] = lab["raw"]          # 原始代碼：說明（只放 title／API）
        out["boundary_date"] = _date(o.boundary_date)
        out["all_unavailable"] = bool(o.all_unavailable)
        if not out["bars"]:
            out.update(status="insufficient", note="ohlc_series() 沒有回傳任何交易日")
    except Exception as e:   # analysis 拋錯 → 錯誤狀態，頁面照常（改顯示原始價）
        out.update(status="error", note=f"ohlc_series() 執行失敗：{type(e).__name__}: {e}", bars={}, events=[])
    return out


def kline(source: DataSource, code: str, rows: list[dict], mode: str = "adj") -> dict:
    """依模式組 K 線：adj＝前復權（ohlc_series 的 adj_*；不可用的交易日不畫，不以原始價補）；raw＝原始價（daily_prices）。
    adj 完全不可用時改顯示原始價並說明（fallback）。markers：視窗內除權息／減資事件（lightweight-charts setMarkers 格式）。"""
    mode = "raw" if mode == "raw" else "adj"
    o = stock_ohlc(source, code)
    dates = [r["date"] for r in rows]
    res = {"requested": mode, "mode": mode, "fallback_note": "", "status": o["status"], "note": o["note"],
           "events": [], "boundary_date": o["boundary_date"], "all_unavailable": o["all_unavailable"],
           "engine": o["engine"], "candles": [], "volume": [], "markers": [], "skipped": 0, "fallback_raw": "",
           "unavailable": o.get("unavailable", ""), "unavailable_code": o.get("unavailable_code", ""),
           "unavailable_raw": o.get("unavailable_raw", "")}
    if mode == "adj":
        for d in dates:
            b = o["bars"].get(d)
            if b and b["status"] == "ok" and None not in (b["adj_open_twd"], b["adj_high_twd"], b["adj_low_twd"], b["adj_close_twd"]):
                res["candles"].append({"time": d, "open": b["adj_open_twd"], "high": b["adj_high_twd"],
                                       "low": b["adj_low_twd"], "close": b["adj_close_twd"]})
                if b["adj_volume_shares"] is not None:
                    res["volume"].append({"time": d, "value": b["adj_volume_shares"]})
            else:
                res["skipped"] += 1
        if not res["candles"]:
            why = o["note"] or ("還原價整段不可用" if o["all_unavailable"] else "視窗內沒有可用的還原價")
            if not o["note"] and o.get("unavailable"):
                why = f"{why}，原因為{o['unavailable']}"
            res["fallback_raw"] = o.get("unavailable_raw", "") or o["note"]
            res.update(mode="raw", fallback_note=f"前復權 K 線不可用：{why}；改顯示原始價。")
    if res["mode"] == "raw":
        res["candles"] = [{"time": r["date"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
                          for r in rows if r.get("close") is not None]
        res["volume"] = [{"time": r["date"], "value": r["volume"]} for r in rows if r.get("volume") is not None]
        res["skipped"] = 0
    c = res["candles"]   # 圖說期間＝實際畫出的那段（HANDOFF §18.5-1）
    res["drawn_start"], res["drawn_end"], res["drawn_days"] = (c[0]["time"], c[-1]["time"], len(c)) if c else ("", "", 0)
    res["caption_period"] = f"{c[0]['time']}～{c[-1]['time']} 共 {len(c)} 個交易日" if c else "沒有可畫的交易日"
    if dates:
        lo, hi = dates[0], dates[-1]
        res["events"] = [e for e in o["events"] if lo <= e["date"] <= hi]
        # color 由前端決定（HANDOFF §18.3-2，TOKENS §12），後端不給
        res["markers"] = [{"time": e["date"], "position": "aboveBar", "shape": "arrowDown",
                           "text": e["short"] + ("" if e["status"] == "applied" else "（未還原）")}
                          for e in res["events"] if e["date"] in set(dates)]
    return res
