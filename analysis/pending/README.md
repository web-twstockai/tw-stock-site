# 待合入：整股無成交三項（DECISIONS 2026-11-10 12:05）

本目錄只放 patch 與說明，**沒有任何 .py 檔**，套件不會 import、pytest 不會收集。尚未套用到正式程式。

- patch：`analysis/pending/no_trade_decisions.patch`（sha256 `e8c9ce6d933e62b981ab87c32744143d192cf33658874ba639019c11c9b553ca`）
- 實作與測試的暫存目錄：`/workspace/scratch/analysis-pending/`（`base/`＝12:02 主目錄原樣、`work/`＝修改後；patch＝兩者 diff）
- 暫存目錄全套 pytest：240 passed（原 226 項＋新增 `tests/test_no_trade_decisions.py` 14 項）
- 版本號：**未升版**（仍 analysis-0.8.0）；USAGE 變更紀錄列標為「待合入」，合入時由負責人定版號

## 內容
1. 有收盤價但成交量 NULL：評估日與視窗內都算資料錯誤（`EXCL_BAD_DATA`）；視窗內仍計入缺口合計，另拆新欄 `window_bad_data_days`。
2. 前一交易日整股無成交：`change_check=after_no_trade`（不算 mismatch）；來源 X／`change_note=無比價` 顯示「無比價」；有 `daily_prices.next_ref_price_twd`（TPEx 次日參考價）時才比對，不一致為 `mismatch`。
3. `is_no_trade` 只做交叉檢查，寫在新說明欄 `data_note`，不改結果碼。
改動檔：adjust.py、io_sqlite.py、screener.py、series.py、verify_independent.py、writer.py、tables_draft.md、USAGE.md、tests/test_screener.py、新增 tests/test_no_trade_decisions.py、docs/indicators.md §8。

## 合入前提（全部滿足才合入）
1. 當日 18:00、22:00 排程已跑完，且沒有排程正在執行。
2. 資料工程的停牌誤標修正已到位（`pipeline/reclassify_prices.py`），`next_ref_price_twd` 已回填（12:18 唯讀快照中該欄全為 NULL）。
3. 正式庫 `screen_results` 已由資料工程遷移：補欄 `window_bad_data_days INTEGER`、`data_note TEXT`；`change_check` 的 CHECK 加入 `'after_no_trade'`（SQLite 需重建表）。**未遷移時 writer 會整批拒寫並報錯**（不會寫入半批）。
4. 網站 `web/app/models.py` 的 `CHANGE_CHECK_LABELS`／`NOTES`／`STATE` 補 `after_no_trade`（否則顯示原始代碼），並可顯示 `data_note`。

## 合入步驟（在 /workspace/tw-stock-site）
1. 試套：`patch -p1 --dry-run < analysis/pending/no_trade_decisions.patch`（12:18 已對主目錄試套成功）。
2. 套用：`patch -p1 < analysis/pending/no_trade_decisions.patch`。
3. 升版：改 `analysis/version.py` 與 `analysis/USAGE.md` 開頭版本號；把 USAGE 變更紀錄「待合入（未升版；合入時由負責人定版號）」改成新版號。
4. 測試：`cd analysis && .venv/bin/python -m pytest -q`，須全過。
5. 重驗（唯讀快照）：`python analysis/verify_independent.py`（主程式與獨立實作 0 不一致），並以 `/workspace/scratch/analysis-pending/real_check.py`、`real_series_check.py` 比對（12:1x 快照結果：screener status／排除碼 0 變化；change_series 490 筆改為 after_no_trade，其中「無比價」36 筆）。
6. 更新 docs/verification.md 的重驗紀錄。

還原：`patch -R -p1 < analysis/pending/no_trade_decisions.patch`。
