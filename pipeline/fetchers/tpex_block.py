"""上櫃鉅額成交：當日逐筆（有代號，只能當日抓）與歷史彙總（以名稱比對）。

- 逐筆端點 block_day_result.php 忽略 date 參數，永遠回「最新交易日」，回應頂層 date=YYYYMMDD（2026-11-17 實測）。
- 歷史彙總 openapi tpex_daily_trading_block：每列 = 某日某證券名稱的鉅額成交（TradingDate 民國、Name、股數、金額），
  **沒有證券代號**；同日同名多筆（逐筆／配對）要加總。本模組以 securities 現行上櫃名稱比對代號：同名多檔者視為歧義，不寫入。
- 口徑：block_shares（股）、block_amount_twd（元）都**包含在** volume_shares／amount_twd 內，不扣除。
- 未涵蓋的日期（逐筆日期不符、也不在歷史彙總涵蓋區間）一律 NULL（未知），不補 0。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from pipeline.db import touch_source
from pipeline.httputil import FetchError, RateLimited, tpex

log = logging.getLogger("pipeline.tpex_block")

BLOCK_TICKS_URL = "https://www.tpex.org.tw/web/stock/block_trade/daily_qutoes/block_day_result.php?l=zh-tw&response=json"
BLOCK_HIST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_daily_trading_block"
SRC_TICKS = "tpex_block_day"
SRC_NAME = "name_match"


def _num(s) -> int:
    t = str(s).replace(",", "").strip()
    try:
        return int(float(t)) if t not in ("", "--", "---") else 0
    except ValueError:
        return 0


def _roc7_to_iso(s: str) -> str | None:
    s = str(s).strip()
    if len(s) != 7 or not s.isdigit():
        return None
    return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"


def parse_block_ticks(data: dict) -> tuple[str | None, dict[str, tuple[int, int]]]:
    """回傳 (資料日 ISO, {代號: (股數, 金額)})。"""
    d = str(data.get("date") or "").strip()
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None
    out: dict[str, list[int]] = {}
    for t in data.get("tables") or []:
        fields = t.get("fields") or []
        if "代號" not in fields or "成交股數" not in fields:
            continue
        ic, iv = fields.index("代號"), fields.index("成交股數")
        ia = next((i for i, f in enumerate(fields) if f.startswith("成交值")), None)
        for r in t.get("data") or []:
            code = str(r[ic]).strip()
            acc = out.setdefault(code, [0, 0])
            acc[0] += _num(r[iv])
            acc[1] += _num(r[ia]) if ia is not None else 0
    return iso, {k: (v[0], v[1]) for k, v in out.items()}


def parse_block_hist(rows: list[dict]) -> tuple[dict[tuple[str, str], tuple[int, int]], str | None, str | None]:
    """回傳 ({(ISO 日期, 名稱): (股數, 金額)}, 涵蓋起, 涵蓋迄)。"""
    agg: dict[tuple[str, str], list[int]] = {}
    lo = hi = None
    for r in rows:
        iso = _roc7_to_iso(r.get("TradingDate", ""))
        if not iso:
            continue
        name = str(r.get("Name", "")).strip()
        acc = agg.setdefault((iso, name), [0, 0])
        acc[0] += _num(r.get("NumberOfSharesTraded"))
        acc[1] += _num(r.get("TradingValue"))
        lo = iso if lo is None or iso < lo else lo
        hi = iso if hi is None or iso > hi else hi
    return {k: (v[0], v[1]) for k, v in agg.items()}, lo, hi


@dataclass
class BlockContext:
    ticks_date: str | None = None
    ticks: dict = field(default_factory=dict)
    hist: dict = field(default_factory=dict)
    cov_start: str | None = None
    cov_end: str | None = None
    code_to_name: dict = field(default_factory=dict)
    name_to_codes: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    rate_limited: bool = False
    requests: int = 0

    def lookup(self, iso: str, symbol: str) -> tuple[int, int, str] | None:
        """(block 股數, 金額, 來源)；無法判定回 None（寫 NULL）。"""
        if self.ticks_date == iso:
            sh, am = self.ticks.get(symbol, (0, 0))
            return sh, am, SRC_TICKS
        if self.cov_start and self.cov_start <= iso <= (self.cov_end or iso):
            name = self.code_to_name.get(symbol)
            if not name or len(self.name_to_codes.get(name, ())) != 1:
                return None   # 無名稱或同名多檔：歧義，不寫
            sh, am = self.hist.get((iso, name), (0, 0))
            return sh, am, SRC_NAME
        return None


_CTX: BlockContext | None = None


def reset_context() -> None:
    global _CTX
    _CTX = None


def _names(conn: sqlite3.Connection) -> tuple[dict, dict]:
    c2n, n2c = {}, {}
    for sym, name in conn.execute("SELECT symbol, name FROM securities WHERE is_current=1 AND market='TPEx'"):
        n = (name or "").strip()
        c2n[sym] = n
        n2c.setdefault(n, []).append(sym)
    return c2n, n2c


def get_context(conn: sqlite3.Connection, *, fetch: bool = True) -> BlockContext:
    """每個行程最多 2 個請求（逐筆 1、歷史彙總 1）。限流或失敗時回傳不完整的 context（對應日期寫 NULL）。"""
    global _CTX
    if _CTX is not None:
        return _CTX
    ctx = BlockContext()
    ctx.code_to_name, ctx.name_to_codes = _names(conn)
    if fetch:
        for url, kind in ((BLOCK_TICKS_URL, "ticks"), (BLOCK_HIST_URL, "hist")):
            if ctx.rate_limited:
                break
            try:
                resp = tpex.request("GET", url, retries=2)
                ctx.requests += 1
                data = resp.json()
                if kind == "ticks":
                    ctx.ticks_date, ctx.ticks = parse_block_ticks(data)
                    touch_source(conn, "tpex_block_ticks", url=url, ok=True, http_status=resp.status_code, data_date=ctx.ticks_date)
                else:
                    ctx.hist, ctx.cov_start, ctx.cov_end = parse_block_hist(data)
                    touch_source(conn, "tpex_block_hist", url=url, ok=True, http_status=resp.status_code, data_date=ctx.cov_end)
            except RateLimited as e:
                ctx.rate_limited = True
                ctx.errors.append(f"{kind}:{e}")
                touch_source(conn, f"tpex_block_{kind}", url=url, ok=False, http_status=e.status, error=str(e))
            except (FetchError, ValueError) as e:
                ctx.errors.append(f"{kind}:{e}")
                touch_source(conn, f"tpex_block_{kind}", url=url, ok=False, error=str(e)[:300])
        log.info("block context ticks_date=%s ticks=%s hist_rows=%s cov=%s~%s errors=%s",
                 ctx.ticks_date, len(ctx.ticks), len(ctx.hist), ctx.cov_start, ctx.cov_end, ctx.errors)
    _CTX = ctx
    return ctx


def attach_block(rows: list[dict], iso: str, ctx: BlockContext) -> int:
    """把鉅額欄位寫入 rows（就地）。回傳有判定結果（非 NULL）的列數。"""
    n = 0
    for r in rows:
        res = ctx.lookup(iso, r["symbol"])
        if res is None:
            r["block_shares"] = r["block_amount_twd"] = r["block_source"] = None
        else:
            r["block_shares"], r["block_amount_twd"], r["block_source"] = res
            n += 1
    return n
