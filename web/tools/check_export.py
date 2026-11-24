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


HUB_REQUIRED = (("/hub", "/hub?mode=M1", "/hub?mode=M2", "/hub?mode=M3", "/hub?mode=M4", "/hub?mode=M6", "/hub?mode=M7")
                + tuple(f"/hub?mode=M{i}" for i in range(9, 22))   # analysis-0.10.0 M9～M21
                + ("/hub?mode=M2&sort=trade_value", "/industry"))
IND_LINK_RE = re.compile(r'<a class="ind-tile[^"]*" href="([^"]+)"')


def check_extra_pages(out: Path, info: dict) -> list[str]:
    """篩選中心與產業頁：必收的網址都要有檔案；產業總覽上的每個產業連結都要開得到（不是死連結）。"""
    problems: list[str] = []
    extra = info.get("extra_pages")
    if extra is None:
        return ["build-info.json 沒有 extra_pages（篩選中心／產業頁），匯出程式與自檢版本不符"]
    miss = [k for k in HUB_REQUIRED if k not in extra]
    if miss:
        problems.append(f"篩選中心／產業頁缺少：{', '.join(miss)}")
    nofile = [k for k, f in extra.items() if not (out / f).is_file()]
    if nofile:
        problems.append(f"篩選中心／產業頁有 {len(nofile)} 個檔案不存在（例：{', '.join(nofile[:3])}）")
    ind_file = extra.get("/industry")
    if ind_file and (out / ind_file).is_file():
        from urllib.parse import unquote
        base = (out / ind_file).parent
        dead = []
        for h in IND_LINK_RE.findall((out / ind_file).read_text("utf-8")):
            t = unquote(html.unescape(h).split("#", 1)[0])
            p = (base / t)
            if not (p.is_file() or (p / "index.html").is_file()):
                dead.append(t)
        if dead:
            problems.append(f"產業總覽有 {len(dead)} 個產業連結開不到（例：{', '.join(dead[:3])}）")
    return problems


LEGACY_NEED = ('name="robots" content="noindex"', 'http-equiv="refresh"', 'rel="canonical"')


def check_legacy_redirects(out: Path, info: dict, stocks: list) -> list[str]:
    """v2 舊網址轉址頁 stock/<代號>/index.html：每個都要轉到存在的殼頁 stock/?code=<同代號>、有 noindex／canonical／
    可點連結、代號在搜尋清單內；build-info 記錄的個數要與實際一致。目錄裡不能有其他檔案（不是 v1 個股頁殘留）。"""
    problems: list[str] = []
    want = info.get("legacy_redirects")
    root = out / "stock"
    stubs = sorted(p for p in root.glob("*/index.html")) if root.is_dir() else []
    if want is None:
        return problems if not stubs else [f"build-info 沒記 legacy_redirects，卻有 {len(stubs):,} 個 stock/<代號>/ 轉址頁"]
    if len(stubs) != int(want):
        problems.append(f"舊網址轉址頁 {len(stubs):,} 個，與 build-info.legacy_redirects {want} 不一致")
    if stubs and not (root / "index.html").is_file():
        problems.append("舊網址轉址頁的目標殼頁 stock/index.html 不存在")
    codes = {str(s["code"]) for s in stocks}
    bad, extra, unknown = [], [], []
    for f in stubs:
        code = f.parent.name
        t = f.read_text("utf-8", errors="replace")
        tgt = f'../?code={code}'
        if not all(x in t for x in LEGACY_NEED) or f'url={tgt}"' not in t or f'<a href="{tgt}"' not in t:
            bad.append(code)
        if code not in codes:
            unknown.append(code)
        if any(p.name != "index.html" for p in f.parent.iterdir()):
            extra.append(code)
    if bad:
        problems.append(f"舊網址轉址頁有 {len(bad):,} 個內容不完整（缺 noindex／refresh／canonical／連結；例：{', '.join(bad[:5])}）")
    if unknown:
        problems.append(f"舊網址轉址頁有 {len(unknown):,} 個代號不在搜尋清單（例：{', '.join(unknown[:5])}）")
    if extra:
        problems.append(f"舊網址轉址目錄有其他檔案（例：{', '.join(extra[:5])}）")
    return problems


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
        # 全市場模式：未收錄個股頁的股票標 page=false（殼頁顯示說明，不是 404），不要求有 JSON，但必須列在 manifest.nopage
        no_page = [s["code"] for s in stocks if s.get("page", True) is not False
                   and not (out / "data" / "stock" / f"{s['code']}.json.gz").is_file()]
        try:
            _np = json.loads((out / "manifest.json").read_text("utf-8")).get("nopage") or {}
        except Exception:
            _np = {}
        unlisted = [s["code"] for s in stocks if s.get("page", True) is False and s["code"] not in _np]
        if unlisted:
            problems.append(f"搜尋清單有 {len(unlisted):,} 檔標未收錄個股頁、但 manifest.nopage 沒列（例：{', '.join(unlisted[:5])}）")
    else:
        no_page = [s["code"] for s in stocks if not (out / "stock" / s["code"] / "index.html").is_file()]
    if no_page:
        problems.append(f"搜尋清單 api/stocks.json 有 {len(no_page):,} 檔沒有個股頁（例：{', '.join(no_page[:5])}）")
    outside = [s["code"] for s in stocks if universe and s["code"] not in universe]
    if outside:
        problems.append(f"搜尋清單有 {len(outside):,} 檔不在追蹤範圍（例：{', '.join(outside[:5])}）")
    wl = out / "api" / "watchlist.json"
    if not (out / "watchlist" / "index.html").is_file():
        problems.append("缺少 watchlist/index.html（自選清單頁）")
    if not (out / "scorecard" / "index.html").is_file():
        problems.append("缺少 scorecard/index.html（模式成績單頁）")
    if not (out / "changes" / "index.html").is_file():
        problems.append("缺少 changes/index.html（每日模式異動頁）")
    snap = out / "data" / "mode-snapshot.json"
    if not snap.is_file():
        problems.append("缺少 data/mode-snapshot.json（每日模式異動快照）")
    else:
        try:
            payload = __import__("json").loads(snap.read_text("utf-8"))
            if not isinstance(payload, dict) or not payload.get("as_of") or not isinstance(payload.get("passes"), dict):
                problems.append("data/mode-snapshot.json 缺少 as_of 或 passes")
        except Exception as e:
            problems.append(f"讀不到 data/mode-snapshot.json：{e}")
    if not wl.is_file():
        problems.append("缺少 api/watchlist.json（自選清單資料）")
    else:
        try:
            payload = __import__("json").loads(wl.read_text("utf-8"))
            rows = payload.get("stocks") or {}
            if not isinstance(rows, dict):
                problems.append("api/watchlist.json 的 stocks 不是物件")
                rows = {}
            outside = [c for c in rows if universe and c not in universe]
            if outside:
                problems.append(f"自選資料有 {len(outside):,} 檔不在追蹤範圍（例：{', '.join(outside[:5])}）")
            if v2:
                bad = [c for c, r in rows.items() if r.get("has_page") and not (out / "data" / "stock" / f"{c}.json.gz").is_file()]
            else:
                bad = [c for c, r in rows.items() if r.get("has_page") and not (out / "stock" / c / "index.html").is_file()]
            if bad:
                problems.append(f"自選資料有 {len(bad):,} 檔標 has_page 但沒有個股頁（例：{', '.join(bad[:5])}）")
            linked = [c for c, r in rows.items() if r.get("href") and not r.get("has_page")]
            if linked:
                problems.append(f"自選資料有 {len(linked):,} 檔未收錄個股頁卻仍有 href（例：{', '.join(linked[:5])}）")
        except Exception as e:
            problems.append(f"讀不到 api/watchlist.json：{e}")
    problems += check_extra_pages(out, info)
    if v2:
        problems += check_legacy_redirects(out, info, stocks)
    try:
        man = json.loads((out / "manifest.json").read_text("utf-8"))
        if v2:
            miss = [c for c in (man.get("stocks") or {}) if not (out / "data" / "stock" / f"{c}.json.gz").is_file()]
            both = [c for c in (man.get("nopage") or {}) if c in (man.get("stocks") or {})]
            if both:
                problems.append(f"manifest 有 {len(both):,} 檔同時列在 stocks 與 nopage（例：{', '.join(both[:5])}）")
            outside_np = [c for c in (man.get("nopage") or {}) if universe and c not in universe]
            if outside_np:
                problems.append(f"manifest.nopage 有 {len(outside_np):,} 檔不在追蹤範圍（例：{', '.join(outside_np[:5])}）")
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
