"""選股的唯一呼叫點：一律呼叫研究分析的 analysis/screener.py（網站不自行計算指標）。

FakeSource 與 SqliteSource 都提供 screener_frame()（analysis 介面的 DataFrame），
這裡只負責：參數轉換 → 呼叫 screen() → 把輸出轉成網站顯示用的 ScreenResult。
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .data_source import PROJECT_ROOT, DataSource, DataUnavailable, source_href
from .models import (TEST_LABEL, VOLUME_BASIS_STATUS_LABELS, Condition, Excluded, ScreenParams, ScreenResult, ScreenRow, Val,
                     UNKNOWN_CODE_LABEL, analysis_text_zh, screen_extra_fields, zh_text)

# 研究分析模組所在目錄；預設 PROJECT_ROOT/analysis。TWSITE_ANALYSIS_DIR 只供測試或驗收切換（例如套用 pending patch 的暫存副本），
# 網站不改 analysis/ 本身。
ANALYSIS_DIR = Path(os.environ.get("TWSITE_ANALYSIS_DIR") or (PROJECT_ROOT / "analysis"))
ANALYSIS_SCREENER = ANALYSIS_DIR / "screener.py"

SORT_KEYS = {
    "code": ("代號", lambda r: r.code),
    "change_pct": ("漲跌幅", lambda r: r.change_pct.value or 0),
    "turnover": ("成交金額", lambda r: r.turnover.value or 0),
    "vol_ratio": ("量比", lambda r: r.vol_ratio.value or 0),
    "close": ("收盤", lambda r: r.close.value or 0),
}
_MOD = None
_CACHE: dict[tuple, ScreenResult] = {}
_CACHE_MAX = 48


def analysis_screener_available() -> bool:
    return ANALYSIS_SCREENER.exists()


def ensure_analysis_path() -> None:
    """analysis/ 模組彼此以頂層名稱 import（如 screener → adjust）；附加到 sys.path 尾端，避免蓋過網站自己的模組。"""
    p = str(ANALYSIS_SCREENER.parent)
    if p not in sys.path:
        sys.path.append(p)


def analysis_module(name: str):
    """以 analysis 的頂層名稱匯入（screener／series／narrative／io_sqlite 共用同一份模組物件，
    narrative.summary_narrative() 的 isinstance(ScreenResult) 檢查才會成立）。"""
    ensure_analysis_path()
    mod = importlib.import_module(name)
    f = getattr(mod, "__file__", "") or ""
    if not f.startswith(str(ANALYSIS_SCREENER.parent)):
        raise ImportError(f"{name} 不是 analysis/{name}.py（{f}）")
    return mod


def use_analysis_dir(path) -> Path:
    """切換研究分析模組目錄（測試／驗收用）：移除舊目錄的 sys.path 與已載入模組、清掉網站端所有快取。回傳舊目錄。"""
    global ANALYSIS_DIR, ANALYSIS_SCREENER, _MOD
    old = ANALYSIS_DIR
    new = Path(path)
    for d in {str(old), str(new)}:
        while d in sys.path:
            sys.path.remove(d)
    for name, m in list(sys.modules.items()):
        f = getattr(m, "__file__", None) or ""
        if f.startswith(str(old) + os.sep) or f.startswith(str(new) + os.sep) or name in ("analysis_io_sqlite", "analysis_screener"):
            sys.modules.pop(name, None)
    ANALYSIS_DIR, ANALYSIS_SCREENER, _MOD = new, new / "screener.py", None
    clear_cache()
    from . import analysis_hooks, revenue_page
    analysis_hooks.clear_cache()
    getattr(revenue_page, "_CORE_CACHE", {}).clear()
    return old


def _mod():
    global _MOD
    if _MOD is None:
        _MOD = analysis_module("screener")
    return _MOD


def clear_cache() -> None:
    _CACHE.clear()
    from . import analysis_hooks, hub, modes_hub
    analysis_hooks._FRAME_CACHE.clear()
    hub.clear_cache()            # 篩選中心 M1～M21 計算結果
    modes_hub.clear_cache()      # analysis/modes.py evaluate_modes() 結果與登記表


# EXCL_SUSPENDED（確認停牌）是事實不是資料缺漏 → 不算覆蓋不足（第六批負責人決定）
# analysis-0.9.2（§8.7）：無成交、停止買賣（含 EXCL_WINDOW_NO_TRADE／EXCL_WINDOW_SUSPENDED）是「無法評估」，也不算覆蓋不足；
# EXCL_WINDOW_GAPS 只代表真正缺資料或資料錯誤，仍算。
# 「資料覆蓋不足」類排除代碼（0 檔時用來判斷是資料問題還是真的沒人符合）
COVERAGE_CODES = {
    "EXCL_ADJ_COVERAGE_GAP", "EXCL_NO_DATA", "EXCL_INSUFFICIENT_HISTORY", "EXCL_NO_ROW_SUSPENDED_OR_MISSING",
    "EXCL_WINDOW_GAPS", "EXCL_ADJ_FACTOR_MISSING",
    "EXCL_VOLUME_BASIS",   # analysis-0.9.0：量額口徑非總量＝資料口徑問題（資料工程補 dailyQuotes 後恢復）
}
_PARAM_RE = None


def analysis_defaults() -> dict:
    """analysis ScreenParams 的預設值（表單顯示用）；analysis 載入失敗時退回 USAGE.md §3 的一般參數預設（只供表單顯示，
    不會拿來計算——載入失敗時頁面顯示「無法篩選」）。"""
    import dataclasses
    try:
        return {f.name: f.default for f in dataclasses.fields(_mod().ScreenParams)}
    except Exception:
        return dict(USAGE_DEFAULTS)


USAGE_DEFAULTS = {"ma_short": 20, "ma_long": 60, "breakout_lookback": 20, "volume_lookback": 20,
                  "volume_multiplier": 1.5, "min_turnover": 100_000_000, "enable_trend": True, "enable_breakout": True,
                  "enable_volume": True, "enable_turnover": True, "coverage_policy": "strict"}


def exclusion_labels() -> dict:
    """排除代碼中文說明：優先 analysis.EXCLUSION_CODES，缺的才用網站備援（models.EXCLUSION_FALLBACK）。"""
    from .models import EXCLUSION_FALLBACK
    try:
        codes = dict(_mod().EXCLUSION_CODES)
    except Exception:
        codes = {}
    out: dict = {}
    for c, lab in codes.items():
        if c == "EXCL_NO_ROW_SUSPENDED_OR_MISSING" and "EXCL_SUSPENDED" not in codes:
            out["EXCL_SUSPENDED"] = EXCLUSION_FALLBACK["EXCL_SUSPENDED"]   # 確認停牌排在「待確認」之前
        out[c] = lab
    for c, lab in EXCLUSION_FALLBACK.items():
        out.setdefault(c, lab)
    # analysis 說明裡的表名／欄名換中文；換不完 → 未知代碼備援格式
    return {c: zh_text(lab, c)["text"] for c, lab in out.items()}


def not_evaluable_codes() -> frozenset:
    """95% 分母要移除的「無法評估」代碼：analysis screener.NOT_EVALUABLE_CODES（0.9.2 起），舊版用網站備援。"""
    from .models import NOT_EVALUABLE_FALLBACK
    try:
        return frozenset(_mod().NOT_EVALUABLE_CODES)
    except Exception:
        return NOT_EVALUABLE_FALLBACK


def not_evaluable_label(code: str) -> str:
    """無法評估代碼的畫面短標，例「無法評估（無成交）」；不是無法評估 → 空字串。"""
    from .models import NOT_EVALUABLE_KINDS
    if not code or code not in not_evaluable_codes():
        return ""
    return f"無法評估（{NOT_EVALUABLE_KINDS.get(code, '其他')}）"


def exclusion_label(code: str) -> str:
    if not code:
        return ""
    return exclusion_labels().get(code) or UNKNOWN_CODE_LABEL.format(raw=code)


def exclusion_label_source(code: str) -> str:
    try:
        if code in _mod().EXCLUSION_CODES:
            return "analysis/screener.py EXCLUSION_CODES"
    except Exception:
        pass
    return "網站備援說明（analysis 尚未提供）"


def error_param(msg: str) -> str:
    """從 analysis 錯誤訊息找出是哪個參數（只用來標示欄位，不改訊息）。"""
    import re
    from .models import PARAM_LABELS
    m = re.search("|".join(sorted(map(re.escape, PARAM_LABELS), key=len, reverse=True)), msg)
    if m:
        return m.group(0)
    if "至少要啟用" in msg:
        return "enable_trend"
    return ""


def _key(source: DataSource, p: ScreenParams) -> tuple:
    try:
        u = source.universe
        uk = (u.name, u.symbols)
    except Exception as e:   # 設定錯誤：不快取成功結果
        uk = ("error", str(e))
    from .analysis_hooks import data_version
    return (id(source), uk, data_version(source)) + p.key()


def run_screen(source: DataSource, params: ScreenParams) -> ScreenResult:
    k = _key(source, params)
    if k not in _CACHE:
        if len(_CACHE) >= _CACHE_MAX:   # 參數組合很多時只留最近的結果（全市場一組結果約數 MB）
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[k] = _compute(source, params)
    import copy
    res = copy.copy(_CACHE[k])
    res.rows = list(res.rows)
    res.params = params
    key = SORT_KEYS.get(params.sort, SORT_KEYS["turnover"])[1]
    res.rows.sort(key=key, reverse=(params.order != "asc"))
    return res


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _compute(source: DataSource, p: ScreenParams) -> ScreenResult:
    engine = "analysis/screener.py screen()"
    res = ScreenResult(params=p, trade_date="—", engine=engine, is_test=source.is_test)
    if source.is_test:
        res.notes.append("目前為測試資料，結果不代表真實行情，也不構成投資建議。")
    try:
        res.non_common = source.excluded_securities()
    except DataUnavailable:
        res.non_common = []
    if not analysis_screener_available():
        res.error = "analysis/screener.py 尚未提供"
        return res
    res.extra["params"] = {}
    from .universe import UniverseError
    try:
        uni = source.universe
    except (UniverseError, OSError) as e:
        res.error = f"追蹤範圍設定錯誤：{e}"
        return res
    res.extra.update(universe_name=uni.name, universe_label=uni.label, universe_text=uni.text,
                     universe_source=uni.source)
    res.notes.append(f"{uni.text}（設定 TWSITE_UNIVERSE；清單來源 {uni.source}）。範圍外的股票不評估、不計入排除。")
    try:
        from .analysis_hooks import _frame
        frame = {"kwargs": _frame(source)}   # 依資料版本快取，與個股頁共用（同一份 screener_frame()）
    except DataUnavailable as e:
        res.error = f"無行情資料：{e}"
        return res
    kw = frame["kwargs"]
    if kw.get("prices") is None or len(kw["prices"]) == 0:
        res.error = "無行情資料：daily_prices 沒有現行列"
        return res
    try:
        m = _mod()
    except Exception as e:  # analysis 更新中或相依缺漏：顯示狀態，不回 500
        sys.modules.pop("analysis_screener", None)
        res.error = f"analysis/screener.py 載入失敗：{type(e).__name__}: {e}"
        return res
    try:
        ap = m.ScreenParams(**p.values)  # 原樣交給 analysis 驗證，不夾值、不修正
    except (ValueError, TypeError) as e:
        res.error = f"參數錯誤（{type(e).__name__}）：{e}"
        res.extra.update(error_type=type(e).__name__, error_message=str(e), error_param=error_param(str(e)))
        return res
    try:
        out = m.screen(params=ap, **kw)
    except (ValueError, TypeError, KeyError) as e:
        res.error = f"篩選器無法執行：{type(e).__name__}: {e}"
        return res
    td = str(out.as_of.date())
    res.trade_date = td
    summ = out.summary
    res.universe_count = len(summ)
    res.extra["analysis_result"] = out   # 給 narrative.summary_narrative()（同一份 ScreenResult）
    res.extra["analysis_version"] = getattr(out, "analysis_version", "")
    counts = out.exclusion_counts()
    labels = exclusion_labels()
    res.extra["exclusion_counts"] = counts
    res.extra["exclusion_labels"] = {c: labels.get(c) or UNKNOWN_CODE_LABEL.format(raw=c) for c in counts}
    res.extra["exclusion_stats"] = sorted(
        [{"code": c, "label": labels.get(c) or UNKNOWN_CODE_LABEL.format(raw=c), "count": int(n), "coverage": c in COVERAGE_CODES,
          "not_evaluable": not_evaluable_label(c)}
         for c, n in counts.items()], key=lambda x: -x["count"])
    res.extra["params"] = dict(out.params)
    n_cov = sum(n for c, n in counts.items() if c in COVERAGE_CODES)
    res.extra["coverage_excluded"] = int(n_cov)
    vb_status = getattr(out, "volume_basis_status", None)   # analysis-0.9.0 起才有；舊版為 None
    res.extra["volume_basis_status"] = vb_status
    res.extra["volume_basis_status_text"] = VOLUME_BASIS_STATUS_LABELS.get(vb_status or "", "")
    cols = set(summ.columns)
    try:
        from .models import basis_counts_text
        res.extra["basis_counts_text"] = basis_counts_text(source.volume_basis_counts(td), td)
    except Exception:   # 口徑分布只是說明，讀不到不影響選股結果
        res.extra["basis_counts_text"] = ""
    src_label = TEST_LABEL if source.is_test else "daily_prices（經 analysis/screener.py 計算）"
    conds_by = {sid: g for sid, g in out.conditions.groupby("symbol")} if len(out.conditions) else {}
    for r in summ.to_dict("records"):
        sid = str(r["symbol"])
        name = r.get("name") if isinstance(r.get("name"), str) else sid
        ext = screen_extra_fields(r, cols, vb_status or "ok")
        if r["status"] == "EXCLUDED":
            ec = str(r["exclusion_code"])
            lab = labels.get(ec) or UNKNOWN_CODE_LABEL.format(raw=ec)
            raw_reason = r["exclusion_reason"] if isinstance(r.get("exclusion_reason"), str) else ""
            z = zh_text(raw_reason, ec)
            det = z["text"]
            for pre in (lab + "：", lab + ":", lab):   # analysis 原因常以同一句說明開頭 → 不重複顯示
                if det.startswith(pre):
                    det = det[len(pre):].lstrip()
                    break
            res.excluded.append(Excluded(sid, name, f"{lab}｜{det}" if det else lab, ec, lab, det, ext,
                                         raw_reason if not z["known"] else ""))
            continue
        if r["status"] != "PASS":
            continue
        row_href = source_href("daily_prices", symbol=sid, price_date=td)
        ref = {"table": "daily_prices", "key": f"symbol={sid}, price_date={td}"}
        scr_href = f"/screener#row-{sid}"
        scr_ref = {"table": "analysis/screener.py 輸出", "key": f"symbol={sid}, as_of={td}"}

        def V(x, unit, digits=2, href=row_href, rf=ref):
            return Val(_num(x), unit, src_label, td, is_test=source.is_test, digits=digits, href=href, ref=rf)

        conds = []
        g = conds_by.get(r["symbol"])
        if g is not None:
            for c in g.to_dict("records"):
                passed = c["passed"]
                passed = None if passed is None or (isinstance(passed, float) and math.isnan(passed)) else bool(passed)
                conds.append(Condition(c["condition_id"], f"{c['condition_name']}（門檻 {c['threshold_text']}）",
                                       passed, c["reason"] or c["actual_text"] or "", bool(c["enabled"])))
        mk = r.get("market")
        res.rows.append(ScreenRow(
            code=sid, name=name, market={"TWSE": "上市", "TPEx": "上櫃"}.get(mk, _market(source, sid)), trade_date=td,
            close=V(r["close_twd"], "新台幣元"), change_pct=V(r["change_pct"], "%", 2, scr_href, scr_ref),
            turnover=V(r["amount_twd"], "新台幣元", 0), ma_short=V(r["ma_short"], "新台幣元", 2, scr_href, scr_ref),
            ma_long=V(r["ma_long"], "新台幣元", 2, scr_href, scr_ref),
            vol_ratio=V(r["volume_ratio"], "倍", 2, scr_href, scr_ref), conditions=conds,
            change_check=r.get("change_check") if isinstance(r.get("change_check"), str) else "", extra=ext))
    res.extra["status_by_symbol"] = {
        str(r["symbol"]): {"status": r["status"],
                           "code": r["exclusion_code"] if isinstance(r.get("exclusion_code"), str) else "",
                           "reason": r["exclusion_reason"] if isinstance(r.get("exclusion_reason"), str) else "",
                           "change_check": r.get("change_check") if isinstance(r.get("change_check"), str) else "",
                           "extra": screen_extra_fields(r, cols, vb_status or "ok")}
        for r in summ.to_dict("records")}
    if not res.rows:
        # 沒有任何一檔完成條件評估（全部被排除，且有覆蓋不足）→ coverage；
        # 只要有股票完成評估但未符合（FAIL），0 檔就是「無符合」，不能說成覆蓋不足（第八批 sqlite 實測修正）
        n_eval = (res.universe_count or 0) - len(res.excluded)
        res.extra["zero_kind"] = "coverage" if n_cov and n_eval <= 0 else "no_match"
    res.extra["evaluated_count"] = max((res.universe_count or 0) - len(res.excluded), 0)
    return res


def _market(source: DataSource, code: str) -> str:
    try:
        s = source.get_stock(code)
    except DataUnavailable:
        s = None
    return s["market"] if s else "—"


# ---- 條件白話說明（analysis-0.7.0 explain.py；網站不自寫說明文字）----
EXPLAIN_KEYS = {"C1_TREND": "trend", "C2_BREAKOUT": "breakout", "C3_VOLUME": "volume", "C4_TURNOVER": "turnover"}


def explanations(values: Optional[dict] = None) -> dict:
    """呼叫 explain.condition_explanations(params)／common_explanation(params)。
    values：analysis 驗證後實際套用的參數（res.extra["params"]）；None 或空＝參數有誤 → 以 analysis 預設值說明並註明。
    回傳 {"status": ok|error, "error": str, "using_defaults": bool, "conditions": {trend|breakout|volume|turnover: {...}},
    "common": str, "engine": "analysis/explain.py"}；任何失敗都回 error 狀態，不拋例外。"""
    out = {"status": "ok", "error": "", "using_defaults": not values, "conditions": {}, "common": "",
           "engine": "analysis/explain.py condition_explanations()／common_explanation()"}
    try:
        E = analysis_module("explain")
        m = _mod()
        p = m.ScreenParams(**values) if values else m.ScreenParams()
        for x in E.condition_explanations(p):
            out["conditions"][EXPLAIN_KEYS.get(x["condition_id"], x["condition_id"])] = {
                "condition_id": x["condition_id"], "title": x["title"], "enabled": bool(x["enabled"]),
                "text": x["text"]}
        out["common"] = E.common_explanation(p)
        if set(out["conditions"]) != set(EXPLAIN_KEYS.values()) or not isinstance(out["common"], str):
            raise ValueError(f"回傳格式不符：{sorted(out['conditions'])}")
    except Exception as e:   # 載入失敗、參數錯誤、回傳格式錯誤 → 錯誤狀態，頁面照常
        out.update(status="error", error=f"analysis/explain.py 無法提供條件說明（{type(e).__name__}：{e}）",
                   conditions={}, common="")
    return out
