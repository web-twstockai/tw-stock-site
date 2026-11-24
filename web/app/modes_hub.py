"""篩選中心新模式 M9～M21（analysis-0.10.0 modes.py；docs/indicators.md §12）與模式登記表。

- 登記表：analysis/modes.py registry()（讀不到模組時退回 analysis/mode_registry.json）。名稱、說明、排序指標、
  勝率顯示文字取登記表。等級不讀登記表 tier 欄位，改由 win_rate 依 §11 有效勝率與門檻計算（見 tier_of）。
- 計算：evaluate_modes() 每個（資料版本, 評估日）只算一次後快取，只留 summary（conditions 與輸入資料算完即釋放）。
  SqliteSource 一律以全市場計算（USAGE §9：橫斷面排名 M12／M14／M19 須用全市場），再由呼叫端篩到追蹤範圍。
"""
from __future__ import annotations

import gc
import json
import threading
import time
from typing import Any, Optional

from .data_source import DataSource

# 等級門檻（H AO 2026-11-24 18:20）：有效 5 日勝率 < 進階門檻不得放進階，< 高級門檻不得放高級。
# 有效勝率＝登記表 win_rate 的官方數字，且須符合 docs/indicators.md §11.2.7（N≥100 且 D≥30）；
# display 為「計算中／樣本不足／資料不足」或沒有數字 → 無有效勝率 → 基本。
TIER_WINRATE_ADVANCED = 0.70
TIER_WINRATE_PREMIUM = 0.80
ENV_TIER_ADVANCED = "TWSITE_TIER_WINRATE_ADVANCED"
ENV_TIER_PREMIUM = "TWSITE_TIER_WINRATE_PREMIUM"
MIN_WINRATE_N = 100   # §11.2.7 去重後事件數
MIN_WINRATE_D = 30    # §11.2.7 訊號日數
_PENDING_DISPLAY = frozenset({"計算中", "樣本不足", "資料不足"})
_RATE_KEYS = ("rate", "value", "p", "pct", "win", "net", "net_rate")
_N_KEYS = ("n", "N", "events", "event_count", "n_events")
_D_KEYS = ("d", "D", "days", "signal_days", "n_days", "signal_day_count")
_TIER_CLASS = {"基本": "t1", "進階": "t2", "高級": "t3"}


def _env_rate(name: str, default: float) -> float:
    import os
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    s = str(raw).strip().replace("%", "")
    try:
        v = float(s)
    except ValueError:
        return default
    if v > 1.0:
        v = v / 100.0
    return v


def tier_thresholds() -> tuple[float, float]:
    """(進階門檻, 高級門檻)；環境變數不合法（非 0 < 進階 ≤ 高級 ≤ 1）則用預設 0.70／0.80。"""
    adv = _env_rate(ENV_TIER_ADVANCED, TIER_WINRATE_ADVANCED)
    pre = _env_rate(ENV_TIER_PREMIUM, TIER_WINRATE_PREMIUM)
    if not (0 < adv <= pre <= 1):
        return TIER_WINRATE_ADVANCED, TIER_WINRATE_PREMIUM
    return adv, pre


def pct_label(rate: float) -> str:
    """0.70 →『70%』；0.699 →『69.9%』（去掉多餘小數 0）。"""
    p = rate * 100
    s = f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{s}%"


def _first_num(d: dict, keys: tuple) -> Optional[float]:
    import math
    for k in keys:
        if k not in d or d[k] is None or d[k] == "":
            continue
        try:
            f = float(d[k])
        except (TypeError, ValueError):
            continue
        if math.isnan(f):
            continue
        return f
    return None


def _normalize_rate(v: float) -> float:
    return v / 100.0 if v > 1.0 else v


def _rate_from_display(disp) -> Optional[float]:
    import re
    if disp is None or disp == "":
        return None
    if isinstance(disp, (int, float)):
        return _normalize_rate(float(disp))
    s = str(disp).strip()
    if s in _PENDING_DISPLAY:
        return None
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*%", s)
    if m:
        return float(m.group(1)) / 100.0
    try:
        return _normalize_rate(float(s))
    except ValueError:
        return None


def effective_win_rate(win_rate: Optional[dict]) -> Optional[float]:
    """登記表 win_rate 的「有效勝率」（0～1）。不符合 §11 顯示條件則回 None。

    判斷：
    - 來源欄位：優先 rate／value／p／pct／win／net／net_rate；否則解析 display（「70%」或數字）。
    - display 為「計算中」「樣本不足」「資料不足」→ 視為尚未有官方勝率（即使其他欄有研究數字也不用）。
    - 若有事件數 N 或訊號日數 D（n／N／events…、d／D／signal_days…），須 N≥100 且 D≥30（§11.2.7）；不足則無效。
    - evaluate_modes() 目前不輸出勝率；現況登記表只有 display＝計算中／樣本不足，故全部為 None。
    """
    w = win_rate or {}
    if not isinstance(w, dict):
        return None
    disp = w.get("display")
    if isinstance(disp, str) and disp.strip() in _PENDING_DISPLAY:
        return None
    rate = _first_num(w, _RATE_KEYS)
    if rate is not None:
        rate = _normalize_rate(rate)
    else:
        rate = _rate_from_display(disp)
    if rate is None:
        return None
    n = _first_num(w, _N_KEYS)
    d = _first_num(w, _D_KEYS)
    if n is not None and n < MIN_WINRATE_N:
        return None
    if d is not None and d < MIN_WINRATE_D:
        return None
    return rate


def tier_of(mode: Optional[dict], thresholds: Optional[tuple[float, float]] = None) -> str:
    """依模式登記列的 win_rate 決定等級；不讀 mode['tier']。"""
    adv, pre = thresholds if thresholds is not None else tier_thresholds()
    rate = effective_win_rate((mode or {}).get("win_rate"))
    if rate is None:
        return "基本"
    if rate + 1e-12 >= pre:
        return "高級"
    if rate + 1e-12 >= adv:
        return "進階"
    return "基本"


def tier_rule_text() -> str:
    """篩選中心區塊標頭：新規則說明（含「依事先登記的同一套規則」）。"""
    adv, pre = tier_thresholds()
    return (f"等級依事先登記的同一套規則，以歷史 5 日勝率（扣成本後報酬＞0 的比例；"
            f"N≥{MIN_WINRATE_N} 且 D≥{MIN_WINRATE_D}）劃分：無有效勝率或未達 {pct_label(adv)} 為基本，"
            f"達 {pct_label(adv)} 為進階，達 {pct_label(pre)} 為高級。等級只是標示，不代表保證或較準。")


def tier_groups() -> list[tuple]:
    """（等級名, CSS class, 各區說明）。說明由門檻產生，不用登記表 rule。"""
    adv, pre = tier_thresholds()
    rules = {
        "基本": f"無有效歷史 5 日勝率，或勝率未達 {pct_label(adv)}。",
        "進階": f"歷史 5 日勝率 ≥ {pct_label(adv)}。",
        "高級": f"歷史 5 日勝率 ≥ {pct_label(pre)}。",
    }
    return [(name, _TIER_CLASS[name], rules[name]) for name in ("基本", "進階", "高級")]


def empty_tier_message() -> str:
    adv, pre = tier_thresholds()
    return f"目前沒有模式達標（進階需歷史 5 日勝率 ≥ {pct_label(adv)}，高級 ≥ {pct_label(pre)}）"


def stock_tier_rule() -> str:
    adv, pre = tier_thresholds()
    return (f"模式等級依事先登記的同一套規則，以歷史 5 日勝率劃分：無有效勝率或未達 {pct_label(adv)} 為基本，"
            f"達 {pct_label(adv)} 為進階，達 {pct_label(pre)} 為高級。")


_REG: dict[str, Any] = {}
_LOCK = threading.Lock()
_CACHE: dict[tuple, "ModesLite"] = {}
_CACHE_MAX = 4


def registry() -> Optional[dict]:
    """模式登記表（dict）；讀不到回 None（篩選中心退回 hub.py 內建的 M1～M8）。"""
    from .screening import ANALYSIS_DIR, analysis_module
    key = str(ANALYSIS_DIR)
    if key in _REG:
        return _REG[key]
    reg = None
    try:
        reg = analysis_module("modes").registry()
    except Exception:
        p = ANALYSIS_DIR / "mode_registry.json"
        try:
            reg = json.loads(p.read_text("utf-8"))
        except Exception:
            reg = None
    _REG[key] = reg
    return reg


def registry_modes() -> dict[str, dict]:
    reg = registry() or {}
    return {m["id"]: m for m in reg.get("modes") or []}


def exclusion_codes() -> dict:
    """MODE_EXCLUSION_CODES（EXCL_*、REV_*、FIN_* 中文）；舊版 analysis 沒有 → {}。"""
    try:
        return dict(__import__("app.screening", fromlist=["x"]).analysis_module("modes").MODE_EXCLUSION_CODES)
    except Exception:
        return {}


class ModesLite:
    """evaluate_modes() 結果的精簡版：summary（每模式每檔一列）＋計算資訊。"""

    def __init__(self, summary, as_of: str, scope_all: bool, seconds: float, analysis_version: str,
                 ranked_counts: dict):
        self.summary = summary
        self.as_of = as_of
        self.scope_all = scope_all
        self.seconds = seconds
        self.analysis_version = analysis_version
        self.ranked_counts = ranked_counts   # 橫斷面排名模式：排名母體（當日可評估檔數，全市場）
        self._by: dict[str, Any] = {}

    def mode_summary(self, mid: str, symbols=None):
        if mid not in self._by:
            s = self.summary
            self._by[mid] = s[s["mode_id"] == mid].reset_index(drop=True)
        s = self._by[mid]
        if symbols is not None:
            s = s[s["symbol"].astype(str).isin(set(symbols))].reset_index(drop=True)
        return s


def _inputs(src: DataSource, as_of: Optional[str]):
    """(evaluate_modes 參數, 是否全市場)。SqliteSource：io_sqlite.load_mode_inputs(symbols=None, coverage_scope="all")；
    其他來源（測試資料）：screener_frame() 的同一份輸入＋monthly_revenues／income_statements 現行列，範圍＝追蹤範圍。"""
    import inspect

    import pandas as pd

    from . import analysis_hooks
    from .screening import analysis_module
    M = analysis_module("modes")
    db = getattr(src, "db_path", None)
    if db is not None and not src.is_test:
        IO = analysis_module("io_sqlite")
        mi = IO.load_mode_inputs(db, as_of=as_of, symbols=None, coverage_scope="all")
        return mi.mode_kwargs(), True
    sig = set(inspect.signature(M.evaluate_modes).parameters)
    kw = {k: v for k, v in analysis_hooks._frame(src).items() if k in sig}
    def rows(t):
        try:
            r = src.get_source_rows(t, {}, limit=10_000_000)
        except Exception:
            r = []
        return pd.DataFrame(r) if r else None
    kw.update(revenues=rows("monthly_revenues"), income_statements=rows("income_statements"))
    try:
        scope_all = src.universe.symbols is None
    except Exception:
        scope_all = False
    return kw, scope_all


def compute(src: DataSource, as_of: Optional[str] = None, screen_result=None) -> ModesLite:
    """計算一次（不查快取）。screen_result：同評估日、同範圍的 screen() 預設參數結果（可省約 12 秒）。"""
    from .screening import analysis_module
    M = analysis_module("modes")
    t0 = time.monotonic()
    kw, scope_all = _inputs(src, as_of)
    sr = None
    if screen_result is not None:
        try:   # 範圍一致（全市場輸入配全市場 screen；測試資料配同一份 frame）且評估日相同才沿用
            same_day = as_of is None or str(screen_result.as_of.date()) == str(as_of)
            same_scope = len(screen_result.summary) == kw["prices"]["symbol"].nunique() or not scope_all
            sr = screen_result if (same_day and same_scope and screen_result.params == analysis_module(
                "screener").ScreenParams().to_dict()) else None
        except Exception:
            sr = None
    r = M.evaluate_modes(**kw, screen_result=sr)
    summary = r.summary.copy()
    for c in ("symbol", "mode_id", "status"):
        summary[c] = summary[c].astype(str)
    ranked = {}
    for mid in ("M12", "M14", "M19"):
        s = summary[summary["mode_id"] == mid]
        ranked[mid] = int((s["status"] != "EXCLUDED").sum())
    lite = ModesLite(summary, str(r.as_of.date()), scope_all, round(time.monotonic() - t0, 1),
                     getattr(r, "analysis_version", ""), ranked)
    del r, kw, sr
    release()
    return lite


def release() -> None:
    """算完釋放輸入：gc＋malloc_trim（glibc；沒有則略過）。"""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _key(src: DataSource, as_of: Optional[str]):
    from .analysis_hooks import _universe_key, data_version
    from .screening import ANALYSIS_DIR
    return (id(src), data_version(src), _universe_key(src), as_of, str(ANALYSIS_DIR))


def result(src: DataSource, as_of: Optional[str] = None, screen_result=None) -> ModesLite:
    """快取版：同一（來源, 資料版本, 範圍, 評估日）只算一次；同時多個請求等同一次計算（鎖）。"""
    k = _key(src, as_of)
    hit = _CACHE.get(k)
    if hit is not None:
        return hit
    with _LOCK:
        hit = _CACHE.get(k)
        if hit is not None:
            return hit
        lite = compute(src, as_of, screen_result)
        while len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[k] = lite
        return lite


def cached(src: DataSource, as_of: Optional[str] = None) -> Optional[ModesLite]:
    return _CACHE.get(_key(src, as_of))


def clear_cache() -> None:
    _CACHE.clear()
    _REG.clear()
