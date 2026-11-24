"""篩選中心新模式 M9～M21（analysis-0.10.0 modes.py；docs/indicators.md §12）與模式登記表。

- 登記表：analysis/modes.py registry()（讀不到模組時退回 analysis/mode_registry.json）。等級、名稱、說明、排序指標、
  勝率顯示文字一律取登記表；網站不自行判斷。
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
