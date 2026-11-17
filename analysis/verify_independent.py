"""獨立重算核對：以「純 Python 迴圈」重新實作篩選器全部邏輯，逐值比對 screener.py 的輸出。

刻意採不同實作：
- 以 sqlite3 標準庫直接查詢、list/dict 結構，不使用 pandas / numpy（含 rolling、shift、遮罩相乘）。
- 不經 analysis/io_sqlite.py（被核對方 screener 走 io_sqlite → pandas；本檔自寫 SQL），連讀取層一併核對。
- 還原因子以「逐列往後掃描事件」雙層迴圈計算；平均逐項加總、最高逐項比較。

浮點容許誤差：數值欄位 math.isclose(rel_tol=1e-9, abs_tol=1e-9)；
狀態、排除原因代碼、四個條件通過與否必須完全相同。

用法：
  analysis/.venv/bin/python analysis/verify_independent.py                  # 合成資料（預設），寫 docs/verification.md
  analysis/.venv/bin/python analysis/verify_independent.py --no-write       # 只印結果
  analysis/.venv/bin/python analysis/verify_independent.py --db data/twstock.db --label 真實資料-30檔  # 真實資料（入庫後）
真實資料結果寫入報告的「真實資料核對」節，與合成資料分開，不混用。
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

REL_TOL = 1e-9
ABS_TOL = 1e-9
NUMERIC_FIELDS = ("close_twd", "change_pct", "volume_shares", "amount_twd", "ma_short", "ma_long",
                  "prior_high_twd", "prior_avg_volume_shares", "volume_ratio")
EXACT_FIELDS = ("status", "exclusion_code", "pass_trend", "pass_breakout", "pass_volume", "pass_turnover",
                "change_check", "coverage_note", "window_gap_days", "window_halt_days", "window_missing_days",
                "window_no_trade_days", "window_bad_data_days", "volume_basis", "window_volume_basis_days")
# 獨立寫一份對映（不 import adjust）
TYPE_ALIAS = {"ex_dividend": "cash_dividend", "ex_rights": "stock_dividend"}
DIVIDEND_TYPES = ("cash_dividend", "stock_dividend", "ex_both")
KINDS6 = ("cash_dividend", "stock_dividend", "ex_both", "capital_reduction", "par_value_change", "stock_split")
COV_ALIAS = {k: (k,) for k in KINDS6}
COV_ALIAS.update({"dividend": DIVIDEND_TYPES, "ex_right_dividend": DIVIDEND_TYPES, "ex_dividend": ("cash_dividend",),
                  "ex_rights": ("stock_dividend",), "capital_reduction_restore": ("capital_reduction",)})
LIMIT_DATE = date(2015, 6, 1)
REPORT_PATH = ROOT / "docs" / "verification.md"


# ---------------- 讀取（sqlite3，純 Python） ----------------
def _d(s: Optional[str]) -> Optional[date]:
    return None if s in (None, "") else date.fromisoformat(str(s)[:10])


def _f(v: Any) -> Optional[float]:
    return None if v is None or v == "" else float(v)


FULL_MIN = 100          # 獨立寫一份：某市場某日現行列數 < 100＝整個市場都缺行情（停牌 insufficient 規則）
COV_ACCEPT = {"all": ("all",), "tracked": ("sample30", "all")}   # 覆蓋表 scope（有 scope 欄才篩選）


def load_db(db_path: Path, as_of: Optional[str] = None, lookback: int = 250, coverage_scope: str = "all",
            symbols: Optional[list] = None) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    with closing(con):
        cal_rows = con.execute("SELECT calendar_date, is_trade_day FROM trading_calendar").fetchall()
        trade_all = sorted(_d(c) for c, t in cal_rows if int(t) == 1)
        non_trade = {_d(c) for c, t in cal_rows if int(t) != 1}
        if as_of is None:
            # 獨立實作的預設評估日（規則同 adjust.AS_OF_RULE）：範圍＝securities 現行普通股（沒有就用行情中的代號），
            # 只看範圍內代號、且是交易日的現行行情日，取最大值
            scope = {r[0] for r in con.execute("SELECT symbol FROM securities WHERE is_current=1 AND is_common=1")}
            if not scope:
                scope = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM daily_prices WHERE is_current=1")}
            tset = set(trade_all)
            cand = [_d(d) for sym, d in con.execute("SELECT symbol, price_date FROM daily_prices WHERE is_current=1")
                    if sym in scope]
            as_of_d = max(d for d in cand if d in tset)
        else:
            as_of_d = _d(as_of)
        i = trade_all.index(as_of_d)
        start = trade_all[max(0, i - lookback + 1)]
        prices = []
        tset_all = set(trade_all)
        traded_all: dict[str, list] = {}     # 全表（不限窗口）評估日以前有成交的交易日：停牌恢復推定用
        mkt_count: dict = {}                 # (market, date) → 評估日以前現行列數（逐列累加）
        pc = [r[1] for r in con.execute("PRAGMA table_info(daily_prices)")]
        chg = "COALESCE(\"change\", change_twd)" if "change" in pc else "change_twd"
        hal = "is_halted" if "is_halted" in pc else "0"
        cno = "change_note" if "change_note" in pc else "NULL"
        nref = "next_ref_price_twd" if "next_ref_price_twd" in pc else "NULL"
        vb_present = "volume_basis" in pc            # 缺欄＝研究模式（不檢查口徑）
        vbc = "volume_basis" if vb_present else "NULL"
        for row in con.execute("SELECT symbol, market, price_date, open_twd, high_twd, low_twd, close_twd, "
                               "volume_shares, amount_twd, " + chg + ", change_sign, " + hal + ", " + cno + ", " + nref +
                               ", " + vbc +
                               " FROM daily_prices WHERE is_current=1"):
            d = _d(row[2])
            if d <= as_of_d:
                mkt_count[(row[1], d)] = mkt_count.get((row[1], d), 0) + 1
            if d <= as_of_d and d in tset_all and row[6] is not None and row[7] is not None and float(row[7]) > 0:
                traded_all.setdefault(row[0], []).append(d)
            if start <= d <= as_of_d:
                prices.append({"symbol": row[0], "market": row[1], "date": d, "open": _f(row[3]), "high": _f(row[4]),
                               "low": _f(row[5]), "close": _f(row[6]), "volume": _f(row[7]), "turnover": _f(row[8]),
                               "chg": _f(row[9]), "sign": (row[10] or "").strip() or None,
                               "halted": row[11] is not None and int(row[11]) == 1,
                               "note": (row[12] or "").strip() or None, "ref": _f(row[13]),
                               "vb": (row[14] or "").strip() or None})
        actions = []
        ac = [r[1] for r in con.execute("PRAGMA table_info(corporate_actions)")]
        opt = [(c if c in ac else "NULL") for c in ("cash_subscription_ratio", "share_exchange_ratio")]
        for row in con.execute("SELECT symbol, action_date, action_type, prev_close_twd, reference_twd, "
                               "dividend_value_twd, stock_dividend_per_thousand, capital_reduction_ratio, "
                               "refund_per_share_twd, " + ", ".join(opt) + " FROM corporate_actions WHERE is_current=1"):
            d = _d(row[1])
            if start <= d <= as_of_d:
                t = row[2].strip()
                actions.append({"symbol": row[0], "date": d, "type": TYPE_ALIAS.get(t, t), "prev": _f(row[3]),
                                "ref": _f(row[4]), "div": _f(row[5]), "spt": _f(row[6]), "ratio": _f(row[7]),
                                "refund": _f(row[8]), "csr": _f(row[9]), "xr": _f(row[10])})
        halts = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_halts'").fetchone():
            hc = [r[1] for r in con.execute("PRAGMA table_info(trading_halts)")]
            q = "SELECT symbol, halt_date, " + ("resume_date" if "resume_date" in hc else "NULL") + ", " + \
                ("market" if "market" in hc else "NULL") + " FROM trading_halts" + \
                (" WHERE is_current=1" if "is_current" in hc else "")
            for sym, h0, r0, m0 in con.execute(q):    # 不讀 resume_basis／resume_date_inferred
                if h0:
                    halts.setdefault(sym.strip(), []).append((_d(h0), _d(r0) if r0 else None, m0))
        secs = {}
        for row in con.execute("SELECT symbol, market, listed_date FROM securities WHERE is_current=1 AND is_common=1"):
            secs[row[0]] = {"market": row[1], "listed": _d(row[2])}
        cov = "coverage_table_missing"
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corporate_actions_coverage'").fetchone():
            ccols = [r[1] for r in con.execute("PRAGMA table_info(corporate_actions_coverage)")]
            q = "SELECT market, action_kind, coverage_start, " + ("coverage_end" if "coverage_end" in ccols else "NULL") + \
                ", " + ("scope" if "scope" in ccols else "'__noscope__'") + \
                " FROM corporate_actions_coverage" + (" WHERE is_current=1" if "is_current" in ccols else "")
            rows0 = con.execute(q).fetchall()
            if "scope" in ccols:
                ok = COV_ACCEPT[coverage_scope]
                if coverage_scope == "tracked":    # 追蹤清單須全部屬 universe_members sample30，否則（或表不存在）只認 all
                    mem = set()
                    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_members'").fetchone():
                        mem = {str(r[0]).strip() for r in con.execute(
                            "SELECT symbol FROM universe_members WHERE universe_id='sample30'")}
                    req = {str(x).strip() for x in (symbols or [])}
                    if not mem or not req or not req <= mem:
                        ok = ("all",)
                rows = [r[:4] for r in rows0 if r[4] is not None and str(r[4]).strip() in ok]
                if rows0 and not rows:
                    rows = None      # 有列但全部不採用：當作無覆蓋（與空表同樣走 strict）
            else:
                rows = [r[:4] for r in rows0]
            if not rows:
                cov = "coverage_table_empty"
            else:
                cov = {}
                for m, k, s0, e0 in rows:
                    if k.strip() in COV_ALIAS and s0:
                        for kk in COV_ALIAS[k.strip()]:
                            cov.setdefault((m, kk), []).append((_d(s0), _d(e0) if e0 else date(9999, 12, 31)))
    trade = [d for d in trade_all if start <= d <= as_of_d]
    # 獨立實作停牌規則（2026-11-17 18:31 核可）：有官方恢復日→[停牌日, 恢復日)；無→停到停牌日後第一個成交日前一天，
    # 評估日以前都沒成交→至今仍停牌。只用 ≤ as_of 的成交（不前視）
    # 0.9.1 insufficient 規則（2026-11-17 19:41 核可）：推定區間（無官方恢復日）內，某市場列數 < FULL_MIN 的交易日＝整個市場都缺行情，
    # 本股當日無資料列時不算停牌；停牌日當天不受影響。只看評估日以前的列數（不前視）
    full_days = {k for k, n in mkt_count.items() if n >= FULL_MIN}
    all_mkts = {m for m, _ in full_days}
    spans = {}
    for sym, lst in halts.items():
        tl = sorted(traded_all.get(sym, []))
        for a, b, m0 in lst:
            if a > as_of_d:
                continue
            holes = set()
            if b is None:
                later = [x for x in tl if x > a]
                b = later[0] if later else None
                for x in trade_all:
                    if a < x <= as_of_d and (b is None or x < b):
                        if m0 is not None:
                            hole = (m0, x) not in full_days
                        else:
                            hole = all((mm, x) not in full_days for mm in (all_mkts or {""}))
                        if hole:
                            holes.add(x)
            spans.setdefault(sym, []).append((a, b, holes))
    return {"trade": trade, "non_trade": non_trade, "as_of": as_of_d, "start": start, "prices": prices,
            "actions": actions, "secs": secs, "cov": cov, "halts": halts, "halt_spans": spans, "vb_present": vb_present}


def halted_on(data, sym, d, has_row=True):
    for a, b, holes in data["halt_spans"].get(sym, []):
        if a <= d and (b is None or d < b):
            if not has_row and d in holes:
                continue
            return True
    return False


def cov_start(cov, market, kind, as_of):
    """回傳 (含 as_of 之連續覆蓋段起日, 問題字串)。逐日走訪合併相鄰區間（獨立於 adjust 的排序合併實作）。"""
    segs = list(cov.get((market, kind)) or [])
    if kind == "stock_split":   # 分割依變更面額辦理：par_value_change 覆蓋列也算（DECISIONS 2026-11-10 03:45）
        segs += list(cov.get((market, "par_value_change")) or [])
    if not segs:
        return None, "coverage_missing:" + str(market) + ":" + kind
    inside = [sg for sg in segs if sg[0] <= as_of <= sg[1]]
    if not inside:
        firsts = min(sg[0] for sg in segs)
        if as_of < firsts:
            return None, "window_before_coverage_start:" + str(market) + ":" + kind + ":" + firsts.isoformat()
        last = max(sg[1] for sg in segs)
        if as_of > last:
            return None, "coverage_end_before_as_of:" + str(market) + ":" + kind + ":" + last.isoformat()
        return None, "coverage_hole:" + str(market) + ":" + kind + ":" + as_of.isoformat()
    start = min(sg[0] for sg in inside)
    changed = True
    while changed:
        changed = False
        for a0, b0 in segs:
            if a0 < start and b0 >= start - timedelta(days=1):
                start = a0
                changed = True
    return start, None


def cov_notes(cov, market, need_from, as_of=None):
    if isinstance(cov, str):
        return [cov]
    as_of = need_from if as_of is None else as_of
    out = []
    for kind in KINDS6:
        s, prob = cov_start(cov, market, kind, as_of)
        if prob:
            out.append(prob)
        elif s > need_from:
            out.append("window_before_coverage_start:" + str(market) + ":" + kind + ":" + s.isoformat())
    return out


def tick(price):
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def _kind(r):
    """獨立實作：valid／bad_volume（有收盤、量 NULL）／no_trade。"""
    if r["close"] is not None and r["volume"] is None:
        return "bad_volume"
    if r["close"] is not None and r["volume"] is not None and r["volume"] > 0:
        return "valid"
    return "no_trade"


def change_check_after_nt(close, chg, sign, note, ref):
    """獨立實作：前一交易日整股無成交時的漲跌核對（DECISIONS 2026-11-10 12:05 ②）。"""
    if (sign is not None and sign.upper() == "X") or (chg is None and note == "無比價"):
        return "after_no_trade"
    if chg is None:
        return "after_no_trade"
    v = chg
    if sign == "-" and v > 0:
        v = -v
    if sign == "+" and v < 0:
        return "source_sign_conflict"
    if ref is not None and ref > 0:
        return "after_no_trade" if abs((close - ref) - v) <= 0.005 + 1e-9 else "mismatch"
    return "after_no_trade"


def _prev_no_trade_row(data, by_date, d):
    tr = data["trade"]
    i = tr.index(d) if d in tr else -1
    if i <= 0:
        return None
    r = by_date.get(tr[i - 1])
    return r if (r is not None and _kind(r) == "no_trade") else None


def change_check(close, base, is_ev, chg, sign):
    """獨立實作的 change 交叉檢查；容許值：非事件日 0.005 元，事件日 半檔+0.005 元。"""
    if chg is None:
        return "no_source"
    if sign is not None and sign.upper() == "X":
        return "source_not_comparable"
    v = chg
    if sign == "-" and v > 0:
        v = -v
    if sign == "+" and v < 0:
        return "source_sign_conflict"
    tol = 0.005 + (tick(base) / 2.0 if is_ev else 0.0)
    return "ok" if abs((close - base) - v) <= tol + 1e-9 else "mismatch"


# ---------------- 獨立重算 ----------------
def _bad(r: dict[str, Any], check_turnover: bool) -> bool:
    vals = [r["open"], r["high"], r["low"], r["close"], r["volume"]]
    for v in vals:
        if v is None:
            return True
    o, h, l, c, v = vals
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or v < 0:
        return True
    if h < l or c < l or c > h or o < l or o > h:
        return True
    if check_turnover and (r["turnover"] is None or r["turnover"] < 0):
        return True
    return False


def recompute(data: dict[str, Any], p: dict[str, Any], universe: list[str]) -> dict[str, dict[str, Any]]:
    trade = data["trade"]
    pos = {}
    for i, d in enumerate(trade):
        pos[d] = i
    by_sym: dict[str, list] = {}
    mkt_days = set()
    for r in data["prices"]:
        by_sym.setdefault(r["symbol"], []).append(r)
        mkt_days.add((r["market"], r["date"]))
    acts: dict[str, list] = {}
    for a in data["actions"]:
        acts.setdefault(a["symbol"], []).append(a)
    need = max(p["ma_long"], p["ma_short"], p["breakout_lookback"] + 1, p["volume_lookback"] + 1)
    out = {}
    for sid in universe:
        rows = sorted(by_sym.get(sid, []), key=lambda x: x["date"])
        out[sid] = _one(sid, rows, sorted(acts.get(sid, []), key=lambda x: x["date"]), data, pos, mkt_days, p, need)
    return out


def ind_resolve_one(a, our_prev, p):
    """回傳 (代碼或 None, f, s)。"""
    if a["prev"] is not None:
        if a["prev"] <= 0 or abs(a["prev"] / our_prev - 1.0) > p["adj_prev_close_tolerance"]:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        basep = a["prev"]
    else:
        basep = our_prev
    if a["type"] in DIVIDEND_TYPES:
        if a["ref"] is None:
            return "EXCL_ADJ_FACTOR_MISSING", None, None
        if a["ref"] <= 0:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        f = a["ref"] / basep
        csr = a.get("csr")
        if csr is not None and csr < 0:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        sub = csr if csr is not None else 0.0
        if a["type"] == "cash_dividend":
            sf = 1.0 + sub
        elif a["spt"] is not None:
            sf = 1.0 + a["spt"] / 1000.0 + sub
        else:
            if sub > 0:
                return "EXCL_ADJ_FACTOR_MISSING", None, None
            div = 0.0 if a["type"] == "stock_dividend" else a["div"]
            if (not p["derive_share_factor"]) or div is None:
                return "EXCL_ADJ_FACTOR_MISSING", None, None
            sf = (basep - div) / a["ref"]
        if sf < 1.0 - 1e-9:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        return None, f, sf
    if a["type"] == "capital_reduction":
        if a["ratio"] is None:
            return "EXCL_ADJ_FACTOR_MISSING", None, None
        if not (0 < a["ratio"] <= 1):
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        ref = a["ref"]
        if ref is None:
            if a["refund"] is None:
                return "EXCL_ADJ_FACTOR_MISSING", None, None
            ref = (basep - a["refund"]) / a["ratio"]
        if ref <= 0:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        return None, ref / basep, a["ratio"]
    if a["type"] in ("par_value_change", "stock_split"):
        x = a.get("xr")   # schema §3.5 share_exchange_ratio（不以減資換股率替代）
        if x is None:
            return "EXCL_ADJ_FACTOR_MISSING", None, None
        if x <= 0:
            return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
        if a["ref"] is not None:
            if a["ref"] <= 0 or abs(a["ref"] * x / basep - 1.0) > p["adj_prev_close_tolerance"]:
                return "EXCL_ADJ_FACTOR_INCONSISTENT", None, None
            return None, a["ref"] / basep, x
        return None, 1.0 / x, x
    return "EXCL_ADJ_FACTOR_MISSING", None, None


def ind_resolve_all(acts, valid, p, after):
    """依日期解析 action_date > after（None＝全部）的事件；事件日前無有效交易日者略過；同日同類重複 → 不一致。"""
    sel = [a for a in acts if after is None or a["date"] > after]
    sel.sort(key=lambda a: a["date"])
    kinds = [("dividend" if a["type"] in DIVIDEND_TYPES else a["type"], a["date"]) for a in sel]
    out = []
    for a, k in zip(sel, kinds):
        our_prev = None
        for r in valid:
            if r["date"] < a["date"]:
                our_prev = r["close"]
        if our_prev is None:
            continue
        if kinds.count(k) > 1:
            out.append((a["date"], "EXCL_ADJ_FACTOR_INCONSISTENT", None, None))
            continue
        code, f, sf = ind_resolve_one(a, our_prev, p)
        out.append((a["date"], code, f, sf))
    return out


def _one(sid, g, acts, data, pos, mkt_days, p, need):
    wx = {}

    def ex(code):
        return {"status": "EXCLUDED", "exclusion_code": code, **wx}

    as_of = data["as_of"]
    sec = data["secs"].get(sid)
    if sec is None:
        return ex("EXCL_NOT_COMMON_STOCK")
    market = sec["market"]
    seen = set()
    for r in g:
        if r["date"] in seen:
            return ex("EXCL_BAD_DATA")
        seen.add(r["date"])
    for r in g:
        if r["date"] not in pos:
            return ex("EXCL_BAD_DATA")
    t = None
    for r in g:
        if r["date"] == as_of:
            t = r
    if t is None:
        if (market, as_of) not in mkt_days:
            return ex("EXCL_NO_DATA")
        return ex("EXCL_SUSPENDED") if halted_on(data, sid, as_of, has_row=False) else ex("EXCL_NO_ROW_SUSPENDED_OR_MISSING")
    vbp = data.get("vb_present", False)
    if vbp:
        wx["volume_basis"] = t["vb"]
    if t["close"] is None or t["volume"] == 0:
        if t["halted"] or halted_on(data, sid, as_of):
            return ex("EXCL_SUSPENDED")
        return ex("EXCL_NO_TRADE")
    if _bad(t, True):
        return ex("EXCL_BAD_DATA")
    if vbp and (p["enable_volume"] or p["enable_turnover"]) and t["vb"] != "incl_block":
        return ex("EXCL_VOLUME_BASIS")      # 獨立實作 §9.2（5a）

    lst = None
    if sec["listed"] is not None and sec["listed"] >= data["trade"][0]:
        lst = 0
        while lst < len(data["trade"]) and data["trade"][lst] < sec["listed"]:
            lst += 1
        if pos[as_of] - lst + 1 < p["min_listing_days"]:
            return ex("EXCL_NEW_LISTING")

    valid = []
    for r in g:
        if r["volume"] is not None and r["volume"] > 0 and r["close"] is not None:
            valid.append(r)
    if len(valid) < need:
        return ex("EXCL_INSUFFICIENT_HISTORY")
    W = valid[len(valid) - need:]
    w0 = W[0]["date"]
    by_date = {r["date"]: r for r in g}
    vd = {r["date"] for r in valid}
    nh = nm = nn = nb = 0
    for d in data["trade"]:
        if w0 <= d <= as_of and d not in vd:
            r = by_date.get(d)
            if r is not None and _kind(r) == "bad_volume":
                nb += 1
            elif (r is not None and r["halted"]) or halted_on(data, sid, d, has_row=r is not None):
                nh += 1
            elif r is None:
                nm += 1
            else:
                nn += 1
    wx.update(window_gap_days=nh + nm + nn + nb, window_halt_days=nh, window_missing_days=nm, window_no_trade_days=nn,
              window_bad_data_days=nb)
    if (pos[as_of] - pos[w0] + 1) - need > p["max_gap_days"]:
        return ex("EXCL_WINDOW_GAPS")
    if nb > 0:
        return ex("EXCL_BAD_DATA")
    for r in W:
        if _bad(r, False):
            return ex("EXCL_BAD_DATA")
    if vbp:                                  # 獨立實作 §9.3 方案 A（9a）
        cnt = 0
        for r in W[len(W) - 1 - p["volume_lookback"]:len(W) - 1]:
            if r["vb"] != "incl_block":
                cnt += 1
        wx["window_volume_basis_days"] = cnt
        if p["enable_volume"] and cnt > 0:
            return ex("EXCL_VOLUME_BASIS")

    notes = cov_notes(data["cov"], market, w0, as_of)
    note = ";".join(notes) if notes else None
    if notes and p["coverage_policy"] == "strict":
        return {"status": "EXCLUDED", "exclusion_code": "EXCL_ADJ_COVERAGE_GAP", "coverage_note": note, **wx}

    events = []
    for (d, code, f, sf) in ind_resolve_all(acts, valid, p, w0):
        if code is not None:
            return ex(code)
        events.append((d, f, sf))

    ac, ah, av = [], [], []
    for r in W:
        F = 1.0
        S = 1.0
        for (d, f, s) in events:
            if d > r["date"]:
                F = F * f
                S = S * s
        ac.append(r["close"] * F)
        ah.append(r["high"] * F)
        av.append(r["volume"] * S)

    if p["check_unexplained_gap"]:
        for i in range(1, len(W)):
            lim = p["legacy_max_daily_move"] if W[i]["date"] < LIMIT_DATE else p["max_daily_move"]
            if abs(ac[i] / ac[i - 1] - 1.0) > lim:
                if lst is not None and pos[W[i]["date"]] - lst + 1 <= p["new_listing_no_limit_days"]:
                    continue
                return ex("EXCL_UNEXPLAINED_GAP")

    n = len(W)
    tot = 0.0
    for i in range(n - p["ma_short"], n):
        tot += ac[i]
    ma_s = tot / p["ma_short"]
    tot = 0.0
    for i in range(n - p["ma_long"], n):
        tot += ac[i]
    ma_l = tot / p["ma_long"]
    ph = None
    for i in range(n - 1 - p["breakout_lookback"], n - 1):
        if ph is None or ah[i] > ph:
            ph = ah[i]
    tot = 0.0
    for i in range(n - 1 - p["volume_lookback"], n - 1):
        tot += av[i]
    pav = tot / p["volume_lookback"]
    close, vol, to = t["close"], t["volume"], t["turnover"]
    base = W[n - 2]["close"]
    is_ev = False
    for (d, f, s) in events:
        if W[n - 2]["date"] < d <= as_of:
            base = base * f
            is_ev = True
    chk = change_check(close, base, is_ev, t["chg"], t["sign"])
    pr = _prev_no_trade_row(data, by_date, as_of)
    if pr is not None and not is_ev:
        chk = change_check_after_nt(close, t["chg"], t["sign"], t.get("note"), pr.get("ref"))
    c1 = close > ma_s and ma_s > ma_l
    c2 = close > ph
    c3 = vol >= pav * p["volume_multiplier"]
    c4 = to >= p["min_turnover"]
    ok = True
    for en, c in ((p["enable_trend"], c1), (p["enable_breakout"], c2), (p["enable_volume"], c3), (p["enable_turnover"], c4)):
        if en and not c:
            ok = False
    return {"status": "PASS" if ok else "FAIL", "exclusion_code": None, "close_twd": close,
            "change_pct": (close / base - 1.0) * 100.0, "change_check": chk, "coverage_note": note, **wx, "volume_shares": vol, "amount_twd": to,
            "ma_short": ma_s, "ma_long": ma_l, "prior_high_twd": ph, "prior_avg_volume_shares": pav,
            "volume_ratio": vol / pav, "pass_trend": c1, "pass_breakout": c2, "pass_volume": c3, "pass_turnover": c4}


# ---------------- 序列（均線、漲跌幅）獨立重算 ----------------
def _sym_rows(data, sym):
    g = sorted([r for r in data["prices"] if r["symbol"] == sym], key=lambda r: r["date"])
    valid = [r for r in g if r["volume"] is not None and r["volume"] > 0 and r["close"] is not None]
    acts = [a for a in data["actions"] if a["symbol"] == sym]
    sec = data["secs"].get(sym)
    market = sec["market"] if sec else (g[-1]["market"] if g else None)
    lst = None
    if sec and sec["listed"] is not None and sec["listed"] >= data["trade"][0]:
        lst = 0
        while data["trade"][lst] < sec["listed"]:
            lst += 1
    return g, valid, acts, market, lst


def ind_ma_series(data, sym, windows, p):
    g, valid, acts, market, lst = _sym_rows(data, sym)
    pos = {d: i for i, d in enumerate(data["trade"])}
    cands = []
    all_bad = False
    cov = data["cov"]
    if isinstance(cov, str):
        all_bad = True
    else:
        starts = []
        for kind in KINDS6:
            st0, prob = cov_start(cov, market, kind, data["as_of"])
            if prob:
                all_bad = True
            else:
                starts.append(st0)
        if not all_bad and valid and valid[0]["date"] < max(starts):
            cands.append(max(starts))
    res = ind_resolve_all(acts, valid, p, None)
    if not all_bad:
        for (d, code, f, sf) in res:
            if code is not None:
                cands.append(d)
        ok = [(d, f, sf) for (d, code, f, sf) in res if code is None]
        lo = max(cands) if cands else None
        v = [r for r in valid if lo is None or r["date"] >= lo]
        if p["check_unexplained_gap"]:
            adj = []
            for r in v:
                F = 1.0
                for (d, f, sf) in ok:
                    if d > r["date"]:
                        F *= f
                adj.append(r["close"] * F)
            for i in range(1, len(v)):
                lim = p["legacy_max_daily_move"] if v[i]["date"] < LIMIT_DATE else p["max_daily_move"]
                if abs(adj[i] / adj[i - 1] - 1.0) > lim:
                    if lst is not None and pos[v[i]["date"]] - lst + 1 <= p["new_listing_no_limit_days"]:
                        continue
                    cands.append(v[i]["date"])
    bd = max(cands) if cands else None
    ok = [(d, f, sf) for (d, code, f, sf) in res if code is None and (bd is None or d > bd)]
    out = {}
    adjs = []
    for i, r in enumerate(valid):
        usable = (not all_bad) and (bd is None or r["date"] >= bd)
        if usable:
            F = 1.0
            for (d, f, sf) in ok:
                if d > r["date"]:
                    F *= f
            adjs.append(r["close"] * F)
        else:
            adjs.append(None)
        row = {"status": "ok" if usable else "unavailable", "adj_close_twd": adjs[-1]}
        for w in windows:
            if i + 1 < w:
                row[f"ma_{w}"] = None
                continue
            win = adjs[i - w + 1:i + 1]
            if any(x is None for x in win):
                row[f"ma_{w}"] = None
            else:
                tot = 0.0
                for x in win:
                    tot += x
                row[f"ma_{w}"] = tot / w
        out[r["date"]] = row
    return out


def ind_change_series(data, sym, p):
    g, valid, acts, market, lst = _sym_rows(data, sym)
    res = ind_resolve_all(acts, valid, p, None)
    out = {}
    by_date = {x["date"]: x for x in g}
    for i in range(1, len(valid)):
        r, pv = valid[i], valid[i - 1]
        between = [x for x in res if pv["date"] < x[0] <= r["date"]]
        if any(x[1] is not None for x in between):
            out[r["date"]] = {"status": "unavailable"}
            continue
        base = pv["close"]
        for x in between:
            base = base * x[2]
        chk = change_check(r["close"], base, len(between) > 0, r["chg"], r["sign"])
        pr = _prev_no_trade_row(data, by_date, r["date"])
        if pr is not None and not between:
            chk = change_check_after_nt(r["close"], r["chg"], r["sign"], r.get("note"), pr.get("ref"))
        out[r["date"]] = {"status": "ok", "change_pct": (r["close"] / base - 1.0) * 100.0, "change_check": chk}
    return out


def run_series_comparison(db_path: Path, n_dates: int = 3, windows=(20, 60)) -> dict[str, Any]:
    import io_sqlite
    import screener
    import series

    full = load_db(db_path)
    syms = sorted({r["symbol"] for r in full["prices"]})
    stats = {"rows": 0, "values": 0, "mismatches": [], "dates": full["trade"][-n_dates:], "symbols": len(syms)}
    for d in stats["dates"]:
        ds = d.isoformat()
        data = load_db(db_path, as_of=ds)
        for label, overrides in (("預設參數", {}), ("不推算股數因子", {"derive_share_factor": False})):
            p = screener.ScreenParams(**overrides)
            pd_ = p.to_dict()
            for sym in syms:
                kw = io_sqlite.load_symbol_inputs(db_path, sym, as_of=ds)
                ms = series.ma_series(**kw, windows=windows, params=p)
                mine = ind_ma_series(data, sym, windows, pd_)
                for r in ms.to_dict("records"):
                    dd = r["price_date"].date()
                    if r["status"] == "no_trade":
                        if dd in mine:
                            stats["mismatches"].append((label, ds, sym, str(dd), "no_trade", "有成交"))
                        continue
                    b = mine.get(dd)
                    stats["rows"] += 1
                    if b is None:
                        stats["mismatches"].append((label, ds, sym, str(dd), "列", "缺"))
                        continue
                    for f in ("status", "adj_close_twd", *[f"ma_{w}" for w in windows]):
                        stats["values"] += 1
                        va, vb = _norm(r.get(f)), b.get(f)
                        same = (va == vb) if (f == "status" or va is None or vb is None) else \
                            math.isclose(float(va), float(vb), rel_tol=REL_TOL, abs_tol=ABS_TOL)
                        if not same:
                            stats["mismatches"].append((label, ds, sym, f"{dd} {f}", va, vb))
                cs = series.change_series(**kw, params=p)
                mc = ind_change_series(data, sym, pd_)
                for r in cs.to_dict("records"):
                    if r["status"] == "first_row":
                        continue
                    dd = r["price_date"].date()
                    b = mc.get(dd, {})
                    stats["rows"] += 1
                    for f in ("status", "change_pct", "change_check"):
                        if f != "status" and b.get("status") != "ok":
                            continue
                        stats["values"] += 1
                        va, vb = _norm(r.get(f)), b.get(f)
                        same = (va == vb) if (f != "change_pct" or va is None or vb is None) else \
                            math.isclose(float(va), float(vb), rel_tol=REL_TOL, abs_tol=ABS_TOL)
                        if not same:
                            stats["mismatches"].append((label, ds, sym, f"{dd} {f}", va, vb))
    return stats


# ---------------- 比對 ----------------
def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


PARAM_SETS = {
    "預設參數": {},
    "替代參數A（MA10/30、回看10日、量×2、金額5千萬）": dict(
        ma_short=10, ma_long=30, breakout_lookback=10, volume_lookback=10,
        volume_multiplier=2.0, min_turnover=50_000_000.0),
    "替代參數B（coverage_policy=gap_check、不推算股數因子、容許停牌8日）": dict(
        coverage_policy="gap_check", derive_share_factor=False, max_gap_days=8),
    "替代參數C（不檢查跳空）": dict(check_unexplained_gap=False),
}


def run_comparison(db_path: Path, n_dates: int = 20, coverage_scope: str = "all",
                   symbols: Optional[list] = None) -> dict[str, Any]:
    import io_sqlite
    import screener

    full = load_db(db_path)
    dates = full["trade"][-n_dates:]
    stats = {"param_sets": list(PARAM_SETS), "values": 0, "rows": 0, "mismatches": [], "dates": dates,
             "status_counts": {}}
    for d in dates:
        ds = d.isoformat()
        inputs = io_sqlite.load_screen_inputs(db_path, as_of=ds, symbols=symbols, coverage_scope=coverage_scope)
        mine_data = load_db(db_path, as_of=ds, coverage_scope=coverage_scope, symbols=symbols)
        universe = sorted({r["symbol"] for r in mine_data["prices"]} | set(mine_data["secs"]))
        if symbols is not None:
            universe = sorted(set(symbols))
        for label, overrides in PARAM_SETS.items():
            params = screener.ScreenParams(**overrides)
            res = screener.screen(**inputs.screen_kwargs(), params=params, universe=universe)
            mine = recompute(mine_data, params.to_dict(), universe)
            summ = {r["symbol"]: r for r in res.summary.to_dict("records")}
            for sid in universe:
                a, b = summ.get(sid, {}), mine[sid]
                stats["rows"] += 1
                key = (label, b["status"])
                stats["status_counts"][key] = stats["status_counts"].get(key, 0) + 1
                for f in EXACT_FIELDS:
                    if b["status"] == "EXCLUDED" and f not in b:
                        continue
                    stats["values"] += 1
                    if _norm(a.get(f)) != _norm(b.get(f)):
                        stats["mismatches"].append((label, ds, sid, f, _norm(a.get(f)), _norm(b.get(f))))
                if b["status"] == "EXCLUDED":
                    continue
                for f in NUMERIC_FIELDS:
                    stats["values"] += 1
                    va, vb = _norm(a.get(f)), _norm(b.get(f))
                    if va is None or vb is None or not math.isclose(float(va), float(vb), rel_tol=REL_TOL, abs_tol=ABS_TOL):
                        stats["mismatches"].append((label, ds, sid, f, va, vb))
    return stats


def render_section(stats: dict[str, Any], source_desc: str) -> list[str]:
    lines = [
        f"- 樣本來源：{source_desc}",
        "- 被核對實作：`analysis/io_sqlite.py` → `analysis/screener.py`（pandas 向量化與 rolling）",
        "- 獨立實作：`analysis/verify_independent.py`（sqlite3 自寫查詢、純 Python 迴圈，不用 pandas/numpy、不經 io_sqlite）",
        f"- 評估日：交易日曆最後 {len(stats['dates'])} 個交易日（{stats['dates'][0]}～{stats['dates'][-1]}）",
        f"- 參數組：{'；'.join(stats['param_sets'])}",
        f"- 浮點容許誤差：數值欄位 `math.isclose(rel_tol={REL_TOL:g}, abs_tol={ABS_TOL:g})`；狀態、排除代碼、四個條件通過與否須完全一致。",
        "",
        "| 項目 | 數量 |", "|---|---|",
        f"| 比對（股票×評估日×參數組）筆數 | {stats['rows']} |",
        f"| 逐值比對數 | {stats['values']} |",
        f"| 不一致數 | {len(stats['mismatches'])} |",
        "", "| 參數組 | 狀態（獨立實作） | 筆數 |", "|---|---|---|",
    ]
    for (label, st), cnt in sorted(stats["status_counts"].items()):
        lines.append(f"| {label} | {st} | {cnt} |")
    if stats["mismatches"]:
        lines += ["", "不一致明細（最多 50 筆）：", "", "| 參數組 | 評估日 | 代號 | 欄位 | screener | 獨立重算 |",
                  "|---|---|---|---|---|---|"]
        for m in stats["mismatches"][:50]:
            lines.append("| " + " | ".join(str(x) for x in m) + " |")
    return lines


def render_series_section(st: dict[str, Any]) -> list[str]:
    lines = [
        "", "### 均線序列與漲跌幅序列核對", "",
        "- 被核對實作：`analysis/io_sqlite.load_symbol_inputs` → `analysis/series.ma_series`（MA20、MA60）與 `series.change_series`",
        "- 獨立實作：`verify_independent.ind_ma_series`／`ind_change_series`（純 Python；可信邊界＝覆蓋起點、因子失敗事件日、"
        "最近一次無法解釋跳空日三者最晚者，邊界前整段標 unavailable）",
        f"- 評估日：最後 {len(st['dates'])} 個交易日（{st['dates'][0]}～{st['dates'][-1]}），每個評估日對全部 {st['symbols']} 個代號"
        "各產生整段序列；參數組：預設參數；不推算股數因子",
        "- 比對欄位：均線＝status、adj_close_twd、ma_20、ma_60（逐日）；漲跌幅＝status、change_pct、change_check（逐日，首列除外）",
        "",
        "| 項目 | 數量 |", "|---|---|",
        f"| 比對（序列列）筆數 | {st['rows']} |",
        f"| 逐值比對數 | {st['values']} |",
        f"| 不一致數 | {len(st['mismatches'])} |",
    ]
    if st["mismatches"]:
        lines += ["", "| 參數組 | 評估日 | 代號 | 欄位 | series | 獨立重算 |", "|---|---|---|---|---|---|"]
        for m in st["mismatches"][:50]:
            lines.append("| " + " | ".join(str(x) for x in m) + " |")
    return lines


def render_report(synth_lines: list[str], test_summary: str, real_lines: Optional[list[str]] = None) -> str:
    now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 篩選器獨立重算核對紀錄", "",
        "> ⚠️ **第一節為「測試用合成資料」核對，數字不是正式選股結果，不得引用為市場資料。**" +
        ("第二節為真實資料快照的程式一致性核對，資料尚未完整（見該節說明），亦非正式選股結果。" if real_lines
         else "尚無任何真實行情核對。"), "",
        f"- 最後更新：{now}（Asia/Taipei，UTC+8）",
        "- 輸入介面：已對齊 `docs/schema.md`（2026-11-10 03:14 檔案版本）與 DECISIONS 2026-11-10 03:45：daily_prices（含 change／change_twd／change_sign，可選 is_halted）／corporate_actions（含 cash_subscription_ratio／share_exchange_ratio，缺欄可容錯）"
        "／corporate_actions_coverage（market, action_kind, coverage_start, coverage_end…，可多段，六類 action_kind；stock_split 由 par_value_change 覆蓋）／trading_halts（可選，schema.md §3.5b）"
        "／trading_calendar／securities，只取 is_current=1。"
        "正式結果覆蓋政策一律 strict；參數組 B 的 gap_check 只為驗證程式分支，不是正式結果。",
        "", "## 一、測試用合成資料核對", "",
        *synth_lines,
    ]
    if test_summary:
        lines += ["", "### 單元測試（pytest，測試用合成資料）", "", test_summary]
    lines += ["", "## 二、真實資料核對", ""]
    lines += real_lines if real_lines else [
        "尚未執行：`data/twstock.db` 尚未建立、真實行情尚未入庫。入庫後以 "
        "`verify_independent.py --db data/twstock.db --label …` 執行，結果寫於本節，與合成資料分開。"]
    return "\n".join(lines) + "\n"


STALE_NOTE = ("> ⚠️ 本節未隨合成資料核對重跑（重跑真實資料節：`verify_independent.py --db …`）；"
              "內容為本節所記時間的快照與當時版本程式所得，不反映之後的程式變更。")


def _keep_real_section() -> Optional[list[str]]:
    """合成資料模式：保留既有真實資料節（加註未重跑），沒有則回傳 None。"""
    if not REPORT_PATH.exists():
        return None
    m = re.search(r"\n## 二、真實資料核對\n\n(.*?)(?=\n## |\Z)", REPORT_PATH.read_text(encoding="utf-8"), re.S)
    if not m or m.group(1).startswith("尚未執行"):
        return None
    body = [ln for ln in m.group(1).rstrip("\n").split("\n")]
    while body and (body[0].startswith("> ⚠️ 本節未隨") or body[0] == ""):
        body.pop(0)
    return [STALE_NOTE, ""] + body


def _other_sections() -> str:
    """保留 verification.md 中「一、」「二、」以外的節（例如「三、月營收」，由 verify_revenue.py 維護），原樣附在後面。"""
    if not REPORT_PATH.exists():
        return ""
    text = REPORT_PATH.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", text)
    keep = [p for p in parts if p.startswith("## ") and not p.startswith(("## 一、", "## 二、"))]
    return "".join("\n" + p.rstrip("\n") + "\n" for p in keep)


def main(argv: Optional[list[str]] = None) -> int:
    import synthetic_data

    ap = argparse.ArgumentParser(description="篩選器獨立重算核對")
    ap.add_argument("--db", type=Path, default=None, help="真實資料庫路徑；未給則使用測試用合成資料")
    ap.add_argument("--label", default="", help="真實資料樣本說明（例如 30 檔驗證樣本）")
    ap.add_argument("--dates", type=int, default=20)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--test-summary", default="", help="附加於報告的 pytest 結果文字")
    a = ap.parse_args(argv)

    if a.db is None:
        with tempfile.TemporaryDirectory() as td:
            db = synthetic_data.build_sqlite(Path(td) / "synthetic.db")
            stats = run_comparison(db, a.dates)
            sst = run_series_comparison(db, min(a.dates, 3))
        desc = ("測試用合成資料：`analysis/tests/synthetic_data.py` 依 docs/schema.md DDL 建立的暫存 SQLite"
                "（同內容 CSV 在 `analysis/tests/fixtures/`；代號 TEST01～TEST33 皆為虛構）")
        section = render_section(stats, desc) + render_series_section(sst)
        real = _keep_real_section()
    else:
        stats = run_comparison(a.db, a.dates)
        sst = run_series_comparison(a.db, min(a.dates, 3))
        section = None
        real = render_section(stats, f"真實資料 `{a.db}`（{a.label or '未標示'}）") + render_series_section(sst)
    print(f"比對筆數 {stats['rows']}，逐值 {stats['values']}，不一致 {len(stats['mismatches'])}")
    print(f"序列比對 {sst['rows']}，逐值 {sst['values']}，不一致 {len(sst['mismatches'])}")
    for m in sst["mismatches"][:20]:
        print("  序列不一致：", m)
    for m in stats["mismatches"][:20]:
        print("  不一致：", m)
    if not a.no_write:
        if section is None:
            # 真實資料模式：保留既有合成資料節，只替換真實資料節
            old = REPORT_PATH.read_text(encoding="utf-8") if REPORT_PATH.exists() else ""
            m = re.search(r"## 一、測試用合成資料核對\n\n(.*?)\n## 二、", old, re.S)
            section = m.group(1).rstrip("\n").split("\n") if m else ["（尚無合成資料核對紀錄）"]
            # 合成資料節（含單元測試小節）原樣保留
            report = render_report(section, "", real)
        else:
            report = render_report(section, a.test_summary, real)
        report = report.rstrip("\n") + "\n" + _other_sections()
        REPORT_PATH.write_text(report, encoding="utf-8")
        print(f"已寫入 {REPORT_PATH}")
    return 1 if (stats["mismatches"] or sst["mismatches"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
