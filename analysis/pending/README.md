# 待合入：analysis-0.9.0（無成交三項＋量額口徑 §9）→ analysis-0.9.1（停牌規則＋全市場摘要；2026-11-17 19:41 重做：insufficient＋覆蓋表 scope）

本目錄只放 patch 與說明，**沒有任何 .py 檔**，套件不會 import、pytest 不會收集。**尚未套用到正式程式**（主目錄仍為 analysis-0.8.0）。

> **完整套用順序（在專案根目錄 `patch -p1`，2026-11-17 20:1x 在全新副本實測全部成功）**：
> 1. `data/pending/pipeline_090.patch`（sha256 `5fab1201…`；docs/sources.md 一個 hunk 偏移 3 行，屬正常）
> 2. `analysis/pending/analysis_0.9.0.patch`
> 3. `analysis/pending/analysis_0.9.1.patch`（**重做版**，sha256 `385267099d0bbae7f89895d57b774142a93331957f8850014e356a95c482586e`）
> 4. `data/pending/pipeline_phase3a_market.patch`
> 5. `data/pending/pipeline_phase3b.patch`
> 6. `data/pending/pipeline_phase3b_fix1.patch`（sha256 `60fed1b7c76bdc48cac15aba42f421909e9c889a1eafc7442ce680659492758c`）
>
> 0.9.1 的基底是「主目錄 analysis/＋0.9.0 patch」，不能單獨套在 0.8.0 上。`no_trade_decisions.patch` 不要套。`analysis_0.9.1_v1.patch` 是備援（見下），**不要和重做版一起套**。

| 檔案 | 說明 |
|---|---|
| `analysis_0.9.1.patch` | **重做版（負責人 2026-11-17 19:41 核可）**，第二個套用（必須在已套 0.9.0 的樹上套）。v1 全部內容（停牌規則、全市場摘要）＋資料不足（insufficient）規則＋覆蓋表 scope。版本仍為 analysis-0.9.1。sha256 `385267099d0bbae7f89895d57b774142a93331957f8850014e356a95c482586e` |
| `analysis_0.9.1_v1.patch` | **備援**：18:4x 的第一版 0.9.1（內容未改，只改檔名）。重做版出問題時，可改套這個（同樣套在 0.9.0 上，二選一）。sha256 `daeb140a135deece0968ac66a7b5768765ba461766162e44935a693fbc2cb066` |
| `analysis_0.9.0.patch` | **第一個套用**。相對主目錄 2026-11-17 16:10 的 `analysis/`（`patch -p1`，在專案根目錄執行）。sha256 `1e6d45e5f2d02e44306281ef69ccc02be2f230972394c577c24c5835efeb3e36` |
| `screen_results_migration_0.9.0.md` | 正式庫 `screen_results` 遷移清單（四欄＋change_check CHECK），由資料工程執行 |
| `no_trade_decisions.patch` | **已併入 0.9.0、勿單獨套用**（2026-11-10 的舊 patch，與現行主目錄有衝突；內容已手動合入 `analysis_0.9.0.patch`）。保留僅供追溯 |

- 暫存實作目錄：`/workspace/scratch/analysis-090/`（`base/`＝主目錄 16:10 原樣、`work/`＝修改後；patch＝兩者 diff）。
- 測試：暫存目錄全套 pytest **254 passed**（0.8.0 主目錄 226 項＋無成交 14 項＋量額口徑 14 項）；DDL 來源為 `pipeline/schema.sql`（sha256 `4aa7e38a…0766`，已含 `block_shares`／`block_amount_twd`／`block_source`／`volume_basis`；`docs/schema.md` §4 缺表，自動改用 schema.sql）。另把 patch 套到主目錄全新副本再跑一次，同為 254 passed。

## 內容
1. 無成交三項（DECISIONS 2026-11-10 12:05）：成交量 NULL 有收盤價＝`EXCL_BAD_DATA`（新欄 `window_bad_data_days`）；前一交易日整股無成交＝`change_check=after_no_trade`（有 `next_ref_price_twd` 才比對）；`is_no_trade` 只在 `data_note` 交叉檢查。
2. 量額口徑（docs/indicators.md §9，2026-11-17 16:09 核可，方案 A）：只讀 `daily_prices.volume_basis`（不讀 `block_*`）；只有 `incl_block` 算總量；新排除碼 `EXCL_VOLUME_BASIS`（5a 評估日、9a 均量窗口任一日）；新欄 `volume_basis`、`window_volume_basis_days`；缺欄＝研究模式照算、writer 拒寫；`ohlc_series` 加 `volume_basis` 標示；`verify_independent` 獨立實作。
3. `version.py`＝`analysis-0.9.0`；USAGE.md 0.9.0 變更紀錄、排除碼表、刪除「上櫃不含鉅額」舊文字；tables_draft.md 新欄與 CHECK。
改動檔：adjust.py、io_sqlite.py、screener.py、series.py、verify_independent.py、writer.py、version.py、USAGE.md、tables_draft.md、tests/synthetic_data.py、tests/fixtures/daily_prices.csv（只多一欄 volume_basis=incl_block）、tests/test_screener.py，新增 tests/test_no_trade_decisions.py、tests/test_volume_basis.py。docs/indicators.md §8、§9 已在主目錄改為「已核可，待 0.9.0 合入」（不在 patch 內）。

## analysis-0.9.1 內容（2026-11-17 18:31 核可；暫存目錄 `/workspace/scratch/analysis-091/`，`base/`＝主目錄＋0.9.0、`work/`＝修改後）
1. **停牌規則**（`adjust.resolve_halts`、`io_sqlite.load_halts` 的 `trade_resume_date`、`screener`；`verify_independent` 獨立實作）：
   - 有恢復日 → 停到恢復日前一天；
   - 無恢復日 → 停到停牌日後第一個有官方成交列的前一天（依成交推定）；
   - 一直沒有成交 → 仍停牌；
   - 只用評估日（含）以前的資料，不前視。
   - 恢復日與來源寫在 EXCL_SUSPENDED 明細與 `data_note`，**screen_results 不加欄、遷移清單不變**。
2. **全市場摘要**（`narrative.summary_narrative`）：
   - 「全市場 N 檔／實際完成條件評估 M 檔／符合 K 檔／未納入評估 X 檔」；
   - 排除原因以中文分類列檔數；
   - 落後股註記、缺 volume_basis 研究註記；
   - 新增 `ScreenResult.lagging_symbols`、參數 `scope_label`。
3. 版本 analysis-0.9.1；USAGE.md、summary_rules.md、tables_draft.md（data_note 註解）更新。docs/indicators.md 第 10 節已在主目錄直接寫入（不在 patch 內）。
4. 測試：套 0.9.0＋0.9.1 的全新副本全套 pytest **273 passed**（0.9.0 的 254 項＋`test_halt_rule.py` 13 項＋`test_narrative_fullmarket.py` 6 項）。
5. 唯讀快照（2026-11-17 18:35，sha256 `6f385350…0d26`）的評估日 11-17：0.9.0 的 14 檔 EXCL_SUSPENDED 中，12 檔改為 EXCL_NO_ROW_SUSPENDED_OR_MISSING，4174、8426 仍為 EXCL_SUSPENDED；評估日 11-09 結果碼 0 檔改變。

| 代號 | 0.9.0 | 0.9.1 | 停牌紀錄（0.9.1 data_note） |
|---|---|---|---|
| 1788 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-06-18，恢復日 2026-11-03（依成交推定） |
| 1799 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-03-12，恢復日 2026-03-31（依成交推定） |
| 3226 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-01-09，恢復日 2026-03-31（依成交推定） |
| 3491 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-07-15，恢復日 2026-11-03（依成交推定） |
| 3587 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-07-31，恢復日 2026-11-03（依成交推定） |
| 4127 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-05-13，恢復日 2026-05-14（依成交推定） |
| 4174 | EXCL_SUSPENDED | EXCL_SUSPENDED | 停牌日 2026-11-13，停牌日後至評估日無官方成交，視為仍停牌 |
| 4303 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-04-28，恢復日 2026-04-29（依成交推定） |
| 5263 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-01-21，恢復日 2026-03-31（依成交推定） |
| 5314 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-05-13，恢復日 2026-05-14（依成交推定） |
| 6474 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-06-15，恢復日 2026-06-16（依成交推定） |
| 8047 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-05-13，恢復日 2026-05-14（依成交推定） |
| 8426 | EXCL_SUSPENDED | EXCL_SUSPENDED | 停牌日 2026-11-12，停牌日後至評估日無官方成交，視為仍停牌 |
| 9960 | EXCL_SUSPENDED | EXCL_NO_ROW_SUSPENDED_OR_MISSING | 停牌日 2026-04-28，恢復日 2026-04-29（依成交推定） |

推定恢復日 03-31、11-03 是快照行情缺口造成的（全市場行情 03-31 才開始、06-17～11-02 缺），回補後會自動更正（indicators.md 10.3）。
呼叫端注意：`pipeline/analysis_bridge.py` 呼叫 `summary_narrative(result)` 時，若 `symbols` 不是全市場，請改傳 `scope_label="追蹤範圍"`（屬管線程式，由資料工程改）。

## analysis-0.9.1 重做內容（負責人 2026-11-17 19:41 核可；暫存目錄 `/workspace/scratch/analysis-091v2/`，`base/`＝主目錄＋0.9.0、`work/`＝修改後；patch＝兩者 diff）
1. **資料不足（insufficient）規則**（docs/indicators.md 10.1a）：只對推定區間（無官方恢復日）內，停牌日之後到評估日為止、該市場當日現行列數 < 100 的交易日（整個市場都缺行情）；這些日子本股無資料列 → 不算停牌，算缺漏，評估日遇到則為 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`。停牌日當天仍算停牌；全市場有資料、只是本股無列 → 仍照原規則算停牌。data_note 來源加「資料不足（insufficient）」。管線的 `resume_basis`／`resume_date_inferred` 只寫進 data_note 交叉檢查，不參與判定。不前視（只數評估日（含）以前）。
2. **覆蓋表 scope**（indicators.md 10.5）：`load_screen_inputs`／`load_symbol_inputs` 新參數 `coverage_scope`（預設 `"all"`，只接受 scope＝all；`"tracked"` 接受 sample30／all，必須給 symbols，且 symbols 全部屬 `universe_members` sample30，否則或表不存在 → 只接受 all）。沒有 scope 欄 → 維持現狀。同一區間多列（3b_fix1 主鍵含 scope）正確處理；unknown／subset／all_pending_ratio 都不算完整。不接受的列當作無覆蓋，走 `EXCL_ADJ_COVERAGE_GAP`。範圍寫在 `ScreenResult.coverage_scope_note`，覆蓋有問題的列另附在 data_note。
3. `EXCL_NO_ROW_SUSPENDED_OR_MISSING` 的顯示文字改為「停牌或資料缺漏，待確認（評估日為交易日，但本股無資料列，且無法確認停牌：無停牌紀錄，或推定停牌區間內該市場當日整個市場都缺行情）」。舊文字「同市場有行情…且無停牌紀錄」不適用於新情況。
4. 測試：新增 `tests/test_halt_insufficient.py`（11 項）、`tests/test_coverage_scope.py`（23 項，含同區間多列、unknown、universe_members）；`test_halt_rule.py` 的情境補成「全市場」（每市場 ≥ 100 列）；`test_usage_doc.py` 加 1 項。全套 **308 passed**（v1 是 273 項）。
5. **呼叫端必改（3b 遷移後；屬管線／網站程式，由各自負責人改）**：
   - `pipeline/analysis_bridge.run_screen_and_write`：sample30（傳代號清單）時改為 `load_screen_inputs(db, as_of=…, symbols=syms, coverage_scope="tracked")`；`symbols_mode == "all"` 不用改。
   - `web/app/data_source.py`（追蹤 30 檔選股 `load_screen_inputs(..., symbols=list(uni))`）與 `web/app/analysis_hooks.py`（`load_symbol_inputs`）：傳 `coverage_scope="tracked"`。
   - 不改的後果：3b 遷移後覆蓋表全是 sample30，sample30 結果會全部變成 `EXCL_ADJ_COVERAGE_GAP`（快照實測：20 檔 FAIL → 20 檔 `EXCL_ADJ_COVERAGE_GAP`）。改了之後與 v1 相同（0 檔改變）。3b 遷移前（沒有 scope 欄）改不改都一樣。

### 驗證（2026-11-17 20:1x）
- 全新副本 `/tmp/fullseq2`（主目錄 analysis、docs、pipeline、web、tools＋data/pending；不含正式庫）依上方 6 個 patch 順序套用：全部成功，0 個 .rej；analysis 全套 **308 passed**，pipeline/tests **113 passed、1 skipped**。
- 快照副本（`twstock_snap_1117_1906.db` 複製到 /tmp，sha256 `6f385350…0d26`）以套用後的 `pipeline.db.init_db` 遷移（覆蓋表 scope 全為 sample30，24 列）：`verify_independent` 全市場 1956 檔 × 4 評估日（11-17、11-09、11-02、10-15）× 4 組參數＝31,296 列、83,511 值，**0 不一致**；追蹤 30 檔 × 20 評估日（coverage_scope＝tracked、all 各一次）各 2,400 列，**0 不一致**；未遷移快照預設模式（全市場 × 最後 20 個評估日）156,480 列也 0 不一致。正式庫 sha256 前後不變。
- 與管線 3b_fix1 `infer_resume` 逐筆對照（快照副本）：無官方恢復日 187 筆，管線 insufficient 181 筆分析端全部有缺行情日；管線 inferred_by_trade 6 筆，分析端恢復日完全相同。差異說明見 indicators.md 10.6。

### v1 → 重做版對照（同一快照、同一評估日；`/workspace/scratch/analysis-091v2/compare_v1_v2.py`，輸出 `out/cmp_*.txt`）
| 範圍 | 評估日 | 結果碼改變 | 說明 |
|---|---|---|---|
| 全市場 1956 檔（未遷移） | 11-17 | 2 檔 | 4174、8426：EXCL_SUSPENDED → EXCL_NO_ROW_SUSPENDED_OR_MISSING（停牌日後只有 30 檔行情，整個市場都缺行情） |
| 全市場 1956 檔（未遷移） | 11-09 | 0 檔 | |
| 追蹤 30 檔（未遷移） | 11-17、11-09 | 0 檔 | 30 檔 FAIL，不分 tracked／預設 |
| 追蹤 30 檔（3b 遷移後，tracked） | 11-17、11-09 | 0 檔 | 20 FAIL＋10 EXCL_VOLUME_BASIS（兩版相同；後者來自 3b 遷移，與本 patch 無關） |
| 追蹤 30 檔（3b 遷移後，未傳 tracked） | 11-17、11-09 | 20 檔 | FAIL → EXCL_ADJ_COVERAGE_GAP（呼叫端未改的後果） |
| 全市場（3b 遷移後） | 11-17／11-09 | 22／20 檔 | 20 檔 FAIL → EXCL_ADJ_COVERAGE_GAP（覆蓋表沒有 all 列，全市場評估本來就不該算齊全）＋11-17 的 4174、8426 |

14 檔（11-17）：12 檔兩版都是 EXCL_NO_ROW_SUSPENDED_OR_MISSING（1788、1799、3226、3491、3587、5263 的 data_note 多「推定區間內 N 個交易日整個市場都缺行情…（資料不足（insufficient））」，N＝90、12、48、73、61、40）；4174、8426 由 EXCL_SUSPENDED 改為 EXCL_NO_ROW_SUSPENDED_OR_MISSING。11-09：14 檔都是 EXCL_INSUFFICIENT_HISTORY，兩版相同。

## 合入前提（全部滿足才合入；負責人確認）
1. 當日 18:00、22:00 排程已跑完，且沒有排程正在執行。
2. **資料工程**：正式庫 `daily_prices` 已 migrate 出 `volume_basis`／`block_*` 四欄並依 `source_table` 回填；`screen_results` 已依 `screen_results_migration_0.9.0.md` 遷移（四欄＋`after_no_trade`）；停牌誤標修正已到位、`next_ref_price_twd` 已回填。**未遷移時 writer 整批拒寫並報錯**（不會寫半批），排程會失敗而非寫錯。
3. **網站工程**：`web/app/models.py` 補 `after_no_trade`、`EXCL_VOLUME_BASIS` 顯示文字（取 `screener.EXCLUSION_CODES`），可顯示 `data_note`、`volume_basis`、`window_volume_basis_days`；K 線可依 bars 的 `volume_basis` 標示非總量日。
4. 管線（GitHub Actions）以 0.9.0 寫入前，確認正式庫讀得到 `volume_basis` 欄。

## 合入步驟（在 /workspace/tw-stock-site，由負責人或其指定者執行；推 GitHub 後由 Actions 寫正式庫）
1. 試套 0.9.0：`patch -p1 --dry-run < analysis/pending/analysis_0.9.0.patch`（2026-11-17 16:2x 對主目錄試套成功）。
2. 套用 0.9.0：`patch -p1 < analysis/pending/analysis_0.9.0.patch`。**不要**再套 `no_trade_decisions.patch`。測試須 254 passed。
3. 套用 0.9.1（重做版）：`patch -p1 --dry-run < analysis/pending/analysis_0.9.1.patch` 後 `patch -p1 < analysis/pending/analysis_0.9.1.patch`（2026-11-17 20:1x 在全新副本依完整順序試套成功）。測試：`cd analysis && ../.venv/bin/python -m pytest -q`，須 **308 passed**（若改用備援 v1，須 273 passed）。之後依序套 pipeline_phase3a_market、pipeline_phase3b、pipeline_phase3b_fix1（資料工程負責），analysis 仍須 308 passed。
4. 重驗（唯讀快照）：`verify_independent` 0 不一致；以 `/workspace/scratch/analysis-090/post_apply_compare.py` 做前後比較（10 檔逐檔、4105、6547 單獨列出；10 檔以外應 0 差異）。
5. docs/indicators.md §4 表、§8、§9 的「待 0.9.0 合入」改為「已合入 analysis-0.9.0」；第 10 節與第 0 節「確認停牌」列的「待 0.9.1 合入」改為「已合入 analysis-0.9.1」；summary_rules.md 標記同步。
6. 提交並推 GitHub（負責人決定時點）；正式庫只由 Actions 寫入。

## 回滾（依相反順序）
- 若已套 3a／3b／3b_fix1：先依相反順序退管線 patch（fix1 → 3b → 3a，資料工程負責），再退 analysis。
- 只退 0.9.1：`patch -p1 -R < analysis/pending/analysis_0.9.1.patch`（套的是哪一版就用哪一版退），版本回到 analysis-0.9.0，pytest 應為 254 passed（0.9.1 不改 schema，正式庫不需處理；0.9.1 寫入的列以 `analysis_version='analysis-0.9.1'` 識別）。
- 重做版改用備援：`patch -p1 -R < analysis/pending/analysis_0.9.1.patch` 後 `patch -p1 < analysis/pending/analysis_0.9.1_v1.patch`，pytest 應為 273 passed（v1 沒有 insufficient 規則、不讀覆蓋表 scope；3b 遷移後全市場評估會把 sample30 覆蓋當成齊全）。
- 再退 0.9.0：`patch -p1 -R < analysis/pending/analysis_0.9.0.patch`（或 git revert 對應提交），版本回到 analysis-0.8.0；pytest 應回到 226 passed。
- 正式庫：新增的四欄與放寬的 CHECK 對 0.8.0 無害（0.8.0 writer 不寫新欄，舊列 NULL），**不需回滾 schema**；已由 0.9.0 寫入的列以 `analysis_version='analysis-0.9.0'` 識別，是否作廢由負責人決定（版本化寫入，舊版列保留）。
- docs：indicators.md 標記改回「待 0.9.0 合入」；10.1a、10.5、10.6 為重做版內容，改用 v1 時需標註「未實作」。
