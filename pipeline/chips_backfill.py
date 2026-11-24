"""籌碼回補（2026-11-24；冪等、可續跑，正式庫只由 GitHub Actions 執行）。

  python -m pipeline.run --job chips_backfill [--fix-from 2023-11-01] [--fix-to YYYY-MM-DD] [--max-dates N] [--budget-min 80]
      三大法人（TWSE T86、TPEx insti/dailyTrade）＋融資融券（TWSE MI_MARGN、TPEx margin/balance），由新到舊。
      每個交易日每個未完成的資料集 1 個請求（每日最多 4 個：www.twse.com.tw 2、www.tpex.org.tw 2）。
      檢查點：chips_fetch_status（ok／fields_changed／no_data 2 次＝完成，不再發請求）→ 重跑 0 請求。
  python -m pipeline.run --job tdcc_backfill [--symbols sample30|all] [--fix-from] [--fix-to] [--max-dates N（週數）] [--budget-min 80]
      集保股權分散表單檔歷史（qryStock，官網只保留約 51 週）：每代號每週 1 個 POST（＋每段工作階段 1 個 GET 取表單權杖）。
      已有該週資料（含 opendata）的代號不再查。早於官網最早週的範圍寫 chips_coverage「資料不足」，不發請求。

不要和 market_backfill 並行（同一個 Actions concurrency 群組 update-site，排隊執行；本機也有 assert_no_other_pipeline）。
擋頁／限流第一次就停止該主機（不重試），並記冷卻 RANGE_BLOCK_COOLDOWN_MIN 分鐘。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, timedelta

from pipeline import config, httputil
from pipeline.dates import today
from pipeline.fetchers import chips
from pipeline.runstats import Budget, Tally, finish_with_summary, state_get, state_set

log = logging.getLogger("pipeline.chips_backfill")
JOB = "chips_backfill"
TDCC_JOB = "tdcc_backfill"
DEFAULT_FROM = "2023-11-01"
CHIP_TABLES = ("institutional_trades", "margin_balances", "shareholding_distribution")


def target_dates(conn: sqlite3.Connection, date_from: str | None, date_to: str | None) -> list[str]:
    d0 = date_from or DEFAULT_FROM
    d1 = date_to or (today() - timedelta(days=1)).isoformat()   # 當日由 evening 負責
    return [r[0] for r in conn.execute(
        "SELECT calendar_date FROM trading_calendar WHERE is_trade_day=1 AND calendar_date>=? AND calendar_date<=? ORDER BY calendar_date DESC",
        (d0, d1))]


def run_backfill(conn: sqlite3.Connection, *, date_from: str | None = None, date_to: str | None = None,
                 max_dates: int | None = None, budget_min: float | None = None, datasets: list[str] | None = None) -> dict:
    from pipeline.fetchers.market_prices import _universe

    tally = Tally(conn, CHIP_TABLES)
    budget = Budget(budget_min)
    d0 = date_from or DEFAULT_FROM
    universe = _universe(conn, include_delisted=d0)
    dss = datasets or chips.DAILY_DATASETS
    dates = target_dates(conn, d0, date_to)
    cal_min = conn.execute("SELECT MIN(calendar_date) FROM trading_calendar").fetchone()[0]
    guard = chips.HostGuard(conn)
    out: dict = {"target_dates": len(dates), "calendar_min": cal_min, "datasets": dss,
                 "universe": {m: sum(1 for v in universe.values() if v == m) for m in ("TWSE", "TPEx")},
                 "dates_done": 0, "dates_skipped_done": 0, "status_counts": {}, "stopped": None}
    if cal_min and cal_min > d0:
        out["calendar_missing"] = f"trading_calendar 最早 {cal_min} > fix-from {d0}：之前的日期不在目標內，請先跑 calendar_backfill --fix-from {d0}"
    processed = 0
    last_dur = 0.0
    for iso in dates:
        need = [ds for ds in dss if not chips.is_done(conn, ds, iso)]
        if not need:
            out["dates_skipped_done"] += 1
            continue
        need = [ds for ds in need if chips.host_of(chips.url_for(ds, iso)) not in guard.stopped]
        if not need:
            out["stopped"] = "all_needed_hosts_stopped"
            break
        if max_dates is not None and processed >= max_dates:
            out["stopped"] = "max_dates"
            break
        if budget.expired(reserve_s=min(last_dur * 1.5, 300)):
            out["stopped"] = "budget"
            break
        t0 = budget.elapsed()
        for ds in need:
            r = chips.fetch_dataset_day(conn, ds, iso, universe, guard)
            out["status_counts"][r["status"]] = out["status_counts"].get(r["status"], 0) + 1
        processed += 1
        last_dur = budget.elapsed() - t0
        if all(chips.is_done(conn, ds, iso) for ds in dss):
            out["dates_done"] += 1
        log.info("chips_backfill %s need=%s %.1fs stopped_hosts=%s", iso, need, last_dur, guard.stopped)
    left = [d for d in dates if not all(chips.is_done(conn, ds, d) for ds in dss)]
    out["dates_left"] = len(left)
    out["newest_left"] = left[0] if left else None
    out["oldest_left"] = left[-1] if left else None
    out["stopped_hosts"] = dict(guard.stopped)
    out["coverage"] = [{k: c[k] for k in ("dataset", "market", "range_start", "range_end", "status", "ok_days", "expected_days")}
                       for c in chips.refresh_coverage(conn)]
    out["tally"] = tally.summary()
    return out


def main_job(*, date_from: str | None = None, date_to: str | None = None, max_dates: int | None = None,
             budget_min: float | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, JOB, "all", today().isoformat())
    try:
        out = run_backfill(conn, date_from=date_from, date_to=date_to, max_dates=max_dates, budget_min=budget_min)
        ok = out["dates_left"] == 0 and not out["stopped_hosts"]
        status = "success" if ok else "partial"
        err = None if ok else f"dates_left={out['dates_left']} stopped={out['stopped']} hosts={out['stopped_hosts']}"
        finish_with_summary(conn, run_id, status, out, [f"{h}:{w}" for h, w in out["stopped_hosts"].items()], err)
        log.info("chips_backfill %s %s", status, out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("chips_backfill")
        return 2
    finally:
        conn.close()


# ---------------------------------------------------------------- TDCC qryStock（單檔歷史）
class TdccSession:
    """GET 表單頁取 SYNCHRONIZER_TOKEN（公開表單的防重送權杖，不是帳密），之後每個 POST 回應頁會帶下一個權杖。"""

    def __init__(self, guard: chips.HostGuard):
        self.guard = guard
        self.client = chips.tdcc_client()
        self.token: str | None = None
        self.fir: str | None = None
        self.dates: list[str] = []
        self.requests = 0

    def open(self) -> bool:
        host = chips.host_of(chips.TDCC_QRY_URL)
        if not self.guard.allowed(host):
            return False
        try:
            resp = chips.gated_request(self.client, "GET", chips.TDCC_QRY_URL)
        except httputil.FetchError as e:
            self._fail(host, e)
            return False
        finally:
            self.requests += 1
        form = chips.parse_tdcc_form(resp.text)
        if not form["token"] or not form["dates"]:
            self.guard.blocked(host, "表單頁沒有權杖或日期清單（疑似擋頁或改版）")
            return False
        self.token, self.fir, self.dates = form["token"], form["firDate"], form["dates"]
        return True

    def _fail(self, host: str, e: httputil.FetchError) -> None:
        if e.blocked or isinstance(e, httputil.RateLimited) or e.status in (301, 302, 303, 307, 308, 403, 429):
            self.guard.blocked(host, str(e)[:80])
        else:
            self.guard.error(host, str(e)[:80])

    def query(self, sym: str, iso: str) -> str | None:
        host = chips.host_of(chips.TDCC_QRY_URL)
        if not self.guard.allowed(host):
            return None
        if not self.token and not self.open():
            return None
        data = {"method": "submit", "firDate": self.fir or "", "scaDate": iso.replace("-", ""), "sqlMethod": "StockNo",
                "stockNo": sym, "stockName": "", "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock", "SYNCHRONIZER_TOKEN": self.token}
        try:
            resp = chips.gated_request(self.client, "POST", chips.TDCC_QRY_URL, data=data, headers={"Referer": chips.TDCC_QRY_URL})
        except httputil.FetchError as e:
            self._fail(host, e)
            return None
        finally:
            self.requests += 1
        self.guard.ok(host)
        nxt = chips.parse_tdcc_form(resp.text)["token"]
        self.token = nxt   # 沒有新權杖 → 下次先重新 GET
        return resp.text


def tdcc_scope_symbols(conn: sqlite3.Connection, scope: str) -> dict[str, str]:
    from pipeline.fetchers.market_prices import _universe

    uni = _universe(conn)
    if scope == "all":
        return dict(sorted(uni.items()))
    return {s: uni[s] for s in config.SAMPLE30 if s in uni}


def run_tdcc_backfill(conn: sqlite3.Connection, *, scope: str = "sample30", date_from: str | None = None, date_to: str | None = None,
                      max_dates: int | None = None, budget_min: float | None = None) -> dict:
    tally = Tally(conn, CHIP_TABLES)
    budget = Budget(budget_min)
    d0 = date_from or DEFAULT_FROM
    syms = tdcc_scope_symbols(conn, scope)
    guard = chips.HostGuard(conn)
    sess = TdccSession(guard)
    out: dict = {"scope": scope, "symbols": len(syms), "weeks_done": 0, "queried": 0, "written": 0, "no_data": 0, "stopped": None}
    if not sess.open():
        out["stopped"] = f"tdcc_page_unavailable:{guard.stopped}"
        out["tally"] = tally.summary()
        return out
    avail = sorted(sess.dates, reverse=True)
    oldest = avail[-1]
    state_set(conn, chips.STATE_JOB, "tdcc_oldest_available", "info", oldest)
    conn.commit()
    weeks = [w for w in avail if w >= d0 and (date_to is None or w <= date_to)]
    out.update({"available_weeks": len(avail), "oldest_available": oldest, "newest_available": avail[0], "target_weeks": len(weeks),
                "estimated_requests_full": len(weeks) * len(syms)})
    processed = 0
    for w in weeks:
        have = {r[0] for r in conn.execute("SELECT symbol FROM shareholding_distribution WHERE is_current=1 AND data_date=?", (w,))}
        todo = [s for s in syms if s not in have and not (state_get(conn, TDCC_JOB, f"no_data:{s}:{w}") or (None, 0))[1] >= 2]
        if not todo:
            out["weeks_done"] += 1
            continue
        if max_dates is not None and processed >= max_dates:
            out["stopped"] = "max_dates"
            break
        processed += 1
        for s in todo:
            if budget.expired(reserve_s=60):
                out["stopped"] = "budget"
                break
            html = sess.query(s, w)
            if html is None:
                break
            out["queried"] += 1
            res = chips.parse_tdcc_qry(html, s, w, syms[s], chips.TDCC_QRY_URL)
            if res["status"] == "ok":
                chips.upsert_versioned(conn, "shareholding_distribution", {"symbol": s, "data_date": w}, res["row"], chips.TDCC_CONTENT_KEYS)
                out["written"] += 1
            elif res["status"] == "no_data":
                state_set(conn, TDCC_JOB, f"no_data:{s}:{w}", "no_data", None, bump=True)
                out["no_data"] += 1
            else:
                out.setdefault("errors", []).append(f"{s}:{w}:{res.get('error')}")
                if res["status"] == "fields_changed":
                    out["stopped"] = "fields_changed"
                    break
            if out["queried"] % 50 == 0:
                conn.commit()
        conn.commit()
        if out["stopped"] or guard.stopped:
            out["stopped"] = out["stopped"] or f"host_stopped:{guard.stopped}"
            break
        done = conn.execute("SELECT COUNT(*) FROM shareholding_distribution WHERE is_current=1 AND data_date=?", (w,)).fetchone()[0]
        chips.status_set(conn, chips.TDCC, "ALL", w, "ok", stored=done, sig="TDCC_QRY", stable="tdcc_qryStock", url=chips.TDCC_QRY_URL,
                         notes=f"scope={scope} symbols={len(syms)}")
        conn.commit()
    out["requests"] = sess.requests
    out["stopped_hosts"] = dict(guard.stopped)
    chips.refresh_coverage(conn, tdcc_oldest=oldest)
    out["tally"] = tally.summary()
    return out


def main_tdcc(*, scope: str | None = None, date_from: str | None = None, date_to: str | None = None,
              max_dates: int | None = None, budget_min: float | None = None) -> int:
    from pipeline.db import connect, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    sc = scope or "sample30"
    run_id = start_job(conn, TDCC_JOB, sc, today().isoformat())
    try:
        out = run_tdcc_backfill(conn, scope=sc, date_from=date_from, date_to=date_to, max_dates=max_dates, budget_min=budget_min)
        ok = not out.get("stopped") and not out.get("stopped_hosts")
        finish_with_summary(conn, run_id, "success" if ok else "partial", out, out.get("errors", [])[:50],
                            None if ok else f"stopped={out.get('stopped')}")
        log.info("tdcc_backfill %s", out)
        return 0
    except Exception as e:  # noqa: BLE001
        finish_with_summary(conn, run_id, "failed", {}, [str(e)], str(e))
        log.exception("tdcc_backfill")
        return 2
    finally:
        conn.close()
