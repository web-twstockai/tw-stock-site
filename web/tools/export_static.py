"""靜態匯出：把正式資料（SQLite）的網站頁面輸出成 GitHub Pages 可用的靜態檔。

用法（在 web/ 下）：
  .venv/bin/python tools/export_static.py [--out ../build/site] [--db ../data/twstock.db] [--base /tw-stock-site/]
  .venv/bin/python tools/export_static.py --mode v2 ...   # 匯出 v2（共用殼頁＋資料檔；見 tools/export_v2.py）。預設仍是 v1

做法：
- 先用 SQLite backup API 把資料庫複製成一份快照（避免匯出途中 pipeline 寫入造成前後不一致），網站唯讀讀快照。
- 以 FastAPI TestClient 在行程內呈現頁面（不開任何連接埠、不碰 8765），模板維持單一來源。
- 從首頁、選股、營收、資料狀態、每檔追蹤範圍股票頁開始爬站內連結，依白名單決定要匯出的網址組合。
- 所有站內連結改寫為相對路徑，因此放在 /tw-stock-site/ 子路徑或任何路徑下都能用；
  沒有匯出的組合一律拿掉連結（不留點了會壞的連結或按鈕）。
- 只匯出正式資料：來源不是 sqlite、或頁面出現「測試資料」橫幅，立即中止並以非 0 結束。
- 匯出完成前跑自檢（tools/check_export.py）：首頁行情檔數不得超過追蹤範圍檔數、搜尋清單每檔都要有頁；
  不符就以非 0 結束，輸出目錄維持上一版（deploy_pages.sh 因此失敗、不推送）。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

WEB = Path(__file__).resolve().parents[1]
ROOT = WEB.parent
ASSETS = Path(__file__).resolve().parent / "static_assets"
TPE = timezone(timedelta(hours=8))

TOGGLES = ("enable_trend", "enable_breakout", "enable_volume", "enable_turnover")
SCREENER_SORTS = ("code", "change_pct", "turnover", "vol_ratio", "close")
STOCK_GROUPS = {"price": ("price",), "fin": ("period", "scope", "basis"), "cat": ("cat",)}


class ExportError(RuntimeError):
    pass


# ---------------- 網址 → 檔名 ----------------
def canon(path: str, query: str) -> str:
    """標準化網址鍵：查詢參數依鍵排序、去掉空值。"""
    q = sorted((k, v) for k, v in parse_qsl(query, keep_blank_values=False) if v != "")
    if path.startswith("/stock/"):  # 個股頁 price=adj 與不帶參數是同一頁（伺服器預設前復權）
        q = [(k, v) for k, v in q if not (k == "price" and v == "adj")]
    return path + ("?" + urlencode(q) if q else "")


def _slug(query: str) -> str:
    s = query.replace("=", "-").replace("&", "_")
    if len(s) <= 90 and re.fullmatch(r"[A-Za-z0-9._\-]+", s):
        return s
    return hashlib.sha1(query.encode()).hexdigest()[:16]


def url_to_file(key: str) -> str:
    path, _, query = key.partition("?")
    if path.endswith(".csv"):
        return "csv/" + path[1:-4] + ("__" + _slug(query) if query else "") + ".csv"
    if path.startswith("/api/"):
        return path[1:] + ("__" + _slug(query) if query else "") + ".json"
    d = path.strip("/")
    name = "index.html" if not query else "q_" + _slug(query) + ".html"
    return (d + "/" if d else "") + name


def rel_link(from_file: str, to_file: str) -> str:
    start = posixpath.dirname(from_file) or "."
    r = posixpath.relpath(to_file, start)
    if r == "index.html":
        return "./"
    if r.endswith("/index.html"):
        return r[: -len("index.html")]
    return r


def root_prefix(from_file: str) -> str:
    depth = from_file.count("/")
    return "../" * depth if depth else "./"


# ---------------- 匯出白名單 ----------------
def make_policy(universe: set[str], default_month: str | None = None):
    def allow(key: str) -> bool:
        path, _, query = key.partition("?")
        q = dict(parse_qsl(query))
        if path in ("/", "/status", "/api/stocks"):
            return not q
        if path in ("/screener", "/screener.csv"):
            if not set(q) <= {"f", "sort", "order", *TOGGLES}:
                return False
            # CSV 只匯出預設排序（成交金額大到小）；其他排序的頁面不放匯出鈕（CSV 可自行在試算表排序）
            if path.endswith(".csv") and (q.get("sort", "turnover") != "turnover" or q.get("order", "desc") != "desc"):
                return False
            return all(q[t] == "1" for t in TOGGLES if t in q) and q.get("f", "1") == "1"
        if path in ("/revenue", "/revenue.csv"):
            if not set(q) <= {"month", "market", "high", "sort", "desc", "page"}:
                return False
            if path.endswith(".csv") and ((q.get("sort", "symbol") != "symbol") or "desc" in q):
                return False
            # 排序變化只匯出「預設月份、全部市場、不篩新高」；其他篩選組合只匯出預設排序（避免組合爆量）
            if ("sort" in q and q["sort"] != "symbol") or "desc" in q:
                return (q.get("market", "all") == "all" and "high" not in q
                        and q.get("month", default_month) == default_month)
            return True
        m = re.fullmatch(r"/stock/(\w+)", path)
        if m:
            if m.group(1) not in universe or not set(q) <= {"price", "period", "scope", "basis", "cat"}:
                return False
            changed = 0
            if q.get("price", "adj") != "adj":
                changed += 1
            if any(k in q for k in STOCK_GROUPS["fin"]):
                changed += 1
            if "cat" in q:
                changed += 1
            return changed <= 1
        if path == "/source":
            return True
        return False
    return allow


ATTR_RE = re.compile(r'(\s)(href|src|action)="([^"]*)"')


def _is_external(v: str) -> bool:
    return v.startswith(("http://", "https://", "data:", "mailto:", "javascript:", "#", "//")) or v == ""


def extract_links(page_key: str, body: str) -> list[str]:
    out = []
    page_url = "http://x" + page_key
    for _, attr, raw in ATTR_RE.findall(body):
        v = html.unescape(raw)
        if _is_external(v):
            continue
        u = urlsplit(urljoin(page_url, v))
        if u.path.startswith("/static/"):
            continue
        out.append(canon(u.path, u.query))
    return out


def snapshot_db(db: Path, dest: Path) -> None:
    if not db.exists():
        raise ExportError(f"找不到資料庫：{db}")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


# ---------------- 主流程 ----------------
def export(out: Path, db: Path, base: str = "/tw-stock-site/", max_pages: int = 6000, log=print,
           symbols: list[str] | None = None, screener_combos: bool = True) -> dict:
    """symbols：只匯出這些個股頁（測試用；None＝追蹤範圍全部）。screener_combos：是否預先計算 16×5×2 組選股結果。"""
    t0 = time.time()
    tmpdir = Path(tempfile.mkdtemp(prefix="twsite-export-"))
    saved_env = {k: os.environ.get(k) for k in ("TWSITE_SOURCE", "TWSITE_DB", "TWSITE_UNIVERSE", "TWSITE_STATIC_EXPORT")}
    saved_source = None
    try:
        snap = tmpdir / "twstock.db"
        snapshot_db(db, snap)
        os.environ["TWSITE_SOURCE"] = "sqlite"
        os.environ["TWSITE_DB"] = str(snap)
        os.environ.setdefault("TWSITE_UNIVERSE", "sample30")
        os.environ["TWSITE_STATIC_EXPORT"] = "1"   # 資料狀態頁顯示「本版」資訊，不讀本機發布紀錄
        sys.path.insert(0, str(WEB))
        from fastapi.testclient import TestClient
        from app import data_source
        from app.main import app
        from app import screening
        saved_source = (data_source._SOURCE,)
        data_source.set_source(None)  # 若先前已建立過來源（例如測試），重新依環境變數建立
        screening.clear_cache()
        src = data_source.get_source()
        if src.is_test or not isinstance(src, data_source.SqliteSource):
            raise ExportError(f"資料來源不是正式 SQLite（{type(src).__name__}），中止匯出")
        meta = src.meta()
        universe = set(src.universe_symbols() or [])
        if not universe:
            universe = {s["code"] for s in src.list_stocks()}
        tracking = set(universe)   # 追蹤範圍全部（自檢的上限）；symbols 只縮小匯出的個股頁
        if symbols is not None:
            universe = set(symbols) & universe
        client = TestClient(app, raise_server_exceptions=True)

        seeds = ["/", "/screener", "/screener.csv", "/revenue", "/revenue.csv", "/status", "/api/stocks"]
        seeds += [f"/stock/{c}" for c in sorted(universe)]
        # 選股表單在靜態版只能開關 4 項條件＋排序：預先算好全部組合（16×5×2），表單送出時查表
        for mask in (range(16) if screener_combos else ()):
            for s in SCREENER_SORTS:
                for o in ("desc", "asc"):
                    q = {"f": "1", "sort": s, "order": o}
                    q.update({t: "1" for i, t in enumerate(TOGGLES) if mask >> i & 1})
                    seeds.append(canon("/screener", urlencode(q)))
                    seeds.append(canon("/screener.csv", urlencode(q)))

        # 營收表單（月份×市場×只看新高，預設排序）：預先產生全部組合，表單送出時查表
        rev_html = client.get("/revenue").text
        months = re.findall(r'<option value="(\d{4}-\d{2})"', rev_html)
        dm = re.search(r'<option value="(\d{4}-\d{2})"[^>]*>[^<]*（預設）', rev_html)
        allow = make_policy(universe, dm.group(1) if dm else None)
        for mo in months:
            for mk in ("all", "TWSE", "TPEx"):
                for hi in ("", "1"):
                    seeds.append(canon("/revenue", urlencode({"month": mo, "market": mk, "high": hi, "sort": "symbol"})))

        raw_dir = tmpdir / "raw"
        pages: dict[str, dict] = {}
        skipped: Counter = Counter()
        seen = set()
        queue = deque()
        for s in seeds:
            k = canon(*urlsplit(s)[2:4])
            if k not in seen:
                seen.add(k)
                queue.append(k)
        while queue:
            key = queue.popleft()
            if not allow(key):
                skipped["not_allowed"] += 1
                continue
            if len(pages) >= max_pages:
                raise ExportError(f"匯出頁數超過上限 {max_pages}，請檢查白名單")
            r = client.get(key, follow_redirects=False)
            if r.status_code != 200:
                skipped[f"http_{r.status_code}"] += 1
                continue
            ctype = r.headers.get("content-type", "")
            f = url_to_file(key)
            dest = raw_dir / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            is_html = ctype.startswith("text/html")
            pages[key] = {"file": f, "html": is_html}
            if is_html:
                body = r.text
                if 'data-state="test"' in body or "（測試資料）</title>" in body:
                    raise ExportError(f"{key} 出現測試資料標記，中止匯出（測試資料不得進入公開版）")
                for link in extract_links(key, body):
                    if link not in seen:
                        seen.add(link)
                        queue.append(link)
        log(f"已呈現 {len(pages)} 個網址（略過：{dict(skipped)}），{time.time() - t0:.1f}s")

        # ---- 輸出 ----
        stage = Path(tempfile.mkdtemp(prefix="site-", dir=str(out.parent) if out.parent.exists() else None))
        shutil.copytree(WEB / "app" / "static", stage / "static")
        shutil.copy2(ASSETS / "static.js", stage / "static" / "js" / "static.js")
        shutil.copy2(ASSETS / "static.css", stage / "static" / "css" / "static.css")
        generated = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")
        form_map = {k: v["file"] for k, v in pages.items()
                    if k.startswith(("/screener?", "/revenue?", "/revenue", "/screener")) and v["html"]}
        stocks = json.loads((raw_dir / "api/stocks.json").read_text("utf-8"))
        # 搜尋清單只列有匯出個股頁的股票（公開站開不了的股票不出現在自動完成／搜尋）
        stocks = [s for s in stocks if canon(f"/stock/{s['code']}", "") in pages]
        (raw_dir / "api/stocks.json").write_text(json.dumps(stocks, ensure_ascii=False), "utf-8")
        manifest = {"generated_at": generated, "data_date": meta.get("data_date"), "routes": form_map,
                    "stocks": {s["code"]: url_to_file(f"/stock/{s['code']}") for s in stocks
                               if canon(f"/stock/{s['code']}", "") in pages}}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
        info = dict(data_date=meta.get("data_date"), generated=generated, last_run=meta.get("last_run_finished") or "")
        for key, pg in pages.items():
            dest = stage / pg["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            data = (raw_dir / pg["file"]).read_bytes()
            if pg["html"]:
                data = rewrite_html(key, pg["file"], data.decode("utf-8"), pages, info).encode("utf-8")
            dest.write_bytes(data)
        # 404 頁（GitHub Pages 對任何不存在路徑回這頁，所以連結用絕對子路徑）
        r404 = client.get("/stock/__not_found__")
        (stage / "404.html").write_text(rewrite_html("/404", "404.html", r404.text, pages, info, absolute_base=base),
                                        "utf-8")
        (stage / ".nojekyll").write_text("", "utf-8")
        (stage / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSETS / "pages.yml", stage / ".github" / "workflows" / "pages.yml")
        shutil.copy2(ASSETS / "update.yml", stage / ".github" / "workflows" / "update.yml")   # 雲端更新 workflow（deploy_pages.sh 會整個換掉分支內容，所以也要隨匯出放入）
        (stage / "README.md").write_text(
            "# 台股盤後研究（公開靜態版）\n\n"
            "網址：https://web-twstockai.github.io/tw-stock-site/\n\n"
            "本分支只放自動產生的靜態網頁，不含資料庫與程式原始碼。資料來自證交所、櫃買中心、公開資訊觀測站，"
            "每交易日 18:00、每日 22:00（台北時間）更新後重新產生。\n\n"
            f"- 資料日期：{meta.get('data_date') or '資料不足'}\n- 產生時間：{generated}（台北時間）\n\n"
            "僅供研究參考，不構成投資建議；技術訊號不代表獲利保證；本站不提供下單功能。\n", "utf-8")
        (stage / "build-info.json").write_text(json.dumps(
            {"generated_at_taipei": generated, "data_date": meta.get("data_date"), "pages": len(pages),
             "source": "sqlite（data/twstock.db 快照）", "universe": sorted(universe),
             "tracking_universe": sorted(tracking), "universe_label": src.universe.label,
             # 前端判斷「本版是否落後」用（static.js；與 freshness.expected_for 同一規則：交易日 18:30 後才算當日）
             "trade_days": _trade_days_around(src, meta.get("data_date")), "quotes_ready": "18:30"}, ensure_ascii=False, indent=1), "utf-8")
        # 自檢（tools/check_export.py）：首頁行情檔數 ≤ 範圍檔數、搜尋清單每檔都有頁；不符就中止，不覆蓋上一版
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from check_export import check as _check, summary as _summary
        problems = _check(stage)
        if problems:
            shutil.rmtree(stage, ignore_errors=True)
            raise ExportError("自檢失敗：" + "；".join(problems))
        log(f"自檢通過：{_summary(stage)}")
        if out.exists():
            shutil.rmtree(out)
        stage.rename(out)
        stats = {"pages": len(pages), "html": sum(1 for p in pages.values() if p["html"]),
                 "by_route": dict(Counter(k.split("?")[0].split("/")[1] or "home" for k in pages)),
                 "skipped": dict(skipped), "seconds": round(time.time() - t0, 1), "data_date": meta.get("data_date"),
                 "generated": generated}
        log(f"完成：{json.dumps(stats, ensure_ascii=False)}")
        return stats
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_source is not None:
            from app import data_source as _ds, screening as _sc
            _ds.set_source(saved_source[0])
            _sc.clear_cache()


def _trade_days_around(src, data_date) -> list[str]:
    """資料日期前 14 天到後 60 天的交易日（trading_calendar）；讀不到回空清單（前端就不判斷落後）。"""
    try:
        days = src._trade_days()
        d0 = datetime.fromisoformat(str(data_date)[:10]).date() if data_date else datetime.now(TPE).date()
    except Exception:
        return []
    lo, hi = (d0 - timedelta(days=14)).isoformat(), (d0 + timedelta(days=60)).isoformat()
    return sorted(d for d in days if lo <= d <= hi)


SCREENER_FORM_RE = re.compile(r'(<form class="card params"[^>]*>)(.*?)(</form>)', re.S)
ADV_RE = re.compile(r'(<details class="advanced".*?</details>)', re.S)


def _static_screener_form(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    head = head.replace("<form ", '<form data-static-form="/screener" ', 1)
    # 數值參數在靜態版無法即時重算 → 固定為預設值（disabled，不送出）；四項條件開關與排序仍可用（已預先計算）
    body = re.sub(r'<input type="text"', '<input type="text" disabled', body)

    def adv(mm):
        s = re.sub(r"<input ", "<input disabled ", mm.group(1))
        return s
    body = ADV_RE.sub(adv, body)
    note = ('<p class="static-form-note" data-testid="static-form-note">公開靜態版：四項條件可開關、可改排序（結果已預先由同一套程式算好）；'
            '均線天數、倍數、金額門檻與進階設定固定為預設值，無法在靜態版重算，數字欄已鎖定。</p>')
    return head + note + body + tail


def rewrite_html(key: str, file: str, body: str, pages: dict, info: dict, absolute_base: str | None = None,
                 resolver=None) -> str:
    """resolver：匯出 v2 用（u → 相對網站根目錄的目標或 None）；v1 不傳，行為不變。"""
    page_url = "http://x" + key
    prefix = absolute_base if absolute_base else root_prefix(file)

    def link_for(v: str) -> str | None:
        u = urlsplit(urljoin(page_url, v))
        frag = ("#" + u.fragment) if u.fragment else ""
        if u.path.startswith("/static/"):
            return prefix + u.path[1:]
        if resolver is not None:
            t = resolver(u)
            return None if t is None else prefix + t + frag
        k = canon(u.path, u.query)
        if k in pages:
            if absolute_base:
                t = pages[k]["file"]
                t = t[: -len("index.html")] if t.endswith("index.html") else t
                return absolute_base + t + frag
            return rel_link(file, pages[k]["file"]) + frag
        return None

    def sub(m: re.Match) -> str:
        sp, attr, raw = m.groups()
        v = html.unescape(raw)
        if _is_external(v):
            return m.group(0)
        if attr == "action":
            u = urlsplit(urljoin(page_url, v))
            if resolver is not None:
                if u.path == "/search":
                    return f'{sp}action="#" data-static-path="/search"'
                shell = {"/screener": "screener/", "/revenue": "revenue/"}.get(u.path, "")
                return f'{sp}action="{html.escape(prefix + shell)}" data-v2-form="{u.path}"'
            return f'{sp}action="{html.escape(prefix + url_to_file(canon(u.path, "")) if u.path != "/search" else "#")}" data-static-path="{u.path}"'
        new = link_for(v)
        if new is None:
            return f'{sp}data-static-off="1" title="公開靜態版未提供此頁"'  # 拿掉 href：不留點了會壞的連結
        return f'{sp}{attr}="{html.escape(new)}"'

    body = ATTR_RE.sub(sub, body)
    if key.startswith("/screener") and not key.endswith(".csv"):
        body = SCREENER_FORM_RE.sub(_static_screener_form, body)
    head_inject = (f'<meta name="site-root" content="{prefix}">\n'
                   f'<link rel="stylesheet" href="{prefix}static/css/static.css">\n')
    body = body.replace("</head>", head_inject + "</head>", 1)
    body = body.replace("<html ", '<html data-static="1" ', 1)
    last = f"｜最近一次更新工作完成 {html.escape(str(info['last_run'])[:16].replace("T", " "))}（台北時間）" if info.get("last_run") else ""
    banner = (f'<div class="static-banner" role="note" data-testid="static-banner"><strong>公開靜態版</strong>'
              f'<span>資料日期 <b>{html.escape(str(info["data_date"] or "資料不足"))}</b>{last}｜本頁產生於 {info["generated"]}（台北時間）。'
              f'每交易日 18:00、每日 22:00 更新後重新產生；資料全部來自證交所、櫃買中心、公開資訊觀測站。</span></div>')
    body = re.sub(r'(<main id="main"[^>]*>)', lambda m: m.group(1) + "\n" + banner, body, count=1)
    body = body.replace("</body>", f'<script src="{prefix}static/js/static.js" defer></script>\n</body>', 1)
    return body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="匯出靜態網站（正式 SQLite 資料）")
    ap.add_argument("--out", default=str(ROOT / "build" / "site"))
    ap.add_argument("--db", default=str(ROOT / "data" / "twstock.db"))
    ap.add_argument("--base", default="/tw-stock-site/")
    ap.add_argument("--max-pages", type=int, default=6000)
    ap.add_argument("--mode", choices=("v1", "v2"), default="v1",
                    help="v1＝現行（每個網址一個 HTML，預設）；v2＝共用殼頁＋資料檔（tools/export_v2.py）")
    ap.add_argument("--workers", type=int, default=None, help="v2 的工作行程數（預設 CPU 數−1，最多 6）")
    a = ap.parse_args(argv)
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if a.mode == "v2":
            from export_v2 import export_v2
            export_v2(out, Path(a.db).resolve(), a.base, workers=a.workers)
            return 0
        export(out, Path(a.db).resolve(), a.base, a.max_pages)
    except ExportError as e:
        print(f"匯出失敗：{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
