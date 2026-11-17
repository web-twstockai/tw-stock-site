"""選股條件的白話說明——網站顯示文字的唯一來源（純函式）。

規則：每個條件一句話；數字一律取自傳入的 `ScreenParams`（未傳＝預設參數）；
只描述「怎麼算、比什麼」，不寫效果、勝率、報酬或任何建議。定義依據 docs/indicators.md 第 1、2 節。
"""

from __future__ import annotations

from typing import Any, Optional

from screener import CONDITION_NAMES, ScreenParams

__all__ = ["condition_explanations", "common_explanation", "format_number", "format_amount_twd"]

_TITLES = {"C1_TREND": "趨勢", "C2_BREAKOUT": "突破", "C3_VOLUME": "量能", "C4_TURNOVER": "成交金額"}
_ENABLE = {"C1_TREND": "enable_trend", "C2_BREAKOUT": "enable_breakout",
           "C3_VOLUME": "enable_volume", "C4_TURNOVER": "enable_turnover"}


def format_number(v: float) -> str:
    """整數值不帶小數；其餘去掉尾端 0（1.5 → "1.5"，2.0 → "2"）。"""
    f = float(v)
    if f.is_integer():
        return f"{int(f):,}"
    return f"{f:,.6f}".rstrip("0").rstrip(".")


def format_amount_twd(v: float) -> str:
    """新台幣金額：≥1 億且為 1 萬的整數倍時以「億元」表示，否則以「元」表示（千分位）。"""
    f = float(v)
    if f >= 1e8 and (f / 1e4).is_integer():
        return f"{format_number(f / 1e8)} 億元"
    return f"{format_number(f)} 元"


def condition_explanations(params: Optional[ScreenParams] = None) -> list[dict[str, Any]]:
    """回傳四個條件各一筆：condition_id, condition_name（screener.CONDITION_NAMES）, title, enabled, text。
    未啟用的條件 text 前加「未啟用，不檢查。」。"""
    p = ScreenParams() if params is None else params
    if not isinstance(p, ScreenParams):
        raise TypeError("params 必須是 ScreenParams")
    texts = {
        "C1_TREND": (f"今天收盤價高於最近 {p.ma_short} 個有效交易日（含今天）的平均收盤價（MA{p.ma_short}），"
                     f"而且 MA{p.ma_short} 高於最近 {p.ma_long} 個有效交易日的平均收盤價（MA{p.ma_long}）；均線用前復權還原價計算。"),
        "C2_BREAKOUT": (f"今天收盤價高於前 {p.breakout_lookback} 個有效交易日（不含今天）每日最高價中的最高值；"
                        f"過去的最高價同樣用前復權還原價比較。"),
        "C3_VOLUME": (f"今天成交量（股）至少是前 {p.volume_lookback} 個有效交易日（不含今天）平均成交量的 "
                      f"{format_number(p.volume_multiplier)} 倍；過去的成交量依配股、減資等股數變化調整。"),
        "C4_TURNOVER": (f"今天成交金額至少新台幣 {format_amount_twd(p.min_turnover)}；用來源公布的當日金額，"
                        f"不還原，也不以成交量乘價格估算。"),
    }
    out = []
    for cid in CONDITION_NAMES:
        en = bool(getattr(p, _ENABLE[cid]))
        out.append({"condition_id": cid, "condition_name": CONDITION_NAMES[cid], "title": _TITLES[cid],
                    "enabled": en, "text": ("" if en else "未啟用，不檢查。") + texts[cid]})
    return out


def common_explanation(params: Optional[ScreenParams] = None) -> str:
    """各條件共用的計算前提（一段話）。"""
    p = ScreenParams() if params is None else params
    if not isinstance(p, ScreenParams):
        raise TypeError("params 必須是 ScreenParams")
    return (f"「有效交易日」指有成交、有收盤價的交易日，停牌或無成交的日子跳過不算；"
            f"有效交易日不足 {p.required_valid_days} 天，或計算期間內跳過的交易日超過 {p.max_gap_days} 天，"
            f"該股不評估，列入排除名單並寫明原因。"
            + ("除權息、減資等事件資料未完整涵蓋計算期間時，該股同樣排除，不以未還原價格替代。"
               if p.coverage_policy == "strict" else
               f"目前 coverage_policy＝{p.coverage_policy}，僅供研究比較，不是正式結果。")
            + "啟用的條件要全部同時成立才列入結果。")
