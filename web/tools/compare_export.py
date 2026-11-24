"""比對匯出 v1（現行，每個網址一個 HTML）與匯出 v2（共用殼頁＋資料檔、前端組頁）的內容。

用法（在 web/ 下）：
  .venv/bin/python tools/compare_export.py <v1 目錄> <v2 目錄> [--report out.json]

做法：
- v2 的每一個網址（個股各切換、來源明細、選股 16 組開關×5 種排序×2、月營收各篩選／排序、CSV）都用 node 執行
  tools/static_assets/v2.js（與瀏覽器同一份程式）組出 <main> 內容，再和 v1 同一網址的檔案比對。
- 比對前的正規化：去掉公開靜態版橫幅；連結只比「有沒有連結」（兩版的檔名規則不同）；空白合併。
  另外統計「v1 沒有連結（未匯出的組合）、v2 有」與相反的情況——後者算差異。
- CSV 逐位元組比對。
- 另外檢查 v2 每頁的每個站內連結都指到存在的檔案，或殼頁能呈現的網址。
結束碼：0＝全部一致且沒有壞連結；1＝有差異。
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_static as V1  # noqa: E402

TOOLS = Path(__file__).resolve().parent
BANNER_RE = re.compile(r'<div class="static-banner".*?</div>\s*', re.S)
MAIN_RE = re.compile(r'<main id="main"[^>]*>(.*)</main>', re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
OFF = ' data-static-off="1" title="公開靜態版未提供此頁"'


def norm(h: str) -> str:
    h = BANNER_RE.sub("", h, count=1)
    h = h.replace(OFF, " OFF")
    h = re.sub(r'\s(href|src|action)="[^"]*"', r" \1", h)
    h = re.sub(r'\s(data-static-path|data-v2-form)="[^"]*"', "", h)
    h = h.replace(' data-v2-csv="1"', "").replace(" download>", ">")
    h = re.sub(r"\s+", " ", h).strip()
    return h


def loose(h: str) -> str:
    return re.sub(r">\s+<", "><", h.replace(" OFF", " href"))


def v1_main(path: Path):
    t = path.read_text("utf-8")
    m, ti = MAIN_RE.search(t), TITLE_RE.search(t)
    import html as _h
    return (_h.unescape(ti.group(1)) if ti else ""), (m.group(1) if m else t)


def gz(p: Path):
    return json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))


def first_diff(a: str, b: str, w: int = 140) -> str:
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return f"…v1：{a[max(0, i - 60):i + w]!r}\n   …v2：{b[max(0, i - 60):i + w]!r}"


def run(v1: Path, v2: Path) -> dict:
    man = json.loads((v2 / "manifest.json").read_text("utf-8"))
    reqs, pairs = [], []   # pairs: (id, 類別, 邏輯網址, v1 檔)

    def add(kind, logical, search, csv=False):
        path, _, query = logical.partition("?")
        f = V1.url_to_file(V1.canon(path, query))
        if not (v1 / f).is_file():
            return
        rid = len(reqs)
        reqs.append({"id": rid, "kind": kind, "search": search, "csv": csv, "links": True})
        pairs.append((rid, kind + ("_csv" if csv else ""), logical, f))

    # 個股
    for code in man["stocks"]:
        d = gz(v2 / "data" / "stock" / f"{code}.json.gz")
        for g in ("q", "f", "e"):
            for sk in d[g]:
                qp = parse_qsl(sk)
                add("stock", f"/stock/{code}" + ("?" + urlencode(qp) if qp else ""),
                    "?" + urlencode([("code", code)] + qp))
    # 來源明細
    for p in sorted((v2 / "data" / "source").rglob("*.json.gz")):
        for qk in gz(p):
            qp = [tuple(x.split("=", 1)) for x in qk.split("&")]
            add("source", "/source?" + urlencode(sorted(qp)), "?" + urlencode(sorted(qp)))
    # 選股
    add("screener", "/screener", "")
    for mask in range(16):
        for s in V1.SCREENER_SORTS:
            for o in ("desc", "asc"):
                q = [("f", "1"), ("sort", s), ("order", o)] + [(t, "1") for i, t in enumerate(V1.TOGGLES) if mask >> i & 1]
                add("screener", "/screener?" + urlencode(q), "?" + urlencode(q))
                add("screener", "/screener.csv?" + urlencode(q), "?" + urlencode(q), csv=True)
    add("screener", "/screener.csv", "", csv=True)
    # 月營收
    rd = gz(v2 / "data" / "revenue" / f"{man['default_month']}.json.gz") if man.get("default_month") else {"labels": {}}
    sorts = list(rd.get("labels") or {"symbol": ""})
    add("revenue", "/revenue", "")
    add("revenue", "/revenue.csv", "", csv=True)
    for mo in man["months"]:
        add("revenue", f"/revenue?month={mo}", f"?month={mo}")
        add("revenue", f"/revenue?month={mo}&high=1", f"?month={mo}&high=1")
        for mk in ("all", "TWSE", "TPEx"):
            for hi in ("", "1"):
                for s in sorts:
                    for de in ("", "1"):
                        q = [(k, v) for k, v in (("month", mo), ("market", mk), ("high", hi), ("sort", s), ("desc", de)) if v]
                        add("revenue", "/revenue?" + urlencode(q), "?" + urlencode(q))
                        add("revenue", "/revenue.csv?" + urlencode(q), "?" + urlencode(q), csv=True)
    # v2 全站頁與殼頁的連結
    files = ["index.html", "status/index.html", "stock/index.html", "source/index.html", "screener/index.html",
             "revenue/index.html"]
    # 篩選中心、產業頁：兩版都是伺服器整頁輸出（同檔名），也檢查 v2 版的連結
    served = ["index.html", "status/index.html", "watchlist/index.html", "scorecard/index.html", "changes/index.html"] + sorted(
        p.relative_to(v1).as_posix() for top in ("hub", "industry") for p in (v1 / top).rglob("*.html")
        if (v1 / top).is_dir())
    files += [f for f in served[2:] if (v2 / f).is_file()]
    for f in files:
        reqs.append({"id": len(reqs), "kind": "file", "file": f})

    r = subprocess.run(["node", str(TOOLS / "v2_render.js"), str(v2)], input=json.dumps(reqs), capture_output=True,
                       text=True, check=True)
    res = {x["id"]: x for x in json.loads(r.stdout)}
    report = {"compared": {}, "same": {}, "same_loose": {}, "diffs": [], "missing_v2": [], "bad_links": [],
              "v2_extra_links": 0, "v2_lost_links": 0, "v1_files": {}, "v1_unmatched": {}}
    matched = set()
    for rid, kind, logical, f in pairs:
        matched.add(f)
        x = res[rid]
        report["compared"][kind] = report["compared"].get(kind, 0) + 1
        if x.get("missing"):
            report["missing_v2"].append(logical)
            continue
        if kind.endswith("_csv"):
            same = (v1 / f).read_bytes() == x["text"].encode("utf-8")
            if same:
                report["same"][kind] = report["same"].get(kind, 0) + 1
            else:
                a = (v1 / f).read_bytes().decode("utf-8")
                report["diffs"].append({"url": logical, "kind": kind, "where": first_diff(a, x["text"])})
            continue
        t1, m1 = v1_main(v1 / f)
        a, b = norm(m1), norm(x["html"])
        report["bad_links"] += [f"{logical}：{y}" for y in x.get("bad") or []]
        o1, o2 = a.count(" OFF"), b.count(" OFF")
        report["v2_extra_links"] += max(0, o1 - o2)
        report["v2_lost_links"] += max(0, o2 - o1)
        if a == b and t1 == x["title"]:
            report["same"][kind] = report["same"].get(kind, 0) + 1
        elif loose(a) == loose(b) and t1 == x["title"]:
            report["same_loose"][kind] = report["same_loose"].get(kind, 0) + 1
        else:
            report["diffs"].append({"url": logical, "kind": kind,
                                    "where": ("標題不同：" + t1 + " ≠ " + x["title"]) if t1 != x["title"] else first_diff(loose(a), loose(b))})
    # 首頁、資料狀態頁、篩選中心、產業頁（兩版都是伺服器輸出）
    for f in served:
        matched.add(f)
        kind = {"index.html": "home", "status/index.html": "status"}.get(f) or f.split("/")[0]
        if not (v2 / f).is_file():
            report["compared"][kind] = report["compared"].get(kind, 0) + 1
            report["missing_v2"].append(f)
            continue
        t1, m1 = v1_main(v1 / f)
        t2, m2 = v1_main(v2 / f)
        a, b = norm(m1), norm(m2)
        report["compared"][kind] = report["compared"].get(kind, 0) + 1
        o1, o2 = a.count(" OFF"), b.count(" OFF")
        report["v2_extra_links"] += max(0, o1 - o2)
        report["v2_lost_links"] += max(0, o2 - o1)
        if loose(a) == loose(b):
            report["same"][kind] = report["same"].get(kind, 0) + 1
        else:
            report["diffs"].append({"url": f, "kind": kind, "where": first_diff(loose(a), loose(b))})
    for x in res.values():
        if "file" in next((q for q in reqs if q["id"] == x["id"]), {}):
            report["bad_links"] += [f"{reqs[x['id']]['file']}：{y}" for y in x.get("bad") or []]
    # v1 有、但沒比到的檔
    for p in v1.rglob("*"):
        if p.suffix not in (".html", ".csv") or p.name == "404.html":
            continue
        rel = p.relative_to(v1).as_posix()
        top = rel.split("/")[0]
        report["v1_files"][top] = report["v1_files"].get(top, 0) + 1
        if rel not in matched:
            report["v1_unmatched"].setdefault(top, []).append(rel)
    report["ok"] = not report["diffs"] and not report["missing_v2"] and not report["bad_links"] \
        and not report["v2_lost_links"] and not any(report["v1_unmatched"].values())
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("v1")
    ap.add_argument("v2")
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    rep = run(Path(a.v1), Path(a.v2))
    if a.report:
        Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=1), "utf-8")
    tot = sum(rep["compared"].values())
    same = sum(rep["same"].values()) + sum(rep["same_loose"].values())
    print(f"比對 {tot} 個網址：一致 {same}（其中只差標籤間空白 {sum(rep['same_loose'].values())}）｜差異 {len(rep['diffs'])}｜v2 缺 {len(rep['missing_v2'])}")
    print("  各類：" + "，".join(f"{k} {rep['same'].get(k, 0) + rep['same_loose'].get(k, 0)}/{n}" for k, n in sorted(rep["compared"].items())))
    print(f"  連結：v2 多提供 {rep['v2_extra_links']} 個（v1 未匯出的組合）｜v2 少 {rep['v2_lost_links']} 個｜壞連結 {len(rep['bad_links'])}")
    un = {k: len(v) for k, v in rep["v1_unmatched"].items() if v}
    print(f"  v1 檔案：{rep['v1_files']}｜沒比到：{un or '無'}")
    for d in rep["diffs"][:8]:
        print(f"  ✗ {d['url']}（{d['kind']}）\n   {d['where']}")
    for b in rep["bad_links"][:8]:
        print(f"  ✗ 壞連結 {b}")
    for k, v in list(rep["v1_unmatched"].items())[:4]:
        print(f"  ? v1 沒比到 {k}：{v[:3]}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
