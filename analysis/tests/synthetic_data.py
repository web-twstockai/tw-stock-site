"""測試用合成資料產生器（非真實行情，不得混入正式結果）。

以確定性公式（無亂數）產生與 docs/schema.md 欄位一致的表：trading_calendar、daily_prices、
corporate_actions、securities、actions_coverage（corporate_actions_coverage），另加 trading_halts
（DDL 依 pipeline/migrate.sql）與待遷移欄位 daily_prices.is_halted、corporate_actions.cash_subscription_ratio。

- 代號一律以 "TEST" 開頭、名稱以「測試用合成-」開頭、source 為 synthetic://測試用合成資料。
- 交易日曆：2026-06-01 起，週末與 2026-06-19（合成假日）非交易日，共 90 個交易日。
- 停牌：交易日無行情列（有 trading_halts 紀錄者為確認停牌）或有列但 is_halted=1；無成交以「有列但價格 NULL、量 0」表示。
- 執行 `python tests/synthetic_data.py` 會重寫 tests/fixtures/*.csv。
- build_sqlite() 以 docs/schema.md 的 DDL 建立合成 SQLite（測試 loader 用）。
"""

from __future__ import annotations

import csv
import math
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURE_DIR = HERE / "fixtures"
SCHEMA_MD = HERE.parent.parent / "docs" / "schema.md"
DATA_LABEL = "測試用合成資料（非真實行情，不得混入正式結果）"
SOURCE = "synthetic://測試用合成資料"
FETCHED_AT = "2026-11-10T02:00:00+08:00"
N_DAYS = 90
HOLIDAY = date(2026, 6, 19)

PRICE_COLS = ["symbol", "market", "price_date", "open_twd", "high_twd", "low_twd", "close_twd",
              "change_twd", "change_sign", "volume_shares", "amount_twd", "transactions", "avg_twd",
              "pe_ratio", "raw_volume", "raw_amount", "source", "announced_at", "announced_at_note",
              "fetched_at", "unit", "source_unit", "unit_conversion", "version", "is_current", "checksum"]
ACTION_COLS = ["symbol", "market", "action_date", "action_type", "ex_type_raw", "prev_close_twd",
               "reference_twd", "rights_value_twd", "dividend_value_twd", "rights_dividend_sum_twd",
               "cash_dividend_twd", "stock_dividend_per_thousand", "capital_reduction_ratio",
               "refund_per_share_twd", "limit_up_twd", "limit_down_twd", "auction_base_twd", "source",
               "announced_at", "announced_at_note", "fetched_at", "unit", "source_unit", "unit_conversion",
               "version", "is_current", "checksum"]
CAL_COLS = ["calendar_date", "is_trade_day", "name", "description", "source_kind", "source",
            "announced_at", "announced_at_note", "fetched_at", "unit", "version"]
SEC_COLS = ["symbol", "market", "name", "name_full", "isin", "cfi_code", "industry", "industry_code",
            "listed_date", "is_common", "exclude_reason", "data_date", "source", "announced_at",
            "announced_at_note", "fetched_at", "unit", "source_unit", "unit_conversion", "version",
            "is_current", "checksum"]
COVERAGE_COLS = ["market", "action_kind", "coverage_start"]
# 資料庫已遷移或待遷移、但 docs/schema.md DDL 尚未列的欄位：fixture 帶上，build_sqlite 以 ALTER TABLE 補欄
PRICE_EXTRA = ["is_halted"]
ACTION_EXTRA = ["cash_subscription_ratio", "share_exchange_ratio"]
# trading_halts：DDL 同 docs/schema.md §3.5b（不在 §4 DDL 區塊內，故另列）
HALT_COLS = ["market", "symbol", "halt_date", "halt_time", "resume_date", "resume_time", "halt_type", "source",
             "announced_at", "announced_at_note", "fetched_at", "version", "is_current", "checksum"]
HALTS_DDL = """CREATE TABLE IF NOT EXISTS trading_halts (
  market TEXT NOT NULL, symbol TEXT NOT NULL, halt_date TEXT NOT NULL, halt_time TEXT, resume_date TEXT,
  resume_time TEXT, halt_type TEXT, source TEXT NOT NULL, announced_at TEXT, announced_at_note TEXT,
  fetched_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, is_current INTEGER NOT NULL DEFAULT 1, checksum TEXT,
  PRIMARY KEY(market, symbol, halt_date, halt_time, version));"""


# ---------------- 交易日曆 ----------------
def calendar_rows() -> list[dict[str, Any]]:
    rows, d, n = [], date(2026, 6, 1), 0
    while n < N_DAYS:
        trade = d.weekday() < 5 and d != HOLIDAY
        rows.append({"calendar_date": d.isoformat(), "is_trade_day": int(trade),
                     "name": "合成假日" if d == HOLIDAY else None,
                     "description": DATA_LABEL, "source_kind": "weekend_rule" if d.weekday() >= 5 else "twse_holiday",
                     "source": SOURCE, "fetched_at": FETCHED_AT, "unit": "n/a", "version": 1})
        n += trade
        d += timedelta(days=1)
    return rows


def trade_days() -> list[date]:
    return [date.fromisoformat(r["calendar_date"]) for r in calendar_rows() if r["is_trade_day"]]


# ---------------- 行情產生（內部列） ----------------
def _closes(n, start, drift, amp=0.01, phase=0.0):
    return [start * (1 + drift) ** i * (1 + amp * math.sin(i * 0.7 + phase)) for i in range(n)]


def _vols(n, base, amp=0.2, phase=0.0):
    return [int(base * (1 + amp * math.sin(i * 1.3 + phase))) for i in range(n)]


def _set_bar(r, o, c, v, turnover=None):
    o, c = round(o, 2), round(c, 2)
    h, lo = round(max(o, c) * 1.005, 2), round(min(o, c) * 0.995, 2)
    r.update(open=o, high=max(h, o, c), low=min(lo, o, c), close=c, volume=int(v), state="trade")
    r["turnover"] = int(round(v * (r["high"] + r["low"]) / 2)) if turnover is None else int(turnover)


def _blank(r, state):
    """state = 'suspended'（輸出時刪列）、'no_trade'（價格 NULL、量 0）或 'halted_row'（同 no_trade 但 is_halted=1）。"""
    r.update(open=None, high=None, low=None, close=None, volume=0, turnover=0, state=state)


def _build(D, closes, vols, start=0, ref_open=None):
    rows, prev = [], None
    for i in range(start, len(D)):
        r = {"date": D[i], "f": None, "s": None}
        o = (ref_open or {}).get(i, prev if prev is not None else closes[i])
        _set_bar(r, o, closes[i], vols[i])
        prev = r["close"]
        rows.append(r)
    return rows


def _adjusted_prior(rows, k=20):
    """以內部因子 f/s 還原後，最後一列之前 k 個有效交易日的最高價與平均量（產生情境用）。"""
    valid = [r for r in rows[:-1] if r["state"] == "trade"][-k:]
    ev = [r for r in rows if r["f"] is not None]
    hs, vs = [], []
    for r in valid:
        f = s = 1.0
        for e in ev:
            if e["date"] > r["date"]:
                f *= e["f"]
                s *= e["s"]
        hs.append(r["high"] * f)
        vs.append(r["volume"] * s)
    return max(hs), sum(vs) / len(vs)


def _breakout_last(rows, close_mult=1.03, vol_mult=3.0):
    ph, pv = _adjusted_prior(rows)
    prev = [r for r in rows[:-1] if r["state"] == "trade"][-1]
    _set_bar(rows[-1], prev["close"], ph * close_mult, int(pv * vol_mult) + 1)


def _scenarios():
    """回傳 list of (symbol, name, market, in_securities, listed_date, rows, actions)。"""
    D = trade_days()
    n = len(D)
    out = []

    def add(sid, label, rows, actions=(), market="TWSE", in_sec=True, listed=None, halts=()):
        """halts：[(停牌起日索引, 恢復日索引或 None)] → trading_halts 列。"""
        out.append((sid, f"測試用合成-{label}", market, in_sec, listed, rows, list(actions),
                    [(D[a], D[b] if b is not None else None) for a, b in halts]))

    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _breakout_last(r)
    add("TEST01", "全部通過", r)
    r = _build(D, _closes(n, 100, 0.003, phase=1.0), _vols(n, 2_000_000)); _breakout_last(r, vol_mult=1.2)
    add("TEST02", "量能不足", r)
    r = _build(D, _closes(n, 10, 0.003), _vols(n, 300_000)); _breakout_last(r)
    add("TEST03", "金額不足", r)
    add("TEST04", "空頭趨勢", _build(D, _closes(n, 100, -0.003), _vols(n, 2_000_000)))

    # TEST05 視窗內除息（第 82 日），還原後才是突破
    k = 82
    base = _closes(n, 100, 0.003)
    prev = round(base[k - 1], 2)
    div = round(prev * 0.08, 2)
    ref = round(prev - div, 2)
    f = ref / prev
    r = _build(D, base[:k] + [x * f for x in base[k:]], _vols(n, 2_000_000), ref_open={k: ref})
    r[k].update(f=f, s=1.0)
    _breakout_last(r)
    add("TEST05", "視窗內除息", r, [dict(action_date=D[k], action_type="ex_dividend", ex_type_raw="息",
                                        prev_close_twd=prev, reference_twd=ref, dividend_value_twd=div,
                                        cash_dividend_twd=div, rights_dividend_sum_twd=div)])

    # TEST06 評估日當天除息：漲跌幅以參考價為基準
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000))
    prev = r[-2]["close"]
    ref = round(prev - 3.0, 2)
    _set_bar(r[-1], ref, ref * 1.01, 2_000_000)
    r[-1].update(f=ref / prev, s=1.0)
    add("TEST06", "當日除息", r, [dict(action_date=D[-1], action_type="ex_dividend", prev_close_twd=prev,
                                      reference_twd=ref, dividend_value_twd=3.0, cash_dividend_twd=3.0)])

    # TEST07 減資 50%（彌補虧損，退還股款 0 元由來源明示；僅預告表資料：無參考價）；第 72 日起停止買賣 2 日，第 74 日恢復
    k0, k = 72, 74
    base = _closes(n, 30, 0.003)
    vb = _vols(n, 6_000_000)
    cl = base[:k] + [x * 2.0 for x in base[k:]]
    vv = vb[:k] + [v // 2 for v in vb[k:]]
    r = _build(D, cl, vv)
    for i in (72, 73):
        _blank(r[i], "suspended")
    _set_bar(r[k], r[71]["close"] * 2.0, cl[k], vv[k])
    for i in range(k + 1, n):
        _set_bar(r[i], r[i - 1]["close"], cl[i], vv[i])
    r[k0].update(f=2.0, s=0.5)
    _breakout_last(r)
    add("TEST07", "減資", r, [dict(action_date=D[k0], action_type="capital_reduction", capital_reduction_ratio=0.5,
                                  refund_per_share_twd=0.0)], halts=[(72, 74)])

    # TEST08 除息但缺參考價
    k = 80
    base = _closes(n, 100, 0.003)
    r = _build(D, base[:k] + [x * 0.95 for x in base[k:]], _vols(n, 2_000_000))
    add("TEST08", "缺參考價", r, [dict(action_date=D[k], action_type="ex_dividend", prev_close_twd=r[k - 1]["close"])])

    # TEST09 事件類型 unknown
    r = _build(D, base[:k] + [x / 1.1 for x in base[k:]], _vols(n, 2_000_000))
    add("TEST09", "事件類型未知", r, [dict(action_date=D[k], action_type="unknown", prev_close_twd=r[k - 1]["close"],
                                          reference_twd=round(r[k - 1]["close"] / 1.1, 2))])

    # TEST10 無事件卻跳空 +25%
    k = 77
    add("TEST10", "無法解釋跳空", _build(D, base[:k] + [x * 1.25 for x in base[k:]], _vols(n, 2_000_000)))

    # TEST11 評估日停牌（交易日無列）
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _blank(r[-1], "suspended")
    add("TEST11", "當日無資料列", r)
    # TEST12 評估日無成交
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _blank(r[-1], "no_trade")
    add("TEST12", "當日無成交", r)
    # TEST13 歷史不足（只有最後 40 日、無上市日）
    add("TEST13", "歷史不足", _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000), start=n - 40))

    # TEST14 新上市（上市後第 30 個交易日）
    k = n - 30
    base = _closes(n, 50, 0.003)
    add("TEST14", "新上市", _build(D, base[:k + 1] + [x * 1.2 for x in base[k + 1:]], _vols(n, 2_000_000), start=k),
        listed=D[k])
    # TEST15 上市第 63 日；上市第 5 日 +25%（無漲跌幅限制期間）
    k = n - 63
    j = k + 4
    add("TEST15", "上市初期跳空", _build(D, base[:j] + [x * 1.25 for x in base[j:]], _vols(n, 2_000_000), start=k),
        listed=D[k])

    # TEST16 除權（無償配股 10%），來源缺每仟股配股數 → 股數因子由參考價推算 1.1
    k = 80
    base = _closes(n, 100, 0.003)
    vb = _vols(n, 2_000_000)
    r0 = _build(D, base, vb)
    prev = r0[k - 1]["close"]
    ref = prev / 1.1
    r = _build(D, base[:k] + [x / 1.1 for x in base[k:]], vb[:k] + [int(v * 1.1) for v in vb[k:]], ref_open={k: ref})
    r[k].update(f=ref / r[k - 1]["close"], s=1.1)
    _breakout_last(r)
    add("TEST16", "除權推算股數因子", r, [dict(action_date=D[k], action_type="ex_rights", ex_type_raw="權",
                                             prev_close_twd=r[k - 1]["close"], reference_twd=ref)])

    # TEST17 視窗內停牌 8 日
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000))
    for i in range(70, 78):
        _blank(r[i], "suspended")
    add("TEST17", "長期停牌", r)

    # TEST18 ETF（不在 securities）
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _breakout_last(r)
    add("TEST18", "ETF", r, in_sec=False)

    # TEST19 量與金額恰好等於門檻
    r = _build(D, _closes(n, 50, 0.003), [1_000_000] * n)
    ph, _ = _adjusted_prior(r)
    _set_bar(r[-1], r[-2]["close"], ph * 1.03, 1_500_000, turnover=100_000_000)
    add("TEST19", "門檻邊界", r)

    # TEST20 收盤恰好等於前 20 日最高
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000))
    ph, pv = _adjusted_prior(r)
    o = r[-2]["close"]
    r[-1].update(open=o, high=ph, low=round(min(o, ph) * 0.995, 2), close=ph, volume=int(pv * 3))
    r[-1]["turnover"] = int(round(r[-1]["volume"] * ph))
    add("TEST20", "等於前高", r)

    # TEST21 視窗內 1 日無成交（容許）
    r = _build(D, _closes(n, 100, 0.003, phase=2.0), _vols(n, 2_000_000))
    _blank(r[85], "no_trade")
    _set_bar(r[86], r[84]["close"], r[86]["close"], r[86]["volume"])
    _breakout_last(r)
    add("TEST21", "歷史含無成交日", r)

    # TEST22 評估日資料異常（最高 < 最低）
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000))
    r[-1]["high"], r[-1]["low"] = r[-1]["low"] - 1, r[-1]["high"] + 1
    add("TEST22", "資料異常", r)

    # TEST23 來源前收與行情前收不一致（差 5%）
    k = 82
    base = _closes(n, 100, 0.003)
    r = _build(D, base, _vols(n, 2_000_000))
    prev = r[k - 1]["close"]
    add("TEST23", "事件前收不一致", r, [dict(action_date=D[k], action_type="ex_dividend",
                                            prev_close_twd=round(prev * 1.05, 2), reference_twd=round(prev * 1.05 - 1, 2))])

    # TEST24 上櫃股：上櫃減資覆蓋缺（目前無來源）
    r = _build(D, _closes(n, 100, 0.003, phase=0.5), _vols(n, 2_000_000)); _breakout_last(r)
    add("TEST24", "上櫃覆蓋不足", r, market="TPEx")

    # TEST25 減資 20% 並退還股款 2 元（有官方參考價），停止買賣 3 日
    k0, k = 75, 78
    base = _closes(n, 50, 0.003)
    vb = _vols(n, 3_000_000)
    r = _build(D, base, vb)
    prev = r[k0 - 1]["close"]
    ref = round((prev - 2.0) / 0.8, 4)
    f = ref / prev
    cl = base[:k] + [x * f for x in base[k:]]
    vv = vb[:k] + [int(v * 0.8) for v in vb[k:]]
    r = _build(D, cl, vv)
    for i in range(k0, k):
        _blank(r[i], "suspended")
    _set_bar(r[k], ref, cl[k], vv[k])
    for i in range(k + 1, n):
        _set_bar(r[i], r[i - 1]["close"], cl[i], vv[i])
    r[k0].update(f=f, s=0.8)
    _breakout_last(r)
    add("TEST25", "減資退還股款", r, [dict(action_date=D[k0], action_type="capital_reduction", prev_close_twd=prev,
                                          reference_twd=ref, capital_reduction_ratio=0.8, refund_per_share_twd=2.0)])

    # TEST26 評估日停牌，trading_halts 有紀錄（無資料列）
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _blank(r[-1], "suspended")
    add("TEST26", "確認停牌", r, halts=[(n - 1, None)])
    # TEST27 評估日有列但 is_halted=1
    r = _build(D, _closes(n, 100, 0.003), _vols(n, 2_000_000)); _blank(r[-1], "halted_row")
    add("TEST27", "停牌旗標", r)

    # TEST28 股票分割 1 股換 2 股（第 80 日），有參考價；換股比例在 share_exchange_ratio（schema §3.5）
    k = 80
    base = _closes(n, 200, 0.003)
    vb = _vols(n, 1_000_000)
    prev = round(base[k - 1], 2)
    ref = round(prev / 2.0, 2)
    f = ref / prev
    r = _build(D, base[:k] + [x * f for x in base[k:]], vb[:k] + [v * 2 for v in vb[k:]], ref_open={k: ref})
    r[k].update(f=f, s=2.0)
    _breakout_last(r)
    add("TEST28", "股票分割", r, [dict(action_date=D[k], action_type="stock_split", prev_close_twd=prev,
                                      reference_twd=ref, share_exchange_ratio=2.0)])

    # TEST29 面額變更但缺換股比例 → 因子缺漏
    k = 80
    base = _closes(n, 100, 0.003)
    r = _build(D, base[:k] + [x * 0.1 for x in base[k:]], _vols(n, 2_000_000))
    add("TEST29", "面額變更缺比例", r, [dict(action_date=D[k], action_type="par_value_change")])

    # TEST30 除權（新名稱 stock_dividend），缺每仟股配股且來源現增配股率 > 0 → 無法推算股數因子
    k = 82
    base = _closes(n, 50, 0.003)
    prev = round(base[k - 1], 2)
    ref = round(prev / 1.1, 2)
    r = _build(D, base[:k] + [x * ref / prev for x in base[k:]], _vols(n, 2_000_000))
    add("TEST30", "除權含現增缺配股", r, [dict(action_date=D[k], action_type="stock_dividend", prev_close_twd=prev,
                                            reference_twd=ref, cash_subscription_ratio=0.05)])

    # TEST31 除權息（ex_both）：每仟股配 100 股、現金股利 1 元、現增配股率 0.05（認購價 40 元）
    k = 82
    base = _closes(n, 60, 0.003)
    vb = _vols(n, 3_000_000)
    prev = round(base[k - 1], 2)
    ref = round((prev - 1.0 + 40.0 * 0.05) / (1 + 0.1 + 0.05), 2)
    f, sf = ref / prev, 1.15
    r = _build(D, base[:k] + [x * f for x in base[k:]], vb[:k] + [int(v * sf) for v in vb[k:]], ref_open={k: ref})
    r[k].update(f=f, s=sf)
    _breakout_last(r)
    add("TEST31", "除權息含現增", r, [dict(action_date=D[k], action_type="ex_both", prev_close_twd=prev,
                                         reference_twd=ref, dividend_value_twd=1.0, cash_dividend_twd=1.0,
                                         stock_dividend_per_thousand=100.0, cash_subscription_ratio=0.05)])

    # TEST32 變更面額 10 元→2.5 元（1 舊股換 4 新股，第 78 日），有參考價（TWTB8U 形式）
    k = 78
    base = _closes(n, 400, 0.003)
    vb = _vols(n, 500_000)
    prev = round(base[k - 1], 1)
    ref = round(prev / 4.0, 2)
    f = ref / prev
    r = _build(D, base[:k] + [x * f for x in base[k:]], vb[:k] + [v * 4 for v in vb[k:]], ref_open={k: ref})
    r[k].update(f=f, s=4.0)
    _breakout_last(r)
    add("TEST32", "變更面額有參考價", r, [dict(action_date=D[k], action_type="par_value_change", prev_close_twd=prev,
                                            reference_twd=ref, share_exchange_ratio=4.0)])

    # TEST33 變更面額 1 舊股換 2 新股，來源無參考價 → f＝1÷換股比例
    k = 78
    base = _closes(n, 120, 0.003)
    vb = _vols(n, 1_500_000)
    r = _build(D, base[:k] + [x * 0.5 for x in base[k:]], vb[:k] + [v * 2 for v in vb[k:]],
               ref_open={k: round(base[k - 1] * 0.5, 2)})   # 行情開盤落在新面額價位；事件本身不給參考價
    r[k].update(f=0.5, s=2.0)
    _breakout_last(r)
    add("TEST33", "變更面額無參考價", r, [dict(action_date=D[k], action_type="par_value_change",
                                            share_exchange_ratio=2.0)])
    return out


# 各情境在預設參數、評估日為最後一日、universe＝全部代號時的預期結果
EXPECTED_DEFAULT = {
    "TEST01": ("PASS", None), "TEST02": ("FAIL", None), "TEST03": ("FAIL", None), "TEST04": ("FAIL", None),
    "TEST05": ("PASS", None), "TEST07": ("PASS", None),
    "TEST08": ("EXCLUDED", "EXCL_ADJ_FACTOR_MISSING"), "TEST09": ("EXCLUDED", "EXCL_ADJ_FACTOR_MISSING"),
    "TEST10": ("EXCLUDED", "EXCL_UNEXPLAINED_GAP"), "TEST11": ("EXCLUDED", "EXCL_NO_ROW_SUSPENDED_OR_MISSING"),
    "TEST12": ("EXCLUDED", "EXCL_NO_TRADE"), "TEST13": ("EXCLUDED", "EXCL_INSUFFICIENT_HISTORY"),
    "TEST14": ("EXCLUDED", "EXCL_NEW_LISTING"), "TEST16": ("PASS", None),
    "TEST17": ("EXCLUDED", "EXCL_WINDOW_GAPS"), "TEST18": ("EXCLUDED", "EXCL_NOT_COMMON_STOCK"),
    "TEST19": ("PASS", None), "TEST20": ("FAIL", None), "TEST21": ("PASS", None),
    "TEST22": ("EXCLUDED", "EXCL_BAD_DATA"), "TEST23": ("EXCLUDED", "EXCL_ADJ_FACTOR_INCONSISTENT"),
    "TEST24": ("EXCLUDED", "EXCL_ADJ_COVERAGE_GAP"), "TEST25": ("PASS", None),
    "TEST26": ("EXCLUDED", "EXCL_SUSPENDED"), "TEST27": ("EXCLUDED", "EXCL_SUSPENDED"),
    "TEST28": ("PASS", None), "TEST29": ("EXCLUDED", "EXCL_ADJ_FACTOR_MISSING"),
    "TEST30": ("EXCLUDED", "EXCL_ADJ_FACTOR_MISSING"), "TEST31": ("PASS", None),
    "TEST32": ("PASS", None), "TEST33": ("PASS", None),
}


def _tick(price):
    for lim, t in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < lim:
            return t
    return 5.0


def _source_changes(rows):
    """模擬證交所「漲跌價差」：非事件日＝收盤−前一有效收盤；事件日＝收盤−開盤競價基準（參考價取最近檔位）。"""
    out, prev = {}, None
    events = [r for r in rows if r["f"] is not None]
    for r in rows:
        if r["state"] != "trade":
            continue
        if prev is None:
            out[r["date"]] = (None, None)
        else:
            base, ev = prev["close"], False
            for e in events:
                if prev["date"] < e["date"] <= r["date"]:
                    base *= e["f"]
                    ev = True
            if ev:
                t = _tick(base)
                base = round(round(base / t) * t, 2)
            ch = round(r["close"] - base, 2)
            out[r["date"]] = (ch, "+" if ch > 0 else ("-" if ch < 0 else ""))
        prev = r
    return out


# ---------------- 轉成 schema 表格 ----------------
def _meta(version=1, is_current=1):
    return {"source": SOURCE, "fetched_at": FETCHED_AT, "unit": "volume=股;amount=新台幣元",
            "announced_at_note": DATA_LABEL, "version": version, "is_current": is_current}


def tables() -> dict[str, list[dict[str, Any]]]:
    prices, actions, secs, halts = [], [], [], []
    for sid, name, market, in_sec, listed, rows, acts, hl in _scenarios():
        for a, b in hl:
            halts.append({"market": market, "symbol": sid, "halt_date": a.isoformat(),
                          "resume_date": b.isoformat() if b else None, "halt_type": "測試用合成停牌",
                          "source": SOURCE, "fetched_at": FETCHED_AT, "announced_at_note": DATA_LABEL,
                          "version": 1, "is_current": 1})
        changes = _source_changes(rows)
        for r in rows:
            if r["state"] == "suspended":
                continue
            ch, sign = changes.get(r["date"], (None, None))
            if sid == "TEST04" and r is rows[-1]:
                ch = round(ch + 0.5, 2)  # 刻意與自算不一致，測試交叉檢查只標記不覆蓋
            prices.append({"symbol": sid, "market": market, "price_date": r["date"].isoformat(),
                           "open_twd": r["open"], "high_twd": r["high"], "low_twd": r["low"],
                           "close_twd": r["close"], "change_twd": ch, "change_sign": sign,
                           "volume_shares": r["volume"], "amount_twd": r["turnover"],
                           "is_halted": 1 if r["state"] == "halted_row" else 0, **_meta()})
        for a in acts:
            a = dict(a)
            a["action_date"] = a["action_date"].isoformat()
            actions.append({"symbol": sid, "market": market, **a, **_meta()})
        if in_sec:
            secs.append({"symbol": sid, "market": market, "name": name, "cfi_code": "ESVUFR",
                         "listed_date": listed.isoformat() if listed else None, "is_common": 1,
                         "data_date": trade_days()[-1].isoformat(), **_meta()})
    # 舊版本列（is_current=0），loader／screener 必須忽略
    last = [p for p in prices if p["symbol"] == "TEST01"][-1]
    stale = dict(last, close_twd=1.0, low_twd=1.0, **{k: v for k, v in _meta(version=1, is_current=0).items()})
    last.update(version=2)
    prices.append(stale)
    first = trade_days()[0].isoformat()
    # 上市用 schema 現行相容值（ex_right_dividend＝息、權、權息三類；capital_reduction_restore），上櫃用完整清單名稱
    coverage = [{"market": "TWSE", "action_kind": k, "coverage_start": first}
                for k in ("ex_right_dividend", "capital_reduction_restore", "par_value_change", "stock_split")]
    coverage += [{"market": "TPEx", "action_kind": k, "coverage_start": first}
                 for k in ("cash_dividend", "stock_dividend", "ex_both", "par_value_change", "stock_split")]
    # TPEx capital_reduction 刻意缺（測試 coverage_missing）
    return {"trading_calendar": calendar_rows(), "daily_prices": prices, "corporate_actions": actions,
            "securities": secs, "actions_coverage": coverage, "trading_halts": halts}


_COLS = {"trading_calendar": CAL_COLS, "daily_prices": PRICE_COLS + PRICE_EXTRA,
         "corporate_actions": ACTION_COLS + ACTION_EXTRA, "securities": SEC_COLS, "actions_coverage": COVERAGE_COLS,
         "trading_halts": HALT_COLS}


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def write_fixtures(directory: Path = FIXTURE_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, rows in tables().items():
        path = directory / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(_COLS[name])
            for r in rows:
                w.writerow([_fmt(r.get(c)) for c in _COLS[name]])
        paths.append(path)
    return paths


PIPELINE_SCHEMA_SQL = HERE.parent.parent / "pipeline" / "schema.sql"
REQUIRED_TABLES = ("daily_prices", "corporate_actions", "trading_calendar", "securities", "monthly_revenues")
_DDL_SOURCE = {"path": None, "note": ""}


def _ddl_from_schema_md() -> str | None:
    text = SCHEMA_MD.read_text(encoding="utf-8")
    sec = re.search(r"^## 4\. DDL.*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    m = re.search(r"```sql\n(.*?)```", sec.group(1) if sec else "", re.S)
    return m.group(1) if m else None


def _has_tables(ddl: str) -> list[str]:
    return [t for t in REQUIRED_TABLES if not re.search(rf"CREATE TABLE IF NOT EXISTS {t}\s*\(", ddl)]


def schema_ddl() -> str:
    """測試資料庫 DDL（唯讀取用，不修改來源檔）。
    優先取 docs/schema.md「## 4. DDL」節的第一個完整 sql 區塊（其他節如 §3.5b 也有 sql 區塊，故限定第 4 節）；
    若該區塊不存在、未閉合或缺必要表（例如 schema.md 改寫中被截斷），改用資料工程的可執行 DDL pipeline/schema.sql，
    並記在 schema_ddl_source()。兩者都不完整時拋錯，不自行補 DDL。"""
    ddl = _ddl_from_schema_md()
    if ddl is not None and not _has_tables(ddl):
        _DDL_SOURCE.update(path="docs/schema.md §4", note="")
        return ddl
    why = "找不到 §4 完整 sql 區塊" if ddl is None else f"§4 缺表 {_has_tables(ddl)}"
    if PIPELINE_SCHEMA_SQL.exists():
        alt = PIPELINE_SCHEMA_SQL.read_text(encoding="utf-8")
        if not _has_tables(alt):
            _DDL_SOURCE.update(path="pipeline/schema.sql", note=f"docs/schema.md {why}，改用 pipeline/schema.sql")
            return alt
    raise RuntimeError(f"docs/schema.md {why}，pipeline/schema.sql 也不完整")


def schema_ddl_source() -> dict:
    """最近一次 schema_ddl() 實際使用的 DDL 來源（供報告標示）。"""
    return dict(_DDL_SOURCE)


def build_sqlite(path: Path, with_coverage_table: bool = True, coverage_rows: list | None = None,
                 with_halts_table: bool = True, with_extra_columns: bool = True) -> Path:
    """建立合成 SQLite（DDL 取自 docs/schema.md）；actions_coverage 寫入 corporate_actions_coverage。
    with_halts_table＝False 模擬 trading_halts 不存在；with_extra_columns＝False 模擬尚未遷移 is_halted／cash_subscription_ratio。"""
    path = Path(path)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.executescript(schema_ddl())
        t = tables()
        extras = {"daily_prices": PRICE_EXTRA, "corporate_actions": ACTION_EXTRA}
        for tbl, ex in extras.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({tbl})")}
            for c in ex:
                if with_extra_columns and c not in have:
                    con.execute(f"ALTER TABLE {tbl} ADD COLUMN {c} {'INTEGER' if c == 'is_halted' else 'REAL'}")
        if not with_halts_table:
            con.execute("DROP TABLE IF EXISTS trading_halts")   # DDL 來源（pipeline/schema.sql 06:54 起）已含此表；模擬「表不存在」
        else:
            con.executescript(HALTS_DDL)
            con.executemany(f"INSERT INTO trading_halts ({','.join(HALT_COLS)}) VALUES ({','.join('?' * len(HALT_COLS))})",
                            [[r.get(c) for c in HALT_COLS] for r in t["trading_halts"]])
        for name in ("trading_calendar", "daily_prices", "corporate_actions", "securities"):
            if not with_extra_columns and name in extras:
                cols = [c for c in _COLS[name] if c not in extras[name]]
                con.executemany(f"INSERT INTO {name} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                [[r.get(c) for c in cols] for r in t[name]])
                continue
            cols = _COLS[name]
            con.executemany(f"INSERT INTO {name} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [[r.get(c) for c in cols] for r in t[name]])
        if not with_coverage_table:
            con.execute("DROP TABLE IF EXISTS corporate_actions_coverage")   # 模擬「表不存在」
        else:
            # 形狀依 docs/schema.md DDL：(market, action_kind, coverage_start, coverage_end NOT NULL, source, fetched_at, notes)
            con.execute("CREATE TABLE IF NOT EXISTS corporate_actions_coverage (market TEXT NOT NULL, "
                        "action_kind TEXT NOT NULL, coverage_start TEXT NOT NULL, coverage_end TEXT NOT NULL, "
                        "source TEXT NOT NULL, fetched_at TEXT NOT NULL, notes TEXT, "
                        "PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source))")
            cal_end = calendar_rows()[-1]["calendar_date"]
            con.executemany("INSERT INTO corporate_actions_coverage (market, action_kind, coverage_start, coverage_end, "
                            "source, fetched_at, notes) VALUES (?,?,?,?,?,?,?)",
                            [[r[c] for c in COVERAGE_COLS] + [r.get("coverage_end") or cal_end, "測試用合成資料",
                                                              "2026-11-09T22:00:00+08:00", "測試用合成資料"] for r in
                             (t["actions_coverage"] if coverage_rows is None else coverage_rows)])
        con.commit()
    finally:
        con.close()
    return path


if __name__ == "__main__":
    for pth in write_fixtures():
        print(f"已寫入 {pth}（{DATA_LABEL}）")
