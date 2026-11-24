"""模式成績單：只讀登記表／分析端正式產出的歷史 5 日統計，組 /scorecard 頁面資料。

規則（HANDOFF §33、docs/indicators.md §11）：
- 數字不得取自研究過程的初步稿件或任何研究 CSV。
- win_rate.display 為「計算中／樣本不足／資料不足」時，其他欄位一律不對外顯示。
- 未達 N≥100 且 D≥30 時不顯示任何比例或平均報酬。
- 不顯示停利／停損等非 §11 主口徑欄位。
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from . import modes_hub as MH

PENDING = MH._PENDING_DISPLAY  # 計算中、樣本不足、資料不足
DISCLAIMER = "歷史統計，不代表未來，非投資建議"
WINDEF = "＝扣成本後 5 個交易日報酬＞0 的比例"
# 允許在「非計算中」且達門檻時對外顯示的正式欄位（分析端之後寫入登記表 win_rate 時才會有）
_RATE = MH._RATE_KEYS
_N = MH._N_KEYS
_D = MH._D_KEYS
_AVG = ("avg_return_pct", "mean_return_pct", "avg_net_pct", "mean_net_pct",
        "avg_return", "mean_return", "net_avg_pct", "avg_pct")
_BASE = ("baseline_same_day_pct", "same_day_pct", "same_day_rate", "baseline_same_day",
         "baseline_pct", "benchmark_pct", "random_rate", "peer_rate")
_PERIOD_START = ("period_start", "start", "from", "start_date")
_PERIOD_END = ("period_end", "end", "to", "end_date")
# 明確拒絕（即使正式欄裡混入也不顯示）
_BLOCKED = re.compile(r"stop[_ ]?(loss|profit)|take[_ ]?profit|停利|停損|trailing", re.I)
_RESEARCH_PATH = re.compile(r"screen[_-]?modes[_-]?v[234]", re.I)


def _num(d: dict, keys: tuple) -> Optional[float]:
    return MH._first_num(d, keys)


def _is_pending(display) -> bool:
    s = str(display or "").strip()
    return (not s) or s in PENDING


def _pct_unit(v: Optional[float], key: str) -> Optional[float]:
    """回傳百分點（例如 1.2＝+1.2%）。比例（0～1）的勝率／基準欄轉成百分點；平均報酬若 |v|≤1 且欄名不含 pct，也當比例。"""
    if v is None or not math.isfinite(v):
        return None
    kl = key.lower()
    if "pct" in kl or "percent" in kl:
        return v if abs(v) > 1 or kl.endswith("_pct") and abs(v) > 1 else (v * 100 if abs(v) <= 1 else v)
    # rate／勝率類：>1 視為已是百分點
    if abs(v) <= 1:
        return v * 100
    return v


def official_stats(win_rate: Optional[dict]) -> dict:
    """對外可顯示的官方統計。pending 時只留 display／note，其他欄位一律沒有。"""
    w = dict(win_rate or {}) if isinstance(win_rate, dict) else {}
    # 丢掉被封鎖或不屬正式口徑的鍵（防研究稿殘欄）
    w = {k: v for k, v in w.items() if not _BLOCKED.search(str(k))}
    disp = str(w.get("display") or "計算中").strip() or "計算中"
    note = str(w.get("note") or "").strip()
    if _RESEARCH_PATH.search(note) or re.search(r"\d+(?:\.\d+)?\s*%", note):
        note = ""   # 說明若指向研究稿或夾帶比例，不上畫面
    out = dict(ready=False, status="pending", display="計算中", note=note,
               rate_pct=None, n=None, d=None, avg_return_pct=None, baseline_pct=None,
               period_start=None, period_end=None, mean_ok=False)
    if disp == "樣本不足":
        out.update(status="few", display="樣本不足")
        return out
    if disp == "資料不足" or _is_pending(disp):
        out.update(status="insufficient" if disp == "資料不足" else "pending",
                   display="資料不足" if disp == "資料不足" else "計算中")
        return out

    # 到這裡才是「登記表宣称已有官方數字」；仍須通過 N／D 門檻才揭示
    n = _num(w, _N)
    d = _num(w, _D)
    rate = MH.effective_win_rate(w)
    if (n is not None and n < MH.MIN_WINRATE_N) or (d is not None and d < MH.MIN_WINRATE_D) or rate is None:
        # 有寫數字但未達官方顯示門檻，或無法解析為有效勝率 → 不揭示任何比例
        why = "樣本不足"
        if n is not None or d is not None:
            out.update(status="few", display="樣本不足",
                       note=note or "未達 N≥100 且 D≥30 的顯示門檻，不顯示比例。")
        else:
            out.update(status="pending", display="計算中")
        return out

    # 找出實際用了哪個平均／基準欄名，以便單位判斷
    def pick(keys):
        for k in keys:
            if k in w and w[k] is not None and w[k] != "":
                return k, w[k]
        return None, None

    ak, av = pick(_AVG)
    bk, bv = pick(_BASE)
    avg = _pct_unit(float(av), ak) if ak is not None else None
    base = _pct_unit(float(bv), bk) if bk is not None else None
    # 勝率用有效勝率（0～1）
    start = next((str(w[k]) for k in _PERIOD_START if w.get(k)), None)
    end = next((str(w[k]) for k in _PERIOD_END if w.get(k)), None)
    out.update(ready=True, status="ok", display=MH.pct_label(rate),
               rate_pct=round(rate * 100, 4),
               n=int(n) if n is not None else None,
               d=int(d) if d is not None else None,
               avg_return_pct=None if avg is None else round(avg, 4),
               baseline_pct=None if base is None else round(base, 4),
               period_start=start, period_end=end, note=note,
               mean_ok=MH.mean_return_ok(w))
    return out


def why_text(tier: str, stats: dict, adv: float, pre: float) -> str:
    """白話說明目前為何列在這個等級（只引用門檻與官方狀態，不引用研究稿數字）。

    進階／高級須勝率達門檻且扣成本後平均報酬確定為正（H AO 2026-11-24 20:02）；門檻暫定，待三年資料重設。
    """
    adv_l, pre_l = MH.pct_label(adv), MH.pct_label(pre)
    req = f"進階需歷史 5 日勝率 ≥ {adv_l} 且平均報酬為正；高級 ≥ {pre_l}（{MH.PROVISIONAL_NOTE}）。"
    if stats.get("ready") and stats.get("rate_pct") is not None:
        p = MH.pct_label(stats["rate_pct"] / 100.0)
        if tier == "高級":
            return f"官方歷史 5 日勝率為 {p}，已達高級門檻 {pre_l}，且扣成本後平均報酬為正，因此列在高級。{req}"
        if tier == "進階":
            return (f"官方歷史 5 日勝率為 {p}，已達進階門檻 {adv_l}、尚未達到高級門檻 {pre_l}，"
                    f"且扣成本後平均報酬為正，因此列在進階。{req}")
        if stats["rate_pct"] / 100.0 + 1e-12 >= adv and not stats.get("mean_ok"):
            return (f"官方歷史 5 日勝率為 {p}，已達勝率門檻，但扣成本後平均報酬未確定為正"
                    f"（需 95% 信賴區間下限＞0），因此列在基本。{req}")
        return f"官方歷史 5 日勝率為 {p}，未達進階門檻 {adv_l}，因此列在基本。{req}"
    if stats.get("display") == "樣本不足" or stats.get("status") == "few":
        return (f"此模式目前為「樣本不足」（未達事件數 N≥{MH.MIN_WINRATE_N} 且訊號日數 D≥{MH.MIN_WINRATE_D} 的顯示門檻），"
                f"沒有有效歷史 5 日勝率與平均報酬，因此列在基本。{req}")
    return f"官方歷史 5 日勝率與平均報酬仍在計算中，目前沒有有效數字，因此列在基本。{req}"


def _method_text(adv: float, pre: float) -> dict:
    return dict(
        windef=WINDEF,
        threshold=f"事件數 N≥{MH.MIN_WINRATE_N} 且訊號日數 D≥{MH.MIN_WINRATE_D} 才顯示比例，否則顯示「樣本不足」。",
        cost="次一交易日開盤買進、第 5 個交易日收盤賣出；買、賣手續費各 0.1425%，賣出證交稅 0.3%。股利稅因人而異，未計入。",
        baseline="同日基準：同一批訊號日、所有可評估股票，用同樣規則持有 5 日的比例（確定性計算，不抽亂數）。這就是與「隨機買進」的官方比較。",
        period="統計期間從 2023-11 起（待 3 年行情回補後依同一口徑重算）。",
        tier=(f"等級以有效歷史 5 日勝率與扣成本後平均報酬劃分：{MH.tier_requirement_text()}；"
              f"平均報酬為正指 95% 信賴區間下限＞0，其餘為基本。"),
    )


def build() -> dict:
    """成績單頁模板變數（不觸發 evaluate_modes，只讀登記表）。"""
    from . import hub as H
    adv, pre = MH.tier_thresholds()
    reg = MH.registry() or {}
    raw_modes = MH.registry_modes()
    rows = []
    for spec in H.mode_specs():
        mid = spec["id"]
        raw = raw_modes.get(mid) or {}
        stats = official_stats(raw.get("win_rate"))
        available = "na" not in spec
        rows.append(dict(
            id=mid,
            name=spec.get("name") or mid,
            desc=spec.get("desc") or "",
            tier=spec.get("tier") or MH.tier_of(raw),
            tier_class=MH._TIER_CLASS.get(spec.get("tier") or "基本", "t1"),
            available=available,
            href=f"/hub?mode={mid}#results" if available else "",
            stats=stats,
            why=why_text(spec.get("tier") or "基本", stats, adv, pre),
        ))
    disc = H.public_text(reg.get("disclaimer")) or DISCLAIMER
    return dict(
        rows=rows,
        count=len(rows),
        disclaimer=disc.rstrip("。"),
        storage_note="",  # 占位，模板用 is defined
        method=_method_text(adv, pre),
        adv_label=MH.pct_label(adv),
        pre_label=MH.pct_label(pre),
        pending_note=H.public_text(reg.get("winrate_note")) or "歷史勝率待行情回補後依官方口徑重算；初步研究數字不對外顯示。",
        registry_version=reg.get("registry_version") or "",
    )
