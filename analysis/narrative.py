"""每日摘要文字（規則模板）——介面草案，純函式。

summary_narrative(result, extra_facts, news_events) → Narrative(text, facts_json, facts)
- 只用規則模板陳述程式算出的數值與已發布資訊標題；不寫漲跌原因、不推論因果、不宣稱勝率。
- 零結果明確寫「0 檔」並列出排除原因統計，不放寬條件。
- 文字中每個數字都必須能在 facts 找到（自動檢查，找不到即拋 ValueError，不產出）。
- facts_json：每個數字的來源表／欄位／主鍵／資料日期（對應 analysis/tables_draft.md 的 daily_summaries.facts_json）。
規則見 analysis/summary_rules.md。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

import pandas as pd

from screener import EXCLUSION_CODES, ScreenResult

__all__ = ["Fact", "Narrative", "NEWS_CATEGORIES", "summary_narrative", "untraceable_numbers", "DISCLAIMER"]

NEWS_CATEGORIES = ("官方已公告事實", "媒體報導", "社群未證實訊息", "AI分析推論")
DISCLAIMER = ("說明：以上僅陳述程式計算的數值與已發布資訊的標題，不說明漲跌原因；"
              "技術條件為篩選結果，非投資建議，亦不代表未來表現。")
_NUM = re.compile(r"(?<![A-Za-z_\d.,])[+\-]?\d[\d,]*(?:\.\d+)?%?")
_QUOTED = re.compile(r"〔[^〕]*〕")


@dataclass(frozen=True)
class Fact:
    fact_id: str
    label: str
    value: Any
    display: str
    unit: Optional[str]
    source_table: str
    source_field: str
    source_key: dict
    data_date: Optional[str]
    upstream: Optional[dict] = None


@dataclass(frozen=True)
class Narrative:
    text: str
    facts: list = field(default_factory=list)
    facts_json: str = "[]"
    generation_method: str = "rule"


def _tokens(s: str) -> set[str]:
    return {t.lstrip("+-") for t in _NUM.findall(s)}


def untraceable_numbers(text: str, facts: Sequence[Fact]) -> list[str]:
    """文字中（〔〕引用的原文標題除外）找不到對應 fact（display 或 data_date）的數字。"""
    allowed: set[str] = set()
    for f in facts:
        allowed |= _tokens(f.display)
        if f.data_date:
            allowed |= _tokens(str(f.data_date))  # 資料日期本身是 fact 的屬性，可出現在文字中
    return sorted(_tokens(_QUOTED.sub("", text)) - allowed)


def _d(x: Any) -> str:
    return pd.Timestamp(x).date().isoformat()


def summary_narrative(result: ScreenResult, extra_facts: Sequence[Fact] = (),
                      news_events: Sequence[dict] = (), max_listed: int = 50) -> Narrative:
    """result：screener.screen() 的結果（正式摘要須為 coverage_policy=strict）。
    extra_facts：其他數字（例如 index_daily 加權指數），每筆需含來源表／欄位／主鍵。
    news_events：news_events 表的列（dict：event_id, version, category, title, first_published_at, symbols），只列標題與分類。
    symbols＝同版本 news_event_symbols 的 symbol 清單（related_symbols_json 已移除，傳入舊鍵 related_symbols 會拋錯）。"""
    if not isinstance(result, ScreenResult):
        raise TypeError("result 必須是 ScreenResult")
    as_of = _d(result.as_of)
    key0 = {"as_of": as_of, "params_hash": result.params_hash}
    facts: list[Fact] = []

    def F(label, value, display, unit, field_, key=None, table="screen_results", upstream=None):
        f = Fact(f"F{len(facts) + 1:03d}", label, value, display, unit, table, field_,
                 dict(key or key0), as_of, upstream)
        facts.append(f)
        return display

    p = result.params
    s = result.summary
    lines = [f"【盤後技術篩選摘要】資料日期：{F('資料日期', as_of, as_of, None, 'as_of')}"]

    conds, off = [], []
    if p["enable_trend"]:
        conds.append(f"收盤 > MA{F('短均線日數', p['ma_short'], str(p['ma_short']), '交易日', 'params_json.ma_short')}"
                     f" > MA{F('長均線日數', p['ma_long'], str(p['ma_long']), '交易日', 'params_json.ma_long')}")
    else:
        off.append("趨勢")
    if p["enable_breakout"]:
        conds.append(f"收盤 > 前 {F('突破回看日數', p['breakout_lookback'], str(p['breakout_lookback']), '交易日', 'params_json.breakout_lookback')} 個交易日最高價")
    else:
        off.append("突破")
    if p["enable_volume"]:
        conds.append(f"成交量 ≥ 前 {F('均量回看日數', p['volume_lookback'], str(p['volume_lookback']), '交易日', 'params_json.volume_lookback')} 日均量 × "
                     f"{F('量能倍數', p['volume_multiplier'], f'{p['volume_multiplier']:g}', '倍', 'params_json.volume_multiplier')}")
    else:
        off.append("量能")
    if p["enable_turnover"]:
        conds.append(f"成交金額 ≥ {F('成交金額門檻', p['min_turnover'], f'{p['min_turnover']:,.0f}', '元', 'params_json.min_turnover')} 元")
    else:
        off.append("成交金額")
    lines.append("篩選條件：" + "；".join(conds) + (f"（未啟用：{'、'.join(off)}）" if off else "") + "。")

    n_all = len(s)
    passed = s[s["status"] == "PASS"].sort_values(["amount_twd", "symbol"], ascending=[False, True])
    n_pass, n_fail = len(passed), int((s["status"] == "FAIL").sum())
    n_ex = int((s["status"] == "EXCLUDED").sum())
    lines.append(f"評估 {F('評估檔數', n_all, str(n_all), '檔', 'count(*)')} 檔："
                 f"符合 {F('符合檔數', n_pass, str(n_pass), '檔', 'count(status=PASS)')} 檔、"
                 f"未符合 {F('未符合檔數', n_fail, str(n_fail), '檔', 'count(status=FAIL)')} 檔、"
                 f"排除 {F('排除檔數', n_ex, str(n_ex), '檔', 'count(status=EXCLUDED)')} 檔。")

    if n_pass == 0:
        lines.append(f"今日 {F('符合檔數', 0, '0', '檔', 'count(status=PASS)')} 檔符合全部已啟用條件。")
    else:
        lines.append("符合條件（依成交金額由大到小）：")
        for r in passed.head(max_listed).itertuples(index=False):
            k = dict(key0, symbol=r.symbol)
            up = {"table": "daily_prices", "key": {"symbol": r.symbol, "price_date": as_of},
                  "source": r.price_source, "fetched_at": r.price_fetched_at}
            sym = F("代號", r.symbol, r.symbol, None, "symbol", k)
            nm = F("名稱", r.name, r.name, None, "name", k, table="securities") if r.name else ""
            lines.append(
                f"- {sym} {nm}：收盤 {F('收盤', r.close_twd, f'{r.close_twd:,.2f}', '元', 'close_twd', k, upstream=up)} 元，"
                f"漲跌幅 {F('漲跌幅', r.change_pct, f'{r.change_pct:+.2f}%', '%', 'change_pct', k)}，"
                f"成交金額 {F('成交金額', r.amount_twd, f'{r.amount_twd:,.0f}', '元', 'amount_twd', k, upstream=up)} 元，"
                f"MA{p['ma_short']} {F('MA短', r.ma_short, f'{r.ma_short:,.2f}', '元', 'ma_short', k)}，"
                f"MA{p['ma_long']} {F('MA長', r.ma_long, f'{r.ma_long:,.2f}', '元', 'ma_long', k)}，"
                f"量比 {F('量比', r.volume_ratio, f'{r.volume_ratio:.2f}', '倍', 'volume_ratio', k)}。")
        if n_pass > max_listed:
            lines.append(f"（另有 {F('未列出檔數', n_pass - max_listed, str(n_pass - max_listed), '檔', 'count')} 檔，見選股頁）")

    counts = result.exclusion_counts()
    if counts:
        lines.append("排除原因：")
        for code, cnt in counts.items():
            lines.append(f"- {code}：{F('排除檔數－' + code, cnt, str(cnt), '檔', 'count(exclusion_code)', dict(key0, exclusion_code=code))} 檔"
                         f"（{EXCLUSION_CODES.get(code, '')}）")
        if counts.get("EXCL_ADJ_COVERAGE_GAP"):
            lines.append("除權息／減資資料回補完成前，上述因覆蓋不足而排除的股票不評估；結果如實顯示，不改用寬鬆政策。")

    for f in extra_facts:
        if not isinstance(f, Fact):
            raise TypeError("extra_facts 每筆必須是 Fact")
        facts.append(f)
        lines.append(f"{f.label}：{f.display}{f.unit or ''}（{f.source_table}.{f.source_field}，{f.data_date}）")

    if news_events:
        lines.append("同日相關資訊（只列標題與分類，不推論與股價的關係）：")
        for e in news_events:
            cat = e.get("category")
            if cat not in NEWS_CATEGORIES:
                raise ValueError(f"新聞分類必須是 {NEWS_CATEGORIES}，收到 {cat!r}")
            if "related_symbols" in e:
                raise ValueError("related_symbols 已移除（DECISIONS 2026-11-10 03:45）：請改傳 news_event_symbols 的 symbols")
            k = {"event_id": e.get("event_id"), "version": e.get("version")}
            pub = F("發布時間", e.get("first_published_at"), str(e.get("first_published_at")), None,
                    "first_published_at", k, table="news_events")
            F("標題", e.get("title"), "", None, "title", k, table="news_events")
            tag = "（未證實，僅供線索）" if cat == "社群未證實訊息" else ""
            syms = list(dict.fromkeys(e.get("symbols") or []))
            rel = "、".join(syms)
            for sym in syms:
                F("關聯代號", sym, sym, None, "symbol", dict(k, symbol=sym), table="news_event_symbols")
            lines.append(f"- 〔{e.get('title')}〕｜{cat}{tag}｜發布 {pub}" + (f"｜關聯 {rel}" if rel else ""))

    lines.append(DISCLAIMER)
    text = "\n".join(lines)
    bad = untraceable_numbers(text, facts)
    if bad:
        raise ValueError(f"摘要含無法追溯的數字：{bad}")
    fj = json.dumps([asdict(f) for f in facts], ensure_ascii=False, default=str)
    return Narrative(text=text, facts=facts, facts_json=fj, generation_method="rule")
