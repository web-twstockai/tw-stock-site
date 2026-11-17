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

import gzip
import html as _html
import json
import multiprocessing as mp
import os
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
class Linker:
    def __init__(self, stocks: set, months: set, src_ok: dict):
        self.stocks, self.months, self.src_ok = stocks, months, src_ok

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
            if m.group(1) not in self.stocks or any(k not in STOCK_Q for k, _ in pairs):
                return None
            pairs = [(k, v) for k, v in pairs if not (k == "price" and v == "adj")]
            return "stock/?" + urlencode([("code", m.group(1))] + sorted(pairs))
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
            extra = ' data-v2-csv="1"' if u.path.endswith(".csv") else ""
            return f'{sp}{attr}="{_html.escape(prefix + t + frag)}"{extra}'
        out = ATTR_RE.sub(sub, body)
        return re.sub(r'( data-v2-csv="1")\s+download\b', r"\1", out)

    def resolver(self):
        return lambda u: self.target(u)


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


def stock_job(arg):
    code, assigned = arg
    t0 = time.time()
    M, src = G["main"], G["src"]
    c = M.stock_context(src, code, {})
    page = render("stock.html", nav="", **c)
    if 'data-state="test"' in page:
        raise ExportError(f"/stock/{code} 出現測試資料標記，中止匯出")
    title, main = split_main(page)
    parts = split_stock(main)
    data = {"code": code, "t": title, "pre": parts["pre"], "r": parts["r"],
            "q": {"": parts["q"]}, "f": {"": parts["f"]}, "e": {"": parts["e"]}}
    key0 = f"/stock/{code}"
    frags = [main]
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
        frags.append(sec)
        todo.extend(links_in(key0 + "?" + query, sec))
    # 來源明細：這檔連到的全部（已由主行程渲染的跳過）
    known = dict(G["src_known"])
    items: dict = {}
    source_bfs(src_keys, known, items)
    linker = Linker(G["stocks"], G["months"], known)
    pre = "../"
    for name in ("pre", "r"):
        data[name] = linker.rewrite(data[name], key0, pre)
    for g in ("q", "f", "e"):
        data[g] = {sk: linker.rewrite(h, key0 + ("?" + urlencode(parse_qsl(sk)) if sk else ""), pre)
                   for sk, h in data[g].items()}
    size = gz_json(G["out"] / "data" / "stock" / f"{code}.json.gz", data)
    out_items = {qk: (t, linker.rewrite(m, k, pre), k) for qk, (t, m, k) in items.items()}
    new_known = {k: v for k, v in known.items() if k not in G["src_known"]}
    return code, {"variants": sum(len(data[g]) for g in ("q", "f", "e")), "sources": len(items),
                  "bytes": size, "sec": round(time.time() - t0, 3)}, out_items, new_known


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
    return A.ScreenResult(as_of=base.as_of, params=ap.to_dict(), params_hash=A.params_hash(ap),
                          analysis_version=base.analysis_version, summary=s, conditions=c,
                          computed_at=base.computed_at)


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


def rev_job(month):
    return month, build_revenue_month(month)


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


# ---------------- 主流程 ----------------
def export_v2(out: Path, db: Path, base: str = "/tw-stock-site/", log=print, symbols=None, workers: int | None = None) -> dict:
    t0 = time.time()
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

        # 2) 選股（主行程：analysis 全開跑一次）
        t1 = time.time()
        scr = build_screener(client, log)
        timing["screener"] = round(time.time() - t1, 1)

        n_workers = workers or max(1, min(6, (os.cpu_count() or 2) - 1))
        ctx = mp.get_context("fork")
        # 3) 月營收（工作行程，逐月）
        t1 = time.time()
        G["src_known"] = {}
        with ctx.Pool(n_workers) as pool:
            revs = dict(pool.map(rev_job, months, chunksize=1))
        timing["revenue"] = round(time.time() - t1, 1)

        # 4) 全站頁、選股、營收連到的來源明細：有 symbol（且為匯出股票）的交給該股工作行程，其餘主行程渲染
        t1 = time.time()
        glinks = []
        for key in ("/", "/status"):
            glinks += links_in(key, raw[key].text)
        for m in scr["masks"].values():
            for part in ("a", "form", "mid", "z", "src", "empty"):
                if m.get(part):
                    glinks += links_in("/screener", m[part])
            for tr in m.get("rows") or []:
                glinks += links_in("/screener", tr)
        for mo, e in revs.items():
            for part in ("a", "form", "gap", "cnt", "mid", "have_static", "z", "b"):
                if e.get(part):
                    glinks += links_in("/revenue", e[part])
            for tr in e.get("rows") or []:
                glinks += links_in("/revenue", tr)
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
        G["src_known"] = dict(known)
        timing["global_sources"] = round(time.time() - t1, 1)

        # 5) 個股（工作行程）
        t1 = time.time()
        stats = {}
        items = {qk: (t, m, k, "g") for qk, (t, m, k) in gitems.items()}
        jobs = [(c, sorted(assigned[c])) for c in sorted(universe)]
        with ctx.Pool(n_workers) as pool:
            for code, st, its, kn in pool.imap_unordered(stock_job, jobs, chunksize=4):
                stats[code] = st
                known.update(kn)
                for qk, (t, m, k) in its.items():
                    items.setdefault(qk, (t, m, k, "w"))
        timing["stocks"] = round(time.time() - t1, 1)

        # 6) 改寫主行程片段的連結並寫檔
        t1 = time.time()
        linker = Linker(universe, set(months), known)
        shards: dict = {}
        for qk, (t, m, k, who) in items.items():
            mm = linker.rewrite(m, k, "../") if who == "g" else m
            shards.setdefault(source_shard(parse_qsl(k.partition("?")[2])), {})[qk] = [t, mm]
        src_bytes = sum(gz_json(stage / "data" / "source" / f"{sh}.json.gz", d) for sh, d in shards.items())
        for m in scr["masks"].values():
            for part in ("a", "form", "mid", "z", "src", "empty", "topen", "tclose"):
                if m.get(part):
                    m[part] = linker.rewrite(m[part], "/screener", "../")
            if m.get("form"):
                m["form"] = V1.SCREENER_FORM_RE.sub(V1._static_screener_form, m["form"])
            if m.get("rows"):
                m["rows"] = [linker.rewrite(x, "/screener", "../") for x in m["rows"]]
        scr_bytes = gz_json(stage / "data" / "screener.json.gz", scr)
        rev_bytes = 0
        for mo, e in revs.items():
            for part in ("a", "form", "gap", "cnt", "mid", "have_static", "z", "b"):
                if e.get(part):
                    e[part] = linker.rewrite(e[part], "/revenue", "../")
            e.pop("have", None)
            if e.get("rows"):
                e["rows"] = [linker.rewrite(x, "/revenue", "../") for x in e["rows"]]
            rev_bytes += gz_json(stage / "data" / "revenue" / f"{mo}.json.gz", e)

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
        stocks = [s for s in json.loads(raw["/api/stocks"].text) if s["code"] in universe]
        (stage / "api").mkdir(parents=True, exist_ok=True)
        (stage / "api" / "stocks.json").write_text(json.dumps(stocks, ensure_ascii=False), "utf-8")
        first = sorted(universe)[0]
        shell_src = {"stock": client.get(f"/stock/{first}").text, "screener": client.get("/screener").text,
                     "revenue": rev_html,
                     "source": client.get(next(iter(items.values()))[2]).text if items else raw["/status"].text}
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
                    "stocks": {s["code"]: f"stock/?code={s['code']}" for s in stocks},
                    "months": months, "default_month": dm.group(1) if dm else None, "g_shards": G_SHARDS,
                    "routes": {}}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
        (stage / "build-info.json").write_text(json.dumps(
            {"format": "v2", "generated_at_taipei": generated, "data_date": meta.get("data_date"),
             "pages": len(stats), "source": "sqlite（data/twstock.db 快照）", "universe": sorted(universe),
             "tracking_universe": sorted(tracking), "universe_label": src.universe.label,
             "trade_days": V1._trade_days_around(src, meta.get("data_date")), "quotes_ready": "18:30"},
            ensure_ascii=False, indent=1), "utf-8")
        timing["write"] = round(time.time() - t1, 1)
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
        result = {"mode": "v2", "stocks": len(stats), "seconds": round(time.time() - t0, 1), "timing": timing,
                  "bytes": size, "files": nfiles, "stock_json_bytes": sum(s["bytes"] for s in stats.values()),
                  "source_bytes": src_bytes, "screener_bytes": scr_bytes, "revenue_bytes": rev_bytes,
                  "source_pages": len(items), "workers": n_workers, "data_date": meta.get("data_date"),
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
