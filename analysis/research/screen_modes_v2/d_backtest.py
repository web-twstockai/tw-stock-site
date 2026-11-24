"""研究用（非正式程式）：依 docs/indicators.md §11.2 計算各候選模式的 5 交易日歷史統計。

- 訊號日 S 收盤後成立 → T1（S 的下一個交易日）開盤進場 → 從 T1 起第 5 個交易日（T5）收盤出場；還原價。
- 扣成本報酬 n＝P5×(1−0.001425−0.003) ÷ [P1×(1+0.001425)] − 1；上漲＝n＞0。
- T1 無有效成交（停牌／無成交／無列）→ 未進場；T1 開盤＝漲停價 → 開盤漲停未進場；兩者都不入分母、另列件數。
- T5 無有效成交 → 延到之後第一個有效交易日收盤，最多再等 20 個交易日；超過或超出資料尾端 → 無法結算。
- 同一檔：前一個訊號起 5 個交易日內（持有期內）的訊號不重複計入（§11.2.6）。
- 母體＝analysis screen()（預設參數）當日 status 為 PASS／FAIL（可評估）的股票；EXCLUDED 一律不發訊號。
- 基準＝同期每天所有可評估股票都當作訊號（不去重）；同日基準＝只取模式有事件的那些訊號日。
- 顯示門檻：N ≥ 100 且 D ≥ 30，否則「樣本不足」。
用法：python d_backtest.py DB ADJ_PANEL PANEL_DIR OUTDIR
"""
import sys, json
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import c_features as CF

FEE, TAX = 0.001425, 0.003
HOLD, MAX_DELAY = 5, 20
B, SEED = 2000, 20261124


def tick(p):
    return np.select([p < 10, p < 50, p < 100, p < 500, p < 1000], [0.01, 0.05, 0.1, 0.5, 1.0], 5.0)


def forward_returns(g, trade_days):
    """對每個 (symbol, 交易日 i) 計算「若 i 為訊號日」的結果：ret（扣成本）、gross、flag。
    flag：ok／no_entry（T1 無有效成交）／limit_up（開盤漲停）／unsettled（無法結算或超出資料尾端）。"""
    syms = sorted(g["symbol"].unique())
    T = len(trade_days)
    di = {d: i for i, d in enumerate(trade_days)}
    si = {s: j for j, s in enumerate(syms)}
    shape = (T, len(syms))
    valid = np.zeros(shape, bool); ao = np.full(shape, np.nan); ac = np.full(shape, np.nan)
    ro = np.full(shape, np.nan)
    r_i = g["date"].map(di).to_numpy(); c_i = g["symbol"].map(si).to_numpy()
    ok = (g["status"] == "ok").to_numpy() & g["adj_open_twd"].notna().to_numpy()
    valid[r_i[ok], c_i[ok]] = True
    ao[r_i, c_i] = g["adj_open_twd"].to_numpy(); ac[r_i, c_i] = g["adj_close_twd"].to_numpy()
    ro[r_i, c_i] = g["open_twd"].to_numpy()
    # 下一個有效日索引（含自己）
    nxt = np.full(shape, T, dtype=np.int64)
    cur = np.full(len(syms), T, dtype=np.int64)
    for i in range(T - 1, -1, -1):
        cur = np.where(valid[i], i, cur); nxt[i] = cur
    ret = np.full(shape, np.nan); gross = np.full(shape, np.nan)
    flag = np.full(shape, "", dtype=object)
    cols = np.arange(len(syms))
    for i in range(T):
        j = i + 1
        if j >= T:
            flag[i] = "unsettled"; continue
        e_ok = valid[j]
        # 漲停：T1 參考價＝S 還原收盤 ÷ T1 還原因子（＝還原收盤 × 原始開盤 / 還原開盤）
        ref = ac[i] * ro[j] / ao[j]
        lim = np.floor(ref * 1.1 / tick(ref * 1.1) + 1e-9) * tick(ref * 1.1)
        lu = e_ok & (ro[j] >= lim - 1e-6)
        k0 = j + HOLD - 1
        if k0 >= T:
            k = np.full(len(syms), T)
        else:
            k = nxt[k0]
        settle = (k < T) & (k <= k0 + MAX_DELAY)
        kk = np.minimum(k, T - 1)
        p5 = ac[kk, cols]; p1 = ao[j]
        n = p5 * (1 - FEE - TAX) / (p1 * (1 + FEE)) - 1
        gr = p5 / p1 - 1
        f = np.where(~e_ok, "no_entry", np.where(lu, "limit_up", np.where(~settle, "unsettled", "ok")))
        flag[i] = f
        ret[i] = np.where(f == "ok", n, np.nan); gross[i] = np.where(f == "ok", gr, np.nan)
    return syms, ret, gross, flag


def dedup(sig_rows, sig_cols):
    """同一檔 5 個交易日內只算第一個訊號（輸入已依 (col,row) 排序）。"""
    keep = np.zeros(len(sig_rows), bool)
    last = {}
    for n, (r, c) in enumerate(zip(sig_rows, sig_cols)):
        lr = last.get(c)
        if lr is None or r >= lr + HOLD:
            keep[n] = True; last[c] = r
    return keep


def boot(day_w, day_n, base_w=None, base_n=None, b=B, seed=SEED):
    rng = np.random.default_rng(seed)
    D = len(day_w)
    idx = rng.integers(0, D, size=(b, D))
    w = day_w[idx].sum(1) / day_n[idx].sum(1)
    lo, hi = np.percentile(w, [2.5, 97.5])
    if base_w is None:
        return lo, hi, None, None
    bw = base_w[idx].sum(1) / base_n[idx].sum(1)
    e = w - bw
    elo, ehi = np.percentile(e, [2.5, 97.5])
    return lo, hi, elo, ehi
