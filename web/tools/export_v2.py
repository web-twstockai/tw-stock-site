"""靜態匯出 v2（新模式；預設仍是 v1 export_static.export）。

用法（在 web/ 下）：
  .venv/bin/python tools/export_static.py --mode v2 [--out ../build/site] [--workers 6]

和 v1 的差別：
- 個股頁：一個共用殼頁 stock/index.html（網址 stock/?code=2330），每檔一份 data/stock/<代號>.json.gz。
  JSON 裡是同一份 stock.html 模板輸出的段落（行情／月營收／財報／事件），切換（還原／原始價、財報期間、事件分類）
  只放「有變化的那一段」。前端（static/js/v2.js）依網址組出整頁，不另寫一份模板。
- 來源明細：殼頁 source/index.html（source/?table=…），內容依 symbol 分檔 data/source/s/<代號>.json.gz，
  沒有 symbol 的分到 data/source/g/<雜湊>.json.gz。
- 選股：每組條件開關的結果由 analysis 在匯出時算好（四項條件全開跑一次 analysis screen()，
  其他開關組合只改「啟用與否」：通過＝已啟用條件全部成立，與 analysis 同一規則；
  比對工具逐組與 analysis 實跑結果比對）。前端只做篩選、排序、分頁與 CSV。
- 月營收：每個月份一份 data/revenue/<月份>.json.gz（已排好的各種排序順序＋每列 HTML＋每列 CSV），
  前端做市場／新高篩選、排序、分頁與 CSV。
- 首頁、資料狀態頁、404、api/stocks.json 與 v1 相同（伺服器模板輸出），只是連結指向 v2 殼頁。
- 匯出前同樣跑 tools/check_export.py 自檢（v2 會另外檢查每檔都有 JSON、殼頁存在）。
"""
from __future__ import annotations

import gc
import gzip
import html as _html
import json
import multiprocessing as mp
import os
import pickle
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_static as V1  # noqa: E402
from export_static import ATTR_RE, ASSETS, ROOT, TOGGLES, TPE, WEB, ExportError, _is_external  # noqa: E402

MAIN_RE = re.compile(r'<main id="main"[^>]*>(.*)</main>', re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
STOCK_Q = ("price", "period", "scope", "basis", "cat")
GROUP_OF = {"price": "q", "period": "f", "scope": "f", "basis": "f", "cat": "e"}
STOCK_SECTIONS = (("q", "quote"), ("r", "revenue"), ("f", "fin"), ("e", "events"))
SCREENER_Q = {"f", "sort", "order", "page", *TOGGLES}
REVENUE_Q = {"month", "market", "high", "sort", "desc", "page"}
G_SHARDS = 16
SCRIPT_RE = re.compile(r'<script src="([^"]+)"( defer)?></script>\n?')

# 匯出期間由 export_v2() 設定；fork 出來的工作行程直接沿用（copy-on-write）
G: dict = {}


# ---------------- 共用小工具 ----------------
def qkey(pairs) -> str:
    """查詢參數的比對鍵（未編碼；依鍵、值排序）。v2.js 的 qkey() 是同一規則。"""
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def fnv(s: str) -> int:
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def source_shard(pairs) -> str:
    sym = dict(pairs).get("symbol")
    return f"s/{sym}" if sym and re.fullmatch(r"[A-Za-z0-9]+", sym) else f"g/{fnv(qkey(pairs)) % G_SHARDS}"


def split_main(page: str) -> tuple[str, str]:
    m = MAIN_RE.search(page)
    t = TITLE_RE.search(page)
    if not m or not t:
        raise ExportError("頁面缺 <main> 或 <title>，模板結構與匯出 v2 預期不符")
    return _html.unescape(t.group(1)), m.group(1)


def split_stock(main: str) -> dict:
    out, pos = {}, []
    for g, sid in STOCK_SECTIONS:
        i = main.find(f'<section class="card" id="{sid}"')
        if i < 0 or (pos and i < pos[-1]):
            raise ExportError(f"個股頁找不到段落 #{sid}（stock.html 結構改了，匯出 v2 需同步）")
        pos.append(i)
    out["pre"] = main[:pos[0]]
    ends = pos[1:] + [len(main)]
    for (g, _), a, b in zip(STOCK_SECTIONS, pos, ends):
        out[g] = main[a:b]
    return out


def links_in(page_key: str, body: str) -> list[str]:
    return V1.extract_links(page_key, body)


def gz_json(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6, mtime=0)
    path.write_bytes(data)
    return len(data)


# ---------------- 連結（app 網址 → v2 檔案或殼頁網址，相對於網站根目錄） ----------------
class Target(str):
    """Linker.target() 的結果；attrs＝要加在連結上的屬性（例如沒產生個股頁的標記）。"""
    attrs = ""


NOPAGE_ATTRS = ' data-nopage="1"'   # static.css 會在連結後加「（未收錄個股頁）」；點進去由 v2.js 顯示說明（不是 404）


class Linker:
    def __init__(self, stocks: set, months: set, src_ok: dict, nopage: set | frozenset = frozenset()):
        """stocks＝有個股頁的股票；nopage＝在追蹤範圍內、但這版沒產生個股頁的股票（連到殼頁並標記，不是死連結）。"""
        self.stocks, self.months, self.src_ok, self.nopage = stocks, months, src_ok, nopage
        self.extra = G.get("extra") or {}   # 篩選中心／產業頁：網址鍵 → 檔案（主行程先算好，工作行程 fork 時帶著）

    def target(self, u) -> str | None:
        path = u.path
        pairs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False) if v != ""]
        if path == "/":
            return "" if not pairs else None
        if path == "/status":
            return "status/" if not pairs else None
        if path == "/api/stocks":
            return "api/stocks.json" if not pairs else None
        if path.startswith("/static/"):
            return path[1:]
        m = re.fullmatch(r"/stock/(\w+)", path)
        if m:
            if m.group(1) in self.nopage:
                t = Target("stock/?" + urlencode([("code", m.group(1))]))
                t.attrs = NOPAGE_ATTRS
                return t
            if m.group(1) not in self.stocks or any(k not in STOCK_Q for k, _ in pairs):
                return None
            pairs = [(k, v) for k, v in pairs if not (k == "price" and v == "adj")]
            return "stock/?" + urlencode([("code", m.group(1))] + sorted(pairs))
        if path == "/hub" or path == "/industry" or path.startswith("/industry/"):
            f = self.extra.get(V1.canon(path, u.query))
            if f is None:
                return None   # 其他模式、排序連結：拿掉連結（不可點），不是死連結
            return f[: -len("index.html")] if f.endswith("index.html") else f
        if path == "/source":
            return ("source/?" + urlencode(sorted(pairs))) if self.src_ok.get(qkey(pairs)) else None
        if path in ("/screener", "/screener.csv"):
            q = dict(pairs)
            if not set(q) <= SCREENER_Q or any(q[t] != "1" for t in TOGGLES if t in q) or q.get("f", "1") != "1":
                return None
            if q and "f" not in q and set(q) - {"sort", "order", "page"}:
                return None
            tail = ("&" if pairs else "?") + "csv=1" if path.endswith(".csv") else ""
            return "screener/" + (("?" + urlencode(sorted(pairs))) if pairs else "") + tail
        if path in ("/revenue", "/revenue.csv"):
            q = dict(pairs)
            if not set(q) <= REVENUE_Q or ("month" in q and q["month"] not in self.months):
                return None
            tail = ("&" if pairs else "?") + "csv=1" if path.endswith(".csv") else ""
            return "revenue/" + (("?" + urlencode(sorted(pairs))) if pairs else "") + tail
        return None

    def rewrite(self, body: str, page_key: str, prefix: str) -> str:
        """片段內的站內連結改成 v2 網址（prefix＝片段所在頁到網站根目錄）；沒匯出的組合拿掉連結。"""
        page_url = "http://x" + page_key

        def sub(m):
            sp, attr, raw = m.groups()
            v = _html.unescape(raw)
            if _is_external(v):
                return m.group(0)
            u = urlsplit(urljoin(page_url, v))
            if attr == "action":
                shell = {"/screener": "screener/", "/revenue": "revenue/"}.get(u.path)
                if u.path == "/search":
                    return f'{sp}action="#" data-static-path="/search"'
                return f'{sp}action="{_html.escape(prefix + (shell or ""))}" data-v2-form="{u.path}"'
            t = self.target(u)
            if t is None:
                return f'{sp}data-static-off="1" title="公開靜態版未提供此頁"'
            frag = ("#" + u.fragment) if u.fragment else ""
            extra = (' data-v2-csv="1"' if u.path.endswith(".csv") else "") + getattr(t, "attrs", "")
            return f'{sp}{attr}="{_html.escape(prefix + t + frag)}"{extra}'
        out = ATTR_RE.sub(sub, body)
        return re.sub(r'( data-v2-csv="1")\s+download\b', r"\1", out)

    def resolver(self):
        return lambda u: self.target(u)


# ---------------- 篩選中心／產業頁 ----------------
def render_extra_pages(client, log=print) -> dict:
    """/hub（預設＋M1～M4、M6、M7、M2 依成交金額）、/industry 與產業總覽連到的每個產業頁（預設排序）。
    回傳 {網址鍵: 原始 HTML}；任何一頁非 200 或出現測試資料標記就中止。"""
    keys = list(V1.HUB_PAGES) + ["/industry"]
    out: dict = {}
    i = 0
    while i < len(keys):
        key = keys[i]
        i += 1
        r = client.get(key)
        if r.status_code != 200:
            raise ExportError(f"{key} 回 {r.status_code}")
        body = r.text
        if 'data-state="test"' in body or "（測試資料）</title>" in body:
            raise ExportError(f"{key} 出現測試資料標記，中止匯出")
        out[key] = body
        if key == "/industry":
            keys += [k for k in dict.fromkeys(links_in(key, body))
                     if k.startswith("/industry/") and V1.is_extra_page(k) and k not in keys]
    log(f"篩選中心／產業頁：{len(out)} 頁（產業 {sum(k.startswith('/industry/') for k in out)} 個）")
    return out


# ---------------- 模板呈現（直接用 app 的 Jinja 環境；與路由輸出相同） ----------------
def render(tpl: str, **c) -> str:
    return G["templates"].get_template(tpl).render(**G["base"], **c)


def render_source(key: str):
    """回傳 (status, title, main, links)；非 200（例如查無資料表）回 (status, None, None, [])。"""
    from fastapi import HTTPException
    M = G["main"]
    pairs = parse_qsl(key.partition("?")[2])
    try:
        c, status = M.source_context(G["src"], dict(pairs))
    except HTTPException as e:
        return e.status_code, None, None, []
    if status != 200:
        return status, None, None, []
    page = render("source.html", **c)
    if 'data-state="test"' in page:
        raise ExportError(f"{key} 出現測試資料標記，中止匯出")
    title, main = split_main(page)
    return 200, title, main, links_in(key, main)


def source_bfs(keys, known: dict, items: dict, assign_to=None) -> None:
    """渲染 keys 可達的來源明細（known：鍵 → 是否 200；items：鍵 → (title, main_raw, key)）。
    assign_to(key) 回傳 True 的鍵不在這裡渲染（交給對應個股的工作行程）。"""
    todo = deque(k for k in keys)
    while todo:
        key = todo.popleft()
        qk = qkey(parse_qsl(key.partition("?")[2]))
        if qk in known or (assign_to and assign_to(key)):
            continue
        st, title, main, ls = render_source(key)
        known[qk] = st == 200
        if st == 200:
            items[qk] = (title, main, key)
            todo.extend(x for x in ls if x.startswith("/source?"))


# ---------------- 個股（工作行程） ----------------
def _variant(code: str, base_c: dict, g: str, pairs) -> str:
    M, src = G["main"], G["src"]
    qs = dict(pairs)
    c2 = dict(base_c)
    if g == "q":
        cq = M.stock_context(src, code, qs, parts=("screen",))
        c2.update({k: v for k, v in cq.items() if k not in ("period", "scope", "basis", "cat")})
    elif g == "f":
        period, scope, basis, _cat, _pm = M.stock_params(qs)
        b, fin = M.stock_fin(src, code, period, scope, basis, "basis" in qs)
        c2.update(period=period, scope=scope, basis=b, fin=fin)
    else:
        cat = M.stock_params(qs)[3]
        c2.update(cat=cat, events=src.events(code, 30, cat))
    _t, main = split_main(render("stock.html", nav="", **c2))
    return split_stock(main)[g]


def _write_source_shard(path: Path, entries: dict) -> int:
    """寫一份來源明細分檔；檔案已存在（別的行程先寫了同一檔）就合併，已有的鍵不覆蓋。"""
    if path.exists():
        old = json.loads(gzip.decompress(path.read_bytes()))
        for k, v in entries.items():
            old.setdefault(k, v)
        entries = old
    return gz_json(path, entries)


def _split_own(code: str, items: dict) -> tuple[dict, dict]:
    """工作行程渲染的來源明細 → (本股分檔 s/<代號> 的, 其他的)。本股的直接由工作行程寫檔，不傳回主行程（省主行程記憶體）。"""
    own, other = {}, {}
    for qk, v in items.items():
        (own if source_shard(parse_qsl(v[2].partition("?")[2])) == f"s/{code}" else other)[qk] = v
    return own, other


def stock_job(arg):
    """一檔：有個股頁（has_page）→ 個股 JSON＋它連到的來源明細；沒有個股頁 → 只渲染指派給它的來源明細
    （例如選股表、首頁連到的行情列）。本股的來源明細分檔在這裡直接寫出；回傳只帶其他分檔的項目。"""
    code, assigned, has_page = arg
    t0 = time.time()
    M, src = G["main"], G["src"]
    if not has_page:
        known = dict(G["src_known"])
        items: dict = {}
        source_bfs(list(assigned), known, items)
        linker = Linker(G["stocks"], G["months"], known, G["nopage"])
        out_items = {qk: (t, linker.rewrite(m, k, "../"), k) for qk, (t, m, k) in items.items()}
        own, other = _split_own(code, out_items)
        sb = _write_source_shard(G["out"] / "data" / "source" / f"s/{code}.json.gz",
                                 {qk: [t, m] for qk, (t, m, k) in own.items()}) if own else 0
        new_known = {k: v for k, v in known.items() if k not in G["src_known"]}
        return code, {"variants": 0, "sources": len(items), "bytes": 0, "source_bytes": sb, "page": False,
                      "src_key": next((k for _t, _m, k in out_items.values()), None),
                      "sec": round(time.time() - t0, 3)}, other, new_known
    c = M.stock_context(src, code, {})
    page = render("stock.html", nav="", **c)
    if 'data-state="test"' in page:
        raise ExportError(f"/stock/{code} 出現測試資料標記，中止匯出")
    title, main = split_main(page)
    parts = split_stock(main)
    data = {"code": code, "t": title, "pre": parts["pre"], "r": parts["r"],
            "q": {"": parts["q"]}, "f": {"": parts["f"]}, "e": {"": parts["e"]}}
    key0 = f"/stock/{code}"
    todo = deque(links_in(key0, main))
    seen = set()
    src_keys = list(assigned)
    while todo:
        k = todo.popleft()
        if k in seen:
            continue
        seen.add(k)
        if k.startswith("/source?"):
            src_keys.append(k)
            continue
        path, _, query = k.partition("?")
        if path != key0:
            continue
        pairs = [(a, b) for a, b in parse_qsl(query) if b != "" and not (a == "price" and b == "adj")]
        groups = {GROUP_OF.get(a, "?") for a, _ in pairs}
        if not pairs or len(groups) != 1 or "?" in groups:
            continue
        g = groups.pop()
        sk = qkey(pairs)
        if sk in data[g]:
            continue
        sec = _variant(code, c, g, pairs)
        data[g][sk] = sec
        todo.extend(links_in(key0 + "?" + query, sec))
    # 來源明細：這檔連到的全部（已由主行程渲染的跳過）
    known = dict(G["src_known"])
    items: dict = {}
    source_bfs(src_keys, known, items)
    linker = Linker(G["stocks"], G["months"], known, G["nopage"])
    pre = "../"
    for name in ("pre", "r"):
        data[name] = linker.rewrite(data[name], key0, pre)
    for g in ("q", "f", "e"):
        data[g] = {sk: linker.rewrite(h, key0 + ("?" + urlencode(parse_qsl(sk)) if sk else ""), pre)
                   for sk, h in data[g].items()}
    n_variants = sum(len(data[g]) for g in ("q", "f", "e"))
    size = gz_json(G["out"] / "data" / "stock" / f"{code}.json.gz", data)
    del data, c, page, main, parts
    out_items = {qk: (t, linker.rewrite(m, k, pre), k) for qk, (t, m, k) in items.items()}
    del items
    own, other = _split_own(code, out_items)
    sb = _write_source_shard(G["out"] / "data" / "source" / f"s/{code}.json.gz",
                             {qk: [t, m] for qk, (t, m, k) in own.items()}) if own else 0
    new_known = {k: v for k, v in known.items() if k not in G["src_known"]}
    return code, {"variants": n_variants, "sources": len(out_items),
                  "bytes": size, "source_bytes": sb, "page": True,
                  "src_key": next((k for _t, _m, k in out_items.values()), None),
                  "sec": round(time.time() - t0, 3)}, other, new_known


# ---------------- 選股 ----------------
def _derive(base, ap, A):
    """四項條件全開的 analysis 結果 → 另一組啟用開關的結果（通過＝已啟用條件全部成立；排除與數值不變）。"""
    import pandas as pd
    en = {"C1_TREND": ap.enable_trend, "C2_BREAKOUT": ap.enable_breakout, "C3_VOLUME": ap.enable_volume,
          "C4_TURNOVER": ap.enable_turnover}
    col = {"C1_TREND": "pass_trend", "C2_BREAKOUT": "pass_breakout", "C3_VOLUME": "pass_volume",
           "C4_TURNOVER": "pass_turnover"}
    s = base.summary.copy()
    ev = s["status"] != "EXCLUDED"
    ok = pd.Series(True, index=s.index)
    for cid, cn in col.items():
        if en[cid]:
            ok &= s[cn].map(lambda x: x is not None and not (isinstance(x, float) and x != x) and bool(x))
    s.loc[ev, "status"] = ["PASS" if x else "FAIL" for x in ok[ev]]
    c = base.conditions.copy()
    c["enabled"] = c["condition_id"].map(en)
    c["passed"] = c["passed"].astype(object)
    c["reason"] = c["reason"].astype(object)
    sym_ev = set(s.loc[ev, "symbol"])
    for cid in en:
        if en[cid]:
            continue
        m = (c["condition_id"] == cid) & c["symbol"].isin(sym_ev)
        why = c.loc[m, "reason"].map(lambda r: re.sub(r"^(成立|不成立)：", "", r or ""))
        c.loc[m, "passed"] = None
        c.loc[m, "reason"] = "未啟用（不列入判斷）；參考：" + why
    # 用 dataclasses.replace 沿用 base 的其他欄位：analysis-0.9.x 起 ScreenResult 多了 volume_basis_status、
    # coverage_scope_note、lagging_symbols 等，重新建構會被設回預設值，推導頁就少了「未檢查量額口徑」等說明（v1／v2 不一致）
    import dataclasses
    return dataclasses.replace(base, params=ap.to_dict(), params_hash=A.params_hash(ap), summary=s, conditions=c)


def _derivable(base, p) -> bool:
    """由「四項全開」結果推導是否等於實跑：analysis-0.9.0 的 EXCL_VOLUME_BASIS 只在 C3（窗口與評估日）或 C4（評估日）
    啟用時檢查，關掉 C3 後排除名單可能不同 → 有這個碼時，關 C3 的組合改為實跑 analysis（不推導）。"""
    try:
        has_vb = bool((base.summary["exclusion_code"] == "EXCL_VOLUME_BASIS").any())
    except Exception:
        return False
    return not has_vb or bool(p.enable_volume)


def build_screener(client, log) -> dict:
    """回傳 {"masks": {m: {...}}, "default": m, ...}（片段尚未改寫連結）。"""
    from app import screening, main as M
    A = screening._mod()
    orig = A.screen
    base_params = A.ScreenParams()
    others = [f for f in base_params.__dataclass_fields__ if not f.startswith("enable_")]
    flags = ("enable_trend", "enable_breakout", "enable_volume", "enable_turnover")
    cache: dict = {}
    # 預設（四項全開）結果：首頁已算過，直接沿用同一份 analysis ScreenResult
    cache["base"] = M.run_screen(G["src"], M.ScreenParams()).extra.get("analysis_result")

    def screen(params=None, **kw):
        p = params if params is not None else base_params
        same = all(getattr(p, f) == getattr(base_params, f) for f in others)
        if same and not all(getattr(p, f) for f in flags) and cache.get("base") is not None \
                and _derivable(cache["base"], p):
            return _derive(cache["base"], p, A)
        return orig(params=params, **kw)
    A.screen = screen
    data = {"masks": {}, "rowsets": [], "page_size": M.SCREENER_PAGE_SIZE}
    try:
        mac = G["templates"].env.get_template("_macros.html").make_module(G["base"])
        for mask in ["d"] + list(range(16)):
            q = {} if mask == "d" else {"f": "1", "sort": "turnover", "order": "desc",
                                        **{t: "1" for i, t in enumerate(TOGGLES) if mask >> i & 1}}
            key = V1.canon("/screener", urlencode(q))
            r = client.get(key)
            if r.status_code != 200:
                raise ExportError(f"{key} 回 {r.status_code}")
            _t, main = split_main(r.text)
            req = type("Q", (), {"query_params": q})
            p = M.parse_params(req)
            res = M.run_screen(G["src"], p)
            i_form = main.index('<form class="card params"')
            j_form = main.index("</form>", i_form) + len("</form>")
            i_res = main.index('<section class="card" id="results">')
            j_res = main.index("</section>", i_res) + len("</section>")
            sec = main[i_res:j_res]
            ent = {"a": main[:i_form], "form": main[i_form:j_form], "mid": main[j_form:i_res], "z": main[j_res:],
                   "n": len(res.rows), "U": "None" if res.universe_count is None else res.universe_count, "td": res.trade_date,
                   "src": sec[sec.index('<p class="srcline"'):-len("</section>")]}
            if res.rows:
                full = str(mac.screen_table(res))
                tb = full.index("<tbody>") + len("<tbody>")
                te = full.index("</tbody>")
                ent["topen"], ent["tclose"] = full[:tb], full[te:]
                trs = re.findall(r'<tr id="row-[^"]+">.*?</tr>', full[tb:te], re.S)
                if len(trs) != len(res.rows):
                    raise ExportError(f"選股結果列切分不符（{len(trs)}≠{len(res.rows)}）")
                codes = [x.code for x in res.rows]
                ent["rows"] = trs
                ent["codes"] = codes
                order = {}
                cpos = {cd: i for i, cd in enumerate(codes)}
                for s_ in M.SORT_KEYS:
                    for o in ("desc", "asc"):
                        rr = M.run_screen(G["src"], M.ScreenParams(values=p.values, raw=p.raw, sort=s_, order=o)).rows
                        order[f"{s_}:{o}"] = [cpos[x.code] for x in rr]
                ent["ord"] = order
            else:
                ent["empty"] = str(mac.screen_table(res))
            # CSV（與 /screener.csv 同一份 screener_csv_parts；每列先序列化）
            import csv as _csv
            import io as _io
            head, rows, tail = M.screener_csv_parts(res)

            def line(cells):
                b = _io.StringIO()
                _csv.writer(b, lineterminator="\r\n").writerow(cells)
                return b.getvalue()
            ent["csv"] = {"head": "".join(line(x) for x in head), "rows": [line(x) for x in rows],
                          "tail": "".join(line(x) for x in tail), "name": M.screener_csv_name(res)}
            data["masks"][str(mask)] = ent
        log(f"選股：{len(data['masks'])} 組（d＝預設頁，0～15＝開關組合），全開結果 {data['masks']['15']['n']} 檔")
    finally:
        A.screen = orig
        screening.clear_cache()
    data["sorts"] = {k: lb for k, (lb, _f) in M.SORT_KEYS.items()}
    return data


# ---------------- 月營收 ----------------
def build_revenue_month(month: str) -> dict:
    from app import revenue_page as RP, main as M
    client = G["client"]
    old = RP.PAGE_SIZE
    RP.PAGE_SIZE = 10 ** 9   # 一次呈現全部列，切出每列 HTML（列內容與分頁無關）
    try:
        r = client.get(V1.canon("/revenue", urlencode({"month": month})))
    finally:
        RP.PAGE_SIZE = old
    if r.status_code != 200:
        raise ExportError(f"/revenue?month={month} 回 {r.status_code}")
    _t, main = split_main(r.text)
    v = RP.build(G["src"], month=month)
    ent = {"month": month, "status": v["status"], "have": v["have"], "page_size": old, "labels": dict(RP.SORT_KEYS)}
    i_form = main.index('<form class="rev-filter"')
    j_form = main.index("</form>", i_form) + len("</form>")
    ent["a"], ent["form"] = main[:i_form], main[i_form:j_form]
    if v["status"] == "error":
        ent["b"] = main[j_form:]
        return ent
    i_cnt = main.index('<p class="rev-count"', j_form)
    j_cnt = main.index("</p>", i_cnt) + 4
    i_have = main.index('<section class="card" id="have"', j_cnt)
    j_have = main.index("</section>", i_have) + len("</section>")
    ent.update(gap=main[j_form:i_cnt], cnt=main[i_cnt:j_cnt], mid=main[j_cnt:i_have], have=main[i_have:j_have],
               z=main[j_have:])
    if not v["have"]:
        ent["have_static"] = ent["have"]
    rows = v.get("all_rows") or []
    if rows:
        trs = {m.group(1): m.group(0) for m in re.finditer(r'<tr id="row-([^"]+)"[^>]*>.*?</tr>', ent["have"], re.S)}
        ent["rows"] = [trs[r["symbol"]] for r in rows]
        ent["mk"] = [r["market"] for r in rows]
        ent["hi"] = [r["high_status"] for r in rows]
        sym = [r["symbol"] for r in rows]
        pos = {s: i for i, s in enumerate(sym)}
        order = {}
        for s in RP.SORT_KEYS:
            for d in (False, True):
                vv = RP._select(dict(v), "all", False, s, d, 1)
                order[f"{s}:{int(d)}"] = [pos[x["symbol"]] for x in vv["filtered"]]
        ent["ord"] = order
        import csv as _csv
        import io as _io

        def line(cells):
            b = _io.StringIO()
            _csv.writer(b, lineterminator="\r\n").writerow(cells)
            return b.getvalue()
        ent["csvrows"] = [line(["" if x["csv"][c] is None else x["csv"][c] for c in RP.CSV_COLUMNS]) for x in rows]
    # CSV 表頭（與 /revenue.csv 同一份：依篩選組合變的只有第一行）
    resp = client.get(V1.canon("/revenue.csv", urlencode({"month": month})))
    body = resp.content.decode("utf-8")
    body = body[1:] if body.startswith("\ufeff") else body
    lines = body.split("\r\n")
    hdr_end = lines.index(",".join(RP.CSV_COLUMNS)) + 1
    ent["csvhead"] = "\r\n".join(lines[1:hdr_end]) + "\r\n"
    ent["csvname"] = re.search(r'filename="([^"]+)"', resp.headers.get("content-disposition", "")).group(1)
    ent["universe_text"] = v["universe_text"]
    return ent


REV_LINK_PARTS = ("a", "form", "gap", "cnt", "mid", "have_static", "z", "b")


def rev_job(month):
    """一個月份：結果先寫到暫存檔（主行程最後才逐月讀回改寫連結、寫出），只回傳它連到的站內網址。"""
    e = build_revenue_month(month)
    links = []
    for part in REV_LINK_PARTS:
        if e.get(part):
            links += links_in("/revenue", e[part])
    for tr in e.get("rows") or []:
        links += links_in("/revenue", tr)
    path = G["tmp"] / f"rev-{month}.pkl"
    path.write_bytes(pickle.dumps(e, protocol=pickle.HIGHEST_PROTOCOL))
    return month, str(path), list(dict.fromkeys(links))


# ---------------- 殼頁 ----------------
def make_shell(raw_page: str, kind: str, file: str, linker: Linker, info: dict, base: str) -> str:
    prefix = V1.root_prefix(file)
    scripts = []

    def keep(m):
        s = m.group(1)
        if s.startswith("/static/"):
            scripts.append(s[1:])
            return ""
        return m.group(0)
    body = SCRIPT_RE.sub(keep, raw_page)
    label = {"stock": "個股", "source": "來源明細", "screener": "選股", "revenue": "月營收追蹤"}[kind]
    body = re.sub(r"<title>.*?</title>", lambda _m: f"<title>{label}｜台股盤後研究</title>", body, count=1, flags=re.S)
    root = (f'<div id="v2-root" data-v2="{kind}" data-v2-scripts="{_html.escape(" ".join(scripts))}" aria-live="polite">'
            f'<p class="chart-msg" data-state="loading">載入中…</p>'
            f'<noscript><p class="note">公開靜態版的這一頁需要啟用 JavaScript 才能顯示（資料由瀏覽器讀取本站預先匯出的檔案）。</p></noscript></div>\n')
    body = MAIN_RE.sub(lambda m: m.group(0).replace(m.group(1), root), body, count=1)
    body = V1.rewrite_html("/" + kind, file, body, {}, info, resolver=linker.resolver())
    body = body.replace("</body>", f'<script src="{prefix}static/js/v2.js" defer></script>\n</body>', 1)
    return body


# ---------------- 記憶體控制 ----------------
MAX_TASKS = 40   # 工作行程處理這麼多檔後換新行程（釋放 SQLite 列快取、分析結果快取等累積的記憶體）
REV_WORKERS = int(os.environ.get("TWSITE_EXPORT_REV_WORKERS") or 3)   # 營收工作行程數上限（TWSITE_EXPORT_REV_WORKERS 可改）
AUTO_ALL_PAGES_MAX = 300   # pages="auto"：追蹤範圍不超過這個檔數（例如 sample30）時全部產生個股頁


def _rss_mb() -> int:
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _pre_fork() -> None:
    """fork 工作行程前：回收垃圾並凍結現有物件（gc.freeze），子行程的 GC 不再掃這些物件，
    共用頁面不會因為改寫 GC 標記而被複製（copy-on-write）。"""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)   # 把已釋放的記憶體還給系統（glibc；其他平台略過）
    except Exception:
        pass
    gc.freeze()


PAGE_EXTRA_PROVIDERS: list = []   # 預留接口：callable(src) → 代號集合；產業頁上線後把「產業清單上的股票」掛在這裡


def _hits(base_res) -> set:
    """預設參數下，四項條件中至少一項成立的股票（＝16 組開關裡至少有一組會列入結果；排除的不算）。"""
    if base_res is None:
        return set()
    s = base_res.summary
    cols = [c for c in ("pass_trend", "pass_breakout", "pass_volume", "pass_turnover") if c in s.columns]
    if not cols:
        return set()
    ok = s["status"] != "EXCLUDED"
    anyp = s[cols].map(lambda x: x is not None and not (isinstance(x, float) and x != x) and bool(x)).any(axis=1)
    return {str(x) for x in s.loc[ok & anyp, "symbol"]}


def top_value_symbols(src, n: int = 300) -> set:
    """當日成交金額前 n 名：只算追蹤範圍內的股票，取範圍內最新交易日（daily_prices，is_current=1）的
    amount_twd 由大到小（同額依代號）；沒有成交金額的列不算。唯讀查詢（SqliteSource._q，mode=ro）。"""
    if n <= 0:
        return set()
    q = getattr(src, "_q", None)
    if q is None:
        raise ExportError("top_value 需要 SQLite 資料來源")
    uni = {str(s["code"]) for s in src.universe_stocks()}
    latest = [r for r in q("SELECT symbol, MAX(price_date) AS d FROM daily_prices WHERE is_current=1 GROUP BY symbol")
              if str(r["symbol"]) in uni and r["d"]]
    if not latest:
        return set()
    day = max(r["d"] for r in latest)
    rows = [r for r in q("SELECT symbol, amount_twd FROM daily_prices WHERE is_current=1 AND price_date=? "
                         "AND amount_twd IS NOT NULL", (day,)) if str(r["symbol"]) in uni]
    rows.sort(key=lambda r: (-float(r["amount_twd"]), str(r["symbol"])))
    return {str(r["symbol"]) for r in rows[:n]}


# TWSITE_PAGE_EXTRA 可用的具名提供者（「名稱:參數」）；預設不啟用，例：TWSITE_PAGE_EXTRA=top_value:300
NAMED_PAGE_EXTRA = {"top_value": lambda src, arg: top_value_symbols(src, int(arg or 300))}


# 全市場（追蹤範圍 > AUTO_ALL_PAGES_MAX）且沒設 TWSITE_PAGE_EXTRA 時的預設；TWSITE_PAGE_EXTRA=none 關閉
DEFAULT_FULLMARKET_EXTRA = "top_value:300"


def page_extra_spec(full_market: bool) -> str:
    """實際使用的 TWSITE_PAGE_EXTRA：有設就用（none／off／空字串＝不加）；沒設時全市場用 DEFAULT_FULLMARKET_EXTRA。"""
    v = os.environ.get("TWSITE_PAGE_EXTRA")
    if v is None:
        return DEFAULT_FULLMARKET_EXTRA if full_market else ""
    return "" if v.strip().lower() in ("", "none", "off", "0") else v.strip()


def _extra_symbols(src, page_extra, spec: str | None = None) -> set:
    """另外要產生個股頁的代號：PAGE_EXTRA_PROVIDERS、page_extra，以及 spec（預設＝page_extra_spec(False)，即環境變數
    TWSITE_PAGE_EXTRA）。spec 以逗號分隔多項；每項是具名提供者（NAMED_PAGE_EXTRA，例 top_value:300）或清單檔路徑。"""
    if spec is None:
        spec = page_extra_spec(False)
    out: set = set()
    for prov in list(PAGE_EXTRA_PROVIDERS) + ([page_extra] if page_extra is not None else []):
        v = prov(src) if callable(prov) else prov
        out |= {str(x).strip() for x in (v or []) if str(x).strip()}
    for f in [x.strip() for x in spec.split(",") if x.strip()]:
        name, _, arg = f.partition(":")
        if name in NAMED_PAGE_EXTRA:
            if arg and not arg.isdigit():
                raise ExportError(f"TWSITE_PAGE_EXTRA={f}：{name} 的參數要是正整數")
            out |= NAMED_PAGE_EXTRA[name](src, arg)
            continue
        p = Path(f) if Path(f).is_absolute() else ROOT / f
        if not p.is_file():
            raise ExportError(f"TWSITE_PAGE_EXTRA={f}：不是具名提供者（{'／'.join(NAMED_PAGE_EXTRA)}），也找不到清單檔")
        out |= {ln.split("#", 1)[0].strip() for ln in p.read_text("utf-8").splitlines()} - {""}
    return out


def page_symbols(mode: str, universe: set, tracking: set, base_res, src, page_extra=None) -> tuple[set, str]:
    """要產生個股頁的股票（回傳 (集合, 說明)）。
    - "all"：匯出範圍全部；
    - "hits"：符合任一篩選條件的股票（_hits）＋另外指定的（PAGE_EXTRA_PROVIDERS／page_extra／TWSITE_PAGE_EXTRA）；
    - "auto"（預設）：追蹤範圍 ≤ AUTO_ALL_PAGES_MAX 檔 → all，否則 hits（全市場）。"""
    if mode not in ("auto", "all", "hits"):
        raise ExportError(f"pages 只能是 auto／all／hits，收到 {mode!r}")
    full = len(tracking) > AUTO_ALL_PAGES_MAX   # 全市場：預設另收當日成交金額前 300 名（TWSITE_PAGE_EXTRA=none 關閉）
    if mode == "auto":
        mode = "hits" if full else "all"
    if mode == "all":
        return set(universe), f"匯出範圍全部 {len(universe):,} 檔"
    spec = page_extra_spec(full)
    hits = _hits(base_res) & universe
    extra = _extra_symbols(src, page_extra, spec) & universe
    return hits | extra, (f"符合任一篩選條件 {len(hits):,} 檔＋另外指定 {len(extra - hits):,} 檔"
                          f"（{spec or '無另外指定'}）"
                          f"（預設參數；其餘 {len(universe - hits - extra):,} 檔未收錄個股頁）")


# ---------------- 主流程 ----------------
def export_v2(out: Path, db: Path, base: str = "/tw-stock-site/", log=print, symbols=None, workers: int | None = None,
              pages: str = "auto", page_extra=None) -> dict:
    """pages：個股頁產生範圍（見 page_symbols()）；page_extra：另外要產生個股頁的代號（可迭代或 callable(src) → 代號），
    例如之後的產業頁成分股；也可用環境變數 TWSITE_PAGE_EXTRA 指定清單檔。"""
    t0 = time.time()
    mem: dict = {}
    tmpdir = Path(tempfile.mkdtemp(prefix="twsite-export2-"))
    saved_env = {k: os.environ.get(k) for k in ("TWSITE_SOURCE", "TWSITE_DB", "TWSITE_UNIVERSE", "TWSITE_STATIC_EXPORT")}
    saved_source = None
    stage = None
    timing = {}
    try:
        snap = tmpdir / "twstock.db"
        V1.snapshot_db(db, snap)
        os.environ["TWSITE_SOURCE"] = "sqlite"
        os.environ["TWSITE_DB"] = str(snap)
        os.environ.setdefault("TWSITE_UNIVERSE", "sample30")
        os.environ["TWSITE_STATIC_EXPORT"] = "1"
        sys.path.insert(0, str(WEB))
        from fastapi.testclient import TestClient
        from app import data_source, screening, main as M
        saved_source = (data_source._SOURCE,)
        data_source.set_source(None)
        screening.clear_cache()
        src = data_source.get_source()
        if src.is_test or not isinstance(src, data_source.SqliteSource):
            raise ExportError(f"資料來源不是正式 SQLite（{type(src).__name__}），中止匯出")
        meta = src.meta()
        universe = set(src.universe_symbols() or []) or {s["code"] for s in src.list_stocks()}
        tracking = set(universe)
        if symbols is not None:
            universe = set(symbols) & universe
        client = TestClient(M.app, raise_server_exceptions=True)
        stage = Path(tempfile.mkdtemp(prefix="site2-", dir=str(out.parent) if out.parent.exists() else None))
        G.update(main=M, src=src, templates=M.templates, client=client, out=stage, stocks=universe,
                 base=dict(request=None, meta=meta, is_test=src.is_test, site_sources=src.source_status()))

        # 1) 全站頁（首頁、資料狀態、API、404）原樣呈現
        raw = {}
        for key in ("/", "/status", "/api/stocks"):
            r = client.get(key)
            if r.status_code != 200:
                raise ExportError(f"{key} 回 {r.status_code}")
            raw[key] = r
            if key != "/api/stocks" and ('data-state="test"' in r.text or "（測試資料）</title>" in r.text):
                raise ExportError(f"{key} 出現測試資料標記，中止匯出")
        r404 = client.get("/stock/__not_found__")
        rev_html = client.get("/revenue").text
        months = re.findall(r'<option value="(\d{4}-\d{2})"', rev_html)
        dm = re.search(r'<option value="(\d{4}-\d{2})"[^>]*>[^<]*（預設）', rev_html)
        G["months"] = set(months)
        timing["pages"] = round(time.time() - t0, 1)
        mem["pages"] = _rss_mb()

        # 2) 選股（主行程：analysis 全開跑一次）
        t1 = time.time()
        scr = build_screener(client, log)
        timing["screener"] = round(time.time() - t1, 1)
        mem["screener"] = _rss_mb()

        # 2b) 要產生個股頁的股票（見 page_symbols()）；其餘追蹤範圍內的股票連到殼頁並標「未收錄個股頁」
        base_res = M.run_screen(src, M.ScreenParams()).extra.get("analysis_result")
        page_set, page_why = page_symbols(pages, universe, tracking, base_res, src, page_extra)
        nopage = frozenset(universe - page_set)   # symbols（測試縮小範圍）以外的股票維持「靜態版未提供」
        G.update(stocks=page_set, nopage=nopage)
        log(f"個股頁：{len(page_set):,} 檔（{page_why}）；未收錄個股頁 {len(nopage):,} 檔")

        # 2c) 篩選中心、產業頁（app/hub.py、app/industry.py）：主行程整頁呈現。
        #     hub 各模式的 screen() 由 screening.run_screen 快取，同一組參數整次匯出只算一次（8 個 hub 網址共用）；
        #     產業頁用預設參數結果（就是上面選股已算過的那一組）。在 fork 之前做完，工作行程不會重算。
        t1 = time.time()
        extra_raw = render_extra_pages(client, log)
        G["extra"] = {k: V1.url_to_file(k) for k in extra_raw}
        timing["extra_pages"] = round(time.time() - t1, 1)
        mem["extra_pages"] = _rss_mb()

        n_workers = workers or max(1, min(6, (os.cpu_count() or 2) - 1))
        ctx = mp.get_context("fork")
        # 3) 月營收（工作行程，逐月；結果寫暫存檔，主行程只收連結）
        t1 = time.time()
        G["src_known"] = {}
        G["tmp"] = tmpdir
        _pre_fork()
        # 每個營收工作行程要整月全市場資料（約 300 MB），同時開太多會變成整次匯出的峰值 → 最多 REV_WORKERS 個
        with ctx.Pool(max(1, min(n_workers, REV_WORKERS)), maxtasksperchild=MAX_TASKS) as pool:
            rev_files = {mo: (f, ls) for mo, f, ls in pool.imap(rev_job, months, chunksize=1)}
        gc.unfreeze()
        timing["revenue"] = round(time.time() - t1, 1)
        mem["revenue"] = _rss_mb()

        # 4) 全站頁、選股、營收連到的來源明細：有 symbol（且為匯出股票）的交給該股工作行程，其餘主行程渲染
        t1 = time.time()
        glinks = []
        for key in ("/", "/status"):
            glinks += links_in(key, raw[key].text)
        for key, body in extra_raw.items():
            glinks += links_in(key, body)
        for m in scr["masks"].values():
            for part in ("a", "form", "mid", "z", "src", "empty"):
                if m.get(part):
                    glinks += links_in("/screener", m[part])
            for tr in m.get("rows") or []:
                glinks += links_in("/screener", tr)
        for _f, ls in rev_files.values():
            glinks += ls
        # 選股資料先存暫存檔、釋放（最後才讀回改寫連結），工作行程 fork 時不必帶著它
        scr_file = tmpdir / "screener.pkl"
        scr_file.write_bytes(pickle.dumps(scr, protocol=pickle.HIGHEST_PROTOCOL))
        del scr
        assigned: dict = {c: set() for c in universe}

        def assign(key):
            sym = dict(parse_qsl(key.partition("?")[2])).get("symbol")
            if sym in assigned:
                assigned[sym].add(key)
                return True
            return False
        known: dict = {}
        gitems: dict = {}
        source_bfs([k for k in dict.fromkeys(glinks) if k.startswith("/source?")], known, gitems, assign_to=assign)
        del glinks
        G["src_known"] = dict(known)
        timing["global_sources"] = round(time.time() - t1, 1)
        mem["global_sources"] = _rss_mb()

        # 5) 個股（工作行程）：有個股頁的股票整頁；沒有個股頁但有來源明細指派的，只渲染來源明細。
        #    每檔寫完立刻釋放；本股的來源明細分檔由工作行程直接寫出，主行程只收其他分檔的項目。
        t1 = time.time()
        stats = {}
        items = {qk: (t, m, k, "g") for qk, (t, m, k) in gitems.items()}
        del gitems
        jobs = [(c, sorted(assigned[c]), c in page_set) for c in sorted(universe) if c in page_set or assigned[c]]
        del assigned
        _pre_fork()
        with ctx.Pool(n_workers, maxtasksperchild=MAX_TASKS) as pool:
            for code, st, its, kn in pool.imap_unordered(stock_job, jobs, chunksize=2):
                stats[code] = st
                known.update(kn)
                for qk, (t, m, k) in its.items():
                    items.setdefault(qk, (t, m, k, "w"))
        gc.unfreeze()
        timing["stocks"] = round(time.time() - t1, 1)
        mem["stocks"] = _rss_mb()

        # 6) 改寫主行程片段的連結並寫檔
        t1 = time.time()
        linker = Linker(page_set, set(months), known, nopage)
        shards: dict = {}
        for qk, (t, m, k, who) in items.items():
            mm = linker.rewrite(m, k, "../") if who == "g" else m
            shards.setdefault(source_shard(parse_qsl(k.partition("?")[2])), {})[qk] = [t, mm]
        src_bytes = sum(st.get("source_bytes", 0) for st in stats.values())
        src_bytes += sum(_write_source_shard(stage / "data" / "source" / f"{sh}.json.gz", d) for sh, d in shards.items())
        src_pages = sum(1 for v in known.values() if v)
        n_pages = sum(1 for st in stats.values() if st.get("page"))
        del shards
        scr = pickle.loads(scr_file.read_bytes())
        scr_file.unlink()
        for m in scr["masks"].values():
            for part in ("a", "form", "mid", "z", "src", "empty", "topen", "tclose"):
                if m.get(part):
                    m[part] = linker.rewrite(m[part], "/screener", "../")
            if m.get("form"):
                m["form"] = V1.SCREENER_FORM_RE.sub(V1._static_screener_form, m["form"])
            if m.get("rows"):
                m["rows"] = [linker.rewrite(x, "/screener", "../") for x in m["rows"]]
        scr_bytes = gz_json(stage / "data" / "screener.json.gz", scr)
        del scr
        rev_bytes = 0
        for mo in months:   # 逐月讀回、改寫、寫出、釋放
            f, _ls = rev_files[mo]
            e = pickle.loads(Path(f).read_bytes())
            Path(f).unlink()
            for part in REV_LINK_PARTS:
                if e.get(part):
                    e[part] = linker.rewrite(e[part], "/revenue", "../")
            e.pop("have", None)
            if e.get("rows"):
                e["rows"] = [linker.rewrite(x, "/revenue", "../") for x in e["rows"]]
            rev_bytes += gz_json(stage / "data" / "revenue" / f"{mo}.json.gz", e)
            del e
        mem["write_data"] = _rss_mb()

        # 7) 殼頁、全站頁、靜態檔
        shutil.copytree(WEB / "app" / "static", stage / "static")
        shutil.copy2(ASSETS / "static.js", stage / "static" / "js" / "static.js")
        shutil.copy2(ASSETS / "static.css", stage / "static" / "css" / "static.css")
        shutil.copy2(ASSETS / "v2.js", stage / "static" / "js" / "v2.js")
        generated = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")
        info = dict(data_date=meta.get("data_date"), generated=generated, last_run=meta.get("last_run_finished") or "")
        res = linker.resolver()
        for key, file in (("/", "index.html"), ("/status", "status/index.html")):
            (stage / file).parent.mkdir(parents=True, exist_ok=True)
            (stage / file).write_text(V1.rewrite_html(key, file, raw[key].text, {}, info, resolver=res), "utf-8")
        for key, body in extra_raw.items():
            file = G["extra"][key]
            (stage / file).parent.mkdir(parents=True, exist_ok=True)
            (stage / file).write_text(V1.rewrite_html(key, file, body, {}, info, resolver=res), "utf-8")
        del extra_raw
        stocks = [s for s in json.loads(raw["/api/stocks"].text) if s["code"] in universe]
        for s_ in stocks:   # 沒產生個股頁的股票仍可搜尋，標 page=false（static.js 顯示「未收錄個股頁」）
            if s_["code"] in nopage:
                s_["page"] = False
        (stage / "api").mkdir(parents=True, exist_ok=True)
        (stage / "api" / "stocks.json").write_text(json.dumps(stocks, ensure_ascii=False), "utf-8")
        first = sorted(page_set or universe)[0]
        src_key = next(iter(items.values()))[2] if items else next((st["src_key"] for st in stats.values()
                                                                    if st.get("src_key")), None)
        shell_src = {"stock": client.get(f"/stock/{first}").text, "screener": client.get("/screener").text,
                     "revenue": rev_html,
                     "source": client.get(src_key).text if src_key else raw["/status"].text}
        for kind, page in shell_src.items():
            f = f"{kind}/index.html"
            (stage / kind).mkdir(parents=True, exist_ok=True)
            (stage / f).write_text(make_shell(page, kind, f, linker, info, base), "utf-8")
        (stage / "404.html").write_text(V1.rewrite_html("/404", "404.html", r404.text, {}, info, absolute_base=base,
                                                        resolver=res), "utf-8")
        # 舊網址（v1 的 stock/<代號>/…）轉到 v2 殼頁，外部書籤不失效
        p404 = stage / "404.html"
        p404.write_text(p404.read_text("utf-8").replace("</body>", (
            "<script>(function(){var m=location.pathname.match(/\\/stock\\/([A-Za-z0-9]+)\\/(?:index\\.html|q_[^\\/]*)?$/);"
            f"if(m) location.replace({json.dumps(base)}+'stock/?code='+m[1]+location.hash)}})();</script>\n</body>"), 1), "utf-8")
        (stage / ".nojekyll").write_text("", "utf-8")
        (stage / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSETS / "pages.yml", stage / ".github" / "workflows" / "pages.yml")
        shutil.copy2(ASSETS / "update.yml", stage / ".github" / "workflows" / "update.yml")   # 雲端更新 workflow（deploy_pages.sh 會整個換掉分支內容，所以也要隨匯出放入）
        (stage / "README.md").write_text(
            "# 台股盤後研究（公開靜態版）\n\n網址：https://web-twstockai.github.io/tw-stock-site/\n\n"
            "本分支只放自動產生的靜態網頁，不含資料庫與程式原始碼。資料來自證交所、櫃買中心、公開資訊觀測站，"
            "每交易日 18:00、每日 22:00（台北時間）更新後重新產生。\n\n"
            f"- 資料日期：{meta.get('data_date') or '資料不足'}\n- 產生時間：{generated}（台北時間）\n- 匯出格式：v2（共用殼頁＋資料檔）\n\n"
            "僅供研究參考，不構成投資建議；技術訊號不代表獲利保證；本站不提供下單功能。\n", "utf-8")
        manifest = {"format": "v2", "generated_at": generated, "data_date": meta.get("data_date"),
                    "stocks": {s["code"]: f"stock/?code={s['code']}" for s in stocks if s["code"] not in nopage},
                    "nopage": {s["code"]: s.get("name") or "" for s in stocks if s["code"] in nopage},
                    "months": months, "default_month": dm.group(1) if dm else None, "g_shards": G_SHARDS,
                    "routes": {}}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
        (stage / "build-info.json").write_text(json.dumps(
            {"format": "v2", "generated_at_taipei": generated, "data_date": meta.get("data_date"),
             "pages": n_pages, "nopage": len(nopage), "page_policy": page_why,
             "source": "sqlite（data/twstock.db 快照）", "universe": sorted(universe),
             "tracking_universe": sorted(tracking), "universe_label": src.universe.label,
             "trade_days": V1._trade_days_around(src, meta.get("data_date")), "quotes_ready": "18:30",
             "extra_pages": dict(sorted(G["extra"].items()))},
            ensure_ascii=False, indent=1), "utf-8")
        timing["write"] = round(time.time() - t1, 1)
        mem["end"] = _rss_mb()
        from check_export import check as _check, summary as _summary
        problems = _check(stage)
        if problems:
            raise ExportError("自檢失敗：" + "；".join(problems))
        log(f"自檢通過：{_summary(stage)}")
        if out.exists():
            shutil.rmtree(out)
        stage.rename(out)
        stage = None
        size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        nfiles = sum(1 for p in out.rglob("*") if p.is_file())
        result = {"mode": "v2", "stocks": n_pages, "nopage": len(nopage), "tracking": len(tracking),
                  "seconds": round(time.time() - t0, 1), "timing": timing, "main_rss_mb": mem,
                  "bytes": size, "files": nfiles, "stock_json_bytes": sum(s["bytes"] for s in stats.values()),
                  "source_bytes": src_bytes, "screener_bytes": scr_bytes, "revenue_bytes": rev_bytes,
                  "source_pages": src_pages, "workers": n_workers, "data_date": meta.get("data_date"),
                  "generated": generated}
        log(f"完成：{json.dumps(result, ensure_ascii=False)}")
        return result
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        G.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_source is not None:
            from app import data_source as _ds, screening as _sc
            _ds.set_source(saved_source[0])
            _sc.clear_cache()
