# screen_results 遷移清單（analysis-0.9.0 合入前，由資料工程執行）

> 2026-11-17 16:3x（Asia/Taipei，UTC+8）撰寫。依據：DECISIONS 2026-11-10 12:05（無成交三項）、docs/indicators.md §9（負責人 2026-11-17 16:09 核可，方案 A）。
> 分析模組**不寫**正式庫；本清單給資料工程在 `pipeline/db.py migrate()`（或一次性作業）實作，正式庫只由 GitHub Actions 寫入。
> 欄位定義的單一來源：`analysis/tables_draft.md`（套用 `analysis_0.9.0.patch` 後的版本）。

## 1. 現況（2026-11-17 16:05 唯讀快照 `twstock_snap_1117.db`）
- `screen_results` 58 欄；四個新欄**都沒有**；`change_check` 的 CHECK 為 `('ok','mismatch','no_source','source_not_comparable','source_sign_conflict')`，**沒有** `after_no_trade`。
- 現有 270 列，`analysis_version` 最大值 `analysis-0.8.0`。
- 索引：`ux_screen_results_current`、`ix_screen_results_day`、`ix_screen_results_symbol`、`ix_screen_results_excl`（重建表時要一併重建）。
- `daily_prices` 也還沒有 `volume_basis`／`block_*` 四欄（`pipeline/schema.sql` 已有，正式庫待 migrate）。

## 2. 新增欄位（四欄，皆可為 NULL，舊列保持 NULL）

| 欄位 | 型別 | CHECK | 意義 | 來源 |
|---|---|---|---|---|
| `window_bad_data_days` | INTEGER | — | 觀察窗內「有收盤價但成交量 NULL」的日數（資料錯誤；已計入 `window_gap_days` 合計） | DECISIONS 2026-11-10 12:05 ① |
| `data_note` | TEXT | — | 說明欄：`after_no_trade` 比對說明、「無比價」、`is_no_trade` 交叉檢查；不改結果碼 | 同上 ②③ |
| `volume_basis` | TEXT | `CHECK (volume_basis IN ('incl_block','excl_block'))`（NULL 允許） | 評估日 `daily_prices.volume_basis` 原值；NULL＝口徑不明或沒算到評估日列 | §9 |
| `window_volume_basis_days` | INTEGER | — | 均量窗口（評估日前 `volume_lookback` 個有效交易日）內非 `incl_block` 的日數；沒算到窗口時為 NULL | §9 方案 A |

SQLite 可直接 `ALTER TABLE screen_results ADD COLUMN …`（帶 CHECK 的欄也可以），但因第 3 節必須重建表，建議**一次重建**時一併加入。

## 3. CHECK 變更：`change_check` 加 `after_no_trade`
```
change_check TEXT CHECK (change_check IN ('ok', 'mismatch', 'no_source', 'source_not_comparable', 'source_sign_conflict', 'after_no_trade'))
```
SQLite 不能修改既有 CHECK，必須重建表：
1. 在交易內 `CREATE TABLE screen_results_new (…)`，DDL 取自套用後的 `analysis/tables_draft.md`（**保留** `CHECK (symbol NOT LIKE 'TEST%')` 以及正式庫已有、草稿也有的 `screen_run_id` 等欄）；
2. `INSERT INTO screen_results_new (舊欄清單) SELECT 舊欄清單 FROM screen_results;`（四個新欄留 NULL，不回填、不推算）；
3. `DROP TABLE screen_results; ALTER TABLE screen_results_new RENAME TO screen_results;`
4. 重建第 1 節列出的四個索引；`PRAGMA foreign_key_check`、`PRAGMA integrity_check`；列數須仍為遷移前列數。
冪等：已有 `after_no_trade` 與四欄時跳過。

## 4. writer 的保護（不必另做，但要知道）
- `writer.write_screen_results` 會先檢查 `change_check` 的 CHECK；沒有 `after_no_trade` 時**整批拒寫**並報錯（一列都不寫）。
- 輸入行情缺 `daily_prices.volume_basis` 欄（`volume_basis_status=volume_basis_column_missing`）時**整批拒寫**。
- 目標表缺上述新欄時，writer 只寫存在的欄並在 `WriteReport.dropped_columns` 列出；遷移完成後應為空清單。

## 5. 遷移後檢查
- `PRAGMA table_info(screen_results)` 含四欄；`sqlite_master.sql` 的 change_check CHECK 含 `after_no_trade`。
- `daily_prices` 有 `volume_basis`，且 10 檔上櫃股的現行列 `source_table='tradingStock'` 的口徑為 `excl_block`、dailyQuotes 為 `incl_block`（回填結果由資料工程確認）。
