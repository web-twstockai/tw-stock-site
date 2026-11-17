"""narrative.py 單元測試（測試用合成資料）。"""
import json

import pytest

import narrative as N
import screener as S

CAUSAL = ("因為", "由於", "受惠", "帶動", "利多", "利空", "導致", "推升", "拖累", "看好", "預期將")


def _res(T, **kw):
    a = dict(prices=T["daily_prices"], calendar=T["trading_calendar"], actions=T["corporate_actions"],
             securities=T["securities"], actions_coverage=T["actions_coverage"], halts=T["trading_halts"])
    a.update(kw)
    return S.screen(**a)


def test_zero_result_says_zero(T):
    r = _res(T, actions_coverage=None)  # 無覆蓋表 → strict 全數排除
    n = N.summary_narrative(r)
    assert "今日 0 檔符合全部已啟用條件。" in n.text
    assert "EXCL_ADJ_COVERAGE_GAP" in n.text and "不改用寬鬆政策" in n.text
    assert N.untraceable_numbers(n.text, n.facts) == []


def test_pass_list_and_facts_json(T):
    r = _res(T)
    n = N.summary_narrative(r)
    passed = r.summary[r.summary["status"] == "PASS"]
    assert len(passed) > 0
    for sym in passed["symbol"]:
        assert f"- {sym}" in n.text
    fj = json.loads(n.facts_json)
    assert n.generation_method == "rule"
    assert all({"fact_id", "source_table", "source_field", "source_key", "display"} <= set(f) for f in fj)
    assert len({f["fact_id"] for f in fj}) == len(fj)


def test_no_causal_words(T):
    n = N.summary_narrative(_res(T), news_events=[
        {"event_id": "E1", "category": "媒體報導", "title": "測試標題 2026 年營收",
         "first_published_at": "2026-11-09 18:00", "version": 1, "symbols": ["TEST01"]}])
    assert not any(w in n.text for w in CAUSAL)
    assert N.DISCLAIMER in n.text


def test_news_title_numbers_ignored_but_category_validated(T):
    r = _res(T)
    ok = N.summary_narrative(r, news_events=[{"event_id": "E1", "category": "社群未證實訊息", "title": "傳言 99 億",
                                              "first_published_at": "2026-11-09", "symbols": []}])
    assert "未證實，僅供線索" in ok.text
    with pytest.raises(ValueError):
        N.summary_narrative(r, news_events=[{"event_id": "E2", "category": "利多消息", "title": "x",
                                             "first_published_at": "2026-11-09"}])


def test_untraceable_number_raises(T):
    f = N.Fact("X1", "加權指數", 12345.6, "12,345.60", "點", "index_daily", "close", {"index_code": "TAIEX"},
               "2026-11-09")
    n = N.summary_narrative(_res(T), extra_facts=[f])
    assert "12,345.60" in n.text
    assert N.untraceable_numbers("收盤 777 點", n.facts) == ["777"]


def test_type_checks(T):
    with pytest.raises(TypeError):
        N.summary_narrative({"x": 1})
    with pytest.raises(TypeError):
        N.summary_narrative(_res(T), extra_facts=["12"])
