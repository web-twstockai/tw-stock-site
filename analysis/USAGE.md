# 研究分析模組呼叫方式（給網站工程）

**版本：`analysis-0.8.0`**（`analysis/version.py` 的 `ANALYSIS_VERSION`，寫入 `screen_results.analysis_version`）。每批程式變更完成後版本號加一，並同步更新本檔；`tests/test_usage_doc.py` 檢查本檔的版本號、排除代碼與覆蓋類別是否與程式常數一致。

### 變更紀錄

| 版本 | 日期 | 變更 |
|---|---|---|
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
| `io_sqlite.py` | 唯讀 SQLite 讀取層 |

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
                             lookback_trade_days: int = 250) -> ScreenInputs
io_sqlite.load_symbol_inputs(db_path, symbol: str, as_of: str | None = None,
                             lookback_trade_days: int = 250) -> dict   # 直接展開給 ma_series／change_series
io_sqlite.load_coverage(con) -> (DataFrame | None, status)
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
| halts | `trading_halts`：symbol, halt_date（可選 resume_date, is_current）；None＝無停牌資訊（確認停牌只能靠 `is_halted`） |

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
narrative.summary_narrative(result, extra_facts=(), news_events=(), max_listed=50) -> Narrative
```
- 只用規則模板（`generation_method = "rule"`），不寫漲跌原因；0 檔時固定寫「今日 0 檔符合全部已啟用條件。」並列排除原因統計。
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
| change_base_twd, source_change_twd, change_check, change_check_diff_twd | 漲跌幅基準價、來源帶號漲跌、交叉檢查結果與差值 |
| volume_shares, amount_twd | 成交量（股）、成交金額（元）；**暫定口徑＝官方公布總量，兩市場目前不同**：上市（STOCK_DAY／MI_INDEX）含零股、盤後定價、鉅額；上櫃（tradingStock）不含鉅額，零股／盤後未聲明。跨市場比較要留意，網站須標示（docs/indicators.md 第 0 節） |
| ma_short, ma_long | MA20、MA60（還原後，元） |
| prior_high_twd, prior_avg_volume_shares, volume_ratio | 前 20 日最高（還原）、前 20 日均量（股，股數因子還原）、量比 |
| pass_trend, pass_breakout, pass_volume, pass_turnover | 各條件是否成立（EXCLUDED 為空） |
| price_version | 讀取當時 `daily_prices.version` |
| n_valid_days, window_start, window_gap_days | 有效交易日數、視窗起日、視窗內非有效交易日數（合計，與 max_gap_days 比較） |
| window_halt_days, window_missing_days, window_no_trade_days | 上一欄拆解：確認停牌／無資料列且無停牌紀錄／有列無成交 |
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

EXCL_NOT_COMMON_STOCK、EXCL_BAD_DATA、EXCL_NO_DATA、**EXCL_SUSPENDED**（確認停牌：trading_halts 或 daily_prices.is_halted）、**EXCL_NO_ROW_SUSPENDED_OR_MISSING**（無列且無停牌紀錄：停牌或資料缺漏，待確認）、EXCL_NO_TRADE、EXCL_NEW_LISTING、EXCL_INSUFFICIENT_HISTORY、EXCL_WINDOW_GAPS、EXCL_ADJ_COVERAGE_GAP、EXCL_ADJ_FACTOR_MISSING、EXCL_ADJ_FACTOR_INCONSISTENT、EXCL_UNEXPLAINED_GAP。

### 5.1 停牌：`EXCL_SUSPENDED` 與 `EXCL_NO_ROW_SUSPENDED_OR_MISSING`

停牌資訊來源（兩者任一即「確認停牌」）：
- `trading_halts`（資料工程建表，欄位依 `pipeline/migrate.sql`：market, symbol, halt_date, resume_date, …, is_current）：交易日 d 若 halt_date ≤ d 且（resume_date 為 NULL 或 d < resume_date）即為停牌日。`load_screen_inputs` 自動讀取；**表不存在時不報錯**，`inputs.halts_status == "halts_table_missing"`、`halts=None`。
- `daily_prices.is_halted = 1`（欄位存在才讀，不存在視為 0）。

| 評估日情況 | 排除代碼 |
|---|---|
| 該市場當日完全無行情 | `EXCL_NO_DATA` |
| 本股無列，且確認停牌 | `EXCL_SUSPENDED` |
| 本股無列，且無停牌紀錄（可能停牌，也可能漏抓） | `EXCL_NO_ROW_SUSPENDED_OR_MISSING` |
| 本股有列但無成交，且確認停牌（或 is_halted=1） | `EXCL_SUSPENDED` |
| 本股有列但無成交，非確認停牌 | `EXCL_NO_TRADE` |

網站顯示：`EXCL_SUSPENDED` 可直接寫「停牌」；`EXCL_NO_ROW_SUSPENDED_OR_MISSING` 必須寫「停牌或資料缺漏，待確認」，不要寫成停牌。
視窗缺口：`window_gap_days` ＝ `window_halt_days`（確認停牌）＋ `window_missing_days`（無列且無停牌紀錄）＋ `window_no_trade_days`（有列無成交），合計與 `max_gap_days` 比較（超過即 `EXCL_WINDOW_GAPS`）。
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
