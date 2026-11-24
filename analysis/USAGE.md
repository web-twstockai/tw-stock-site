# 研究分析模組呼叫方式（給網站工程）

**版本：`analysis-0.10.0`**（`analysis/version.py` 的 `ANALYSIS_VERSION`，寫入 `screen_results.analysis_version`）。每批程式變更完成後版本號加一，並同步更新本檔；`tests/test_usage_doc.py` 檢查本檔的版本號、排除代碼與覆蓋類別是否與程式常數一致。

### 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
| analysis-0.10.0 | 2026-11-24 | **篩選中心新增模式 M9～M21＋單一登記表（專案負責人核可 2026-11-24 16:05，暫定分級；docs/indicators.md §12）**：新增 `modes.py`：`evaluate_modes()`（M9 站上季線、M10 營收創高＋站上季線、M11 營收年增加速、M12 短期超跌、M13 接近 120 日高點、M14 趨勢樣板（Minervini 縮短版）、M15 RSI(2) 多頭回檔、M16 多頭回檔月線、M17 均線糾結突破、M18 創 120 日新高、M19 CANSLIM 簡化、M20 三白兵、M21 月線上穿季線），每檔每模式 PASS／FAIL／EXCLUDED＋中文符合／未符合理由＋逐條件明細；排除沿用 screen() 代碼與 NOT_EVALUABLE_CODES、`coverage_stats()` 同 screener；月營收／財報取評估日已過法定期限的最新月份／季度，缺資料記 `REV_*`／`FIN_*`（不回退舊資料）；`REGISTRY`／`registry()`／`analysis/mode_registry.json`（M1～M21 的 id、中文名、一句話說明、條件、參數、完整版參數、暫定等級、勝率顯示「計算中」）；`eps_quarter_state()`（單季 EPS，M5／M8 可用）；`io_sqlite.load_mode_inputs()`／`load_income_statements()`。M1～M8 規則不變，只改登記表上的等級標示。研究稿 docs/rd/SCREEN_MODES_v2.md 新增第 9 節「預先登記」。 |
| analysis-0.9.2 | 2026-11-24 | **無法評估與 95% 覆蓋率分母（專案負責人轉達 H AO 核可 2026-11-24 13:32；docs/indicators.md §8.7）**：沒成交與停止買賣的股票個別標「無法評估」，不算資料不齊，自 95% 分母移除；篩選門檻與條件不變（視窗合計缺口仍與 `max_gap_days` 比較，無完整視窗仍不能符合）。新增 `EXCL_WINDOW_NO_TRADE`（視窗內官方無成交日單獨已超過門檻）、`EXCL_WINDOW_SUSPENDED`（視窗內確認停牌或減資／變更面額停止買賣日單獨已超過門檻）；`EXCL_WINDOW_GAPS` 改為真正缺資料或資料錯誤（官方有行情但本庫無列／資料錯誤，或與官方事實合計才超過但官方事實單獨未超過）。`NOT_EVALUABLE_CODES`＝`EXCL_NEW_LISTING`、`EXCL_SUSPENDED`、`EXCL_NO_TRADE`、`EXCL_WINDOW_NO_TRADE`、`EXCL_WINDOW_SUSPENDED`；`coverage_stats(summary)`／`ScreenResult.coverage_stats()` 給出分母＝母體−無法評估、可評估比例。停止買賣日取自 `corporate_actions.announced_at_note`「停止買賣日=YYYY-MM-DD」（上櫃 bulletin）；若只有恢復買賣日（上市 TWTAUU 註明 action_date＝恢復買賣日），則由恢復日前連續無列日推定，最多 15 日（`adjust.corp_suspension_dates`）。評估日無列且落在該期間 → `EXCL_SUSPENDED`。`io_sqlite` 補讀窗口外的減資／變更面額事件（恢復日可能晚於 as_of）。`verify_independent` 獨立實作。測試：`tests/test_coverage_denominator.py`。網站分母與標示須同步（本版不改 `web/`，見該測試檔與文件說明）。 |
| analysis-0.9.1 | 2026-11-17 | **停牌規則（負責人 2026-11-17 18:31 核可；docs/indicators.md「停牌規則」）**：停牌從停牌日開始算；有官方恢復日停到恢復日前一天；沒有恢復日時停到停牌日之後第一個有官方成交列（有收盤價且成交量 > 0 的交易日）的前一天（標「依成交推定」）；之後一直沒有成交視為仍停牌。只用評估日（含）以前的資料，不前視。新增 `adjust.resolve_halts`／`HaltSpan`／`halt_span_on`／`halt_note`、`HALT_SOURCES`（官方恢復日／依成交推定／仍停牌）；`normalize_halts` 回傳 3 欄（多 `trade_resume_date`）；`io_sqlite.load_halts` 在資料庫端算 `trade_resume_date`（停牌日後、評估日以前第一個成交日，只在 resume_date 為 NULL 時）；EXCL_SUSPENDED 明細與 `data_note`（「停牌紀錄：…」）寫明恢復日與來源；`window_halt_days` 依新規則計算；`verify_independent` 獨立實作。screen_results 不加欄（來源寫在 data_note）。**全市場摘要（narrative.py）**：改為「{範圍} N 檔／實際完成條件評估 M 檔／符合 K 檔／未納入評估 X 檔（完成評估但未符合 F 檔）」，排除原因依 `narrative.EXCLUSION_GROUPS` 以中文分類列檔數；`ScreenResult.lagging_symbols`（新欄位，screener 依 AS_OF_RULE 第 4 點計算）非空時加註「最新行情列早於評估日，不能據此判斷當日是否交易」；`volume_basis_status` 非 ok 時加註研究試算、不作正式結果；新增參數 `scope_label`（預設「全市場」，只評估追蹤清單時請傳「追蹤範圍」）。零結果改寫「今日 0 檔符合全部已啟用條件；不放寬條件找替代股。」。測試：新增 `tests/test_halt_rule.py`、`tests/test_narrative_fullmarket.py`。**重做（負責人 2026-11-17 19:41 核可；舊版 patch 改名 analysis_0.9.1_v1.patch 作備援）**：①**停牌 insufficient 規則**：只有推定區間（無官方恢復日的「依成交推定／仍停牌」）內、停牌日之後到評估日為止「整個市場都缺行情」的交易日（該市場當日 `daily_prices` 現行列數 < `adjust.FULL_MARKET_MIN_ROWS`＝100，與管線 `halt_fix.FULL_MARKET_MIN_ROWS` 同值），本股又沒有資料列時不算停牌，改計 `window_missing_days`；評估日遇到則為 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`。停牌日當天仍算停牌；全市場有資料、只是本股沒有列仍照原規則算停牌；本股有（無成交）列仍算停牌。`data_note` 來源多一種「資料不足（insufficient）」（`adjust.HALT_SRC_INSUFFICIENT`），寫明缺行情日數與起訖。不用管線的 `resume_date_inferred` 判定；`trading_halts` 有 `resume_basis`／`resume_date_inferred` 欄時只寫進 `data_note`「管線交叉檢查：…」。`HaltSpan` 多 `n_holes`／`hole_first`／`hole_last`／`holes`；`normalize_halts` 回傳 7 欄；`resolve_halts` 多 `trade_days`／`is_hole`；`halt_span_on`／`is_halt_day` 多 `has_row`；`market_days` 多 `n_rows` 欄（無此欄時 market_days 內的日子都視為全市場有資料＝舊行為）。②**覆蓋表 scope**：`load_screen_inputs`／`load_symbol_inputs` 新參數 `coverage_scope`（`"all"` 預設＝全市場評估，只接受 scope＝all；`"tracked"`＝追蹤清單，接受 sample30 或 all，必須同時給 symbols）。資料庫沒有 scope 欄時維持現狀。不接受的列視為無覆蓋，走原 strict 排除碼（`EXCL_ADJ_COVERAGE_GAP`）。採用的範圍寫在 `ScreenInputs.coverage_scope_note`／`ScreenResult.coverage_scope_note` 與覆蓋有問題各列的 `data_note`。追蹤清單以 `universe_members`（universe_id＝sample30；管線 3b_fix1 定義的權威鏡像）核對，表不存在時保守只認 all；同一區間多個 scope 列（fix1 主鍵含 scope）正確處理，unknown／subset／all_pending_ratio 都不算完整。新增 `io_sqlite.load_coverage_scoped`、`tracked_members`、`COVERAGE_SCOPES`。測試：新增 `tests/test_halt_insufficient.py`、`tests/test_coverage_scope.py`。選股條件與門檻不變 |
| analysis-0.9.0 | 2026-11-17 | **量額口徑（docs/indicators.md §9，負責人 2026-11-17 16:09 核可，方案 A）**：選股採總量 `volume_basis=incl_block`（兩市場都含鉅額，但組成不完全一致；上櫃是否含盤後定價、零股場次待查證）；`io_sqlite` 只讀 `daily_prices.volume_basis`，不讀 `block_*`；新排除碼 `EXCL_VOLUME_BASIS`——5a 評估日口徑非 `incl_block`（C3 或 C4 啟用時）、9a 均量窗口（評估日前 `volume_lookback` 個有效交易日）任一日非 `incl_block`（C3 啟用時）；summary 新增 `volume_basis`、`window_volume_basis_days`；輸入缺 `volume_basis` 欄時研究照算、`ScreenResult.volume_basis_status`／`ScreenInputs.volume_basis_status`＝`volume_basis_column_missing`，`writer.write_screen_results` 拒寫；`ohlc_series` bars 新增 `volume_basis`（只作標示）；`verify_independent` 獨立實作同一規則。**無成交三項（DECISIONS 2026-11-10 12:05 核可，原 pending 無成交 patch 併入）**：**DECISIONS 2026-11-10 12:05 核可三項**：① 有收盤價但成交量 NULL 的列一律為資料錯誤——評估日 `EXCL_BAD_DATA`；視窗內計入缺口合計（門檻口徑不變），另拆成新欄 `window_bad_data_days`，缺口未超過門檻時 `EXCL_BAD_DATA`（過去算成 `window_no_trade_days`）；`ma_series`／`ohlc_series` 該日仍為 `no_trade`，另標 `unavailable_code=EXCL_BAD_DATA`。② 前一交易日有列但整股無成交時，`change_check` 改為新狀態 `after_no_trade`（不算 mismatch）：來源 X 或 `change_note=無比價` 顯示「無比價」；有官方參考價（選用欄 `daily_prices.next_ref_price_twd`，TPEx 次日參考價）時以「收盤−參考價」比對，超過容許值才標 `mismatch`；無參考價則不比對。自算漲跌幅與基準不變。③ `is_no_trade` 只做交叉檢查，寫入新說明欄 `data_note`，不改結果碼。`writer` 寫入前檢查 `screen_results.change_check` 的 CHECK，未允許 `after_no_trade` 時整批不寫並報錯；`tables_draft.md` 新增 `window_bad_data_days`、`data_note` 並擴充 CHECK（需資料工程遷移）。`verify_independent` 同步。選股條件與門檻不變。測試：合成行情補 `volume_basis=incl_block`（fixture 重產，其他欄不變）、新增 `tests/test_volume_basis.py`。選股條件與門檻不變 |
| analysis-0.8.0 | 2026-11-10 | **預設評估日改規則**（as_of 未傳入時）：由「整張 daily_prices 最新交易日」改為「追蹤範圍內有現行行情列的最新交易日」（`scope_latest_trade_day-v1`），範圍外股票的較新日期不再影響；統一由 `adjust.resolve_as_of()` 決定，SQLite 入口 `io_sqlite.default_as_of()`，`load_screen_inputs`／`load_symbol_inputs`／`screener.screen`／`series.*`／`verify_independent` 全部改用同一規則；`ScreenInputs.as_of_resolution` 記錄規則、範圍檔數與落後代號。docs/indicators.md 新增第 8 節整股無成交日規格（程式行為未變）。測試：新增 tests/test_as_of.py（6 項）；test_io_sqlite 改為明確傳入市場最新日；schema 比對容許 pipeline 新增而 analysis 不讀的 `change_note`、`is_no_trade`。選股條件與門檻未變 |
| analysis-0.7.0 | 2026-11-10 | 新增 `explain.py`：`condition_explanations(params)`（四條件白話說明，網站唯一文字來源，數字取自參數）、`common_explanation(params)`；`series.ohlc_series()`（個股 K 線前復權 OHLC＋還原量＋除權息事件清單，口徑與 screener／ma_series 相同）；`revenue.py` 模組說明改為與 indicators.md §7.3 一致的中性措辭；篩選邏輯與門檻未變（sample30 真實資料檢查未發現程式錯誤） |
| analysis-0.6.0 | 2026-11-10 | `writer.write_screen_results`／`write_daily_summary` 新增 `screen_run_id` 參數（產生該批 screen_results 的 `job_runs.run_id`，由 pipeline.run 傳入；不納入 checksum；表上有欄才寫，目前只有 daily_summaries 有）；新增 `revenue.py`（F-001 近 12 個月新高 `rev_12m_high-v1`、年增率 `rev_yoy-v1`）與獨立核對 `verify_revenue.py`；`tests/synthetic_data.schema_ddl()` 在 schema.md DDL 區塊不完整時改用 `pipeline/schema.sql`；`verify_independent.py` 改寫報告時保留「三、」之後的節 |
| analysis-0.5.0 | 2026-11-10 | stock_split 覆蓋由 par_value_change 涵蓋（只有 par_value_change 列或另寫 stock_split 列皆可，DECISIONS 03:45 ①）；面額變更／分割的換股比例只取 `share_exchange_ratio`（不再以 `capital_reduction_ratio` 替代）；新增 `writer.py`（screen_results／daily_summaries／news_events＋news_event_symbols 冪等版本化寫入）；`ScreenResult.computed_at`、`ScreenInputs.input_max_fetched_at`、`io_sqlite.max_fetched_at()`；`summary` 新增 `price_version`；`summary_narrative` 的新聞關聯代號改用 `symbols`（取自 news_event_symbols，舊鍵 `related_symbols` 拋錯）；tables_draft：移除 related_symbols_json、三表加 computed_at／input_max_fetched_at、volume_shares 註記改為官方總量口徑依市場不同 |
| analysis-0.4.0 | 2026-11-10 | 事件類型完整清單 6 類（cash_dividend、stock_dividend、ex_both、capital_reduction、par_value_change、stock_split；相容 ex_dividend／ex_rights）；面額變更／分割有換股比例才調整，否則 `EXCL_ADJ_FACTOR_MISSING`；來源 `cash_subscription_ratio` 優先用於股數因子；停牌拆成 `EXCL_SUSPENDED`（trading_halts／is_halted 確認）與 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`；讀取 `trading_halts`（表不存在可容錯）；`summary` 新增 `window_halt_days`／`window_missing_days`／`window_no_trade_days`；覆蓋類別 6 類；量額改記為暫定官方總量口徑（兩市場不同）；新增 `version.py`、`requirements.txt`；`tables_draft.md` 新增 `news_event_symbols` |
| analysis-0.3.0 | 2026-11-10 | 對齊 schema.md 02:35 版：覆蓋表多段合併、漲跌幅來源交叉檢查、`EXCL_SUSPENDED` 改名 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`、新增均線／漲跌幅序列與獨立核對 |
| analysis-0.2.0 及更早 | 2026-11-10 前 | 初版篩選器、前復權、排除代碼、合成資料測試（細節未逐條記錄） |

最後更新：2026-11-10（Asia/Taipei，UTC+8）。定義與規則見 `docs/indicators.md`；資料表見 `docs/schema.md`；研究分析寫入表草案見 `analysis/tables_draft.md`。
Python 環境：`analysis/.venv`（Python 3.13.5；版本鎖定於 `analysis/requirements.txt`）。依負責人決議各自維持 venv、版本對齊；專案根目錄 `.venv`（資料工程用，同為 Python 3.13.5）目前未安裝 pandas／numpy，無需對齊，日後安裝時請用 requirements.txt 的版本。
建立：`python3.13 -m venv analysis/.venv && analysis/.venv/bin/pip install -r analysis/requirements.txt`

模組：

| 檔案 | 內容 |
|---|---|
| `adjust.py` | 共用：前復權因子、覆蓋區間、無法解釋跳空、漲跌幅基準與來源交叉檢查（screener 與 series 共用同一份程式） |
| `screener.py` | 每日篩選 `screen()` |
| `writer.py` | 結果表冪等版本化寫入（screen_results／daily_summaries／news_events＋news_event_symbols） |
| `revenue.py` | 月營收指標：`evaluate_revenue()`（近 12 個月新高、年增率）、`load_monthly_revenues()`（唯讀） |
| `verify_revenue.py` | 月營收獨立核對（純 Python），寫入 `docs/verification.md` 第三節 |
| `version.py` | `ANALYSIS_VERSION` |
| `series.py` | 個股圖：`ma_series()`（還原收盤與 MA20／MA60）、`change_series()`（逐日漲跌幅）、`ohlc_series()`（K 線前復權 OHLC＋除權息清單） |
| `explain.py` | 選股條件白話說明 `condition_explanations()`／`common_explanation()` |
| `narrative.py` | 規則模板摘要 `summary_narrative()` → 文字＋facts_json |
| `io_sqlite.py` | 唯讀 SQLite 讀取層（含 `load_mode_inputs()`） |
| `modes.py` | 篩選中心模式 M9～M21 `evaluate_modes()` 與 M1～M21 登記表（`mode_registry.json`）；見第 9 節 |
| `mode_registry.json` | 登記表檔案版（由 `python modes.py --write-registry` 產生，測試檢查與程式一致；網站讀這份） |

## 1. 最短用法

```python
import sys; sys.path.insert(0, "/workspace/tw-stock-site/analysis")
from io_sqlite import load_screen_inputs, load_symbol_inputs
from screener import ScreenParams, screen
from series import ma_series, change_series
from narrative import summary_narrative

DB = "/workspace/tw-stock-site/data/twstock.db"
inputs = load_screen_inputs(DB, as_of=None, symbols=SAMPLE30)   # None＝追蹤範圍內有行情的最新交易日（見下方「預設評估日」）；正式寫入請明確傳 as_of
result = screen(**inputs.screen_kwargs(), params=ScreenParams())   # 預設＝SPEC 四條件、strict

result.selected()          # 入選（PASS）股票；0 檔就是空表，請顯示「0 檔」，不要自動放寬
result.summary             # 每檔一列（含排除原因）
result.conditions          # 每檔 × 4 條件
result.exclusion_counts()  # {排除代碼: 檔數}
result.as_of, result.params, result.params_hash, result.analysis_version
inputs.coverage_status     # ok／coverage_table_missing／coverage_table_empty
inputs.halts_status        # ok／halts_table_missing（trading_halts 不存在時不報錯，只是無法確認停牌）
inputs.input_max_fetched_at  # 本次讀入各表列 fetched_at 最大值（寫入結果表用）
result.computed_at         # 計算時間（ISO-8601 +08:00）

kw = load_symbol_inputs(DB, "2330", as_of=None)    # 個股圖
ma_series(**kw)            # 每日：還原收盤、MA20、MA60、因子、不可用原因
change_series(**kw)        # 每日：漲跌幅、基準價、來源交叉檢查

n = summary_narrative(result)   # n.text、n.facts_json、n.generation_method == "rule"
```

使用者調整條件時，只要換成 `ScreenParams(...)` 重新呼叫 `screen()`，參數越界會拋 `ValueError`、型別錯誤會拋 `TypeError`，請把錯誤訊息顯示給使用者，不要自行修正數值。

**覆蓋政策**：預設與所有正式結果一律 `coverage_policy="strict"`，程式不會自動改用 `gap_check`。除權息／減資回補完成前，結果會照實是 0 檔、排除原因 `EXCL_ADJ_COVERAGE_GAP` 並附 `coverage_note`。`gap_check` 只供研究比較，不可寫入 `screen_results` 或顯示為正式結果。

## 2. 函式簽名

```python
io_sqlite.load_screen_inputs(db_path, as_of: str | None = None, symbols: Iterable[str] | None = None,
                             lookback_trade_days: int = 250, coverage_scope: str = "all") -> ScreenInputs
io_sqlite.load_symbol_inputs(db_path, symbol: str, as_of: str | None = None,
                             lookback_trade_days: int = 250, coverage_scope: str = "all") -> dict   # 直接展開給 ma_series／change_series
io_sqlite.load_coverage(con, coverage_scope="all") -> (DataFrame | None, status)
io_sqlite.load_coverage_scoped(con, coverage_scope="all") -> (DataFrame | None, status, note | None)
io_sqlite.load_halts(con, start, as_of, symbols=None) -> (DataFrame | None, status)
```

**預設評估日（as_of 未傳入；analysis-0.8.0 起）**——全套件唯一規則 `adjust.resolve_as_of()`（`AS_OF_RULE="scope_latest_trade_day-v1"`）：
```python
r = io_sqlite.default_as_of(DB, symbols=SAMPLE30)   # symbols=None → securities 現行普通股
r.as_of, r.rule, r.n_scope, r.n_with_row, r.lagging   # lagging＝範圍內最新行情日早於 as_of 的代號
```
1. 只看追蹤範圍內代號的 `daily_prices` 現行列（含整股無成交列），日期必須是 `trading_calendar` 交易日；範圍外股票的日期一律不看。
2. as_of＝範圍內各檔最新日期的最大值；交易日曆有、範圍內卻沒有行情的日子（例：2026-10-23）不會被選中。
3. 範圍內最新日期不一致時仍取最大值，落後的代號在該日依排除碼處理（多為 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`），as_of 不因它改變；範圍內完全沒有行情→`ValueError`。
4. `series.*` 未傳 as_of 時範圍＝該檔本身；個股頁請傳入與選股相同的 as_of。
- 唯讀開啟 SQLite，只取 `is_current = 1`。`symbols` 可限定代號（例如第二階段 30 檔）。
- 讀取範圍：`trading_calendar`（as_of 往前 lookback 個交易日）、`daily_prices`（含可選 `change`／`change_twd`／`change_sign`／`is_halted`）、`corporate_actions`（含可選 `cash_subscription_ratio`／`share_exchange_ratio`，欄位存在才讀）、`securities`（is_common=1）、各市場「有行情的日期」（不受 symbols 限制）、`corporate_actions_coverage`、`trading_halts`（存在才讀；只取與讀取期間重疊者）。
- 覆蓋表狀態 `ScreenInputs.coverage_status`：表不存在 → `coverage_table_missing`（`actions_coverage=None`）；空表 → `coverage_table_empty`；表存在但缺 market／action_kind／coverage_start 欄 → 拋 `ValueError`（schema 不一致不靜默略過）。有 `coverage_end`／`source`／`fetched_at` 時一併讀。
- `ScreenInputs.screen_kwargs()` 可直接展開給 `screen()`。

```python
screener.screen(prices, calendar, actions=None, securities=None, params=None, as_of=None,
                universe=None, actions_coverage=None, market_days=None, halts=None) -> ScreenResult
```
| 參數 | 內容 |
|---|---|
| prices | `daily_prices` 欄位：symbol, price_date, open_twd, high_twd, low_twd, close_twd, volume_shares, amount_twd（可選 market, change, change_twd, change_sign, is_halted, is_current, source, fetched_at） |
| calendar | `trading_calendar`：calendar_date, is_trade_day |
| actions | `corporate_actions`：symbol, action_date, action_type（cash_dividend／stock_dividend／ex_both／capital_reduction／par_value_change／stock_split；相容 ex_dividend、ex_rights）（可選 prev_close_twd, reference_twd, dividend_value_twd, stock_dividend_per_thousand, capital_reduction_ratio, refund_per_share_twd, cash_subscription_ratio, share_exchange_ratio, is_current） |
| securities | `securities`：symbol（可選 market, name, listed_date）。提供時預設 universe＝此表 |
| params | `ScreenParams`，參數表見 docs/indicators.md 第 5 節 |
| as_of | 評估日（必須是交易日）；None＝追蹤範圍（universe；未給則 securities，再無則行情代號）內有行情的最新交易日 |
| universe | 指定要列出的代號；不在 securities 的代號會標 `EXCL_NOT_COMMON_STOCK` |
| actions_coverage | `corporate_actions_coverage`：DataFrame[market, action_kind, coverage_start（, coverage_end）] 或 `{("TWSE","dividend"): "2020-01-01"}`／`{(…): ("起日", "迄日")}`；**None＝表不存在、空表＝表為空**，strict 下都排除 |
| market_days | DataFrame[market, price_date]，各市場有行情的日期；None 時由 prices 推得 |
| halts | `trading_halts`：symbol, halt_date（可選 resume_date, is_current, trade_resume_date〔io_sqlite 算出的依成交推定恢復日，≤ as_of〕）；None＝無停牌資訊（確認停牌只能靠 `is_halted`）。記憶體內呼叫若沒有 trade_resume_date，就只用傳入 prices（≤ as_of）推定 |

```python
series.ma_series(prices, actions, calendar, symbol, windows=(20, 60), as_of=None,
                 actions_coverage=None, securities=None, params=None) -> DataFrame
series.change_series(prices, actions, calendar, symbol, as_of=None,
                     actions_coverage=None, securities=None, params=None) -> DataFrame
```
- 使用與 `screen()` 相同的還原程式（`adjust.py`），as_of 當天的 MA 與 `screen()` 的 `ma_short`／`ma_long` 完全相同（測試與獨立核對都有比對）。
- `ma_series` 欄位：price_date, symbol, close_twd（原始）, adj_factor（F）, share_factor（S）, adj_close_twd, status（ok／no_trade／unavailable）, unavailable_code, unavailable_reason, `ma_20`, `ma_20_note`, `ma_60`, `ma_60_note`。
  - 不足 N 日：`ma_N` 為 NULL、`ma_N_note = INSUFFICIENT_HISTORY`。
  - 因子缺漏／不一致、無法解釋跳空、覆蓋起日之前：可信邊界之前整段 `status = unavailable`、`adj_close_twd` 與 MA 為 NULL、`ma_N_note = ADJ_UNAVAILABLE`，`unavailable_code` 為對應排除代碼；邊界後重新起算 N 日。
  - 沒有覆蓋表或空表：全段 unavailable。
  - 停牌／無成交日：`status = no_trade`、MA 為 NULL（`NO_TRADE`），不補值。圖表請畫成斷點，不要連線補值。
- `change_series` 欄位：price_date, symbol, close_twd, prev_price_date, prev_close_twd, change_base_twd, is_event_day, change_twd_calc, change_pct, source_change_twd, change_check（ok／mismatch／no_source／source_not_comparable／source_sign_conflict）, change_check_diff_twd, change_tolerance_twd, status（ok／unavailable／first_row）, note。
  - 自算 `change_pct` 是正式值；`mismatch` 只標記，不覆蓋。定義與容許誤差見 indicators.md 1.1。

```python
narrative.summary_narrative(result, extra_facts=(), news_events=(), max_listed=50, scope_label="全市場") -> Narrative
```
- 只用規則模板（`generation_method = "rule"`），不寫漲跌原因、勝率或保證；0 檔時固定寫「今日 0 檔符合全部已啟用條件；不放寬條件找替代股。」並列排除原因統計。
- 檔數句（analysis-0.9.1）：「{scope_label} N 檔／實際完成條件評估 M 檔／符合 K 檔／未納入評估 X 檔（完成評估但未符合 F 檔）。」N＝summary 列數、M＝PASS＋FAIL、X＝EXCLUDED。`scope_label` 預設「全市場」；**只評估追蹤清單（load_screen_inputs 傳 symbols）時呼叫端要傳「追蹤範圍」等**，否則會誤稱全市場。
- 排除原因依 `narrative.EXCLUSION_GROUPS`（每個排除碼恰屬一類，中文名稱）列「- 類別：n 檔」。
- `result.lagging_symbols` 非空：加「另有 L 檔最新行情列早於評估日，不能據此判斷當日是否交易（已依上列原因處理）。」
- `result.volume_basis_status` 非 ok：加 `narrative.RESEARCH_NOTE`（研究試算、不作為正式結果）。
- `extra_facts`：其他數字（例如 `index_daily` 指數）用 `narrative.Fact(...)` 傳入，需含來源表、欄位、主鍵、資料日期。
- `news_events`：`news_events` 現行列（dict：event_id, version, category, title, first_published_at, symbols）；`symbols`＝同版本 `news_event_symbols.symbol` 清單（`related_symbols_json` 已移除，傳舊鍵 `related_symbols` 會拋 `ValueError`）；只列標題與分類，分類必須是 SPEC 四類，否則 `ValueError`。社群訊息自動加「未證實，僅供線索」。
- 產生時檢查：文字中每個數字（〔〕內的原文標題除外）都必須出現在某個 fact 的顯示值或資料日期中，否則拋 `ValueError`，不輸出。
- `Narrative.facts_json` 直接存入 `daily_summaries.facts_json`。

### 2.1 寫入結果表 `writer.py`（研究分析寫入；表由資料工程建立）

```python
import writer as W
con = W.connect_writable(DB, allow_production=True)   # 正式庫必須明示；未給即 PermissionError（負責人通知前不執行）
r1 = W.write_screen_results(con, result, inputs.input_max_fetched_at, screen_run_id=run_id)   # 同一 (as_of, params_hash) 整批一個交易
r2 = W.write_daily_summary(con, n, result, input_max_fetched_at=inputs.input_max_fetched_at, screen_run_id=run_id,
                           news_event_refs=[{"event_id": …, "version": …, "category": …}])
r3 = W.write_news_event(con, event_dict, symbols=[{"symbol", "evidence_type", "evidence", "source_news_id"
                                                   或 "source_material_key_json"}])   # news_events＋news_event_symbols 同一交易
r1.inserted, r1.versioned, r1.skipped, r1.dropped_columns
```
- 冪等：checksum 相同 → 跳過；不同 → 舊現行列 `is_current=0`、新版 `version＝最大版＋1`。失敗整批回滾，不刪舊列。
- checksum 不含 `computed_at`／`input_max_fetched_at`／`generated_at`／`screen_run_id`／`version`／`is_current`。
- `screen_run_id`＝產生該批 screen_results 的 `job_runs.run_id`（實際 schema 的主鍵名是 `run_id`），由 pipeline.run 傳入；接受正整數或其字串，存成字串；資料庫有 `job_runs` 表時會檢查該 run 存在，不存在拋 `ValueError`。寫入 `daily_summaries.screen_run_id`；`screen_results` 目前沒有此欄，有欄時才一併寫入。內容相同而跳過時，只在現行列 screen_run_id 為 NULL 時補上，已有值不覆寫（保留第一次產生的 run）。未傳＝NULL。
- `computed_at`＝計算時間；`generated_at`＝寫入時間（網站「存檔時間」）；`input_max_fetched_at`＝所用輸入列 fetched_at 最大值。
- 只接受 `coverage_policy="strict"`；代號 TEST 開頭會被表的 CHECK 擋下。
- 只寫表上存在的欄位；表少欄位時列在 `dropped_columns`（2026-11-10 唯讀確認：正式庫已有 window 三欄；`screen_results` 沒有 `screen_run_id`），缺必要欄位則拋錯。
- `daily_summaries` 必須在同批 `screen_results` 寫入後才寫（`screen_ref_json` 引用其現行列 checksum）；另寫 `data_date`＝summary_date、`provider`＝「研究分析規則（analysis/narrative.py）」、`screen_run_id`＝傳入值（見上）。
- 網站查個股相關事件：`news_event_symbols JOIN news_events ON (event_id, version) AND is_current = 1`。

`screen()`、`ma_series()`、`change_series()`、`summary_narrative()` 都是純函式：不讀寫檔案、不修改傳入的 DataFrame，相同輸入得到相同結果。

### 2.2 月營收指標 `revenue.py`（定義見 docs/indicators.md 第 7 節）

```python
import revenue as R
df = R.load_monthly_revenues(DB)                     # 唯讀；只取 is_current = 1
out = R.evaluate_revenue(df, month="2026-10")        # 純函式；每檔一列，欄位 R.OUTPUT_COLUMNS
out[out.rev_12m_high_status == R.STATUS_TRUE]        # true／false／insufficient；rev_12m_high 為 True／False／None
```
- 原因代碼與說明：`R.REASON_CODES`；定義版本：`R.REV_12M_HIGH_VERSION`（`rev_12m_high-v1`）、`R.REV_YOY_VERSION`（`rev_yoy-v1`），每列都附。
- 年增率 `yoy_pct` 由 `revenue_twd`／`revenue_prev_year_twd` 重算；去年同月為 0、NULL 或負值時為空並附原因，不產生 inf／NaN；來源值放在 `source_yoy_pct`。
- 測試用合成資料：`tests/revenue_synthetic.py`（代號 TESTR*，fixture `tests/fixtures/monthly_revenues_synthetic.csv`）。

### 2.3 條件白話說明 `explain.py`（網站顯示文字的唯一來源）

```python
import explain as E
for x in E.condition_explanations(params):   # params＝ScreenParams；未給＝預設
    x["condition_id"], x["title"], x["enabled"], x["text"]   # 未啟用時 text 以「未啟用，不檢查。」開頭
E.common_explanation(params)                   # 有效交易日、缺漏天數上限、覆蓋不足排除等共用前提
```
- 數字一律取自參數（MA 天數、回看日數、倍數、金額門檻：≥1 億且整萬以「億元」表示，否則「元」）；只寫怎麼算，不寫效果、勝率或報酬。網頁模板請改用這裡的文字，勿另寫一份。

### 2.4 個股 K 線 `series.ohlc_series()`

```python
import io_sqlite as I, series as SE
o = SE.ohlc_series(**I.load_symbol_inputs(DB, "2330", as_of="2026-11-09"))   # 可傳 params=ScreenParams(...)
o.bars      # 每交易日一列：原始 open/high/low/close_twd、volume_shares、amount_twd；adj_factor、share_factor；
            # adj_open/high/low/close_twd、adj_volume_shares；status（ok／no_trade／unavailable）、unavailable_code／reason
o.events    # 除權息／減資等事件：action_date, action_type, action_kind, status（applied／before_boundary／failed）,
            # price_factor（f）, share_factor（s）, code, detail, desc
o.boundary_date, o.all_unavailable   # 可用範圍起日；整段不可用（例如覆蓋表為空）
```
- 口徑與 screener 相同：以 as_of 為基準前復權（還原價＝原始價×F，還原量＝原始量×S，成交金額不還原）；可用範圍規則與 `ma_series` 相同，邊界前 status=unavailable、還原值 NULL（原始值照列，不硬算）。`adj_close_twd` 與 `ma_series` 逐列相同；最後一列還原收盤、前 N 日還原最高、還原均量與 screener 相同（測試驗證）。

## 3. 常用參數（`ScreenParams`）

| 參數 | 預設 | 範圍 | UI 建議 |
|---|---|---|---|
| ma_short / ma_long | 20 / 60 | 2～250 / 3～500，長 > 短 | 數字輸入 |
| breakout_lookback | 20 | 1～250 | 數字輸入 |
| volume_lookback | 20 | 1～250 | 數字輸入 |
| volume_multiplier | 1.5 | 1.0～20.0 | 小數 |
| min_turnover | 100000000 | 0～1e12（元） | 以「億元」輸入後 ×1e8 |
| enable_trend / enable_breakout / enable_volume / enable_turnover | True | bool | 勾選框，至少一項 |
| max_gap_days, max_daily_move, legacy_max_daily_move, min_listing_days, new_listing_no_limit_days, adj_prev_close_tolerance, derive_share_factor, check_unexplained_gap | 見 indicators.md | — | 進階（資料品質），建議不放一般 UI |
| coverage_policy | "strict" | — | **不放 UI**；正式結果固定 strict |

## 4. 輸出欄位

`summary`（每檔一列）：

| 欄位 | 說明 |
|---|---|
| symbol, name, market, price_date | 代號、名稱（securities）、市場、評估日 |
| status | `PASS`／`FAIL`／`EXCLUDED` |
| exclusion_code, exclusion_reason | 排除代碼與中文原因（含日期、數值），只有 EXCLUDED 有值 |
| close_twd, change_pct | 收盤（元）、漲跌幅（%，相對前一有效交易日；除權息日以參考價為基準） |
| change_base_twd, source_change_twd, change_check, change_check_diff_twd | 漲跌幅基準價、來源帶號漲跌、交叉檢查結果與差值。change_check：ok／mismatch／no_source／source_not_comparable／source_sign_conflict／**after_no_trade**（前一交易日整股無成交：不算 mismatch；有官方參考價時才比對，見 docs/indicators.md §8） |
| volume_shares, amount_twd | 成交量（股）、成交金額（元）；**總量口徑（`volume_basis=incl_block`），兩市場都含鉅額**，不扣除 `block_*`。兩市場組成不完全一致：上市（STOCK_DAY／MI_INDEX）官方註記含一般、零股、盤後定價、鉅額，不含拍賣、標購；上櫃（dailyQuotes）含鉅額為實測、含零股成交為實測，是否含盤後定價、零股場次待查證。取不到 dailyQuotes 的日子為 tradingStock（`excl_block`），評估日或均量窗口遇到即 `EXCL_VOLUME_BASIS`（docs/indicators.md §9） |
| volume_basis, window_volume_basis_days | 評估日量額口徑（`incl_block`／`excl_block`／NULL；輸入缺欄時為空）、均量窗口內非 `incl_block` 日數（未算到窗口時為空） |
| ma_short, ma_long | MA20、MA60（還原後，元） |
| prior_high_twd, prior_avg_volume_shares, volume_ratio | 前 20 日最高（還原）、前 20 日均量（股，股數因子還原）、量比 |
| pass_trend, pass_breakout, pass_volume, pass_turnover | 各條件是否成立（EXCLUDED 為空） |
| price_version | 讀取當時 `daily_prices.version` |
| n_valid_days, window_start, window_gap_days | 有效交易日數、視窗起日、視窗內非有效交易日數（合計，與 max_gap_days 比較）。analysis-0.9.2 起另提供 `coverage_stats`（95% 分母＝母體−`NOT_EVALUABLE_CODES`） |
| window_halt_days, window_missing_days, window_no_trade_days, window_bad_data_days | 上一欄拆解：確認停牌／無資料列且無法確認停牌（無停牌紀錄，或推定區間內整個市場都缺行情）／有列無成交／有收盤價但成交量 NULL（資料錯誤；有值即 `EXCL_BAD_DATA`） |
| data_note | 說明欄（只標註，不改結果碼）：無成交次日的漲跌核對說明（含「無比價」）、`is_no_trade` 交叉檢查不一致 |
| n_adj_events, adj_events | 視窗內套用的調整事件數與說明（例：`2026-09-24 ex_dividend f=0.920016 s=1.000000`） |
| coverage_note | 覆蓋問題代碼（見第 6 節）；strict 排除時也填 |
| price_source, price_fetched_at | 當日行情的 source、fetched_at（追溯用） |

`conditions`（每檔 × 4 條件）：symbol, price_date, condition_id（C1_TREND／C2_BREAKOUT／C3_VOLUME／C4_TURNOVER）, condition_name, enabled, actual_value, actual_text, threshold_value, threshold_text, passed（True／False／None＝排除或未啟用）, reason（例「成立：收盤 134.17 > MA20 127.02，…」）, exclusion_code。

顯示建議：
- 入選表照 SPEC 顯示：代號、名稱、交易日期、收盤、漲跌幅、成交金額、MA20、MA60、量比，以及 `conditions.reason`。
- 排除清單顯示 `exclusion_reason`。
- 匯出 CSV：直接匯出 `summary` 加上 `conditions`。
- 數字格式（小數位、億元換算）在顯示層處理，不要回寫計算結果。

## 5. 排除代碼（顯示文字取 `screener.EXCLUSION_CODES`）

EXCL_NOT_COMMON_STOCK、EXCL_BAD_DATA、EXCL_NO_DATA、**EXCL_SUSPENDED**（確認停牌：trading_halts 或 daily_prices.is_halted）、**EXCL_NO_ROW_SUSPENDED_OR_MISSING**（無列且無法確認停牌：停牌或資料缺漏，待確認；含推定停牌區間內整個市場都缺行情的日子）、EXCL_NO_TRADE、**EXCL_VOLUME_BASIS**（量額口徑非總量：評估日 `volume_basis` 不是 `incl_block`〔C3 或 C4 啟用時，排在 EXCL_BAD_DATA 之後、EXCL_NEW_LISTING 之前〕，或均量窗口任一日不是 `incl_block`〔C3 啟用時，排在窗口資料錯誤之後、EXCL_ADJ_COVERAGE_GAP 之前〕；明細列出窗口非總量日數與首日）、EXCL_NEW_LISTING、EXCL_INSUFFICIENT_HISTORY、EXCL_WINDOW_GAPS、**EXCL_WINDOW_NO_TRADE**（無法評估：視窗內官方無成交日過多，不算資料不齊）、**EXCL_WINDOW_SUSPENDED**（無法評估：視窗內官方停牌或減資／變更面額停止買賣日過多，不算資料不齊）、EXCL_ADJ_COVERAGE_GAP、EXCL_ADJ_FACTOR_MISSING、EXCL_ADJ_FACTOR_INCONSISTENT、EXCL_UNEXPLAINED_GAP。

### 5.0 量額口徑 `volume_basis`（analysis-0.9.0）

- 只認 `incl_block` 為總量；`excl_block`、NULL、其他值一律不是總量。程式不讀 `block_shares`／`block_amount_twd`／`block_source`。
- `daily_prices` 沒有 `volume_basis` 欄（例如未遷移的庫）：研究用途照算，`inputs.volume_basis_status`／`result.volume_basis_status == "volume_basis_column_missing"`，所有列不做口徑排除、`volume_basis`／`window_volume_basis_days` 為空；`writer.write_screen_results` 遇到非 `"ok"` 直接拋錯拒寫正式庫。
- C3、C4 都關閉時不做 5a；C3 關閉時 9a 只計數不排除。

### 5.1 停牌：`EXCL_SUSPENDED` 與 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`

停牌資訊來源（兩者任一即「確認停牌」）：
- `trading_halts`（資料工程建表，欄位依 `pipeline/migrate.sql`：market, symbol, halt_date, resume_date, …, is_current）：**analysis-0.9.1 起（負責人 2026-11-17 18:31 核可）**停牌從 halt_date 開始算；有 resume_date 則停到 resume_date 前一天（來源「官方恢復日」）；resume_date 為 NULL 時停到 halt_date 之後第一個有官方成交列（有收盤價且成交量 > 0 的交易日）的前一天（來源「依成交推定」）；halt_date 之後到評估日都沒有成交則視為仍停牌（來源「仍停牌」）。**只用評估日（含）以前的成交，不前視**；halt_date 當天的成交不算恢復。恢復日與來源寫在 EXCL_SUSPENDED 明細與 `data_note`（「停牌紀錄：停牌日 …，…」）。`load_screen_inputs` 自動讀取；**表不存在時不報錯**，`inputs.halts_status == "halts_table_missing"`、`halts=None`。
- `daily_prices.is_halted = 1`（欄位存在才讀，不存在視為 0）。
- **資料不足（insufficient）規則（analysis-0.9.1 重做；負責人 2026-11-17 19:41 核可）**：只對推定區間（resume_date 為 NULL，來源「依成交推定」或「仍停牌」）。停牌日之後、評估日（含）以前、推定恢復日以前的交易日，若該股所屬市場當日 `daily_prices` 現行列數 < 100（`adjust.FULL_MARKET_MIN_ROWS`；停牌紀錄無市場時＝所有市場都 < 100），稱「整個市場都缺行情」。這種日子本股沒有資料列 → **不算停牌**，計入 `window_missing_days`，評估日遇到則為 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`（明細寫「本股無資料列，且該市場當日整個市場都缺行情」）。停牌日當天一律仍算停牌；全市場有資料、只是本股沒有列 → 照原規則算停牌；本股有無成交列 → 仍算停牌；官方恢復日的區間不套用。`data_note` 寫「推定區間內 N 個交易日整個市場都缺行情（起～訖），這些日子本股無資料列時不算停牌（資料不足（insufficient））」。缺行情日只數評估日（含）以前，不前視（管線用整個資料庫判定，見 docs/indicators.md 第 10 節）。`trading_halts.resume_basis`／`resume_date_inferred`（管線 3b 欄）只寫入 `data_note`「管線交叉檢查：…」，不參與判定。

| 評估日情況 | 排除代碼 |
|---|---|
| 該市場當日完全無行情 | `EXCL_NO_DATA` |
| 本股無列，且確認停牌 | `EXCL_SUSPENDED` |
| 本股無列，且無停牌紀錄（可能停牌，也可能漏抓） | `EXCL_NO_ROW_SUSPENDED_OR_MISSING` |
| 本股無列，落在推定停牌區間內但該市場當日整個市場都缺行情（< 100 列；資料不足） | `EXCL_NO_ROW_SUSPENDED_OR_MISSING` |
| 本股有列但無成交，且確認停牌（或 is_halted=1） | `EXCL_SUSPENDED` |
| 本股有列但無成交，非確認停牌 | `EXCL_NO_TRADE` |

網站顯示：`EXCL_SUSPENDED` 可直接寫「停牌」；`EXCL_NO_ROW_SUSPENDED_OR_MISSING` 必須寫「停牌或資料缺漏，待確認」，不要寫成停牌。
視窗缺口：`window_gap_days` ＝ `window_halt_days`（確認停牌）＋ `window_missing_days`（無列且無法確認停牌，含推定區間內整個市場都缺行情的日子）＋ `window_no_trade_days`（有列無成交）＋ `window_bad_data_days`（有收盤價但成交量 NULL），合計與 `max_gap_days` 比較（超過即排除，篩選門檻不變；若官方無成交＋確認停牌＋減資／變更面額停止買賣日單獨已超過門檻，改標 `EXCL_WINDOW_NO_TRADE` 或 `EXCL_WINDOW_SUSPENDED`（無法評估，不進 95% 分母），否則為 `EXCL_WINDOW_GAPS`（資料不齊））；未超過但 `window_bad_data_days` > 0 → `EXCL_BAD_DATA`。整股無成交的判定只看價量，`is_no_trade` 只做交叉檢查（`data_note`）。
`ma_series`／`change_series` 不讀停牌表，停牌日在序列中仍標為無成交（`no_trade`）。

## 6. 覆蓋表 `corporate_actions_coverage`（資料工程建立）

負責人決議欄位 (market, action_kind, coverage_start)；資料工程已建表，另有 coverage_end（NOT NULL）、source、fetched_at、notes，同一 market＋kind 可多段、多來源，本層合併後判定。真實資料庫目前此表為空 → `coverage_table_empty`。
action_kind 完整清單（`adjust.COVERAGE_KINDS`，6 類）：`cash_dividend`、`stock_dividend`、`ex_both`、`capital_reduction`、`par_value_change`、`stock_split`，strict 下每個市場六類都要涵蓋。`stock_split` 由 `par_value_change` 覆蓋（`adjust.COVERAGE_COVERED_BY`）：只有 `par_value_change` 列、或另寫 `stock_split` 列（notes `covered_by=par_value_change`）都算涵蓋；反向不成立。相容值：`ex_right_dividend`／`dividend`（＝前三類）、`ex_dividend`、`ex_rights`、`capital_reduction_restore`。其他值不計入覆蓋。

| coverage_note | 意義 |
|---|---|
| coverage_table_missing | 沒有這張表 |
| coverage_table_empty | 表是空的 |
| coverage_missing:市場:類別 | 該市場該類別沒有列（類別為上述六類之一） |
| window_before_coverage_start:市場:類別:起日 | 視窗起日早於覆蓋起日 |
| coverage_end_before_as_of:市場:類別:迄日 | 覆蓋迄日早於評估日 |
| coverage_hole:市場:類別:評估日 | 評估日落在覆蓋缺口 |
| coverage_scope_none_accepted | 有 scope 欄但所有列的 scope 都不被本次評估範圍接受（`ScreenInputs.coverage_status`） |

### 6.1 覆蓋表 scope（analysis-0.9.1 重做；負責人 2026-11-17 19:41 核可）

管線 3b 起 `corporate_actions_coverage` 多 `scope` 欄；3b_fix1 起值為 `all`／`all_pending_ratio`／`sample30`／`subset`／`unknown`，且 scope 屬主鍵。
- **資料庫有 scope 欄**：`coverage_scope="all"`（預設，全市場評估）只接受 `scope='all'`；`coverage_scope="tracked"`（追蹤清單）接受 `sample30` 或 `all`。其他值（`all_pending_ratio`、`subset`、NULL、大小寫不同…）都不算齊全，當作沒有該列；某類別因此沒有可用列 → 原本的 strict 排除（`EXCL_ADJ_COVERAGE_GAP`，coverage_note 如 `coverage_missing:TWSE:capital_reduction`）。全部列都不接受 → `coverage_status="coverage_scope_none_accepted"`，全部 strict 排除。
- **資料庫沒有 scope 欄**：維持現狀（`coverage_scope_note=None`，data_note 不加字）。
- `"tracked"` 必須同時傳 `symbols`（否則 `ValueError`）；其他值 `ValueError`。預設守保只接受 all。
- **追蹤清單判定（管線 3b_fix1）**：權威來源 `pipeline/config.py` 的 `SAMPLE30`，DB 鏡像 `universe_members`（`universe_id='sample30'`，docs/schema.md §3.30）。`"tracked"` 只有在傳入的 symbols **全部**屬於 `universe_members` sample30（不看 included）時才接受 sample30；表不存在／沒有 sample30 列／有任何代號不在清單內 → 保守只接受 all（`coverage_scope_note` 寫明原因與不在清單的代號）。`io_sqlite.tracked_members(con)` 回傳清單（無表 → None）。
- **同一區間多列（3b_fix1 主鍵含 scope）**：all、all_pending_ratio（all 的特例，與 all 共用一列、不同時存在）、sample30、subset、unknown（舊 NULL 列）可並存。不採用的列直接略過，採用的列照常合併成覆蓋區段；unknown、subset、all_pending_ratio 在兩種模式都不算完整。
- 採用範圍寫在 `ScreenInputs.coverage_scope_note`、`ScreenResult.coverage_scope_note`，並附在覆蓋有問題（coverage_note 非空）各列的 `data_note`：「除權息覆蓋表 scope：評估範圍＝全市場，只接受 all；採用 N 列，不採用 M 列（值 計數，視為無覆蓋）」。
- **呼叫端怎麼傳（3b 遷移後必做，否則 sample30 網站結果會全部變成 EXCL_ADJ_COVERAGE_GAP）**：3b_fix1 的 `db._migrate_coverage_pk` 把舊 NULL 列填成 `sample30`（corporate_actions 代號全在 SAMPLE30 內）或 `unknown`。
  - `pipeline/analysis_bridge.run_screen_and_write`：`symbols_mode == "all"` 時 `load_screen_inputs(db, as_of=…, symbols=None)`（預設 all）；sample30（傳代號清單）時改為 `load_screen_inputs(db, as_of=…, symbols=syms, coverage_scope="tracked")`。可用 `pipeline/scope.scope_of(symbols, conn, market)`：回傳 `sample30` 時傳 `"tracked"`，`all` 時不傳；`subset` 時傳 `"all"`（預設）——分析端的 sample30 判定本來就會再用 universe_members 核對，傳錯也只會變保守。
  - `web/app/data_source.py` 的選股（`load_screen_inputs(..., symbols=list(uni))`，追蹤 30 檔）與 `web/app/analysis_hooks.py` 的 `load_symbol_inputs(...)`：追蹤清單請傳 `coverage_scope="tracked"`。
  - 3b 遷移前（沒有 scope 欄）不傳也不會改變結果。

## 7. 範例：限定 30 檔、調整條件

```python
inputs = load_screen_inputs(DB, as_of="2026-11-09", symbols=["2330", "2317", "6488"])
p = ScreenParams(volume_multiplier=2.0, min_turnover=5e8, enable_trend=False)
r = screen(**inputs.screen_kwargs(), params=p)
for row in r.selected().itertuples():
    print(row.symbol, row.name, row.close_twd, f"{row.change_pct:.2f}%", f"量比 {row.volume_ratio:.2f}")
print(r.exclusion_counts())
```

## 8. 測試

```bash
cd /workspace/tw-stock-site/analysis && .venv/bin/python -m pytest tests -q
.venv/bin/python verify_independent.py --test-summary "…"   # 合成資料獨立重算（篩選＋均線序列＋漲跌幅），更新 docs/verification.md
.venv/bin/python verify_independent.py --db ../data/twstock.db --label "…"   # 真實資料節（與合成資料分開）
.venv/bin/python verify_revenue.py                       # 月營收：測試用合成資料（TESTR*）兩份實作比對，不寫檔
.venv/bin/python verify_revenue.py --db ../data/twstock.db --month 2026-10 --expected 2308,… --write   # 唯讀（mode=ro＋backup 快照）檢查，寫 verification.md 第三節「唯讀檢查、非正式結果」
```
`tests/test_writer.py` 只用暫存 SQLite 測寫入（絕不寫 data/twstock.db）。`tests/test_usage_doc.py` 檢查本檔版本號、排除代碼、覆蓋類別與程式常數一致；改程式時請同步更新本檔與 `version.py`。
測試全部使用 `tests/` 下的測試用合成資料（代號 TEST*），不可匯入正式資料庫或網站正式頁面；`screen_results` 草案以 CHECK 擋下 TEST 開頭代號。

## 9. 篩選中心模式 M9～M21（`modes.py`，analysis-0.10.0；docs/indicators.md §12）

專案負責人核可 2026-11-24 16:05，**暫定分級**。規則定義見 docs/indicators.md 第 12 節，研究依據見 docs/rd/SCREEN_MODES_v2.md（含第 9 節「預先登記」）。

```python
import sys; sys.path.insert(0, "/workspace/tw-stock-site/analysis")
import io_sqlite, modes

mi = io_sqlite.load_mode_inputs(DB, as_of="2026-11-23")          # 行情往前 300 個交易日＋monthly_revenues＋income_statements（唯讀）
r = modes.evaluate_modes(**mi.mode_kwargs())                      # 預設 M9～M21、variant="short"（目前上線版本）
# 已經跑過同一 as_of 的 screen()（預設參數＝M1）時可傳 screen_result=… 省掉約 12 秒；參數不是預設會拋 ValueError
r.selected("M18")          # 符合（PASS）的股票；0 檔就是空表，顯示「0 檔」，不要放寬
r.mode_summary("M18")      # 該模式每檔一列：status PASS／FAIL／EXCLUDED、exclusion_code、exclusion_reason、reason_zh、metric
r.conditions               # 每檔 × 每條件：condition_id、condition_name、actual_text、threshold_text、passed
r.coverage_stats("M18")    # 與 screener.coverage_stats 相同口徑（NOT_EVALUABLE_CODES 不入分母，網站以 95% 判斷「資料不足」）
r.counts()                 # {mode_id: PASS 檔數}
modes.registry()           # 登記表（dict）；檔案版 analysis/mode_registry.json
```

- `summary` 欄位：`mode_id, symbol, name, market, price_date, status, exclusion_code, exclusion_reason, reason_zh, metric, close_twd, amount_twd, variant`。
  `reason_zh`：PASS 為「符合：條件（實際值，門檻 …）；…」，FAIL 為「未符合：」只列沒過的條件；EXCLUDED 看 `exclusion_reason`。
  `metric`：登記表 `metric.key` 指的排序指標（原始比例；顯示時乘 `metric.scale`），只供排序，不代表優劣。
- 排除碼：screen() 的 `EXCL_*`（同第 5 節）、`revenue.REASON_CODES` 的 `REV_*`、`modes.FIN_CODES` 的 `FIN_*`；全部中文在 `modes.MODE_EXCLUSION_CODES`。
  模式需要的有效交易日比 screen 多（M13、M14、M18、M19 約 120～140 日）時：上市日在視窗內記 `EXCL_NEW_LISTING`（不入分母），否則 `EXCL_INSUFFICIENT_HISTORY`；
  前復權可用範圍不夠長記該範圍的原因碼（例如 `EXCL_ADJ_FACTOR_MISSING`）。
- 營收／財報：評估日已過法定期限的最新月份（次月 10 日）／季度（5/15、8/14、11/14、次年 3/31）；該檔沒有那一期資料就是資料不足（`REV_CURRENT_MISSING`、`FIN_CURRENT_MISSING` 等），**不會改用更舊的月份或季度**。
- 橫斷面排名（M12、M14、M19）在「本次評估範圍內當日可評估的股票」之間計算：網站應以全市場（`symbols=None`、`coverage_scope="all"`）計算；只評估追蹤清單時排名意義不同，須標示。
- `variant="full"`：用登記表 `full_params`（M13、M14、M15、M18、M19 的完整版，需 200～251 個有效交易日）；3 年回補前大多是歷史不足，上線版本以登記表 `active_variant` 為準（目前全部 `short`）。
- 勝率：本模組不算勝率；網站一律顯示登記表 `win_rate.display`（「計算中」；M5、M8 為「樣本不足」），回補重算後才更新。不要顯示研究稿的初步數字。
- 效能：2026-11-23 唯讀快照全市場 1,956 檔約 58 秒、峰值約 0.7 GB（screen 約 12 秒＋每檔 ohlc_series）；網站應每個 as_of 算一次後快取，不要在請求中即時算。
- 登記表欄位：`id, tier, tier_prev, name, desc, conditions, research_id, engine, params, full_params, active_variant, history_days, full_history_days, data, metric{key,label,digits,scale}, win_rate{display,note}, cross_sectional, state(existing／production), note`；
  頂層另有 `registry_version, analysis_version, tier_basis, tiers, winrate_display, winrate_note, disclaimer, sort_note, cross_sectional_note`。
  M1～M8 的 `state=existing`：規則仍由 screener／revenue（網站 hub）計算，登記表只提供名稱、說明與新的暫定等級（`tier_prev`＝原等級）。
- 測試：`tests/test_modes.py`（合成資料 `tests/modes_synthetic.py`，代號 TESTM*／TESTF*；含與研究腳本 `research/screen_modes_v2/` 在抽樣日 PASS 名單一致）。
  真實快照比對：`python research/screen_modes_v2/j_equality.py research …` → `… compare …`，報告在 `docs/rd/screen_modes_v2/equality.txt`。
