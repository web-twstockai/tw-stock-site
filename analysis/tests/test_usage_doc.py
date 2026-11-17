"""USAGE.md 與程式常數一致性檢查（避免文件落後程式）。不使用任何資料。"""
import re
from pathlib import Path

import adjust as A
import screener as S
import version as V

USAGE = (Path(__file__).resolve().parents[1] / "USAGE.md").read_text(encoding="utf-8")


def _section(title_prefix: str) -> str:
    m = re.search(r"^## " + re.escape(title_prefix) + r".*?$(.*?)(?=^## )", USAGE, re.S | re.M)
    assert m, f"USAGE.md 找不到第 {title_prefix} 節"
    return m.group(1)


def test_version_header_and_changelog():
    assert S.ANALYSIS_VERSION == V.ANALYSIS_VERSION
    head = USAGE.split("### 變更紀錄")[0]
    assert f"**版本：`{V.ANALYSIS_VERSION}`**" in head
    assert re.search(r"^\| " + re.escape(V.ANALYSIS_VERSION) + r" \|", USAGE, re.M), "變更紀錄缺目前版本"


def test_exclusion_codes_match():
    sec = _section("5.")
    listed = set(re.findall(r"\bEXCL_[A-Z_]+\b", sec))
    assert listed == set(S.EXCLUSION_CODES), (
        f"文件多列 {sorted(listed - set(S.EXCLUSION_CODES))}，文件缺 {sorted(set(S.EXCLUSION_CODES) - listed)}")


def test_coverage_kinds_match():
    sec = _section("6.")
    m = re.search(r"action_kind 完整清單[^：]*：(.*?)，strict", sec)
    assert m, "第 6 節找不到 action_kind 完整清單"
    listed = tuple(re.findall(r"`([a-z_]+)`", m.group(1)))
    assert listed == A.COVERAGE_KINDS
    assert f"{len(A.COVERAGE_KINDS)} 類" in m.group(0)


def test_halts_documented():
    for word in ("trading_halts", "is_halted", "halts_table_missing", "window_halt_days",
                 "window_missing_days", "window_no_trade_days", "writer.py", "computed_at", "input_max_fetched_at",
                 "COVERAGE_COVERED_BY", "price_version"):
        assert word in USAGE, word


def test_covered_by_documented():
    import adjust as A2
    for target, sources in A2.COVERAGE_COVERED_BY.items():
        assert target in USAGE and all(src in USAGE for src in sources)
