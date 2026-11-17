"""月營收「近 12 個月新高」與年增率的獨立核對（純 Python＋sqlite3，不用 pandas、不 import revenue 的計算）。

用法：
  .venv/bin/python verify_revenue.py                       # 只跑測試用合成資料（暫存 SQLite）
  .venv/bin/python verify_revenue.py --db ../data/twstock.db --month 2026-10 --write
      # 唯讀（sqlite URI mode=ro）以 backup API 取一份一致快照到暫存檔，再於快照上核對；
      # --write 時把結果寫入 docs/verification.md「## 三、月營收」節（唯讀檢查、非正式結果）。
"""
from __future__ import annotations

import argparse
import hashlib
import math
import re
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPORT_PATH = HERE.parent / "docs" / "verification.md"
SECTION = "## 三、月營收近 12 個月新高（唯讀檢查、非正式結果）"


def ym_index(s: str) -> int:
    """'YYYY-MM' → 自西元 0 年起的月序數（獨立於 revenue.month_add）。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})", s.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(f"bad month {s!r}")
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def ym_str(i: int) -> str:
    return "%04d-%02d" % (i // 12, i % 12 + 1)


def load(db: Path) -> dict:
    con = sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True)
    try:
        data: dict = {}
        q = ("SELECT symbol, market, revenue_month, revenue_twd, revenue_prev_year_twd FROM monthly_revenues "
             "WHERE is_current = 1")
        for sym, mkt, ym, rev, py in con.execute(q):
            d = data.setdefault(sym, {"market": mkt, "m": {}})
            k = ym_index(ym)
            if k in d["m"]:
                raise ValueError(f"duplicate current row {sym} {ym}")
            d["m"][k] = (rev, py)
        return data
    finally:
        con.close()


def ind_eval(data: dict, sym: str, month: str) -> dict:
    d = data.get(sym)
    t = ym_index(month)
    if d is None or t not in d["m"]:
        return {"status": "insufficient", "reason": "REV_CURRENT_MISSING", "yoy_status": "not_applicable",
                "yoy_reason": "REV_YOY_CURRENT_NULL", "yoy": None, "max": None, "max_months": [], "missing": [],
                "nulls": []}
    cur, py = d["m"][t]
    # 年增率
    if cur is None:
        yoy, ys, yr = None, "not_applicable", "REV_YOY_CURRENT_NULL"
    elif py is None:
        yoy, ys, yr = None, "not_applicable", "REV_YOY_PREV_YEAR_NULL"
    elif py == 0:
        yoy, ys, yr = None, "not_applicable", "REV_YOY_PREV_YEAR_ZERO"
    elif py < 0:
        yoy, ys, yr = None, "not_applicable", "REV_YOY_PREV_YEAR_NEGATIVE"
    else:
        yoy, ys, yr = 100.0 * (cur - py) / py, "ok", None
    missing, nulls, best, best_m = [], [], None, []
    i = t - 12
    while i < t:                      # 逐月走訪 m−12 … m−1
        if i not in d["m"]:
            missing.append(ym_str(i))
        else:
            v = d["m"][i][0]
            if v is None:
                nulls.append(ym_str(i))
            elif best is None or v > best:
                best, best_m = v, [ym_str(i)]
            elif v == best:
                best_m.append(ym_str(i))
        i += 1
    first = min(d["m"])
    if cur is None:
        st, rs = "insufficient", "REV_CURRENT_NULL"
    elif first > t - 12:
        st, rs = "insufficient", "REV_INSUFFICIENT_HISTORY"
    elif missing:
        st, rs = "insufficient", "REV_PRIOR_MONTH_MISSING"
    elif nulls:
        st, rs = "insufficient", "REV_PRIOR_MONTH_NULL"
    elif cur > best:
        st, rs = "true", None
    elif cur == best:
        st, rs = "false", "REV_EQUAL_PRIOR_HIGH"
    else:
        st, rs = "false", None
    return {"status": st, "reason": rs, "yoy_status": ys, "yoy_reason": yr, "yoy": yoy, "max": best,
            "max_months": best_m, "missing": missing, "nulls": nulls, "revenue": cur, "prev_year": py,
            "market": d["market"]}


def compare(db: Path, month: str) -> dict:
    """revenue.evaluate_revenue（pandas）與 ind_eval（純 Python）逐值比對。"""
    sys.path.insert(0, str(HERE))
    import revenue
    df = revenue.evaluate_revenue(revenue.load_monthly_revenues(db), month=month)
    data = load(db)
    mism, rows = [], []
    syms = sorted(s for s, d in data.items() if ym_index(month) in d["m"])
    got = {r["symbol"]: r for r in df.to_dict("records")}
    if sorted(got) != syms:
        mism.append(("symbols", sorted(got), syms))

    def eq(a, b):
        if a is None or (isinstance(a, float) and math.isnan(a)):
            return b is None
        if b is None:
            return False
        return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-9) if isinstance(a, (int, float)) else a == b

    n = 0
    for s in syms:
        a, b = got.get(s, {}), ind_eval(data, s, month)
        rows.append((s, b))
        pairs = [("rev_12m_high_status", "status"), ("rev_12m_high_reason", "reason"), ("yoy_status", "yoy_status"),
                 ("yoy_reason", "yoy_reason"), ("yoy_pct", "yoy"), ("prior_12m_max_twd", "max")]
        for ka, kb in pairs:
            n += 1
            if not eq(a.get(ka), b[kb]):
                mism.append((s, ka, a.get(ka), b[kb]))
        for ka, kb in (("prior_12m_max_months", "max_months"), ("missing_months", "missing"), ("null_months", "nulls")):
            n += 1
            if (a.get(ka) or "") != ",".join(b[kb]):
                mism.append((s, ka, a.get(ka), b[kb]))
    return {"n_symbols": len(syms), "values": n, "mismatches": mism, "rows": rows,
            "version": revenue.REV_12M_HIGH_VERSION, "yoy_version": revenue.REV_YOY_VERSION}


def snapshot(src: Path, dst: Path) -> tuple[str, str]:
    """唯讀開啟來源，以 SQLite backup API 複製一致快照。回傳 (快照時間 UTC+8, 快照 sha256)。"""
    s = sqlite3.connect(f"file:{Path(src).resolve()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        when = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    finally:
        d.close()
        s.close()
    return when, hashlib.sha256(Path(dst).read_bytes()).hexdigest()


def _pct(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def _fmt(v) -> str:
    return "—" if v is None else f"{int(v):,}"


def render_section(st: dict, month: str, src: str, when: str, sha: str, expected: list[str]) -> str:
    flagged = [s for s, b in st["rows"] if b["status"] == "true"]
    exp, got = set(expected), set(flagged)
    L = [SECTION, "",
         "> ⚠️ **唯讀檢查、非正式結果**：只為核對 `analysis/revenue.py` 的定義與程式，不是投資建議，也不代表市場現象"
         "（目前僅收錄 30 檔驗證樣本）。本節只核對月營收，不含 screener。", "",
         f"- 資料來源：`{src}` 以 `sqlite3` URI `mode=ro` 開啟，經 SQLite backup API 取一致快照後核對"
         f"（資料工程仍在寫入）。快照時間：{when}（Asia/Taipei，UTC+8）；快照 sha256：`{sha}`。",
         f"- 定義版本：`{st['version']}`（當月營收嚴格大於前 12 個日曆連續月份的最高值；缺月或 NULL＝資料不足；相等不算）；"
         f"年增率 `{st['yoy_version']}`。",
         f"- 營收月份：{month}；當月有現行列 {st['n_symbols']} 檔。",
         f"- 獨立核對：`analysis/verify_revenue.py`（純 Python 逐月走訪）對 `revenue.evaluate_revenue`（pandas 輸入），"
         f"逐值 {st['values']}，不一致 {len(st['mismatches'])}。",
         f"- 實際標記為新高：{len(flagged)} 檔：{'、'.join(flagged) or '（無）'}。",
         f"- 負責人預期 {len(expected)} 檔：{'、'.join(expected)}。"
         + ("**與預期一致。**" if exp == got else
            f"**與預期不一致**：預期有、實際沒有 {sorted(exp - got) or '無'}；實際有、預期沒有 {sorted(got - exp) or '無'}。"),
         "", "| 代號 | 市場 | 當月營收（元） | 前 12 月最高（元） | 最高所在月份 | 缺漏月份 | NULL 月份 | 新高狀態 | 原因代碼 | 年增率（%） | 年增狀態 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s, b in st["rows"]:
        L.append(f"| {s} | {b.get('market') or '—'} | {_fmt(b.get('revenue'))} | {_fmt(b['max'])} | "
                 f"{'、'.join(b['max_months']) or '—'} | {'、'.join(b['missing']) or '無'} | {'、'.join(b['nulls']) or '無'} | "
                 f"{b['status']} | {b['reason'] or '—'} | {_pct(b['yoy'])} | "
                 f"{b['yoy_status']}{'（' + b['yoy_reason'] + '）' if b['yoy_reason'] else ''} |")
    if st["mismatches"]:
        L += ["", "不一致明細：", ""] + [f"- {m}" for m in st["mismatches"][:50]]
    return "\n".join(L) + "\n"


def write_section(text: str) -> None:
    old = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else "# 篩選器獨立重算核對紀錄\n"
    pat = re.compile(r"\n" + re.escape(SECTION) + r"\n.*?(?=\n## |\Z)", re.S)
    new = pat.sub(lambda _: "\n" + text.rstrip("\n"), old) if pat.search(old) else old.rstrip("\n") + "\n\n" + text
    REPORT_PATH.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="月營收近 12 個月新高獨立核對")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--month", default=None)
    ap.add_argument("--expected", default="", help="負責人預期代號，逗號分隔（只用於報告比對，不影響計算）")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as td:
        if a.db is None:
            sys.path.insert(0, str(HERE / "tests"))
            import revenue_synthetic as RS
            db = RS.build_sqlite(Path(td) / "rev_synth.db")
            st = compare(db, a.month or RS.EVAL_MONTH)
            print(f"[測試用合成資料] {st['n_symbols']} 檔，逐值 {st['values']}，不一致 {len(st['mismatches'])}")
        else:
            snap = Path(td) / "snapshot.db"
            when, sha = snapshot(a.db, snap)
            month = a.month or "2026-10"
            st = compare(snap, month)
            print(f"[唯讀快照 {when}] {st['n_symbols']} 檔，逐值 {st['values']}，不一致 {len(st['mismatches'])}")
            for s, b in st["rows"]:
                print(s, b["status"], b["reason"], b.get("revenue"), b["max"], b["max_months"], b["missing"], b["nulls"])
            if a.write:
                exp = [x.strip() for x in a.expected.split(",") if x.strip()]
                write_section(render_section(st, month, str(a.db), when, sha, exp))
                print(f"已寫入 {REPORT_PATH}")
        for m in st["mismatches"][:20]:
            print("  不一致：", m)
    return 1 if st["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
