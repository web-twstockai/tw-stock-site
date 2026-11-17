"""停牌誤標修正與缺口證據（一次性作業，可重跑、冪等）。

背景（前情，2026-11-10 實測與記錄；見 docs/rd/rounds/2026-W47/review-r2.md、logs/reclassify_before.json）：
  早期把「收盤價為空」整批標成 is_halted=1：全期 729 列、138 檔（2026-03-31～11-09；上櫃 653、上市 76），其中最多只有 11 列
  與官方 trading_halts（盤中暫停交易）同日同股。11-10 起已由 reclassify_prices 重算：停牌只依官方 trading_halts，
  收盤空值＝無成交（is_no_trade=1），不是停牌。
本作業做四件事（用法：python -m pipeline.run --job halt_fix [--fix-from D] [--fix-to D] [--max-dates N] [--no-fetch]）：
  1. 官方證據入庫：halt_evidence.py 的停止買賣證據（1589、3714、1435…）寫入 trading_halts（halt_type='停止買賣'，版本化）；
     並抓兩個官方快照（TWSE 停止買賣 API、tpex_cmode；共 2 個請求）確認／補錄目前仍在停止買賣的股票。
  2. 全期重算 is_halted／is_no_trade：只依官方證據；與現值不同才版本化寫入（舊列 is_current=0），change_note 註明；
     停止買賣區間內若竟有收盤價（證據與行情矛盾）→ 不標停牌，只列入回報。
  3. 缺口稽核 price_gaps：全市場已入庫日中「沒有行情列」的股票日，逐日標示 halted_official／not_listed／insufficient（資料不足）；
     查無官方證據者標「資料不足」，不補造、不推定為停牌。
  4. 前後數字記入 job_runs。
--fix-from／--fix-to／--max-dates：只處理該區間（最多 N 個全市場日，由 --fix-from 起算），供分段執行；各步驟皆冪等，可重跑。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict

from pipeline.dates import now_iso, today
from pipeline.db import touch_source, upsert_versioned
from pipeline.halt_evidence import LISTING_EVIDENCE, STOP_EVIDENCE

log = logging.getLogger("pipeline.halt_fix")

TWSE_STOP_URL = "https://www.twse.com.tw/rwd/zh/violation/stop?response=json"
TPEX_CMODE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_cmode"
STOP_TYPE = "停止買賣"
HALT_KEYS = ["resume_date", "resume_time", "halt_type", "source", "announced_at_note"]
GAP_KEYS = ["status", "reason", "evidence_source", "evidence_level", "note"]
FULL_MARKET_MIN_ROWS = 100     # 某市場某日現行 MI_INDEX／dailyQuotes 列數達此值，視為「全市場日」


# ---------- 1. 證據入庫 ----------
def _stop_note(ev: dict) -> str:
    also = ("；另見 " + " ".join(ev["also"])) if ev.get("also") else ""
    return f"證據等級{ev['level']}｜{ev['reason']}｜查證：{ev['checked']}{also}"


def write_stop_evidence(conn: sqlite3.Connection) -> Counter:
    c: Counter = Counter()
    for ev in STOP_EVIDENCE:
        row = {"market": ev["market"], "symbol": ev["symbol"], "halt_date": ev["start"], "halt_time": "",
               "resume_date": ev["end"], "resume_time": None, "halt_type": STOP_TYPE, "source": ev["source"],
               "announced_at": None, "announced_at_note": _stop_note(ev), "fetched_at": now_iso()}
        st = upsert_versioned(conn, "trading_halts",
                              {"market": ev["market"], "symbol": ev["symbol"], "halt_date": ev["start"], "halt_time": ""},
                              row, HALT_KEYS)
        c[st] += 1
    conn.commit()
    return c


def _roc_cn_date(s: str) -> str | None:
    m = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s or "")
    if not m:
        return None
    return f"{int(m.group(1)) + 1911:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def fetch_snapshots(conn: sqlite3.Connection) -> dict:
    """兩個官方快照（各 1 個請求）。遇限流／失敗只記錄，不中斷其他步驟。"""
    from pipeline.httputil import FetchError, RateLimited, tpex, twse

    out: dict = {"requests": 0, "twse_stop": None, "tpex_cmode": None, "errors": [], "rate_limited": False,
                 "new_from_snapshot": [], "closed": [], "confirmed": [], "unrecorded_cmode": []}
    curated = {e["symbol"]: e for e in STOP_EVIDENCE}
    twse_now: dict[str, str] = {}
    try:
        out["requests"] += 1
        data = twse.request("GET", TWSE_STOP_URL).json()
        if (data.get("stat") or "").lower() != "ok":
            raise FetchError(f"stat={data.get('stat')}")
        rows = (data.get("tables") or [{}])[0].get("data") or []
        for r in rows:
            sym, name, clause, reason, start = (str(x).strip() for x in (list(r) + [""] * 5)[:5])
            iso = _roc_cn_date(start)
            if not sym or not iso:
                continue
            twse_now[sym] = iso
        out["twse_stop"] = len(twse_now)
        touch_source(conn, "twse_violation_stop", url=TWSE_STOP_URL, ok=True, http_status=200)
    except RateLimited as e:
        out["rate_limited"] = True
        out["errors"].append(f"twse_stop:{e}")
        touch_source(conn, "twse_violation_stop", url=TWSE_STOP_URL, ok=False, error=str(e))
    except (FetchError, ValueError, KeyError) as e:
        out["errors"].append(f"twse_stop:{e}")
        touch_source(conn, "twse_violation_stop", url=TWSE_STOP_URL, ok=False, error=str(e))
    if out["twse_stop"] is not None:
        snap_day = today().isoformat()
        for sym, iso in twse_now.items():
            if sym in curated and curated[sym]["start"] == iso:
                out["confirmed"].append(sym)
                continue
            if sym in curated:
                out["errors"].append(f"twse_stop:{sym} 起日 {iso} 與固化證據 {curated[sym]['start']} 不一致")
                continue
            row = {"market": "TWSE", "symbol": sym, "halt_date": iso, "halt_time": "", "resume_date": None,
                   "resume_time": None, "halt_type": STOP_TYPE, "source": TWSE_STOP_URL, "announced_at": None,
                   "announced_at_note": f"證據等級A｜TWSE 停止買賣 API 快照 {snap_day}（持續停止買賣；恢復日未知）",
                   "fetched_at": now_iso()}
            st = upsert_versioned(conn, "trading_halts", {"market": "TWSE", "symbol": sym, "halt_date": iso, "halt_time": ""}, row, HALT_KEYS)
            if st != "unchanged":
                out["new_from_snapshot"].append(sym)
        # 先前由快照補錄、這次快照不再列出者：以快照日為恢復日上界
        for r in conn.execute("SELECT symbol, halt_date, announced_at_note FROM trading_halts WHERE is_current=1 AND market='TWSE' "
                              "AND halt_type=? AND resume_date IS NULL AND source=?", (STOP_TYPE, TWSE_STOP_URL)).fetchall():
            if r[0] not in twse_now:
                row = {"market": "TWSE", "symbol": r[0], "halt_date": r[1], "halt_time": "", "resume_date": snap_day,
                       "resume_time": None, "halt_type": STOP_TYPE, "source": TWSE_STOP_URL, "announced_at": None,
                       "announced_at_note": (r[2] or "") + f"｜快照 {snap_day} 不再列出：恢復日以快照日為上界（非官方恢復日）",
                       "fetched_at": now_iso()}
                st = upsert_versioned(conn, "trading_halts", {"market": "TWSE", "symbol": r[0], "halt_date": r[1], "halt_time": ""}, row, HALT_KEYS)
                if st != "unchanged":
                    out["closed"].append(r[0])
        conn.commit()
    cm_now: set[str] = set()
    try:
        out["requests"] += 1
        data = tpex.request("GET", TPEX_CMODE_URL).json()
        if not isinstance(data, list):
            raise FetchError("tpex_cmode 非陣列")
        for r in data:
            if str(r.get("SuspensionOfTrading") or "").strip():
                cm_now.add(str(r.get("SecuritiesCompanyCode")).strip())
        out["tpex_cmode"] = len(cm_now)
        touch_source(conn, "tpex_cmode", url=TPEX_CMODE_URL, ok=True, http_status=200)
    except RateLimited as e:
        out["rate_limited"] = True
        out["errors"].append(f"tpex_cmode:{e}")
        touch_source(conn, "tpex_cmode", url=TPEX_CMODE_URL, ok=False, error=str(e))
    except (FetchError, ValueError) as e:
        out["errors"].append(f"tpex_cmode:{e}")
        touch_source(conn, "tpex_cmode", url=TPEX_CMODE_URL, ok=False, error=str(e))
    if out["tpex_cmode"] is not None:
        for sym in sorted(cm_now):
            if sym in curated:
                out["confirmed"].append(sym)
            else:
                out["unrecorded_cmode"].append(sym)   # 官方標停止交易但沒有起日證據：不寫入 trading_halts，由人工查證
        # 固化證據中「仍在停止買賣（end=None）」的上櫃股，快照不再標示 → 可能已恢復，需人工確認
        out["maybe_resumed"] = [e["symbol"] for e in STOP_EVIDENCE if e["market"] == "TPEx" and e["end"] is None and e["symbol"] not in cm_now]
    conn.commit()
    return out


# ---------- 2. 全期重算 ----------
# ---------- 0. 停牌統一規則（2026-11-17，與研究分析 0.9.1 對齊）----------
# 停牌從停牌日起算；有恢復日 → 停到恢復日前一天；沒有恢復日 → 停到「停牌日之後第一個有官方成交列」的前一天，
# 推得的恢復日存 resume_date_inferred，resume_basis='inferred_by_trade'（依成交推定）；之後一直沒有成交 → 'ongoing'（仍在停牌）。
# 資料不足以推定（停牌日早於全市場行情起始日、無此代號行情列、推定區間內有缺全市場行情的交易日）→ 'insufficient'（資料不足），不推測。
BASIS_OFFICIAL = "official"
BASIS_INFERRED = "inferred_by_trade"
BASIS_ONGOING = "ongoing"
BASIS_INSUFFICIENT = "insufficient"
OPEN_END = "9999-12-31"


def infer_resume(conn: sqlite3.Connection) -> dict:
    """為所有現行 trading_halts 列填 resume_basis／resume_date_inferred／resume_basis_note（就地更新、不升版；冪等）。"""
    full = full_market_days(conn, None, None, None)
    first_full = {m: (min(v) if v else None) for m, v in full.items()}
    full_set = {m: set(v) for m, v in full.items()}
    max_day = conn.execute("SELECT MAX(price_date) FROM daily_prices WHERE is_current=1").fetchone()[0]
    cal = [r[0] for r in conn.execute(
        "SELECT calendar_date FROM trading_calendar WHERE is_trade_day=1 AND calendar_date<=? ORDER BY calendar_date", (max_day or "0000",))]
    out: Counter = Counter()
    changed = 0
    rows = conn.execute(
        "SELECT rowid, market, symbol, halt_date, resume_date, resume_date_inferred, resume_basis, resume_basis_note "
        "FROM trading_halts WHERE is_current=1").fetchall()
    for r in rows:
        mkt, sym, hd, rd = r["market"], r["symbol"], r["halt_date"], r["resume_date"]
        inferred, note = None, None
        if rd not in (None, ""):
            basis = BASIS_OFFICIAL
        else:
            has_any = conn.execute("SELECT 1 FROM daily_prices WHERE symbol=? AND is_current=1 LIMIT 1", (sym,)).fetchone()
            ff = first_full.get(mkt)
            if not has_any:
                basis, note = BASIS_INSUFFICIENT, "無此代號的行情列（非普通股或未納入），無法依成交推定"
            elif ff is None or hd < ff:
                basis, note = BASIS_INSUFFICIENT, f"停牌日早於{mkt}全市場行情起始日（{ff}），無法依成交推定"
            else:
                trade = conn.execute(
                    "SELECT MIN(price_date) FROM daily_prices WHERE symbol=? AND is_current=1 AND price_date>? AND close_twd IS NOT NULL",
                    (sym, hd)).fetchone()[0]
                span = [d for d in cal if d > hd and (trade is None or d < trade)]
                holes = [d for d in span if d not in full_set.get(mkt, ())]
                if holes:
                    basis, note = BASIS_INSUFFICIENT, f"推定區間內有 {len(holes)} 個交易日缺{mkt}全市場行情（{holes[0]}～{holes[-1]}），無法確認何時恢復"
                elif trade is not None:
                    basis, inferred = BASIS_INFERRED, trade
                    note = f"依成交推定：{hd} 之後第一個有官方成交列為 {trade}，視為恢復買賣日"
                else:
                    basis, note = BASIS_ONGOING, f"{hd} 之後（至 {max_day}）無任何官方成交列，視為仍在停牌"
        out[basis] += 1
        if (r["resume_basis"], r["resume_date_inferred"], r["resume_basis_note"]) != (basis, inferred, note):
            conn.execute("UPDATE trading_halts SET resume_basis=?, resume_date_inferred=?, resume_basis_note=? WHERE rowid=?",
                         (basis, inferred, note, r["rowid"]))
            changed += 1
    conn.commit()
    no_resume = sum(v for k, v in out.items() if k != BASIS_OFFICIAL)
    return {"current_rows": len(rows), "by_basis": dict(out), "no_resume_rows": no_resume, "rows_updated": changed,
            "first_full_day": first_full}


class Coverage:
    """pause＝既有暫停交易語意（resume 空＝只標 halt_date 當日）；stop＝停止買賣（resume 空＝持續中）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.pause_exact: set[tuple[str, str]] = set()
        self.pause_iv: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.stop: dict[str, list[tuple[str, str | None, str, str, str]]] = defaultdict(list)   # sym → (start,end,source,note,type)
        for sym, hd, rd, ht, src, note, basis, rinf in conn.execute(
            "SELECT symbol, halt_date, resume_date, halt_type, source, announced_at_note, resume_basis, resume_date_inferred "
            "FROM trading_halts WHERE is_current=1"
        ):
            if not sym or not hd:
                continue
            # 統一規則：沒有恢復日時，用「依成交推定」的恢復日；ongoing＝開區間（仍在停牌）；其餘維持舊語意
            if rd in (None, ""):
                if basis == BASIS_INFERRED and rinf:
                    rd = rinf
                elif basis == BASIS_ONGOING:
                    rd = None if (ht or "").startswith(STOP_TYPE) else OPEN_END
            if (ht or "").startswith(STOP_TYPE):
                self.stop[str(sym)].append((hd, rd or None, src, note or "", ht))
            elif rd in (None, "", hd):
                self.pause_exact.add((str(sym), hd))
            else:
                self.pause_iv[str(sym)].append((hd, rd))

    def pause(self, sym: str, d: str) -> bool:
        return (sym, d) in self.pause_exact or any(a <= d < b for a, b in self.pause_iv.get(sym, ()))

    def stop_hit(self, sym: str, d: str):
        for a, b, src, note, ht in self.stop.get(sym, ()):
            if a <= d and (b is None or d < b):
                return (a, b, src, note, ht)
        return None


def snapshot_stats(conn: sqlite3.Connection) -> dict:
    out = {}
    for m, hr, hs, nr, ns, cr in conn.execute(
        """SELECT market, SUM(is_halted=1), COUNT(DISTINCT CASE WHEN is_halted=1 THEN symbol END),
                  SUM(is_no_trade=1), COUNT(DISTINCT CASE WHEN is_no_trade=1 THEN symbol END), COUNT(*)
           FROM daily_prices WHERE is_current=1 GROUP BY market"""
    ):
        out[m] = {"halted_rows": hr or 0, "halted_syms": hs or 0, "no_trade_rows": nr or 0, "no_trade_syms": ns or 0, "current_rows": cr}
    r = conn.execute("""SELECT SUM(close_twd IS NULL), COUNT(DISTINCT CASE WHEN close_twd IS NULL THEN symbol END),
                               SUM(close_twd IS NULL AND is_halted=1), SUM(close_twd IS NULL AND COALESCE(is_halted,0)=0 AND is_no_trade=1)
                        FROM daily_prices WHERE is_current=1""").fetchone()
    out["close_null"] = {"rows": r[0] or 0, "syms": r[1] or 0, "halted": r[2] or 0, "no_trade_only": r[3] or 0}
    return out


def recompute_flags(conn: sqlite3.Connection, cov: Coverage, date_from: str | None, date_to: str | None) -> dict:
    from pipeline.fetchers.market_prices import _payload_from_old, _upsert_price

    q = "SELECT * FROM daily_prices WHERE is_current=1 AND (close_twd IS NULL OR is_halted=1 OR is_no_trade IS NULL OR is_halted IS NULL)"
    args: list = []
    if date_from:
        q += " AND price_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND price_date<=?"
        args.append(date_to)
    c: Counter = Counter()
    conflicts: list[str] = []
    transitions: Counter = Counter()
    stamp = today().isoformat()
    rows = conn.execute(q, args).fetchall()
    # 統一規則「停牌從停牌日起算」：停牌日當天有收盤價（盤中暫停）的列，一般條件不會被選到，另外補進來
    seen_keys = {(r["symbol"], r["price_date"]) for r in rows}
    starts = set(cov.pause_exact)
    starts.update((sym, a) for sym, ivs in cov.pause_iv.items() for a, _b in ivs)
    for sym, d in sorted(starts):
        if (sym, d) in seen_keys or (date_from and d < date_from) or (date_to and d > date_to):
            continue
        r0 = conn.execute("SELECT * FROM daily_prices WHERE symbol=? AND price_date=? AND is_current=1", (sym, d)).fetchone()
        if r0 is not None:
            rows.append(r0)
    for i, old in enumerate(rows, 1):
        sym, d, close = old["symbol"], old["price_date"], old["close_twd"]
        no_trade = 1 if close is None else 0
        halted = 1 if cov.pause(sym, d) else 0
        hit = cov.stop_hit(sym, d)
        if hit is not None:
            if close is None:
                halted = 1
            else:
                conflicts.append(f"{sym} {d}：停止買賣證據 [{hit[0]},{hit[1]}) 與收盤價 {close} 矛盾，未標停牌")
        old_h, old_nt = int(old["is_halted"] or 0), old["is_no_trade"]
        if old_h == halted and old_nt is not None and int(old_nt) == no_trade:
            c["unchanged"] += 1
            continue
        if old_nt is None and old_h == halted and no_trade == 0:
            # 正常成交列補 is_no_trade=0：就地補預設，不升版（與 reclassify_prices 相同）
            conn.execute("UPDATE daily_prices SET is_no_trade=0 WHERE symbol=? AND price_date=? AND is_current=1", (sym, d))
            c["in_place_default"] += 1
            continue
        why = "官方 trading_halts／停止買賣證據涵蓋" if halted else "官方證據未涵蓋該日（收盤空值＝無成交，不是停牌）"
        note = f"停牌判定重算 {stamp}：is_halted {old_h}→{halted}、is_no_trade {old_nt}→{no_trade}（{why}）"
        cur = (old["change_note"] or "").strip()
        payload = _payload_from_old(old, {"is_halted": halted, "is_no_trade": no_trade, "fetched_at": now_iso(),
                                          "change_note": f"{cur}；{note}" if cur else note})
        st = _upsert_price(conn, payload)
        c[st] += 1
        transitions[f"halted {old_h}→{halted}, no_trade {old_nt}→{no_trade}"] += 1
        if i % 500 == 0:
            conn.commit()
    conn.commit()
    # 停止買賣證據與行情矛盾（區間內竟有收盤價）：不標停牌，只回報，供人工複核證據
    seen = {x.split("：")[0] for x in conflicts}
    for sym, ivs in cov.stop.items():
        for a, b, _src, _note, _ht in ivs:
            qq = "SELECT price_date, close_twd FROM daily_prices WHERE symbol=? AND is_current=1 AND close_twd IS NOT NULL AND price_date>=?"
            aa: list = [sym, a]
            if b:
                qq += " AND price_date<?"
                aa.append(b)
            if date_from:
                qq += " AND price_date>=?"
                aa.append(date_from)
            if date_to:
                qq += " AND price_date<=?"
                aa.append(date_to)
            for d, cl in conn.execute(qq, aa):
                key = f"{sym} {d}"
                if key not in seen:
                    seen.add(key)
                    conflicts.append(f"{key}：停止買賣證據 [{a},{b}) 與收盤價 {cl} 矛盾，未標停牌")
    return {"rows_examined": len(rows), "counts": dict(c), "transitions": dict(transitions), "conflicts": conflicts[:50],
            "conflict_count": len(conflicts)}


# ---------- 3. 缺口稽核 ----------
def full_market_days(conn: sqlite3.Connection, date_from, date_to, max_dates) -> dict[str, list[str]]:
    q = ("SELECT market, price_date, COUNT(*) FROM daily_prices WHERE is_current=1 AND source_table IN ('MI_INDEX','dailyQuotes')")
    args: list = []
    if date_from:
        q += " AND price_date>=?"
        args.append(date_from)
    if date_to:
        q += " AND price_date<=?"
        args.append(date_to)
    q += " GROUP BY market, price_date HAVING COUNT(*) >= ? ORDER BY price_date"
    args.append(FULL_MARKET_MIN_ROWS)
    days: dict[str, list[str]] = defaultdict(list)
    for m, d, _n in conn.execute(q, args):
        days[m].append(d)
    if max_dates:
        keep = sorted({d for v in days.values() for d in v})[:max_dates]
        ks = set(keep)
        days = defaultdict(list, {m: [d for d in v if d in ks] for m, v in days.items()})
    return days


def audit_gaps(conn: sqlite3.Connection, cov: Coverage, date_from=None, date_to=None, max_dates=None) -> dict:
    days = full_market_days(conn, date_from, date_to, max_dates)
    all_days = sorted({d for v in days.values() for d in v})
    out = {"market_days": {m: len(v) for m, v in days.items()}, "gaps": 0, "by_status": {}, "insufficient": [], "retired": 0,
           "per_symbol": {}}
    if not all_days:
        return out
    lo, hi = all_days[0], all_days[-1]
    have = {(s, d) for s, d in conn.execute("SELECT symbol, price_date FROM daily_prices WHERE is_current=1 AND price_date BETWEEN ? AND ?", (lo, hi))}
    secs = conn.execute("SELECT symbol, market, listed_date FROM securities WHERE is_current=1 AND COALESCE(is_common,1)=1").fetchall()
    listing = {e["symbol"]: e for e in LISTING_EVIDENCE}
    curated = {e["symbol"]: e for e in STOP_EVIDENCE}
    status_c: Counter = Counter()
    per_sym: dict[str, Counter] = defaultdict(Counter)
    for sym, mkt, listed in secs:
        for d in days.get(mkt, ()):
            if (sym, d) in have or (listed and listed > d and sym not in listing):
                continue
            ev = listing.get(sym)
            if ev and d < ev["listed_from"]:
                st, reason, src, lvl = "not_listed", ev["reason"], ev["source"], ev["level"]
                note = f"{sym} 當日尚未以此代號在{mkt}上市櫃（{ev['listed_from']} 起）｜查證：{ev['checked']}"
            elif (h := cov.stop_hit(sym, d)) is not None:
                m = re.search(r"證據等級(\w)", h[3])
                st, src, lvl = "halted_official", h[2], (m.group(1) if m else None)
                reason = (curated[sym]["reason"] if sym in curated else h[3][:200])
                note = f"停止買賣區間 [{h[0]}, {h[1] or '持續中'})；無行情列為正常（未成交）"
            elif cov.pause(sym, d):
                st, reason, src, lvl = "halted_official", "官方暫停交易（trading_halts）涵蓋該日", "trading_halts", "A"
                note = "暫停交易日無行情列"
            else:
                st, reason, src, lvl = "insufficient", "資料不足：全市場日缺該股行情列，且查無官方停止買賣／暫停交易／上市櫃日期證據", None, None
                note = "未補造；可能為抓取缺漏、停止買賣未入庫或代號異動，需人工查證"
                out["insufficient"].append(f"{sym} {d}")
            row = {"symbol": sym, "market": mkt, "price_date": d, "status": st, "reason": reason, "evidence_source": src,
                   "evidence_level": lvl, "note": note, "fetched_at": now_iso()}
            upsert_versioned(conn, "price_gaps", {"symbol": sym, "price_date": d}, row, GAP_KEYS)
            status_c[st] += 1
            per_sym[sym][st] += 1
    # 之後已有行情列的缺口（補抓到了）→ 退役，不再是現行缺口
    cur = conn.execute("""UPDATE price_gaps SET is_current=0 WHERE is_current=1 AND price_date BETWEEN ? AND ?
                          AND EXISTS (SELECT 1 FROM daily_prices p WHERE p.symbol=price_gaps.symbol AND p.price_date=price_gaps.price_date AND p.is_current=1)""", (lo, hi))
    out["retired"] = cur.rowcount
    conn.commit()
    out["gaps"] = sum(status_c.values())
    out["by_status"] = dict(status_c)
    out["per_symbol"] = {s: dict(v) for s, v in sorted(per_sym.items())}
    out["insufficient"] = out["insufficient"][:100]
    return out


# ---------- 主流程 ----------
def apply_unified_rule(conn: sqlite3.Connection) -> dict:
    """離線（0 請求）套用停牌統一規則：推定恢復日＋重算 daily_prices 停牌／無成交旗標。daily／evening 在 halts 之後呼叫。"""
    out = {"resume_rule": infer_resume(conn)}
    out["recompute"] = recompute_flags(conn, Coverage(conn), None, None)
    return out


def run_halt_fix(conn: sqlite3.Connection, *, fetch: bool = True, date_from=None, date_to=None, max_dates=None) -> dict:
    out: dict = {"before": snapshot_stats(conn)}
    out["evidence"] = dict(write_stop_evidence(conn))
    out["snapshots"] = fetch_snapshots(conn) if fetch else {"skipped": True, "requests": 0}
    out["resume_rule"] = infer_resume(conn)
    cov = Coverage(conn)
    out["recompute"] = recompute_flags(conn, cov, date_from, date_to)
    out["gaps"] = audit_gaps(conn, cov, date_from, date_to, max_dates)
    out["after"] = snapshot_stats(conn)
    return out


def main_job(*, fetch: bool = True, date_from=None, date_to=None, max_dates=None) -> int:
    from pipeline.db import connect, finish_job, init_db, start_job
    from pipeline.deadline import assert_no_other_pipeline

    assert_no_other_pipeline()
    conn = connect()
    init_db(conn)
    run_id = start_job(conn, "halt_fix", "all", today().isoformat())
    try:
        out = run_halt_fix(conn, fetch=fetch, date_from=date_from, date_to=date_to, max_dates=max_dates)
        snap_err = out["snapshots"].get("errors") or []
        ok = not snap_err and not out["gaps"]["insufficient"] and not out["recompute"]["conflict_count"]
        status = "success" if ok else "partial"
        err = None if ok else json.dumps({"snapshot_errors": snap_err, "insufficient": len(out["gaps"]["insufficient"]),
                                          "conflicts": out["recompute"]["conflict_count"]}, ensure_ascii=False)
        finish_job(conn, run_id, status, [json.dumps(out, ensure_ascii=False)[:6000]], snap_err, error=err)
        log.info("halt_fix %s %s", status, json.dumps(out, ensure_ascii=False)[:3000])
        return 0
    except Exception as e:   # noqa: BLE001
        finish_job(conn, run_id, "failed", [], [str(e)], error=str(e))
        log.exception("halt_fix")
        return 2
    finally:
        conn.close()
