"""篩選中心（/hub）：docs/SCREENER_HUB.md、docs/design/HUB_HANDOFF.md §1–§8、docs/indicators.md §11。

規則：
- 每個模式的「是否符合」一律由 analysis 判斷：技術面呼叫 analysis/screener.py screen()（經 screening.run_screen，
  只換 analysis 自己的 ScreenParams 參數），月營收呼叫 analysis/revenue.py evaluate_revenue()。網站只做「組合」
  （多個 analysis 結果取交集）與排序，不重寫任何指標。
- analysis 沒有提供的計算（目前：單季 EPS 年增 → M5、M8）顯示「尚未提供」，卡片不可點。
- 5 日勝率：analysis 尚未計算任何勝率（indicators.md §11.3「程式未完成前不得顯示任何勝率數字」），
  所以一律不輸出比例：M5、M8 為「樣本不足」（季頻結構性不足，§11.6.1），其餘為「計算中」（上線初期，HUB_HANDOFF §8-6）。
- 名單、檔數只算追蹤範圍（TWSITE_UNIVERSE）內的股票。
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Optional

from .data_source import DataSource, DataUnavailable, source_href
from .models import (NOT_EVALUABLE_KIND_ORDER, NOT_EVALUABLE_KINDS, STATUS_INSUFFICIENT, STATUS_OK, TEST_LABEL,
                     UNKNOWN_CODE_LABEL, ScreenParams, Val)

COVER = 0.95            # 覆蓋率門檻（HUB_HANDOFF §4.1；分母＝母體−上市未滿−停牌）
TIERS = [
    ("基本", "t1", "只用行情或月營收，規則單純。"),
    ("進階", "t2", "要較長的歷史或財報資料。"),
    ("高級", "t3", "要同時對齊多種資料，樣本最少。"),
]
TIER_NOTE = "等級只代表資料需求與計算複雜度，不代表比較準。"
FREE_NOTE = "目前全部免費開放。"
WINDEF = "＝扣成本後 5 個交易日報酬＞0 的比例"
DISCLAIMER = "歷史統計，不代表未來，非投資建議"
SORT_NOTE = "排序僅為排列方式，不代表優劣。"
M2_SORT_NOTE = ("依收盤價高於 60 日均線的幅度排列；幅度大代表股價離均線較遠（乖離較大），"
                "不代表較強、較好或較值得關注；排序可切換。")
LAUNCH_NOTE = "5 日勝率尚在計算，樣本不足的模式另標示；今日檔數與名單照常。"   # 2026-11-24 專案負責人文案決定

# 研究分析 §11.1 的 8 個模式（文字取自 docs/indicators.md §11.1；計算方式見 _compute_* ）
MODES: list[dict] = [
    dict(id="M1", tier="基本", name="趨勢＋突破＋量能", legacy=True,
         desc="收盤 > MA20 > MA60、收盤突破前 20 日最高、量 ≥ 前 20 日均量 1.5 倍、成交金額 ≥ 1 億元",
         engine="analysis/screener.py screen()（預設參數）", engine_zh="研究分析的技術篩選器（預設四條件）", metric="vol_ratio", metric_label="量比（倍）", metric_digits=2,
         history_days=60),
    dict(id="M2", tier="基本", name="均線多頭排列",
         desc="收盤 > MA5 > MA20 > MA60，且成交金額 ≥ 1 億元",
         engine="analysis/screener.py screen() 兩次：MA5／MA20 與 MA20／MA60（只開趨勢與成交金額條件），兩者都符合",
         engine_zh="研究分析的技術篩選器跑兩次（MA5／MA20、MA20／MA60，只開趨勢與成交金額條件），兩次都符合才列入",
         metric="dist_ma60", metric_label="收盤距 60 日均線（%）", metric_digits=2, history_days=60,
         alt_sort=("trade_value", "成交金額（億元）")),
    dict(id="M3", tier="基本", name="月營收創 12 個月新高",
         desc="最近可得月份的月營收嚴格大於前 12 個月最高（相等不算）",
         engine="analysis/revenue.py evaluate_revenue()（rev_12m_high-v1）", engine_zh="研究分析的月營收評估（近 12 個月新高定義）", metric="yoy", metric_label="月營收年增率（%）",
         metric_digits=2),
    dict(id="M4", tier="進階", name="營收連續 3 個月年增",
         desc="最近可得的連續 3 個月，月營收年增率都 > 0",
         engine="analysis/revenue.py evaluate_revenue()（rev_yoy-v1，連續 3 個月）", engine_zh="研究分析的月營收評估（年增率定義，連續 3 個月）", metric="yoy",
         metric_label="最近月年增率（%）", metric_digits=2),
    dict(id="M5", tier="進階", name="單季 EPS 年增",
         desc="單季 EPS > 0 且大於去年同季",
         na="研究分析模組還沒有提供單季 EPS 推算與年增判斷，本站不自行計算", quarterly=True),
    dict(id="M6", tier="進階", name="創 250 日新高",
         desc="收盤 > 前 250 個交易日最高價（不含當日），且成交金額 ≥ 1 億元",
         engine="analysis/screener.py screen()（突破 250 日，只開突破與成交金額條件）",
         engine_zh="研究分析的技術篩選器（突破天數 250 日，只開突破與成交金額條件）", metric="dist_high",
         metric_label="收盤距前高（%）", metric_digits=2, history_days=251),
    dict(id="M7", tier="高級", name="營收創高＋趨勢",
         desc="月營收創 12 個月新高，且收盤 > MA20 > MA60",
         engine="analysis/revenue.py evaluate_revenue() ＋ analysis/screener.py screen()（只開趨勢條件）",
         engine_zh="研究分析的月營收評估＋技術篩選器（只開趨勢條件），兩者都符合才列入", metric="yoy",
         metric_label="月營收年增率（%）", metric_digits=2, history_days=60),
    dict(id="M8", tier="高級", name="EPS 成長＋營收連續成長＋趨勢",
         desc="單季 EPS 年增、營收連續 3 個月年增，且收盤 > MA20 > MA60",
         na="需要單季 EPS 年增（M5），研究分析模組尚未提供，本站不自行計算", quarterly=True),
]
BASE_IDS = [m["id"] for m in MODES]
NEW_MODE_IDS = [f"M{i}" for i in range(9, 22)]   # analysis-0.10.0 modes.evaluate_modes()（docs/indicators.md §12）
MODE_IDS = BASE_IDS + NEW_MODE_IDS
TIER_CLASS = {"基本": "t1", "進階": "t2", "高級": "t3"}
RANK_NOTE = "排名在全市場當日可評估的 {n:,} 檔之間計算。"
NEW_ENGINE_ZH = "研究分析的模式評估（analysis/modes.py，登記表 {ver}）"


_DOC_RE = __import__("re").compile(r"（docs/[\w./-]+\.md）|(?:docs/)?indicators\.md\s*§\s*(\d+(?:\.\d+)*)\s?|docs/[\w./-]+\.md")


_CODE_RE = __import__("re").compile(r"（[a-z][a-z0-9_]*-v\d+）")
# 內部核可字樣不上畫面（2026-11-24 專案負責人文案決定）：「（專案負責人核可 2026-11-24 16:05）」這類括號整段移除，
# 括號外的「專案負責人核可 2026-11-24 16:05，」「負責人 2026-11-17 18:31 核可」「H AO 核可」等片語也移除；其餘文字保留。
_APPROVAL_PAREN_RE = __import__("re").compile(r"[（(][^（）()]*核可[^（）()]*[）)]")
_APPROVAL_RE = __import__("re").compile(
    r"(?:(?:專案)?負責人|H\s?AO)(?:轉達)?(?:\s*H\s?AO)?\s*(?:\d{4}-\d{2}-\d{2})?\s*(?:\d{1,2}:\d{2})?\s*"
    r"(?:已)?核可(?:\s*\d{4}-\d{2}-\d{2})?(?:\s*\d{1,2}:\d{2})?[，,、；;：:]?\s*")


def strip_approval(t: str) -> str:
    """移除內部核可字樣（人名、日期時間、「核可」），保留其他說明。"""
    t = _APPROVAL_PAREN_RE.sub("", t)
    return _APPROVAL_RE.sub("", t)


def public_text(t: Optional[str]) -> str:
    """登記表文字上畫面前：文件路徑換成中文（網站不顯示英文檔名）。例：「依 docs/indicators.md §11 重算」→「依指標說明第 11 節重算」。"""
    if not t:
        return ""
    def rep(m):
        if m.group(0).startswith("（"):
            return ""
        if m.group(1):
            return f"指標說明第 {m.group(1)} 節"
        return "研究文件"
    t = _DOC_RE.sub(rep, str(t)).replace("依 指標說明", "依指標說明")
    return strip_approval(_CODE_RE.sub("", t))   # 「（rev_12m_high-v1）」這類內部定義代碼、內部核可字樣不上畫面


def _rate(r: Optional[dict], fallback_few: bool) -> dict:
    """勝率只取登記表 win_rate.display／note（「計算中」；M5、M8「樣本不足」）；登記表沒有 → 舊規則。不輸出任何比例。"""
    w = (r or {}).get("win_rate") or {}
    disp = w.get("display") or ("樣本不足" if fallback_few else "計算中")
    return dict(rate_status="rate_few" if disp == "樣本不足" else "rate_pending", rate_display=disp,
                rate_note=public_text(w.get("note")))


def mode_specs() -> list[dict]:
    """M1～M21 的卡片設定：等級、名稱、說明、排序指標、勝率文字取 analysis 模式登記表（mode_registry）；
    M1～M8 的計算方式與說明沿用本檔 MODES（登記表 state=existing）。登記表沒有的新模式不出現（不放不可用的按鈕）。"""
    from . import modes_hub as MH
    reg = MH.registry_modes()
    out = []
    for m in MODES:
        d = dict(m)
        r = reg.get(m["id"])
        if r:
            d.update(tier=r.get("tier") or d["tier"], tier_prev=r.get("tier_prev"), note=public_text(r.get("note")))
        d.update(_rate(r, bool(m.get("quarterly"))))
        out.append(d)
    ver = (MH.registry() or {}).get("registry_version", "")
    for mid in NEW_MODE_IDS:
        r = reg.get(mid)
        if not r or r.get("state") not in (None, "production"):
            continue
        met = r.get("metric") or {}
        d = dict(id=mid, tier=r.get("tier") or "進階", name=public_text(r.get("name")) or mid, desc=public_text(r.get("desc")),
                 conditions=[public_text(x) for x in r.get("conditions") or []], note=public_text(r.get("note")), new=True,
                 engine=r.get("engine") or "analysis/modes.py evaluate_modes()", engine_zh=NEW_ENGINE_ZH.format(ver=ver),
                 metric=met.get("key"), metric_label=met.get("label") or "排序指標",
                 metric_digits=int(met.get("digits") if met.get("digits") is not None else 2),
                 metric_scale=float(met.get("scale") if met.get("scale") is not None else 1),
                 history_days=r.get("history_days"), cross_sectional=bool(r.get("cross_sectional")),
                 variant=r.get("active_variant") or "short")
        d.update(_rate(r, False))
        out.append(d)
    return sorted(out, key=lambda d: int(d["id"][1:]))


def tiers() -> list[tuple]:
    """等級分區（基本／進階／高級）：說明文字＝登記表各級 rule；沒有登記表 → 舊 TIERS。"""
    from . import modes_hub as MH
    reg = MH.registry() or {}
    t = [(x["name"], TIER_CLASS.get(x["name"], "t1"), public_text(x.get("rule"))) for x in reg.get("tiers") or [] if x.get("name")]
    return t or TIERS

# 各模式交給 analysis ScreenParams 的參數（只用 analysis 已有的參數；驗證交給 analysis）
P_M1: dict = {}
P_M2_SHORT = dict(ma_short=5, ma_long=20, enable_breakout=False, enable_volume=False)
P_M2_LONG = dict(enable_breakout=False, enable_volume=False)
P_M6 = dict(breakout_lookback=250, enable_trend=False, enable_volume=False)
P_TREND = dict(enable_breakout=False, enable_volume=False, enable_turnover=False)

YOUNG_CODE = "EXCL_NEW_LISTING"     # 上市未滿（analysis 排除碼）
HALT_CODE = "EXCL_SUSPENDED"        # 確認停牌（事實，不是資料不足）


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def available_month(as_of: str) -> Optional[str]:
    """indicators.md §11.0：月營收以法定期限（次月 10 日）為可得日。評估日 d 可用的最新營收月份：
    d 在當月 10 日（含）以後 → 上個月；否則 → 上上個月。"""
    try:
        d = dt.date.fromisoformat(as_of)
    except (TypeError, ValueError):
        return None
    k = 1 if d.day >= 10 else 2
    y, m = d.year, d.month - k
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


def _month_add(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + k
    while m <= 0:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y}-{m:02d}"


def _screen(src: DataSource, values: dict):
    from .screening import run_screen
    r = run_screen(src, ScreenParams(values=dict(values)))
    out = r.extra.get("analysis_result")
    if r.error or out is None:
        raise RuntimeError(r.error or "analysis/screener.py 沒有回傳結果")
    return r, out.summary


def _status_map(summary) -> dict:
    return {str(r["symbol"]): r for r in summary.to_dict("records")}


def _rev_eval(src: DataSource, targets: list[str], months: list[str]):
    """呼叫 analysis/revenue.py evaluate_revenue()；回傳 {month: {symbol: record}}。"""
    from .screening import analysis_module
    R = analysis_module("revenue")
    rows = src.get_source_rows("monthly_revenues", {}, limit=10_000_000)
    tset = set(targets)
    rows = [r for r in rows if r.get("symbol") in tset]
    out = {}
    for mo in months:
        ev = R.evaluate_revenue(rows, month=mo, symbols=targets)
        out[mo] = {str(r["symbol"]): r for r in ev.to_dict("records")}
    return out, R


def _mode_counts(universe: int, evaluable: int, not_evaluable_by_code: Optional[dict] = None) -> dict:
    """95% 覆蓋率口徑（docs/indicators.md §8.7，analysis-0.9.2）：分母＝母體 − 無法評估（NOT_EVALUABLE_CODES：
    上市未滿、確認停牌／停止買賣、無成交、視窗內無成交或停止買賣過多）；其餘排除（EXCL_WINDOW_GAPS 等）留在分母＝資料不齊。"""
    by = {str(k): int(v) for k, v in (not_evaluable_by_code or {}).items() if int(v)}
    ne = sum(by.values())
    den = universe - ne
    cov = (evaluable / den) if den > 0 else 0.0
    kinds: dict[str, int] = {}
    for c, n in by.items():
        k = NOT_EVALUABLE_KINDS.get(c, "其他")
        kinds[k] = kinds.get(k, 0) + n
    kinds_l = [dict(kind=k, count=kinds[k]) for k in NOT_EVALUABLE_KIND_ORDER if kinds.get(k)]
    kinds_txt = "／".join(f"{x['kind']} {x['count']:,}" for x in kinds_l)
    return dict(universe_count=universe, evaluable_count=evaluable, denominator=den, coverage_ratio=cov,
                excluded_count=max(den - evaluable, 0),   # 資料不齊（留在分母、未能評估）
                not_evaluable_count=ne, not_evaluable_by_code=by, not_evaluable_kinds=kinds_l,
                young_excluded_count=by.get(YOUNG_CODE, 0),
                halted_count=sum(by.get(c, 0) for c in ("EXCL_SUSPENDED", "EXCL_WINDOW_SUSPENDED")),
                coverage_line=f"可評估 {evaluable:,}／分母 {den:,} 檔",
                not_evaluable_text=(f"另有 {ne:,} 檔無法評估（{kinds_txt}），不計入分母" if ne else ""))


def _screen_counts(maps, symbols: list[str], ok_fn) -> dict:
    """maps：一個或多個 analysis summary 的 {代號: 列}。未能評估（ok_fn 為假）的股票，只要任一份結果是
    無法評估代碼（screener.NOT_EVALUABLE_CODES）就算無法評估、不入分母；否則算資料不齊。"""
    from .screening import not_evaluable_codes
    maps = maps if isinstance(maps, list) else [maps]
    ne_codes = not_evaluable_codes()
    by: dict[str, int] = {}
    ev = 0
    for s in symbols:
        if ok_fn(s):
            ev += 1
            continue
        for mp in maps:
            r = mp.get(s) or {}
            c = r.get("exclusion_code")
            if r.get("status") == "EXCLUDED" and c in ne_codes:
                by[c] = by.get(c, 0) + 1
                break
    return _mode_counts(len(symbols), ev, by)


def mode_state(c: dict) -> tuple[str, Optional[int]]:
    """模式狀態（篩選中心卡片與首頁 M1 共用同一個判斷）：可評估比例未達 COVER（95%）→ insufficient_data（不給檔數）；
    否則依符合檔數 ok／zero。回傳 (mode_status, today_count)。"""
    if c["coverage_ratio"] < COVER:
        return "insufficient_data", None
    n = len(c["passed"])
    return ("ok" if n else "zero"), n


def _m1_parts(m1: dict, symbols: list[str], labels: dict, summary=None) -> tuple:
    """M1（analysis 預設篩選）的檔數、符合名單、量比、未能評估原因；篩選中心與首頁共用。
    覆蓋率優先用 analysis 的 screener.coverage_stats(summary)（0.9.2 起）；舊版沒有時用 NOT_EVALUABLE_CODES 自行歸類。"""
    ok = lambda s: m1[s].get("status") != "EXCLUDED"
    passed = [s for s in symbols if m1[s].get("status") == "PASS"]
    counts = None
    if summary is not None:
        try:
            from .screening import analysis_module
            st = analysis_module("screener").coverage_stats(summary)
            counts = _mode_counts(int(st["universe"]), int(st["evaluable"]), st.get("not_evaluable_by_code") or {})
        except AttributeError:
            counts = None
    if counts is None or counts["universe_count"] != len(symbols):
        counts = _screen_counts(m1, symbols, ok)
    return (counts, passed, {s: _num(m1[s].get("volume_ratio")) for s in passed},
            _excl_reasons([m1], symbols, ok, labels))


def m1_card(src: DataSource) -> dict:
    """首頁「技術篩選（預設條件）」用：與 /hub?mode=M1 同一份計算（_screen(P_M1) → _m1_parts → mode_state）。
    回傳 mode_status（ok／zero／insufficient_data／error）、today_count、evaluable_count、denominator、coverage_ratio…"""
    try:
        r1, s1 = _screen(src, P_M1)
        from .screening import exclusion_labels
        m1 = _status_map(s1)
        counts, passed, _metric, reasons = _m1_parts(m1, sorted(m1), dict(exclusion_labels()), s1)
    except Exception as e:
        return dict(mode_status="error", error=f"{type(e).__name__}：{e}", today_count=None)
    d = dict(counts, passed=passed, excluded_reasons=reasons, as_of=r1.trade_date, cover=COVER)
    d["mode_status"], d["today_count"] = mode_state(d)
    return d


def _excl_reasons(maps: list[dict], symbols: list[str], ok_fn, labels: dict) -> list[dict]:
    cnt: dict[str, int] = {}
    for s in symbols:
        if ok_fn(s):
            continue
        code = ""
        for mp in maps:
            r = mp.get(s) or {}
            c = r.get("exclusion_code") or r.get("rev_12m_high_reason") or r.get("yoy_reason") or ""
            if isinstance(c, str) and c and (r.get("status") == "EXCLUDED" or r.get("rev_12m_high_status") == "insufficient"
                                             or r.get("yoy_status") == "not_applicable"):
                code = c
                break
        code = code or "OTHER"
        cnt[code] = cnt.get(code, 0) + 1
    return sorted([{"code": c, "label": labels.get(c) or UNKNOWN_CODE_LABEL.format(raw=c), "count": n}
                   for c, n in cnt.items()], key=lambda x: -x["count"])


def build(src: DataSource, selected: Optional[str] = None, sort: Optional[str] = None) -> dict:
    """篩選中心頁面資料。selected＝模式代號（M1…M21）或 None（未選）。各模式的計算結果依資料版本快取（_core）。"""
    view: dict[str, Any] = dict(
        modes=[], tiers=tiers(), tier_note=_tier_note(), free_note=FREE_NOTE, windef=WINDEF, disclaimer=_disclaimer(),
        launch_note=LAUNCH_NOTE, sort_note=_sort_note(), m2_sort_note=M2_SORT_NOTE, selected=None, error="", as_of="—", universe_label="",
        universe_text="", is_test=src.is_test, revenue_month=None, cover=COVER)
    try:
        uni = src.universe
        view.update(universe_label=uni.label, universe_text=uni.text)
    except Exception as e:
        view["error"] = f"追蹤範圍設定錯誤：{e}"
        return view
    core = _core(src)
    if core.get("fatal"):
        view["error"] = core["fatal"]
        _fill_modes(view, {}, selected)
        return view
    view.update(as_of=core["as_of"], revenue_month=core["revenue_month"], price=core["price"])
    computed = {k: dict(v) for k, v in core["computed"].items()}   # 複本：選取／排序不改到快取
    _fill_modes(view, computed, selected, sort, src)
    return view


_CORE: dict[tuple, tuple] = {}
_CORE_LOCK = __import__("threading").RLock()


def _core_key(src: DataSource) -> tuple:
    from .analysis_hooks import _universe_key, data_version
    from .screening import ANALYSIS_DIR
    # 測試會 monkeypatch 本模組的計算函式 → 函式 id 也進快取鍵
    return (id(src), data_version(src), _universe_key(src), str(ANALYSIS_DIR), id(_screen), id(_mode_counts),
            id(_mode_result), id(_rev_eval))


def clear_cache() -> None:
    _CORE.clear()


def _core(src: DataSource) -> dict:
    """M1～M21 的計算結果（檔數、名單、指標、原因），同一（來源, 資料版本, 範圍）只算一次。"""
    k = _core_key(src)
    hit = _CORE.get(k)
    if hit is not None and hit[0] is src:
        return hit[1]
    with _CORE_LOCK:   # 背景預熱與請求同時到：只算一次
        hit = _CORE.get(k)
        if hit is not None and hit[0] is src:
            return hit[1]
        out = _compute_core(src)
        while len(_CORE) >= 4:
            _CORE.pop(next(iter(_CORE)))
        _CORE[k] = (src, out, (_screen, _mode_counts, _mode_result, _rev_eval))   # 留住函式參照：id 不會被重用
        return out


def _reg_text(key: str, default: str) -> str:
    from . import modes_hub as MH
    return public_text((MH.registry() or {}).get(key)) or default


def _tier_note() -> str:
    return _reg_text("tier_basis", TIER_NOTE)


def _disclaimer() -> str:
    return _reg_text("disclaimer", DISCLAIMER)


def _sort_note() -> str:
    return _reg_text("sort_note", SORT_NOTE)


def _mode_result(src: DataSource, as_of: str, screen_result=None):
    """analysis/modes.py evaluate_modes()（M9～M21）；每個評估日只算一次（modes_hub 快取）。"""
    from . import modes_hub as MH
    return MH.result(src, as_of, screen_result)


def _new_modes(computed: dict, src: DataSource, symbols: list[str], labels: dict, as_of: str, r1) -> None:
    """M9～M21：每個模式用自己的 coverage_stats（只算追蹤範圍內的股票）判斷 95%；0 檔就是 0 檔。"""
    from . import modes_hub as MH
    from .models import zh_text
    from .screening import analysis_module
    specs = {d["id"]: d for d in mode_specs() if d.get("new")}
    if not specs:
        return
    try:
        res = _mode_result(src, as_of, (r1.extra or {}).get("analysis_result"))
        S = analysis_module("screener")
    except Exception as e:
        for mid in specs:
            computed[mid] = dict(error=f"analysis/modes.py 無法計算（{type(e).__name__}：{e}）")
        return
    codes = MH.exclusion_codes()
    lab = dict(labels)
    lab.update({c: zh_text(t, c)["text"] for c, t in codes.items()})
    sym_set = set(symbols)
    for mid, spec in specs.items():
        try:
            summ = res.mode_summary(mid, sym_set)
            if not len(summ):
                raise RuntimeError("analysis 沒有回傳此模式的結果")
            st = S.coverage_stats(summ)
            counts = _mode_counts(int(st["universe"]), int(st["evaluable"]), st.get("not_evaluable_by_code") or {})
            mp = {str(r["symbol"]): r for r in summ.to_dict("records")}
            ok = lambda s, mp=mp: (mp.get(s) or {}).get("status") not in (None, "EXCLUDED")
            passed = [s for s in symbols if (mp.get(s) or {}).get("status") == "PASS"]
            scale = spec.get("metric_scale", 1)
            metric = {s: (_num(mp[s].get("metric")) * scale if _num(mp[s].get("metric")) is not None else None)
                      for s in passed}
            reasons = {s: (mp[s].get("reason_zh") if isinstance(mp[s].get("reason_zh"), str) else "") for s in passed}
            excluded = []
            for s in symbols:
                r = mp.get(s) or {}
                if r.get("status") != "EXCLUDED":
                    continue
                code = r.get("exclusion_code") if isinstance(r.get("exclusion_code"), str) else ""
                why = r.get("exclusion_reason") if isinstance(r.get("exclusion_reason"), str) else ""
                if not code or code not in lab:   # 未知代碼：全站備援格式（不直接顯示英文代碼字樣）
                    why = UNKNOWN_CODE_LABEL.format(raw=code or "—")
                else:
                    why = zh_text(why or lab[code], code)["text"]
                excluded.append(dict(code=s, name=r.get("name") if isinstance(r.get("name"), str) else s,
                                     reason=why, excl_code=code))
            notes = []
            if spec.get("cross_sectional"):
                notes.append(RANK_NOTE.format(n=res.ranked_counts.get(mid, 0)) if res.scope_all
                             else _reg_text("cross_sectional_note", "排名在本次評估範圍內計算。"))
            computed[mid] = dict(counts, passed=passed, metric=metric, reasons=reasons, excluded_rows=excluded,
                                 excluded_reasons=_excl_reasons([mp], symbols, ok, lab), notes=notes)
        except Exception as e:   # 單一模式失敗 → 只有該模式顯示錯誤
            computed[mid] = dict(error=f"{type(e).__name__}：{e}")


def today_modes(src: DataSource, code: str) -> dict:
    """個股頁「今天符合的模式」：與篩選中心同一份計算（_core），只列覆蓋率足夠（95%）且此股 PASS 的模式。
    state：ok（有符合）／none（沒有符合）／error（篩選中心無法計算）。"""
    core = _core(src)
    if core.get("fatal"):
        return dict(state="error", hits=[], insufficient=0, as_of="—", error=core["fatal"])
    items, lack = [], 0
    for m in mode_specs():
        if "na" in m:
            continue
        c = core["computed"].get(m["id"])
        if not c or "error" in c:
            lack += 1
            continue
        state, _n = mode_state(c)
        if state == "insufficient_data":
            lack += 1
            continue
        if code in set(c["passed"]):
            items.append(dict(id=m["id"], name=m["name"], tier=m["tier"], tier_class=TIER_CLASS.get(m["tier"], "t1")))
    return dict(state="ok" if items else "none", hits=items, insufficient=lack, as_of=core["as_of"])


def _compute_core(src: DataSource) -> dict:
    try:
        r1, s1 = _screen(src, P_M1)
    except Exception as e:
        return dict(fatal=f"選股引擎無法執行（{e}）")
    from .screening import exclusion_labels
    labels = dict(exclusion_labels())
    as_of = r1.trade_date
    m1 = _status_map(s1)
    symbols = sorted(m1)
    price = _price_map(src, m1, as_of)
    computed: dict[str, dict] = {}

    def add(mid, counts, passed: list[str], metric: dict, reasons, notes=None, extra=None):
        d = dict(counts)
        d.update(passed=passed, metric=metric, excluded_reasons=reasons, notes=notes or [], **(extra or {}))
        computed[mid] = d

    def safe(mid, fn):
        try:
            fn()
        except Exception as e:   # 單一模式失敗 → 只有該模式顯示錯誤
            computed[mid] = dict(error=f"{type(e).__name__}：{e}")

    # M1：analysis 預設篩選
    def m1f():
        add("M1", *_m1_parts(m1, symbols, labels, s1))
    safe("M1", m1f)

    def m2f():
        _, sa = _screen(src, P_M2_SHORT)
        _, sb = _screen(src, P_M2_LONG)
        ma, mb = _status_map(sa), _status_map(sb)
        ok = lambda s: (ma.get(s) or {}).get("status") not in (None, "EXCLUDED") and \
            (mb.get(s) or {}).get("status") not in (None, "EXCLUDED")
        passed = [s for s in symbols if (ma.get(s) or {}).get("status") == "PASS" and (mb.get(s) or {}).get("status") == "PASS"]
        metric = {}
        for s in passed:   # 收盤距 MA60（%）＝(收盤 ÷ analysis 的 MA60 − 1)×100；兩個值都來自 analysis 輸出
            c, m = _num(mb[s].get("close_twd")), _num(mb[s].get("ma_long"))
            metric[s] = (c / m - 1) * 100 if c is not None and m else None
        add("M2", _screen_counts([mb, ma], symbols, ok), passed, metric, _excl_reasons([mb, ma], symbols, ok, labels))
    safe("M2", m2f)

    def m6f():
        _, s6 = _screen(src, P_M6)
        m6 = _status_map(s6)
        ok = lambda s: (m6.get(s) or {}).get("status") not in (None, "EXCLUDED")
        passed = [s for s in symbols if (m6.get(s) or {}).get("status") == "PASS"]
        metric = {}
        for s in passed:   # 收盤距前高（%）＝(收盤 ÷ analysis 的前 250 日最高 − 1)×100
            c, h = _num(m6[s].get("close_twd")), _num(m6[s].get("prior_high_twd"))
            metric[s] = (c / h - 1) * 100 if c is not None and h else None
        add("M6", _screen_counts(m6, symbols, ok), passed, metric, _excl_reasons([m6], symbols, ok, labels))
    safe("M6", m6f)

    month = available_month(as_of)
    rev: dict = {}
    rev_err = ""
    if month:
        try:
            rev, R = _rev_eval(src, symbols, [month, _month_add(month, -1), _month_add(month, -2)])
            labels.update({k: v for k, v in R.REASON_CODES.items()})
        except Exception as e:
            rev_err = f"analysis/revenue.py 無法評估（{type(e).__name__}：{e}）"
    else:
        rev_err = "無法決定可得的營收月份"
    rnote = [f"營收月份 {month}：依法定期限（次月 10 日）在資料日 {as_of} 已可得的最新月份。"] if month else []

    def m3f():
        if rev_err:
            raise RuntimeError(rev_err)
        rm = rev[month]
        ok = lambda s: (rm.get(s) or {}).get("rev_12m_high_status") in ("true", "false")
        passed = [s for s in symbols if (rm.get(s) or {}).get("rev_12m_high_status") == "true"]
        add("M3", _mode_counts(len(symbols), sum(1 for s in symbols if ok(s))), passed,
            {s: _num(rm[s].get("yoy_pct")) for s in passed}, _excl_reasons([rm], symbols, ok, labels), rnote)
    safe("M3", m3f)

    def m4f():
        if rev_err:
            raise RuntimeError(rev_err)
        ms = [month, _month_add(month, -1), _month_add(month, -2)]
        ok = lambda s: all((rev[mo].get(s) or {}).get("yoy_status") == "ok" for mo in ms)
        passed = [s for s in symbols if ok(s) and all((_num(rev[mo][s].get("yoy_pct")) or 0) > 0 for mo in ms)]
        maps = [rev[mo] for mo in ms]
        add("M4", _mode_counts(len(symbols), sum(1 for s in symbols if ok(s))), passed,
            {s: _num(rev[month][s].get("yoy_pct")) for s in passed}, _excl_reasons(maps, symbols, ok, labels),
            [f"營收月份 {ms[2]}～{ms[0]}（連續 3 個月，依法定期限可得）。"])
    safe("M4", m4f)

    def m7f():
        if rev_err:
            raise RuntimeError(rev_err)
        rm = rev[month]
        _, st = _screen(src, P_TREND)
        mt = _status_map(st)
        ok = lambda s: (rm.get(s) or {}).get("rev_12m_high_status") in ("true", "false") and \
            (mt.get(s) or {}).get("status") not in (None, "EXCLUDED")
        passed = [s for s in symbols if ok(s) and rm[s].get("rev_12m_high_status") == "true" and mt[s].get("status") == "PASS"]
        c = _screen_counts(mt, symbols, ok)
        add("M7", c, passed, {s: _num(rm[s].get("yoy_pct")) for s in passed},
            _excl_reasons([rm, mt], symbols, ok, labels), rnote)
    safe("M7", m7f)

    _new_modes(computed, src, symbols, labels, as_of, r1)
    return dict(as_of=as_of, revenue_month=month, price=price, computed=computed, symbols=symbols)


def _price_map(src: DataSource, m1: dict, as_of: str) -> dict:
    """名單列的收盤、成交金額取評估日 daily_prices 原值；漲跌幅取 analysis screen()（預設參數）的 change_pct
    （analysis 排除的股票沒有漲跌幅 → 顯示資料不足，網站不自算百分比）。"""
    try:
        px = {r["symbol"]: r for r in src.get_source_rows("daily_prices", {"price_date": as_of}, limit=1_000_000)}
    except (DataUnavailable, KeyError):
        px = {}
    try:
        names = {s["code"]: s for s in src.list_stocks()}
    except Exception:
        names = {}
    out = {}
    for sym, r in m1.items():
        p, n = px.get(sym) or {}, names.get(sym) or {}
        out[sym] = dict(name=n.get("name") or (r.get("name") if isinstance(r.get("name"), str) else sym),
                        market=n.get("market") or {"TWSE": "上市", "TPEx": "上櫃"}.get(r.get("market"), "—"),
                        close=_num(p.get("close_twd")), change_pct=_num(r.get("change_pct")),
                        amount=_num(p.get("amount_twd")) if p.get("close_twd") is not None else None)
    return out


def _fill_modes(view: dict, computed: dict, selected: Optional[str], sort: Optional[str] = None, src=None) -> None:
    as_of = view.get("as_of") or "—"
    test = bool(view.get("is_test"))
    out = []
    for m in mode_specs():
        d = dict(m)
        c = computed.get(m["id"])
        d["available"] = "na" not in m
        if not d["available"]:
            d["mode_status"] = "mode_na"
        elif c is None or "error" in c:
            d["mode_status"] = "error"
            d["error"] = (c or {}).get("error") or view.get("error") or "無法計算"
            d["available"] = bool(c)   # 引擎整體失敗時卡片不可點
        else:
            d.update(c)
            d["mode_status"], d["today_count"] = mode_state(c)
        d["selected"] = selected == m["id"] and d["available"]
        out.append(d)
    view["modes"] = out
    sel = next((d for d in out if d["selected"]), None)
    view["selected"] = sel
    if sel and sel.get("mode_status") in ("ok", "zero"):
        sel["sort_key"] = "trade_value" if (sel.get("alt_sort") and sort == "trade_value") else "metric"
        sel["rows"] = _rows(view, sel, test, as_of)


def _rows(view: dict, m: dict, test: bool, as_of: str) -> list[dict]:
    price = view.get("price") or {}
    src_label = TEST_LABEL if test else (m.get("engine") if m.get("new") else "analysis/screener.py screen()（daily_prices）")
    rows = []
    for s in m["passed"]:
        p = price.get(s) or {}
        href = source_href("daily_prices", symbol=s, price_date=as_of)
        ref = {"table": "daily_prices", "key": f"symbol={s}, price_date={as_of}"}

        def V(v, unit, digits=2, note=""):
            if v is None:
                return Val(None, unit, src_label, as_of, status=STATUS_INSUFFICIENT, note=note or "評估日沒有此欄數值（漲跌幅：選股引擎排除此股時不提供，本站不自算）",
                           is_test=test, digits=digits)
            return Val(v, unit, src_label, as_of, is_test=test, digits=digits, href=href, ref=ref)
        mv = (m.get("metric") or {}).get(s)
        rows.append(dict(code=s, name=p.get("name") or s, market=p.get("market") or "—",
                         close=V(p.get("close"), "新台幣元"), change_pct=V(p.get("change_pct"), "%"),
                         amount=V(p["amount"] / 1e8 if p.get("amount") is not None else None, "億元"),
                         metric=Val(mv, "", m.get("engine", ""), as_of, is_test=test, digits=m.get("metric_digits", 2),
                                    status=STATUS_OK if mv is not None else STATUS_INSUFFICIENT,
                                    note="" if mv is not None else "analysis 沒有提供此列的指標值"),
                         _mv=mv, _amt=p.get("amount"), **({"reason": (m.get("reasons") or {}).get(s) or ""}
                                                          if m.get("new") else {})))
    if m.get("sort_key") == "trade_value":
        rows.sort(key=lambda r: (r["_amt"] is None, -(r["_amt"] or 0), r["code"]))
    else:
        rows.sort(key=lambda r: (r["_mv"] is None, -(r["_mv"] or 0), -(r["_amt"] or 0), r["code"]))
    return rows
