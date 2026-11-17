"""靜態匯出自檢（export_static.py 匯出完成前、deploy_pages.sh 推送前都會跑）。

用法（在 web/ 下）：.venv/bin/python tools/check_export.py ../build/site
檢查（任何一項不符 → 列出原因並以 1 結束；deploy_pages.sh 會因此失敗、不推送）：
1. 首頁「行情檔數」不得超過追蹤範圍檔數（build-info.json 的 tracking_universe；舊版用 universe）；上漲＋下跌也不得超過。
2. 搜尋清單 api/stocks.json 的每一檔都要有個股頁（stock/{代號}/index.html），且都在追蹤範圍內。
3. manifest.json 的 stocks 每一檔都要有頁。
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

TIP_RE = re.compile(r'<span class="tip"[^>]*>.*?</span>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def home_counts(index_html: str) -> dict:
    """首頁市場概況那一行：行情檔數 N｜上漲 U｜下跌 D（去掉來源提示框後取數字）。找不到就回空 dict。"""
    b = re.search(r'aria-label="上漲 ([\d,]+) 檔、下跌 ([\d,]+) 檔、其餘 (-?[\d,]+) 檔，共 ([\d,]+) 檔有行情"', index_html)
    if b:   # 漲跌比例條（有行情時的正常版面）
        return {"up": _num(b.group(1)), "down": _num(b.group(2)), "rows": _num(b.group(4))}
    m = re.search(r'<p class="lg-line">行情檔數.*?</p>', index_html, re.S)
    if not m:
        return {}
    text = html.unescape(TAG_RE.sub("", TIP_RE.sub("", m.group(0))))
    out = {}
    for key, label in (("rows", "行情檔數"), ("up", "上漲"), ("down", "下跌")):
        mm = re.search(label + r"\s*([\d,]+)", text)
        if mm:
            out[key] = _num(mm.group(1))
    return out


def check(out: Path) -> list[str]:
    problems: list[str] = []
    try:
        info = json.loads((out / "build-info.json").read_text("utf-8"))
    except Exception as e:
        return [f"讀不到 build-info.json：{e}"]
    universe = set(info.get("tracking_universe") or info.get("universe") or [])   # 追蹤範圍全部
    if not universe:
        problems.append("build-info.json 的 universe 是空的")
    n = len(universe)
    try:
        counts = home_counts((out / "index.html").read_text("utf-8"))
    except OSError as e:
        counts = {}
        problems.append(f"讀不到首頁 index.html：{e}")
    if (out / "index.html").exists() and counts.get("rows") is None:
        problems.append("首頁找不到「行情檔數」（漲跌比例條與文字行都沒有），無法確認範圍；不放行")
    if counts.get("rows") is not None and counts["rows"] > n:
        problems.append(f"首頁行情檔數 {counts['rows']:,} 超過追蹤範圍 {n:,} 檔（範圍外股票被算入）")
    if counts.get("up") is not None and counts.get("down") is not None and counts["up"] + counts["down"] > n:
        problems.append(f"首頁上漲 {counts['up']:,}＋下跌 {counts['down']:,} 超過追蹤範圍 {n:,} 檔")
    try:
        stocks = json.loads((out / "api" / "stocks.json").read_text("utf-8"))
    except Exception as e:
        stocks = []
        problems.append(f"讀不到 api/stocks.json：{e}")
    v2 = info.get("format") == "v2"
    if v2:   # 匯出 v2：共用殼頁＋每檔一份 data/stock/<代號>.json.gz
        for f in ("stock/index.html", "source/index.html", "screener/index.html", "revenue/index.html",
                  "static/js/v2.js", "data/screener.json.gz"):
            if not (out / f).is_file():
                problems.append(f"v2 缺少 {f}")
        no_page = [s["code"] for s in stocks if not (out / "data" / "stock" / f"{s['code']}.json.gz").is_file()]
    else:
        no_page = [s["code"] for s in stocks if not (out / "stock" / s["code"] / "index.html").is_file()]
    if no_page:
        problems.append(f"搜尋清單 api/stocks.json 有 {len(no_page):,} 檔沒有個股頁（例：{', '.join(no_page[:5])}）")
    outside = [s["code"] for s in stocks if universe and s["code"] not in universe]
    if outside:
        problems.append(f"搜尋清單有 {len(outside):,} 檔不在追蹤範圍（例：{', '.join(outside[:5])}）")
    try:
        man = json.loads((out / "manifest.json").read_text("utf-8"))
        if v2:
            miss = [c for c in (man.get("stocks") or {}) if not (out / "data" / "stock" / f"{c}.json.gz").is_file()]
            nomon = [m for m in man.get("months") or [] if not (out / "data" / "revenue" / f"{m}.json.gz").is_file()]
            if nomon:
                problems.append(f"v2 manifest 列的月營收月份有 {len(nomon)} 個沒有資料檔（例：{', '.join(nomon[:3])}）")
        else:
            miss = [c for c, f in (man.get("stocks") or {}).items() if not (out / f).is_file()]
        if miss:
            problems.append(f"manifest.json 有 {len(miss):,} 檔個股頁不存在（例：{', '.join(miss[:5])}）")
    except Exception as e:
        problems.append(f"讀不到 manifest.json：{e}")
    return problems


def summary(out: Path) -> str:
    info = json.loads((out / "build-info.json").read_text("utf-8"))
    c = home_counts((out / "index.html").read_text("utf-8"))
    st = json.loads((out / "api" / "stocks.json").read_text("utf-8"))
    return (f"追蹤範圍 {len(info.get('tracking_universe') or info.get('universe') or []):,} 檔｜首頁行情檔數 {c.get('rows', '—')}｜上漲 {c.get('up', '—')}"
            f"｜下跌 {c.get('down', '—')}｜搜尋清單 {len(st):,} 檔")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("用法：check_export.py <匯出目錄>", file=sys.stderr)
        return 2
    out = Path(argv[0])
    problems = check(out)
    if problems:
        print("自檢失敗：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"自檢通過：{summary(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
