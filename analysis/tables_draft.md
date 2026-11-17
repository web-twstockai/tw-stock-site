# 研究分析寫入表 DDL 草案（screen_results／news_events／news_event_symbols／daily_summaries）

> 狀態：**草案，待專案負責人核可；實際建表一律由資料工程執行**（負責人 2026-11-10 決議 G）。
> 本檔由研究分析提出欄位與約束；研究分析程式只寫入這三張表，以及隨 `news_events` 一併寫入的 `news_event_symbols`（第四張，資料工程建表）。`index_daily`、`news_items`、`news_item_symbols`、
> `material_infos`、`daily_prices`、`corporate_actions`、`corporate_actions_coverage` 由資料工程寫入，研究分析**只讀**。
>
> 最後更新：2026-11-10（Asia/Taipei，UTC+8），analysis-0.6.0。對照 `docs/schema.md` §7.7（四張表已由資料工程建表）與 DECISIONS 2026-11-10 03:45 ③；`data/twstock.db` 實際欄位已含 computed_at／input_max_fetched_at、news_events 已移除 related_symbols_json，daily_summaries 另有 data_date／provider／screen_run_id（網站讀取用）。screen_results 的 window_halt_days／window_missing_days／window_no_trade_days 已補欄（2026-11-10 04:0x 唯讀確認）；screen_results 目前沒有 screen_run_id 欄（writer 有此欄才寫）。
> 寫入程式：`analysis/writer.py`（第 5 節程序的實作；測試 `tests/test_writer.py` 只用暫存 SQLite）。
> 下方 DDL 區塊由 `analysis/tests/test_tables_draft.py` 在記憶體 SQLite（3.46）實際執行並做約束測試。

## 1. 共同約定

| 項目 | 約定 | 理由 |
|---|---|---|
| 時間 | `generated_at`、`reviewed_at` 等為 ISO-8601 含 `+08:00`；日期欄 `YYYY-MM-DD` | 與 schema.md `fetched_at` 一致 |
| 單位 | 量＝股、金額＝新台幣元（暫定口徑：官方公布總量；上市含零股、盤後定價、鉅額，上櫃不含鉅額，兩市場目前不同，見 docs/indicators.md 第 0 節）、價格＝元、百分比欄位名帶 `_pct` 且為「百分點」（例如 1.23 表示 1.23%） | SPEC／負責人決議；與 `screener` 輸出一致 |
| 版本 | `analysis_version`＝`analysis/version.py` 的 `ANALYSIS_VERSION`（`screener.ANALYSIS_VERSION` 同值；目前 `analysis-0.4.0`，變更紀錄見 `analysis/USAGE.md` 開頭） | 程式變更可追溯 |
| 參數 | `params_json`＝`ScreenParams.to_dict()` 依鍵排序的 JSON；`params_hash`＝其 SHA-256 十六進位（`screener.params_hash()`） | 同參數可重現同結果 |
| 冪等 | 自然鍵＋`version`＋`is_current`（與資料工程相同）；寫入前以 `checksum` 比對現行列：相同→不寫；不同→舊列 `is_current=0`、新列 `version=舊+1` | 見第 5 節 |
| 合成資料 | 代號 `TEST` 開頭者以 CHECK 擋下，測試用合成資料不可能寫入正式表 | 原則：合成資料不得混入正式結果 |
| 覆蓋政策 | `screen_results.coverage_policy` 以 CHECK 限定 `strict` | 決議 B：正式結果一律 strict，不自動切 gap_check |

## 2. DDL

```sql
-- ============================================================
-- screen_results：每個 (資料日, 代號, 參數組) 一列；寫入者＝研究分析
-- 來源：analysis/screener.py 的 ScreenResult.summary + conditions（四條件展開成欄）
-- ============================================================
CREATE TABLE IF NOT EXISTS screen_results (
  as_of                         TEXT    NOT NULL,              -- 資料日（交易日）YYYY-MM-DD
  symbol                        TEXT    NOT NULL CHECK (symbol NOT LIKE 'TEST%'),
  params_hash                   TEXT    NOT NULL CHECK (length(params_hash) = 64),
  params_json                   TEXT    NOT NULL CHECK (json_valid(params_json)),
  coverage_policy               TEXT    NOT NULL DEFAULT 'strict' CHECK (coverage_policy = 'strict'),
  market                        TEXT,                          -- TWSE／TPEx
  name                          TEXT,
  status                        TEXT    NOT NULL CHECK (status IN ('PASS', 'FAIL', 'EXCLUDED')),
  exclusion_code                TEXT,                          -- 見 analysis/USAGE.md 排除代碼表
  exclusion_reason              TEXT,                          -- 中文說明＋細節（程式產生）
  coverage_note                 TEXT,                          -- coverage_table_missing／coverage_table_empty／coverage_missing:M:K／window_before_coverage_start:M:K:日期／coverage_end_before_as_of:M:K:日期／coverage_hole:M:K:日期，多項以 ; 分隔
  -- 條件 C1 趨勢：收盤 > MA短 > MA長（還原收盤）
  c1_enabled                    INTEGER NOT NULL CHECK (c1_enabled IN (0, 1)),
  c1_actual_close_twd           REAL,
  c1_threshold_ma_short_twd     REAL,
  c1_threshold_ma_long_twd      REAL,
  c1_passed                     INTEGER CHECK (c1_passed IN (0, 1)),   -- 未啟用或排除時為 NULL
  -- 條件 C2 突破：收盤 > 前 N 日最高價（還原，不含當日）
  c2_enabled                    INTEGER NOT NULL CHECK (c2_enabled IN (0, 1)),
  c2_actual_close_twd           REAL,
  c2_threshold_prior_high_twd   REAL,
  c2_passed                     INTEGER CHECK (c2_passed IN (0, 1)),
  -- 條件 C3 量能：成交量 ≥ 前 N 日均量（股數因子還原）× 倍數
  c3_enabled                    INTEGER NOT NULL CHECK (c3_enabled IN (0, 1)),
  c3_actual_volume_shares       REAL,
  c3_prior_avg_volume_shares    REAL,
  c3_threshold_volume_shares    REAL,                          -- = c3_prior_avg_volume_shares × volume_multiplier
  c3_volume_ratio               REAL,
  c3_passed                     INTEGER CHECK (c3_passed IN (0, 1)),
  -- 條件 C4 成交金額 ≥ 門檻（不還原）
  c4_enabled                    INTEGER NOT NULL CHECK (c4_enabled IN (0, 1)),
  c4_actual_amount_twd          REAL,
  c4_threshold_amount_twd       REAL,
  c4_passed                     INTEGER CHECK (c4_passed IN (0, 1)),
  conditions_json               TEXT    NOT NULL CHECK (json_valid(conditions_json)),  -- ScreenResult.conditions 該股四列原文（含 actual_text／threshold_text／reason）
  -- 當日數值
  close_twd                     REAL,
  change_pct                    REAL,                          -- 收盤 ÷ 基準價 − 1（百分點）；事件日基準＝參考價
  change_base_twd               REAL,
  source_change_twd             REAL,                          -- 來源漲跌（帶號；不覆蓋自算值）
  change_check                  TEXT    CHECK (change_check IN ('ok', 'mismatch', 'no_source', 'source_not_comparable', 'source_sign_conflict')),
  change_check_diff_twd         REAL,
  volume_shares                 REAL,                          -- 官方總量口徑，依市場不同，見 daily_prices.source（上市含零股／盤後定價／鉅額；上櫃不含鉅額）
  amount_twd                    REAL,                          -- 同上口徑（元），跨市場比較要留意
  n_valid_days                  INTEGER,
  window_start                  TEXT,
  window_gap_days               INTEGER,                       -- 視窗內非有效交易日數（＝下三欄合計；門檻判斷用合計）
  window_halt_days              INTEGER,                       -- 其中確認停牌（trading_halts／is_halted）
  window_missing_days           INTEGER,                       -- 其中無資料列且無停牌紀錄
  window_no_trade_days          INTEGER,                       -- 其中有列但無成交
  n_adj_events                  INTEGER,
  adj_events                    TEXT,                          -- 視窗內調整事件與 f／s 因子文字
  -- 追溯
  price_source                  TEXT,                          -- daily_prices.source
  price_fetched_at              TEXT,                          -- daily_prices.fetched_at
  price_version                 INTEGER,                       -- daily_prices.version（讀取當時的現行版）
  analysis_version              TEXT    NOT NULL,              -- analysis/version.py 的 ANALYSIS_VERSION
  computed_at                   TEXT    NOT NULL,              -- 篩選計算時間（ScreenResult.computed_at，ISO-8601 +08:00）
  input_max_fetched_at          TEXT,                          -- 計算所用輸入列 fetched_at 最大值（io_sqlite.ScreenInputs.input_max_fetched_at）
  generated_at                  TEXT    NOT NULL,              -- 寫入時間（writer.py）
  checksum                      TEXT    NOT NULL,              -- 除 computed_at／input_max_fetched_at／generated_at／version／is_current 外全部欄位的 SHA-256
  version                       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  is_current                    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
  PRIMARY KEY (as_of, symbol, params_hash, version),
  CHECK ((status = 'EXCLUDED') = (exclusion_code IS NOT NULL)),
  CHECK (status <> 'PASS' OR (coalesce(c1_passed, 1) = 1 AND coalesce(c2_passed, 1) = 1
                              AND coalesce(c3_passed, 1) = 1 AND coalesce(c4_passed, 1) = 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_screen_results_current
  ON screen_results(as_of, symbol, params_hash) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_screen_results_day
  ON screen_results(as_of, params_hash, status) WHERE is_current = 1;          -- 選股頁：某日某參數的 PASS 清單
CREATE INDEX IF NOT EXISTS ix_screen_results_symbol
  ON screen_results(symbol, as_of) WHERE is_current = 1;                       -- 個股頁：歷次篩選紀錄
CREATE INDEX IF NOT EXISTS ix_screen_results_excl
  ON screen_results(as_of, exclusion_code) WHERE is_current = 1;               -- 排除原因統計

-- ============================================================
-- news_events：去重後的「同一事件」；寫入者＝研究分析
-- 讀取：news_items(news_id …)、news_item_symbols(news_id, symbol, evidence)、material_infos（資料工程寫入）
-- ============================================================
CREATE TABLE IF NOT EXISTS news_events (
  event_id                      TEXT    NOT NULL,              -- 'EV-' + 錨點鍵 SHA-256 前 16 碼（錨點＝官方公告鍵；無官方則最早一則 news_items.event_key）
  category                      TEXT    NOT NULL CHECK (category IN ('官方已公告事實', '媒體報導', '社群未證實訊息', 'AI分析推論')),
  topic_tags_json               TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(topic_tags_json)),     -- 例 ["重大訊息/公司公告","營收財報"]
  title                         TEXT    NOT NULL,              -- 代表標題（官方公告主旨優先；原文，不改寫）
  first_published_at            TEXT    NOT NULL,              -- 成員中最早發布時間
  last_published_at             TEXT    NOT NULL,
  news_ids_json                 TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(news_ids_json)),        -- news_items.news_id 陣列（PK）
  material_info_keys_json       TEXT    CHECK (material_info_keys_json IS NULL OR json_valid(material_info_keys_json)),
                                                               -- material_infos 自然鍵陣列 [{symbol, announce_date, announce_time, content_hash}]
  official_url                  TEXT,                          -- material_infos.url 或官方頁
  source_urls_json              TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(source_urls_json)),     -- 全部成員正規化 URL
  -- related_symbols_json 已移除（DECISIONS 2026-11-10 03:45 ②）：關聯代號以 news_event_symbols 為準
  relation_evidence_json        TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(relation_evidence_json)),
                                                               -- 與同版本 news_event_symbols 同內容的快照（writer.py 由同一份清單產生）
  member_categories_json        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(member_categories_json)), -- 成員分類計數 {"媒體報導": 3, …}
  dedup_method                  TEXT    NOT NULL CHECK (dedup_method IN ('official_anchor', 'url_canonical', 'title_similarity', 'single')),
  dedup_keys_json               TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(dedup_keys_json)),      -- 正規化 URL／content_hash／標題 2-gram 指紋
  dedup_params_json             TEXT    NOT NULL CHECK (json_valid(dedup_params_json)),                 -- {"jaccard_min":0.6,"window_hours":48}
  summary_text                  TEXT,                          -- 規則摘要（只陳述來源內容，不寫原因）
  inference_text                TEXT,                          -- 僅 AI分析推論：可能影響；須同時有依據與待確認事項
  inference_basis_json          TEXT    CHECK (inference_basis_json IS NULL OR json_valid(inference_basis_json)),
  pending_checks_json           TEXT    CHECK (pending_checks_json IS NULL OR json_valid(pending_checks_json)),
  generation_method             TEXT    NOT NULL DEFAULT 'rule' CHECK (generation_method IN ('rule', 'ai', 'rule+ai')),
  analysis_version              TEXT    NOT NULL,
  computed_at                   TEXT    NOT NULL,              -- 事件聚合計算時間
  input_max_fetched_at          TEXT,                          -- 所用 news_items／material_infos 列 fetched_at 最大值
  generated_at                  TEXT    NOT NULL,              -- 寫入時間
  checksum                      TEXT    NOT NULL,              -- 事件欄位＋news_event_symbols 清單的 SHA-256（不含時間欄、version、is_current）
  version                       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  is_current                    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
  PRIMARY KEY (event_id, version),
  CHECK (json_array_length(news_ids_json) > 0 OR material_info_keys_json IS NOT NULL),
  CHECK (category <> '官方已公告事實' OR material_info_keys_json IS NOT NULL OR official_url IS NOT NULL),
  CHECK (inference_text IS NULL OR (inference_basis_json IS NOT NULL AND pending_checks_json IS NOT NULL)),
  CHECK (first_published_at <= last_published_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_news_events_current
  ON news_events(event_id) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_news_events_published
  ON news_events(first_published_at) WHERE is_current = 1;                     -- 當日新聞區塊
CREATE INDEX IF NOT EXISTS ix_news_events_category
  ON news_events(category, first_published_at) WHERE is_current = 1;

-- ============================================================
-- news_event_symbols：事件 × 關聯代號（含證據）；建表＝資料工程，寫入＝研究分析（與 news_events 同一交易寫入）
-- 每個 news_events 版本各自一組列（版本不可變）；查「現行」請 JOIN news_events ON (event_id, version) AND is_current = 1
-- ============================================================
CREATE TABLE IF NOT EXISTS news_event_symbols (
  event_id                      TEXT    NOT NULL,
  version                       INTEGER NOT NULL,              -- = news_events.version
  symbol                        TEXT    NOT NULL CHECK (symbol NOT LIKE 'TEST%'),
  evidence_type                 TEXT    NOT NULL CHECK (evidence_type IN ('官方公告代號', '內文代號', '公司全名')),
  evidence                      TEXT,                          -- 證據原文（news_item_symbols.evidence 或公告欄位）
  source_news_id                INTEGER,                       -- 證據出處 news_items.news_id
  source_material_key_json      TEXT    CHECK (source_material_key_json IS NULL OR json_valid(source_material_key_json)),
  PRIMARY KEY (event_id, version, symbol, evidence_type),
  FOREIGN KEY (event_id, version) REFERENCES news_events(event_id, version),
  CHECK (source_news_id IS NOT NULL OR source_material_key_json IS NOT NULL)       -- 無證據不關聯
);
CREATE INDEX IF NOT EXISTS ix_news_event_symbols_symbol
  ON news_event_symbols(symbol, event_id, version);                            -- 個股頁：依代號查事件

-- ============================================================
-- daily_summaries：每日盤後摘要；寫入者＝研究分析（analysis/narrative.py）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_summaries (
  summary_date                  TEXT    NOT NULL,              -- 資料日（= screen_results.as_of）
  summary_kind                  TEXT    NOT NULL DEFAULT 'post_market' CHECK (summary_kind IN ('post_market')),
  params_hash                   TEXT    NOT NULL CHECK (length(params_hash) = 64),   -- 引用的篩選參數組
  screen_ref_json               TEXT    NOT NULL CHECK (json_valid(screen_ref_json)),
                                                               -- {"as_of","params_hash","screen_checksum"(所引用現行列 checksum 依代號排序後再雜湊),"n_rows"}
  text                          TEXT    NOT NULL,              -- 摘要全文（含固定免責句）
  facts_json                    TEXT    NOT NULL CHECK (json_valid(facts_json)),
                                                               -- narrative.Fact 陣列：fact_id, label, value, display, unit, source_table, source_field, source_key, data_date, upstream
  news_event_ids_json           TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(news_event_ids_json)),  -- 引用的 news_events.event_id（含版本）
  news_categories_json          TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(news_categories_json)), -- 引用新聞分類計數 {"官方已公告事實":1,…}
  n_pass                        INTEGER NOT NULL CHECK (n_pass >= 0),                 -- 0 檔照實寫 0
  n_fail                        INTEGER NOT NULL CHECK (n_fail >= 0),
  n_excluded                    INTEGER NOT NULL CHECK (n_excluded >= 0),
  generation_method             TEXT    NOT NULL CHECK (generation_method IN ('rule', 'ai', 'rule+ai')),
  ai_prompt_version             TEXT,                          -- generation_method 含 ai 時必填
  traceability_check            TEXT    NOT NULL CHECK (traceability_check IN ('pass', 'fail')),       -- 文字數字皆可在 facts_json 找到
  untraceable_json              TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(untraceable_json)),
  review_status                 TEXT    NOT NULL DEFAULT 'unreviewed'
                                        CHECK (review_status IN ('unreviewed', 'approved', 'rejected', 'not_required')),
  reviewed_by                   TEXT,
  reviewed_at                   TEXT,
  review_note                   TEXT,
  analysis_version              TEXT    NOT NULL,
  computed_at                   TEXT    NOT NULL,              -- 摘要產生（計算）時間
  input_max_fetched_at          TEXT,                          -- 所引用篩選結果的 input_max_fetched_at（取最大）
  generated_at                  TEXT    NOT NULL,              -- 寫入時間（網站首頁顯示「存檔時間」）
  data_date                     TEXT,                          -- = summary_date（資料庫已有、網站讀取用；writer 同值寫入）
  provider                      TEXT,                          -- 產生者說明（網站顯示；writer 寫「研究分析規則（analysis/narrative.py）」）
  screen_run_id                 TEXT,                          -- 產生所引用 screen_results 批次的 job_runs.run_id（pipeline.run 傳入；不納入 checksum）
  checksum                      TEXT    NOT NULL,              -- text + facts_json + screen_ref_json + news_event_ids_json 的 SHA-256
  version                       INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  is_current                    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
  PRIMARY KEY (summary_date, summary_kind, params_hash, version),
  CHECK (generation_method = 'rule' OR ai_prompt_version IS NOT NULL),
  CHECK (review_status <> 'not_required' OR generation_method = 'rule'),      -- 只有純規則摘要可免人工審核
  CHECK (traceability_check = 'pass' OR review_status <> 'approved'),          -- 追溯失敗不得核可
  CHECK (review_status IN ('unreviewed', 'not_required') OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_summaries_current
  ON daily_summaries(summary_date, summary_kind, params_hash) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_daily_summaries_review
  ON daily_summaries(review_status, summary_date) WHERE is_current = 1;          -- 待審清單
```

## 3. 欄位對應

### 3.1 `screen_results` ← `screener.screen()`

| 表欄位 | 來源 |
|---|---|
| `as_of`／`params_hash`／`params_json`／`analysis_version` | `ScreenResult.as_of`／`.params_hash`／`json.dumps(.params, sort_keys=True)`／`.analysis_version` |
| `status`…`coverage_note`、`close_twd`…`adj_events`（含 `window_halt_days`／`window_missing_days`／`window_no_trade_days`）、`price_source`、`price_fetched_at` | `ScreenResult.summary` 同名欄位（`ma_short`→`c1_threshold_ma_short_twd`、`ma_long`→`c1_threshold_ma_long_twd`、`prior_high_twd`→`c2_threshold_prior_high_twd`、`prior_avg_volume_shares`→`c3_prior_avg_volume_shares`、`volume_ratio`→`c3_volume_ratio`） |
| `cN_enabled`／`cN_passed`／`conditions_json` | `ScreenResult.conditions`（`enabled`、`passed`；排除時 `passed` 為 NULL） |
| `c3_threshold_volume_shares` | `prior_avg_volume_shares × params.volume_multiplier` |
| `c4_threshold_amount_twd` | `params.min_turnover` |
| `price_version` | 讀 `daily_prices` 時一併取 `version`（io_sqlite 尚未回傳此欄，寫入器實作時補） |

### 3.2 `news_events` 分類對映

`news_items.category`（schema §7.6：官方／媒體／社群未證實／AI推論）→ `news_events.category`：官方→官方已公告事實、媒體→媒體報導、社群未證實→社群未證實訊息、AI推論→AI分析推論。
事件分類＝成員中證據最強者（官方 > 媒體 > 社群未證實）；AI 產生的解釋不改變事件分類，只寫在 `inference_text` 並由摘要另標「AI分析推論」。
關聯代號只寫入 `news_event_symbols`，且只收有證據者（`news_item_symbols.evidence` 或官方公告代號）；無證據不關聯。`related_symbols_json` 已移除（DECISIONS 2026-11-10 03:45 ②）。

### 3.3 `daily_summaries` ← `narrative.summary_narrative()`

`text`、`facts_json`、`generation_method` 直接取 `Narrative`；`traceability_check` 由 `untraceable_numbers()` 判定（產生時若有無法追溯數字即拋錯，故正式列應全為 `pass`；欄位保留給 AI 改寫版本的事後檢查）。

## 4. 主鍵與索引理由

- `screen_results` 主鍵含 `params_hash`：同一天可並存多組參數（例如網站預設與使用者自訂），互不覆蓋；選股頁查詢走 `ix_screen_results_day`。
- `news_events.event_id` 由錨點鍵雜湊而來，重跑同一批新聞得到同一 id；成員增加時升版而非新增事件。
- `daily_summaries` 以 (日期, 種類, 參數) 為自然鍵：一天一份正式摘要；若日後要多種摘要（例如盤中）只需擴充 `summary_kind`。
- 所有「現行」查詢都帶 `is_current = 1`，因此索引一律為部分索引，與 schema.md 對 `daily_prices` 的作法相同。
- 依代號查事件走 `news_event_symbols`（負責人 2026-11-10 決議：**由資料工程建表、由研究分析在寫入 `news_events` 時同一交易一併寫入**）。`news_events.relation_evidence_json` 保留同內容快照，由 `writer.write_news_event()` 以同一份清單產生、同一交易寫入，不可分開更新。
- `news_event_symbols` 主鍵含 `version`：事件升版時寫入新版的整組關聯，舊版列保留，與 `news_events` 的版本一一對應；`ix_news_event_symbols_symbol` 供個股頁查詢。

## 5. 冪等選擇與理由

選擇：**自然鍵＋version＋is_current＋checksum**（與資料工程 `daily_prices` 等表同一套）。

1. 22:00 資料修正（SPEC：18:30 初版、22:00 修正）可能改變收盤或除權息資料，篩選結果隨之改變；舊版必須保留，已發布的 18:30 摘要才能說明當時依據哪一版結果。
2. 摘要的 `screen_ref_json.screen_checksum` 指向當時引用的結果；結果升版後可偵測摘要過期並重產（新摘要升版、舊版保留）。
3. 重跑同一資料同一程式版本時 checksum 相同 → 不寫入、不升版，避免「每跑一次多一列」。
4. 不採 `INSERT OR REPLACE`：會遺失歷史且 REPLACE 實作為刪除再插入，違反「失敗不得刪除上次成功資料」。
5. 不採「每次執行新 run_id 全量附加」：查現行結果要額外找最新 run，且無變更也會不斷累積。

寫入程序（單一交易內）：

```text
BEGIN;
  查現行列 checksum
  相同 → COMMIT（不動）
  不同 → UPDATE … SET is_current = 0 WHERE 自然鍵 AND is_current = 1;
         INSERT … version = 舊版 + 1, is_current = 1;
COMMIT;
```

`screen_results` 以「同一 (as_of, params_hash) 整批」為單位在同一交易內完成，避免網站讀到半批結果。

實作：`writer.write_screen_results()`／`write_daily_summary()`／`write_news_event()`（`news_events`＋`news_event_symbols` 同一交易）。
- checksum 不含 `computed_at`、`input_max_fetched_at`、`generated_at`、`version`、`is_current`：只是重算或重抓、內容不變時不升版。
- 新版本號＝該自然鍵既有最大版＋1；舊版只改 `is_current=0`，不刪除。任何一列失敗，整批回滾。
- `screen_run_id`＝產生該批 screen_results 的 `job_runs.run_id`：寫入 `daily_summaries.screen_run_id`，`screen_results` 有此欄時一併寫入；不納入 checksum（內容相同的重跑不升版、保留原 run_id；原值為 NULL 時補上）；目標庫有 `job_runs` 時須存在該 run_id。
- `computed_at`＝計算時間、`generated_at`＝寫入時間（兩欄並存，依資料庫實際 DDL）；`input_max_fetched_at` 由 `io_sqlite.ScreenInputs.input_max_fetched_at` 提供。
- 只寫入表上存在的欄位；程式有值但表沒有的欄位列在 `WriteReport.dropped_columns`，缺必要欄位則拋錯。
- 寫入 `data/twstock.db` 須 `connect_writable(..., allow_production=True)`；負責人通知前不執行。

## 6. 不在本草案範圍

- 不定義、不寫入 `index_daily`、`news_items`、`news_item_symbols`、`material_infos`、`trading_halts`（資料工程）。
- 所有表（含 `news_event_symbols`）實際建表由資料工程執行；本檔只提欄位草案。
- 不含網站快取表或 JSON 檔格式（網頁開發）。
