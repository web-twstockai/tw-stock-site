"""籌碼資料（2026-11-24 實測後實作）：三大法人個股買賣超、融資融券餘額（上市＋上櫃，每日）、集保股權分散表（每週）。

來源（實測紀錄：docs/sources.md §19；原始回應：logs/chips_probe/）
- twse_t86     https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json         （股）
- tpex_insti   https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=YYYY/MM/DD&id=&response=json （股）
- twse_margin  https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD&selectType=ALL&response=json  （交易單位）
- tpex_margin  https://www.tpex.org.tw/www/zh-tw/margin/balance?date=YYYY/MM/DD&id=&response=json                 （張）
- tdcc_dist    https://opendata.tdcc.com.tw/getOD.ashx?id=1-5（只有最新一週，整批）；
               https://www.tdcc.com.tw/portal/zh/smWeb/qryStock（單檔、近 51 週；需表單權杖，逐檔逐週 1 個 POST）

規則
- 每個（資料集, 日期）1 個請求；所有籌碼請求之間至少 CHIPS_INTERVAL 秒（預設 6，環境變數 PIPELINE_CHIPS_INTERVAL），
  另沿用 httputil 各主機的 RateClient 節流、RateLimited、主機熔斷。
- 一律 single_shot（不在請求層重試）：擋頁（307／302→/errors／429／403／「安全性考量」／非 JSON）第一次就停止該主機的
  所有請求，並在 backfill_state 記冷卻（RANGE_BLOCK_COOLDOWN_MIN 分鐘，Actions 換機器也有效）；暫時性錯誤留給下次作業。
- 欄位版本：欄位清單與已實測版本不同 → 該日標 fields_changed、不寫入（不猜欄位）；恆等式不符超過 5% 也視為欄位改版。
- 只寫 universe（現行普通股＋回補窗口內下市股，同 market_backfill）內的代號；ETF、權證等不寫。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

from pipeline import config, httputil
from pipeline.dates import TZ, now_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.httputil import FetchError, RateLimited, single_shot

log = logging.getLogger("pipeline.chips")

CHIPS_INTERVAL = float(os.environ.get("PIPELINE_CHIPS_INTERVAL", "6"))
IDENTITY_FAIL_RATIO = 0.05

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json"
TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={y}%2F{m}%2F{d}&id=&response=json"
MI_MARGN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={y}%2F{m}%2F{d}&id=&response=json"
TDCC_OD_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TDCC_QRY_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"

# ---- 官方欄位（2023-11-01 與 2026-11-23 兩端實測完全相同）----
T86_FIELDS = [
    "證券代號", "證券名稱",
    "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]
T86_MAP = [
    None, None,
    "foreign_ex_dealer_buy", "foreign_ex_dealer_sell", "foreign_ex_dealer_net",
    "foreign_dealer_buy", "foreign_dealer_sell", "foreign_dealer_net",
    "trust_buy", "trust_sell", "trust_net",
    "dealer_net",
    "dealer_self_buy", "dealer_self_sell", "dealer_self_net",
    "dealer_hedge_buy", "dealer_hedge_sell", "dealer_hedge_net",
    "total_net",
]
# TPEx：7 組「買進股數／賣出股數／買賣超股數」欄名完全相同，只能依位置讀（以恆等式逐列驗證位置沒跑掉）。
TPEX_INSTI_FIELDS = ["代號", "名稱"] + ["買進股數", "賣出股數", "買賣超股數"] * 7 + ["三大法人買賣超股數合計"]
TPEX_INSTI_MAP = [None, None] + [
    f"{g}_{k}" for g in ("foreign_ex_dealer", "foreign_dealer", "foreign_total", "trust", "dealer_self", "dealer_hedge", "dealer_total")
    for k in ("buy", "sell", "net")
] + ["total_net"]
TPEX_INSTI_MAP = [("dealer_net" if c == "dealer_total_net" else c) for c in TPEX_INSTI_MAP]

MI_MARGN_FIELDS = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
                   "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"]
MI_MARGN_MAP = [None, None, "margin_buy", "margin_sell", "margin_cash_repay", "margin_prev_balance", "margin_balance", "margin_limit",
                "short_buy", "short_sell", "short_stock_repay", "short_prev_balance", "short_balance", "short_limit", "offset_qty", "note_flags"]
TPEX_MARGIN_FIELDS = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率(%)", "資限額",
                      "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率(%)", "券限額", "資券相抵(張)", "備註"]
TPEX_MARGIN_MAP = [None, None, "margin_prev_balance", "margin_buy", "margin_sell", "margin_cash_repay", "margin_balance", "margin_finco",
                   "margin_usage_pct", "margin_limit", "short_prev_balance", "short_sell", "short_buy", "short_stock_repay", "short_balance",
                   "short_finco", "short_usage_pct", "short_limit", "offset_qty", "note_flags"]

INST_NUM = [c for c in dict.fromkeys(TPEX_INSTI_MAP + T86_MAP) if c]
MARGIN_NUM = [c for c in dict.fromkeys(MI_MARGN_MAP + TPEX_MARGIN_MAP) if c and c != "note_flags"]
INST_CONTENT_KEYS = ["market"] + INST_NUM + ["fields_sig", "unit"]
MARGIN_CONTENT_KEYS = ["market"] + MARGIN_NUM + ["note_flags", "limit_basis", "fields_sig", "unit"]
LEVELS = range(1, 18)
TDCC_NUM = [f"{p}_{i}" for p in ("holders", "shares", "pct") for i in LEVELS]
TDCC_CONTENT_KEYS = ["level_scheme"] + TDCC_NUM + ["unit"]

NOTE_INST = "來源無公告時間；T86 含一般、零股、盤後定價、鉅額，不含拍賣、標購（官方說明）"
NOTE_INST_TPEX = "來源無公告時間；含普通股、鉅額、零股、綜合帳戶之投信買賣成交量（官方副標）"
NOTE_MARGIN = "來源無公告時間；官方說明：當日晚間公布，『前日餘額』為最終正確數、『今日餘額』為輔助參考"
NOTE_MARGIN_TPEX = "來源無公告時間；限額為 TPEx『資限額／券限額』，與 TWSE『次一營業日限額』口徑不同"
NOTE_TDCC = "來源無公告時間；集保資料日為每週最後營業日"

# 各資料集口徑說明（記在 chips_fetch_status.notes，不逐列重複存；各列 announced_at＝NULL 表示來源無公告時間）
DATASET_NOTES = {"twse_t86": NOTE_INST, "tpex_insti": NOTE_INST_TPEX, "twse_margin": NOTE_MARGIN, "tpex_margin": NOTE_MARGIN_TPEX}

# dataset → (market, source_table, table, fields_sig, unit)
DATASETS = {
    "twse_t86": ("TWSE", "T86", "institutional_trades", "T86_19", "股"),
    "tpex_insti": ("TPEx", "insti_dailyTrade", "institutional_trades", "TPEX_INSTI_24", "股"),
    "twse_margin": ("TWSE", "MI_MARGN", "margin_balances", "MI_MARGN_16", "交易單位"),
    "tpex_margin": ("TPEx", "margin_balance", "margin_balances", "TPEX_MARGIN_20", "張"),
}
DAILY_DATASETS = list(DATASETS)
TDCC = "tdcc_dist"
STATE_JOB = "chips"


def url_for(dataset: str, iso: str) -> str:
    y, m, d = iso.split("-")
    if dataset == "twse_t86":
        return T86_URL.format(ymd=f"{y}{m}{d}")
    if dataset == "twse_margin":
        return MI_MARGN_URL.format(ymd=f"{y}{m}{d}")
    if dataset == "tpex_insti":
        return TPEX_INSTI_URL.format(y=y, m=m, d=d)
    if dataset == "tpex_margin":
        return TPEX_MARGIN_URL.format(y=y, m=m, d=d)
    raise KeyError(dataset)


def client_for(dataset: str) -> httputil.RateClient:
    return httputil.tpex if dataset.startswith("tpex") else httputil.twse


def host_of(url: str) -> str:
    return url.split("/")[2]


def _int(s) -> int | None:
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace(" ", "")
    if t in ("", "-", "--"):
        return None
    try:
        return int(t)
    except ValueError:
        try:
            f = float(t)
        except ValueError:
            return None
        return int(f) if f == int(f) else None


def _float(s) -> float | None:
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------- 主機守門（擋頁即停＋跨作業冷卻）
class HostGuard:
    """同一作業內：主機被擋一次就停止該主機所有請求；暫時性錯誤連續 REDIRECT_STREAK_STOP 次也停。
    跨作業：被擋時在 backfill_state(job='chips', key='cooldown:<host>') 記到期時間，期內不送。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.stopped: dict[str, str] = {}
        self.streak: dict[str, int] = {}

    def allowed(self, host: str) -> bool:
        if host in self.stopped:
            return False
        r = self.conn.execute("SELECT detail FROM backfill_state WHERE job=? AND key=?", (STATE_JOB, f"cooldown:{host}")).fetchone()
        if r and r[0]:
            try:
                until = datetime.fromisoformat(r[0])
            except ValueError:
                until = None
            if until and datetime.now(TZ) < until:
                self.stopped[host] = f"cooldown_until:{r[0]}"
                return False
        return True

    def blocked(self, host: str, why: str) -> None:
        self.stopped[host] = why
        until = (datetime.now(TZ) + timedelta(minutes=config.RANGE_BLOCK_COOLDOWN_MIN)).isoformat(timespec="seconds")
        from pipeline.runstats import state_set
        state_set(self.conn, STATE_JOB, f"cooldown:{host}", "blocked", until, bump=True)
        self.conn.commit()
        log.error("chips: 主機 %s 被擋（%s）→ 本次停止該主機所有請求，冷卻到 %s", host, why, until)

    def error(self, host: str, why: str) -> None:
        self.streak[host] = self.streak.get(host, 0) + 1
        if self.streak[host] >= config.REDIRECT_STREAK_STOP:
            self.stopped[host] = f"consecutive_errors:{self.streak[host]}:{why}"

    def ok(self, host: str) -> None:
        self.streak[host] = 0


_last_any = 0.0


def gated_request(client: httputil.RateClient, method: str, url: str, **kw):
    """所有籌碼請求共用的最小間隔（CHIPS_INTERVAL 秒），再交給 httputil（主機節流、熔斷、RateLimited）；single_shot。"""
    global _last_any
    wait = _last_any + CHIPS_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        with single_shot():
            return client.request(method, url, **kw)
    finally:
        _last_any = time.monotonic()


def _looks_blocked(text_head: str) -> bool:
    return any(k in text_head for k in ("安全性考量", "無法呈現", "FOR SECURITY REASONS", "Access Denied", "Just a moment"))


# ---------------------------------------------------------------- 解析（純函式，單元測試用已存原始回應）
def _base(dataset: str, iso: str, url: str, now: str) -> dict:
    market, stable, _tbl, sig, unit = DATASETS[dataset]
    return {"market": market, "trade_date": iso, "fields_sig": sig, "source_table": stable, "source": url,
            "announced_at": None, "fetched_at": now, "unit": unit}


def parse_t86(data: dict, universe: dict[str, str], iso: str, url: str) -> dict:
    """回傳 {status, rows, page_rows, identity_fail, fields}。"""
    if data.get("stat") != "OK":
        return {"status": "no_data", "rows": [], "page_rows": 0, "identity_fail": 0, "error": str(data.get("stat"))}
    fields = data.get("fields") or []
    if fields != T86_FIELDS:
        return {"status": "fields_changed", "rows": [], "page_rows": len(data.get("data") or []), "identity_fail": 0,
                "error": f"T86 欄位與已實測 19 欄版本不同：{fields}"}
    if data.get("hints") and "股" not in str(data.get("hints")):
        return {"status": "fields_changed", "rows": [], "page_rows": 0, "identity_fail": 0, "error": f"單位改變：{data.get('hints')}"}
    now = now_iso()
    rows, bad, n = [], 0, 0
    for rec in data.get("data") or []:
        n += 1
        sym = str(rec[0]).strip()
        r = {T86_MAP[i]: _int(v) for i, v in enumerate(rec) if T86_MAP[i]}
        ok = (r["dealer_net"] == (r["dealer_self_net"] or 0) + (r["dealer_hedge_net"] or 0)
              and r["total_net"] == (r["foreign_ex_dealer_net"] or 0) + (r["foreign_dealer_net"] or 0) + (r["trust_net"] or 0) + (r["dealer_net"] or 0))
        if not ok:
            bad += 1
        if universe.get(sym) != "TWSE":
            continue
        row = _base("twse_t86", iso, url, now)
        row.update({c: None for c in INST_NUM})
        row.update(r)
        row["symbol"] = sym
        row["announced_at_note"] = None if ok else "恆等式不符"   # 來源無公告時間（announced_at＝NULL）；口徑說明見 NOTE_INST／docs，不逐列重複存
        rows.append(row)
    return _finish(rows, n, bad)


def parse_tpex_insti(data: dict, universe: dict[str, str], iso: str, url: str) -> dict:
    tables = data.get("tables") or []
    if (data.get("stat") or "").lower() != "ok" or not tables or not (tables[0].get("totalCount") or tables[0].get("data")):
        return {"status": "no_data", "rows": [], "page_rows": 0, "identity_fail": 0, "error": str(data.get("stat"))}
    t = tables[0]
    if (t.get("fields") or []) != TPEX_INSTI_FIELDS:
        return {"status": "fields_changed", "rows": [], "page_rows": len(t.get("data") or []), "identity_fail": 0,
                "error": f"insti/dailyTrade 欄位與已實測 24 欄版本不同：{t.get('fields')}"}
    now = now_iso()
    rows, bad, n = [], 0, 0
    for rec in t.get("data") or []:
        n += 1
        sym = str(rec[0]).strip()
        r = {TPEX_INSTI_MAP[i]: _int(v) for i, v in enumerate(rec) if TPEX_INSTI_MAP[i]}
        z = lambda k: r.get(k) or 0  # noqa: E731
        ok = all(z(f"foreign_total_{k}") == z(f"foreign_ex_dealer_{k}") + z(f"foreign_dealer_{k}") for k in ("buy", "sell", "net"))
        ok = ok and z("dealer_total_buy") == z("dealer_self_buy") + z("dealer_hedge_buy")
        ok = ok and z("dealer_total_sell") == z("dealer_self_sell") + z("dealer_hedge_sell")
        ok = ok and z("dealer_net") == z("dealer_self_net") + z("dealer_hedge_net")
        ok = ok and z("total_net") == z("foreign_total_net") + z("trust_net") + z("dealer_net")
        if not ok:
            bad += 1
        if universe.get(sym) != "TPEx":
            continue
        row = _base("tpex_insti", iso, url, now)
        row.update({c: None for c in INST_NUM})
        row.update(r)
        row["symbol"] = sym
        row["announced_at_note"] = None if ok else "恆等式不符"
        rows.append(row)
    return _finish(rows, n, bad)


def parse_mi_margn(data: dict, universe: dict[str, str], iso: str, url: str) -> dict:
    if data.get("stat") != "OK":
        return {"status": "no_data", "rows": [], "page_rows": 0, "identity_fail": 0, "error": str(data.get("stat"))}
    tables = data.get("tables") or []
    t = next((x for x in tables if (x.get("fields") or [None])[0] == "代號"), None)
    if t is None or not t.get("data"):
        return {"status": "no_data", "rows": [], "page_rows": 0, "identity_fail": 0, "error": "沒有個股融資融券表"}
    if t.get("fields") != MI_MARGN_FIELDS:
        return {"status": "fields_changed", "rows": [], "page_rows": len(t.get("data") or []), "identity_fail": 0,
                "error": f"MI_MARGN 欄位與已實測 16 欄版本不同：{t.get('fields')}"}
    return _margin_rows("twse_margin", t["data"], MI_MARGN_MAP, universe, iso, url, "twse_next_day_limit", NOTE_MARGIN)


def parse_tpex_margin(data: dict, universe: dict[str, str], iso: str, url: str) -> dict:
    tables = data.get("tables") or []
    if (data.get("stat") or "").lower() != "ok" or not tables or not (tables[0].get("totalCount") or tables[0].get("data")):
        return {"status": "no_data", "rows": [], "page_rows": 0, "identity_fail": 0, "error": str(data.get("stat"))}
    t = tables[0]
    if t.get("fields") != TPEX_MARGIN_FIELDS:
        return {"status": "fields_changed", "rows": [], "page_rows": len(t.get("data") or []), "identity_fail": 0,
                "error": f"margin/balance 欄位與已實測 20 欄版本不同：{t.get('fields')}"}
    return _margin_rows("tpex_margin", t["data"], TPEX_MARGIN_MAP, universe, iso, url, "tpex_limit", NOTE_MARGIN_TPEX)


def _margin_rows(dataset, data, cmap, universe, iso, url, limit_basis, note) -> dict:
    market = DATASETS[dataset][0]
    now = now_iso()
    rows, bad, n = [], 0, 0
    for rec in data:
        n += 1
        sym = str(rec[0]).strip()
        r: dict = {}
        for i, v in enumerate(rec):
            c = cmap[i]
            if not c:
                continue
            if c == "note_flags":
                r[c] = str(v).strip() or None
            elif c.endswith("_pct"):
                r[c] = _float(v)
            else:
                r[c] = _int(v)
        z = lambda k: r.get(k) or 0  # noqa: E731
        # 恆等式（只記數、不擋寫入：官方說明今日餘額可能後續調帳）
        ok = (z("margin_balance") == z("margin_prev_balance") + z("margin_buy") - z("margin_sell") - z("margin_cash_repay")
              and z("short_balance") == z("short_prev_balance") + z("short_sell") - z("short_buy") - z("short_stock_repay"))
        if not ok:
            bad += 1
        if universe.get(sym) != market:
            continue
        row = _base(dataset, iso, url, now)
        row.update({c: None for c in MARGIN_NUM})
        row["note_flags"] = None
        row.update(r)
        row["symbol"] = sym
        row["limit_basis"] = limit_basis
        row["announced_at_note"] = None if ok else "恆等式不符（可能為次日調帳）"
        rows.append(row)
    # 融資融券恆等式不符不一定是欄位改版（官方說明有次日調帳）；比例超過一半才視為欄位位置改變
    return _finish(rows, n, bad, ratio=0.5)


def _finish(rows: list[dict], n: int, bad: int, ratio: float = IDENTITY_FAIL_RATIO) -> dict:
    if n and bad > n * ratio:
        return {"status": "fields_changed", "rows": [], "page_rows": n, "identity_fail": bad,
                "error": f"恆等式不符 {bad}/{n} 列（>{ratio:.0%}），疑似欄位位置改變，不寫入"}
    return {"status": "ok", "rows": rows, "page_rows": n, "identity_fail": bad}


PARSERS = {"twse_t86": parse_t86, "tpex_insti": parse_tpex_insti, "twse_margin": parse_mi_margn, "tpex_margin": parse_tpex_margin}
CONTENT = {"institutional_trades": INST_CONTENT_KEYS, "margin_balances": MARGIN_CONTENT_KEYS}


# ---------------------------------------------------------------- 抓取狀態（檢查點）
def status_get(conn: sqlite3.Connection, dataset: str, market: str, iso: str):
    return conn.execute("SELECT status, attempts FROM chips_fetch_status WHERE dataset=? AND market=? AND data_date=?",
                        (dataset, market, iso)).fetchone()


def is_done(conn: sqlite3.Connection, dataset: str, iso: str) -> bool:
    """ok／fields_changed（要改程式，不重打）／no_data 已 2 次 → 視為完成（不再發請求）。"""
    r = status_get(conn, dataset, DATASETS[dataset][0], iso)
    if not r:
        return False
    st, att = r[0], r[1]
    return st in ("ok", "fields_changed", "資料不足") or (st == "no_data" and att >= 2)


def status_set(conn, dataset, market, iso, status, *, http=None, page_rows=None, stored=None, ident=None, sig=None,
               stable=None, url=None, error=None, notes=None) -> None:
    conn.execute(
        """INSERT INTO chips_fetch_status(dataset,market,data_date,status,http_status,page_rows,stored_rows,identity_fail,fields_sig,
             source_table,source,attempts,fetched_at,error,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
           ON CONFLICT(dataset,market,data_date) DO UPDATE SET status=excluded.status, http_status=excluded.http_status,
             page_rows=excluded.page_rows, stored_rows=excluded.stored_rows, identity_fail=excluded.identity_fail,
             fields_sig=excluded.fields_sig, source_table=excluded.source_table, source=excluded.source,
             attempts=chips_fetch_status.attempts+1, fetched_at=excluded.fetched_at, error=excluded.error, notes=excluded.notes""",
        (dataset, market, iso, status, http, page_rows, stored, ident, sig, stable, url, now_iso(), error, notes))


# ---------------------------------------------------------------- 單日單資料集
def fetch_dataset_day(conn: sqlite3.Connection, dataset: str, iso: str, universe: dict[str, str], guard: HostGuard) -> dict:
    """1 個請求。回傳 {status, written, skipped_host}。"""
    market, stable, table, sig, _unit = DATASETS[dataset]
    url = url_for(dataset, iso)
    host = host_of(url)
    if not guard.allowed(host):
        return {"status": "skipped_host", "written": 0, "host": host}
    try:
        resp = gated_request(client_for(dataset), "GET", url)
    except RateLimited as e:
        guard.blocked(host, f"rate_limited http={e.status}")
        status_set(conn, dataset, market, iso, "blocked", http=e.status, url=url, stable=stable, error=str(e)[:300])
        touch_source(conn, f"chips_{dataset}", url=url, ok=False, http_status=e.status, error=str(e)[:300])
        return {"status": "blocked", "written": 0}
    except FetchError as e:
        if e.blocked or (e.status in (301, 302, 303, 307, 308, 403, 429)):
            guard.blocked(host, f"http={e.status} {str(e)[:80]}")
            st = "blocked"
        else:
            guard.error(host, str(e)[:80])
            st = "error"
        status_set(conn, dataset, market, iso, st, http=e.status, url=url, stable=stable, error=str(e)[:300])
        touch_source(conn, f"chips_{dataset}", url=url, ok=False, http_status=e.status, error=str(e)[:300])
        conn.commit()
        return {"status": st, "written": 0}
    except Exception as e:  # noqa: BLE001  （網路例外已由 httputil 包成 FetchError；這裡是保險）
        guard.error(host, repr(e)[:80])
        status_set(conn, dataset, market, iso, "error", url=url, stable=stable, error=repr(e)[:300])
        conn.commit()
        return {"status": "error", "written": 0}
    head = resp.text[:400] if resp.content else ""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if data is None or not isinstance(data, dict) or _looks_blocked(head):
        guard.blocked(host, f"非 JSON／擋頁 http={resp.status_code}")
        status_set(conn, dataset, market, iso, "blocked", http=resp.status_code, url=url, stable=stable, error=f"非 JSON：{head[:120]}")
        touch_source(conn, f"chips_{dataset}", url=url, ok=False, http_status=resp.status_code, error="非 JSON／擋頁")
        conn.commit()
        return {"status": "blocked", "written": 0}
    guard.ok(host)
    res = PARSERS[dataset](data, universe, iso, url)
    written = {"inserted": 0, "updated": 0, "unchanged": 0}
    for i, row in enumerate(res["rows"], 1):
        k = upsert_versioned(conn, table, {"symbol": row["symbol"], "trade_date": iso}, row, CONTENT[table])
        written[k] += 1
        if i % 300 == 0:
            conn.commit()
    status_set(conn, dataset, market, iso, res["status"], http=resp.status_code, page_rows=res["page_rows"],
               stored=len(res["rows"]), ident=res["identity_fail"], sig=sig, stable=stable, url=url, error=res.get("error"),
               notes=DATASET_NOTES[dataset])
    touch_source(conn, f"chips_{dataset}", url=url, ok=res["status"] in ("ok", "no_data"), http_status=resp.status_code,
                 data_date=iso if res["status"] == "ok" else None, error=res.get("error"))
    conn.commit()
    return {"status": res["status"], "written": sum(written[k] for k in ("inserted", "updated")), "detail": written}


# ---------------------------------------------------------------- 集保 TDCC
def parse_tdcc_csv(text: str, universe: dict[str, str], url: str) -> dict:
    """opendata 1-5：資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%。回傳 {data_date, rows, page_symbols, status}。"""
    text = text.lstrip("\ufeff")
    rdr = csv.reader(io.StringIO(text))
    header = [h.strip().lstrip("\ufeff") for h in next(rdr, [])]
    want = ["資料日期", "證券代號", "持股分級", "人數", "股數"]
    if header[:5] != want or not (len(header) >= 6 and header[5].replace("佔", "占").startswith("占集保庫存數比例")):
        return {"status": "fields_changed", "rows": [], "data_date": None, "page_symbols": 0, "error": f"欄位改變：{header}"}
    by: dict[str, dict] = {}
    dd = None
    for rec in rdr:
        if len(rec) < 6:
            continue
        d8, sym, lv = rec[0].strip(), rec[1].strip(), _int(rec[2])
        dd = dd or d8
        if lv is None or lv not in LEVELS:
            continue
        g = by.setdefault(sym, {})
        g[f"holders_{lv}"] = _int(rec[3])
        g[f"shares_{lv}"] = _int(rec[4])
        g[f"pct_{lv}"] = _float(rec[5])
    if not dd or not re.fullmatch(r"\d{8}", dd):
        return {"status": "no_data", "rows": [], "data_date": None, "page_symbols": 0, "error": "沒有資料日期"}
    iso = f"{dd[:4]}-{dd[4:6]}-{dd[6:]}"
    now = now_iso()
    rows = []
    for sym, g in by.items():
        if sym not in universe:
            continue
        row = {c: None for c in TDCC_NUM}
        row.update(g)
        row.update({"symbol": sym, "market": universe[sym], "data_date": iso, "level_scheme": "tdcc_17", "source_table": "tdcc_od_1-5",
                    "source": url, "announced_at": None, "announced_at_note": None, "fetched_at": now,
                    "unit": "holders=人;shares=股;pct=%"})
        rows.append(row)
    return {"status": "ok", "rows": rows, "data_date": iso, "page_symbols": len(by)}


def parse_tdcc_qry(html: str, sym: str, want_iso: str, market: str | None, url: str) -> dict:
    """qryStock 單檔單週結果頁。序 1–15＝級距、『合計』→ *_17；沒有『差異數調整』列（*_16＝NULL）。"""
    if "查無此資料" in html:
        return {"status": "no_data", "row": None}
    m = re.search(r"資料日期：\s*(\d{2,3})年(\d{1,2})月(\d{1,2})日", html)
    if not m:
        return {"status": "no_data", "row": None, "error": "找不到資料日期"}
    got = f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if got != want_iso:
        return {"status": "error", "row": None, "error": f"回應資料日 {got} ≠ 查詢 {want_iso}"}
    i = html.find("持股/單位數分級")
    seg = html[i:] if i >= 0 else ""
    row = {c: None for c in TDCC_NUM}
    n = 0
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        tds = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) != 5:
            continue
        label = tds[1].replace("\u3000", "").replace(" ", "")
        if label == "合計":
            lv = 17
        elif label == "差異數調整":
            lv = 16
        else:
            lv = _int(tds[0])
            if lv is None or not 1 <= lv <= 15:
                continue
        row[f"holders_{lv}"] = _int(tds[2])
        row[f"shares_{lv}"] = _int(tds[3])
        row[f"pct_{lv}"] = _float(tds[4])
        n += 1
    if n < 16:
        return {"status": "fields_changed", "row": None, "error": f"分級列數 {n}（預期 15 級＋合計）"}
    row.update({"symbol": sym, "market": market, "data_date": want_iso, "level_scheme": "tdcc_17", "source_table": "tdcc_qryStock",
                "source": url, "announced_at": None, "announced_at_note": "qryStock 無差異數調整列（*_16＝NULL）",
                "fetched_at": now_iso(), "unit": "holders=人;shares=股;pct=%"})
    return {"status": "ok", "row": row}


def parse_tdcc_form(html: str) -> dict:
    tok = re.findall(r'name="SYNCHRONIZER_TOKEN"[^>]*value="([^"]*)"', html)
    fir = re.findall(r'name="firDate"[^>]*value="(\d{8})"', html)
    sel = re.search(r'<select[^>]*id="scaDate".*?</select>', html, re.S)
    opts = re.findall(r'<option[^>]*value="(\d{8})"', sel.group(0)) if sel else []
    return {"token": tok[0] if tok else None, "firDate": fir[0] if fir else None,
            "dates": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in opts]}


def latest_tdcc_date(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT MAX(data_date) FROM shareholding_distribution WHERE is_current=1").fetchone()
    return r[0] if r else None


def fetch_tdcc_latest(conn: sqlite3.Connection, universe: dict[str, str], guard: HostGuard, *, force: bool = False) -> dict:
    """opendata 1-5（整批、只有最新一週，約 2.4 MB）。最新已存資料日距今 ≥8 天才抓（每週最多約 1 個請求）。"""
    last = latest_tdcc_date(conn)
    if not force and last and (today() - date.fromisoformat(last)).days < 8:
        return {"status": "skipped_fresh", "latest": last, "requests": 0}
    host = host_of(TDCC_OD_URL)
    if not guard.allowed(host):
        return {"status": "skipped_host", "requests": 0}
    try:
        resp = gated_request(tdcc_client(), "GET", TDCC_OD_URL, timeout=(20, 120))
    except FetchError as e:
        (guard.blocked if (e.blocked or isinstance(e, RateLimited) or e.status in (301, 302, 303, 307, 308, 403, 429)) else guard.error)(host, str(e)[:80])
        touch_source(conn, "chips_tdcc_od", url=TDCC_OD_URL, ok=False, http_status=e.status, error=str(e)[:300])
        return {"status": "error", "requests": 1, "error": str(e)[:200]}
    text = resp.content.decode("utf-8-sig", errors="replace")
    if _looks_blocked(text[:400]) or "資料日期" not in text[:200]:
        guard.blocked(host, "非 CSV／擋頁")
        return {"status": "blocked", "requests": 1}
    res = parse_tdcc_csv(text, universe, TDCC_OD_URL)
    w = {"inserted": 0, "updated": 0, "unchanged": 0}
    for i, row in enumerate(res["rows"], 1):
        w[upsert_versioned(conn, "shareholding_distribution", {"symbol": row["symbol"], "data_date": row["data_date"]}, row,
                           TDCC_CONTENT_KEYS)] += 1
        if i % 300 == 0:
            conn.commit()
    if res.get("data_date"):
        status_set(conn, TDCC, "ALL", res["data_date"], res["status"], http=resp.status_code, page_rows=res["page_symbols"],
                   stored=len(res["rows"]), sig="TDCC_OD_6", stable="tdcc_od_1-5", url=TDCC_OD_URL, error=res.get("error"))
    touch_source(conn, "chips_tdcc_od", url=TDCC_OD_URL, ok=res["status"] == "ok", http_status=resp.status_code,
                 data_date=res.get("data_date"), error=res.get("error"))
    conn.commit()
    return {"status": res["status"], "data_date": res.get("data_date"), "requests": 1, "written": w}


_tdcc_client: httputil.RateClient | None = None


def tdcc_client() -> httputil.RateClient:
    global _tdcc_client
    if _tdcc_client is None:
        _tdcc_client = httputil.RateClient(max(CHIPS_INTERVAL, 5.0))
    return _tdcc_client


# ---------------------------------------------------------------- 覆蓋表
def refresh_coverage(conn: sqlite3.Connection, date_from: str = "2023-11-01", tdcc_oldest: str | None = None) -> list[dict]:
    """由 chips_fetch_status 重算 chips_coverage（每資料集 ok 範圍＋缺日數；TDCC 官網保存期之前標「資料不足」）。"""
    now = now_iso()
    out = []
    conn.execute("DELETE FROM chips_coverage")
    end_default = (today() - timedelta(days=1)).isoformat()
    for ds, (market, _st, _tbl, _sig, _u) in DATASETS.items():
        r = conn.execute("SELECT MIN(data_date), MAX(data_date), COUNT(*) FROM chips_fetch_status WHERE dataset=? AND market=? AND status='ok' AND data_date>=?",
                         (ds, market, date_from)).fetchone()
        exp = conn.execute("SELECT COUNT(*) FROM trading_calendar WHERE is_trade_day=1 AND calendar_date>=? AND calendar_date<=?",
                           (date_from, end_default)).fetchone()[0]
        cal_min = conn.execute("SELECT MIN(calendar_date) FROM trading_calendar").fetchone()[0]
        nd = conn.execute("SELECT COUNT(*) FROM chips_fetch_status WHERE dataset=? AND market=? AND status IN ('no_data','fields_changed','資料不足') AND data_date>=?",
                          (ds, market, date_from)).fetchone()[0]
        if r[2]:
            st = "ok" if r[0] <= date_from_trade(conn, date_from) and r[2] >= exp else "partial"
            out.append({"dataset": ds, "market": market, "range_start": r[0], "range_end": r[1], "status": st, "expected_days": exp, "ok_days": r[2],
                        "basis": "chips_fetch_status", "notes": f"no_data／欄位改版／資料不足 {nd} 日；交易日曆最早 {cal_min}"})
        else:
            out.append({"dataset": ds, "market": market, "range_start": date_from, "range_end": end_default, "status": "未測", "expected_days": exp,
                        "ok_days": 0, "basis": "尚未抓取", "notes": f"交易日曆最早 {cal_min}"})
    # TDCC：官網只保留約 1 年（qryStock 下拉 51 週）；之前的週一律資料不足（不補造）
    r = conn.execute("SELECT MIN(data_date), MAX(data_date), COUNT(DISTINCT data_date) FROM shareholding_distribution WHERE is_current=1").fetchone()
    oldest = tdcc_oldest or (state_detail(conn, "tdcc_oldest_available"))
    est = False
    if not oldest and r and r[1]:   # 尚未讀過 qryStock 下拉：以「最新週往前 51 週」推估（2026-11-24 實測下拉 51 週）（只用於覆蓋表標示，不產生資料）
        oldest, est = (date.fromisoformat(r[1]) - timedelta(weeks=51)).isoformat(), True
    if oldest and oldest > date_from:
        out.append({"dataset": TDCC, "market": "ALL", "range_start": date_from, "range_end": (date.fromisoformat(oldest) - timedelta(days=1)).isoformat(),
                    "status": "資料不足", "expected_days": None, "ok_days": 0,
                    "basis": "TDCC 官網歷史只保留約 1 年（qryStock 可選最早週）；opendata 1-5 只有最新一週",
                    "notes": f"官網最早可查 {oldest}" + ("（推估：尚未讀取 qryStock 日期清單）" if est else "")})
    if r and r[2]:
        out.append({"dataset": TDCC, "market": "ALL", "range_start": r[0], "range_end": r[1], "status": "partial", "expected_days": None,
                    "ok_days": r[2], "basis": "shareholding_distribution（週）", "notes": "週數＝已存的不同資料日；各週代號數見 chips_fetch_status"})
    for o in out:
        conn.execute("""INSERT OR REPLACE INTO chips_coverage(dataset,market,range_start,range_end,status,expected_days,ok_days,basis,notes,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     (o["dataset"], o["market"], o["range_start"], o["range_end"], o["status"], o["expected_days"], o["ok_days"], o["basis"], o["notes"], now))
    conn.commit()
    return out


def date_from_trade(conn: sqlite3.Connection, date_from: str) -> str:
    r = conn.execute("SELECT MIN(calendar_date) FROM trading_calendar WHERE is_trade_day=1 AND calendar_date>=?", (date_from,)).fetchone()
    return (r[0] if r and r[0] else date_from)


def state_detail(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT detail FROM backfill_state WHERE job=? AND key=?", (STATE_JOB, key)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- evening 增量
def recent_trade_dates(conn: sqlite3.Connection, n: int = 3, as_of: date | None = None) -> list[str]:
    d = (as_of or today()).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT calendar_date FROM trading_calendar WHERE is_trade_day=1 AND calendar_date<=? ORDER BY calendar_date DESC LIMIT ?", (d, n))]


def fetch_recent(conn: sqlite3.Connection, *, days: int = 3, stop_before=None, tdcc: bool = True) -> dict:
    """evening 用：最近 `days` 個交易日（含今日）尚未完成的資料集各 1 個請求＋TDCC 每週 1 個。正常每交易日 4 個請求。"""
    from pipeline.deadline import should_stop
    from pipeline.fetchers.market_prices import _universe

    req0 = dict(httputil.STATS)
    universe = _universe(conn)
    guard = HostGuard(conn)
    res: dict = {"dates": {}, "stopped": None}
    for iso in recent_trade_dates(conn, days):
        for ds in DAILY_DATASETS:
            if is_done(conn, ds, iso):
                continue
            if should_stop(stop_before):
                res["stopped"] = "stop_before"
                break
            r = fetch_dataset_day(conn, ds, iso, universe, guard)
            res["dates"].setdefault(iso, {})[ds] = r["status"]
    if tdcc and not should_stop(stop_before):
        res["tdcc"] = fetch_tdcc_latest(conn, universe, guard)
    refresh_coverage(conn)
    res["stopped_hosts"] = dict(guard.stopped)
    res["requests_by_host"] = {h: n - req0.get(h, 0) for h, n in httputil.STATS.items() if n - req0.get(h, 0)}
    return res
