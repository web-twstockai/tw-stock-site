PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS securities (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL CHECK(market IN ('TWSE','TPEx')),
  name TEXT NOT NULL,
  name_full TEXT,
  isin TEXT,
  cfi_code TEXT NOT NULL,
  industry TEXT,
  industry_code TEXT,
  listed_date TEXT,
  is_common INTEGER NOT NULL DEFAULT 1,
  exclude_reason TEXT,
  data_date TEXT NOT NULL,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'n/a',
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol)
);

CREATE TABLE IF NOT EXISTS securities_history (
  symbol TEXT NOT NULL,
  version INTEGER NOT NULL,
  market TEXT NOT NULL,
  name TEXT NOT NULL,
  name_full TEXT,
  isin TEXT,
  cfi_code TEXT NOT NULL,
  industry TEXT,
  industry_code TEXT,
  listed_date TEXT,
  is_common INTEGER NOT NULL,
  exclude_reason TEXT,
  data_date TEXT NOT NULL,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  checksum TEXT,
  archived_at TEXT NOT NULL,
  PRIMARY KEY(symbol, version)
);

CREATE TABLE IF NOT EXISTS securities_excluded (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  name TEXT,
  cfi_code TEXT,
  section TEXT,
  exclude_reason TEXT NOT NULL,
  data_date TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(symbol, market, data_date, exclude_reason)
);

CREATE TABLE IF NOT EXISTS daily_prices (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  price_date TEXT NOT NULL,
  open_twd REAL,
  high_twd REAL,
  low_twd REAL,
  close_twd REAL,
  change_twd REAL,
  change REAL,
  change_sign TEXT,
  volume_shares INTEGER,
  amount_twd INTEGER,
  transactions INTEGER,
  avg_twd REAL,
  pe_ratio REAL,
  is_halted INTEGER,
  is_no_trade INTEGER,
  change_note TEXT,
  next_ref_price_twd REAL,
  raw_volume TEXT,
  raw_amount TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, price_date, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_prices_current
  ON daily_prices(symbol, price_date) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_daily_prices_date ON daily_prices(price_date);

CREATE TABLE IF NOT EXISTS corporate_actions_coverage (
  market TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  coverage_start TEXT NOT NULL,
  coverage_end TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY(market, action_kind, coverage_start, coverage_end, source)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  action_date TEXT NOT NULL,
  action_type TEXT NOT NULL,
  ex_type_raw TEXT,
  prev_close_twd REAL,
  reference_twd REAL,
  rights_value_twd REAL,
  dividend_value_twd REAL,
  rights_dividend_sum_twd REAL,
  cash_dividend_twd REAL,
  stock_dividend_per_thousand REAL,
  capital_reduction_ratio REAL,
  refund_per_share_twd REAL,
  cash_subscription_ratio REAL,
  share_exchange_ratio REAL,
  limit_up_twd REAL,
  limit_down_twd REAL,
  auction_base_twd REAL,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, action_date, action_type, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_corp_actions_current
  ON corporate_actions(symbol, action_date, action_type) WHERE is_current = 1;


CREATE TABLE IF NOT EXISTS trading_halts (
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  halt_date TEXT NOT NULL,
  halt_time TEXT,
  resume_date TEXT,
  resume_time TEXT,
  halt_type TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(market, symbol, halt_date, halt_time, version)
);
CREATE TABLE IF NOT EXISTS trading_halts (
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  halt_date TEXT NOT NULL,
  halt_time TEXT,
  resume_date TEXT,
  resume_time TEXT,
  halt_type TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(market, symbol, halt_date, halt_time, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trading_halts_current
  ON trading_halts(market, symbol, halt_date, halt_time) WHERE is_current=1;

CREATE TABLE IF NOT EXISTS monthly_revenues (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  revenue_month TEXT NOT NULL,
  revenue_twd INTEGER,
  revenue_prev_month_twd INTEGER,
  revenue_prev_year_twd INTEGER,
  mom_pct REAL,
  yoy_pct REAL,
  cumulative_twd INTEGER,
  cumulative_prev_year_twd INTEGER,
  cumulative_yoy_pct REAL,
  industry TEXT,
  remark TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, revenue_month, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_monthly_revenues_current
  ON monthly_revenues(symbol, revenue_month) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS income_statements (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_quarter INTEGER NOT NULL,
  industry_form TEXT NOT NULL,
  report_basis TEXT NOT NULL DEFAULT '合併',
  period_type TEXT NOT NULL DEFAULT '累計',
  revenue_twd INTEGER,
  gross_profit_twd INTEGER,
  operating_income_twd INTEGER,
  non_operating_twd INTEGER,
  pretax_income_twd INTEGER,
  net_income_twd INTEGER,
  net_income_parent_twd INTEGER,
  eps_twd REAL,
  raw_json TEXT,
  derived INTEGER NOT NULL DEFAULT 0,
  derivation_method TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, fiscal_year, fiscal_quarter, industry_form, report_basis, period_type, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_income_current
  ON income_statements(symbol, fiscal_year, fiscal_quarter, industry_form, report_basis, period_type) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS cash_flow_statements (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_quarter INTEGER NOT NULL,
  report_basis TEXT NOT NULL DEFAULT '合併',
  period_type TEXT NOT NULL DEFAULT '累計',
  operating_cf_twd INTEGER,
  investing_cf_twd INTEGER,
  financing_cf_twd INTEGER,
  fx_effect_twd INTEGER,
  net_increase_twd INTEGER,
  cash_begin_twd INTEGER,
  cash_end_twd INTEGER,
  derived INTEGER NOT NULL DEFAULT 0,
  derivation_method TEXT,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, fiscal_year, fiscal_quarter, report_basis, period_type, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cf_current
  ON cash_flow_statements(symbol, fiscal_year, fiscal_quarter, report_basis, period_type) WHERE is_current = 1;


CREATE TABLE IF NOT EXISTS index_daily (
  index_id TEXT NOT NULL,
  index_name TEXT,
  price_date TEXT NOT NULL,
  close REAL,
  change REAL,
  change_pct REAL,
  change_pct_derived INTEGER,  -- 0=官方百分比；1=change/prev_close*100 推算；NULL=尚未填
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'index_points',
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(index_id, price_date, version)
);
CREATE TABLE IF NOT EXISTS index_daily (
  index_id TEXT NOT NULL,
  index_name TEXT,
  price_date TEXT NOT NULL,
  close REAL,
  change REAL,
  change_pct REAL,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'index_points',
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(index_id, price_date, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_index_daily_current
  ON index_daily(index_id, price_date) WHERE is_current=1;

CREATE TABLE IF NOT EXISTS material_infos (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL,
  announce_date TEXT NOT NULL,
  announce_time TEXT NOT NULL DEFAULT '',
  announced_at TEXT,
  announced_at_note TEXT,
  title TEXT NOT NULL,
  clause TEXT,
  seq_no TEXT,
  detail_text TEXT,
  url TEXT,
  content_hash TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'n/a',
  source_unit TEXT,
  unit_conversion TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY(symbol, announce_date, announce_time, content_hash, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_material_current
  ON material_infos(symbol, announce_date, announce_time, content_hash) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS trading_calendar (
  calendar_date TEXT PRIMARY KEY,
  is_trade_day INTEGER NOT NULL,
  name TEXT,
  description TEXT,
  source_kind TEXT NOT NULL,
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'n/a',
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS job_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  data_date TEXT,
  symbols_scope TEXT,
  success_items TEXT,
  failed_items TEXT,
  error_summary TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  host_note TEXT
);

CREATE TABLE IF NOT EXISTS source_status (
  source_id TEXT PRIMARY KEY,
  source_url TEXT,
  last_success_at TEXT,
  last_attempt_at TEXT,
  last_http_status INTEGER,
  last_data_date TEXT,
  last_error TEXT,
  ok INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_items (
  news_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TEXT,
  url TEXT,
  source TEXT NOT NULL,
  category TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_news_items_event_current
  ON news_items(event_key) WHERE is_current=1;

CREATE TABLE IF NOT EXISTS news_item_symbols (
  news_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  evidence TEXT,
  PRIMARY KEY(news_id, symbol)
);


-- 研究分析寫入表（pipeline 不寫；見 analysis/tables_draft.md）
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
  volume_shares                 REAL,                          -- 一般交易口徑（待 sources.md 確認）
  amount_twd                    REAL,
  n_valid_days                  INTEGER,
  window_start                  TEXT,
  window_gap_days               INTEGER,
  window_halt_days              INTEGER,
  window_missing_days           INTEGER,
  window_no_trade_days          INTEGER,
  n_adj_events                  INTEGER,
  adj_events                    TEXT,                          -- 視窗內調整事件與 f／s 因子文字
  -- 追溯
  price_source                  TEXT,                          -- daily_prices.source
  price_fetched_at              TEXT,                          -- daily_prices.fetched_at
  price_version                 INTEGER,                       -- daily_prices.version（讀取當時的現行版）
  analysis_version              TEXT    NOT NULL,
  generated_at                  TEXT    NOT NULL,
  computed_at                   TEXT,
  input_max_fetched_at          TEXT,
  screen_run_id                 TEXT,                          -- job_runs.run_id（產生本批之 daily job）；不納入 checksum
  checksum                      TEXT    NOT NULL,              -- 除 generated_at／version／is_current／screen_run_id 外全部欄位的 SHA-256
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
  -- related_symbols_json 已移除（DECISIONS 03:45：以 news_event_symbols 為準）
  relation_evidence_json        TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(relation_evidence_json)),
                                                               -- [{symbol, evidence_type: 官方公告代號|內文代號|公司全名, news_id|material_key, evidence(news_item_symbols.evidence 原文)}]
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
  generated_at                  TEXT    NOT NULL,
  computed_at                   TEXT,
  input_max_fetched_at          TEXT,
  checksum                      TEXT    NOT NULL,
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
  generated_at                  TEXT    NOT NULL,
  computed_at                   TEXT,
  input_max_fetched_at          TEXT,
  data_date                     TEXT,                          -- = summary_date
  provider                      TEXT,                          -- 文字產生者；規則式固定「研究分析規則（analysis/narrative.py）」
  screen_run_id                 TEXT,                          -- 產生該批 screen_results 的 job_runs.run_id（字串；FK 語意見 schema.md）
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

-- news_event_symbols：研究分析寫入；多對多關聯（負責人 2026-11-10）



-- news_event_symbols 依 analysis/tables_draft.md 定稿（可重跑）
CREATE TABLE IF NOT EXISTS news_event_symbols (
  event_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  symbol TEXT NOT NULL CHECK (symbol NOT LIKE 'TEST%'),
  evidence_type TEXT NOT NULL CHECK (evidence_type IN ('官方公告代號', '內文代號', '公司全名')),
  evidence TEXT,
  source_news_id INTEGER,
  source_material_key_json TEXT CHECK (source_material_key_json IS NULL OR json_valid(source_material_key_json)),
  PRIMARY KEY (event_id, version, symbol, evidence_type),
  FOREIGN KEY (event_id, version) REFERENCES news_events(event_id, version),
  CHECK (source_news_id IS NOT NULL OR source_material_key_json IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_news_event_symbols_symbol
  ON news_event_symbols(symbol, event_id, version);


-- ============================================================
-- revenue_fetch_status：月營收抓取狀態（市場 × 營收月份），不被其他月份覆蓋
-- ============================================================
CREATE TABLE IF NOT EXISTS revenue_fetch_status (
  market          TEXT    NOT NULL CHECK (market IN ('TWSE','TPEx')),
  revenue_month   TEXT    NOT NULL,              -- YYYY-MM
  status          TEXT    NOT NULL CHECK (status IN ('ok','fail','empty','not_yet_due','partial_pending','source_down')),
  http_status     INTEGER,
  row_count       INTEGER NOT NULL DEFAULT 0,
  source          TEXT    NOT NULL,
  fetched_at      TEXT    NOT NULL,
  error           TEXT,
  PRIMARY KEY (market, revenue_month)
);
CREATE INDEX IF NOT EXISTS ix_revenue_fetch_status_fetched
  ON revenue_fetch_status(fetched_at);


-- Phase 3：全市場覆蓋與宇宙成員（供資料狀態頁讀取）
CREATE TABLE IF NOT EXISTS universe_members (
  universe_id    TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  market         TEXT,
  included       INTEGER NOT NULL,
  exclude_reason TEXT,
  as_of          TEXT NOT NULL,
  source         TEXT,
  fetched_at     TEXT NOT NULL,
  PRIMARY KEY (universe_id, symbol)
);
CREATE INDEX IF NOT EXISTS ix_universe_members_mkt
  ON universe_members(universe_id, market, included);

CREATE TABLE IF NOT EXISTS coverage_summaries (
  coverage_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER,
  universe            TEXT NOT NULL,
  data_kind           TEXT NOT NULL,
  market              TEXT NOT NULL,
  expected_count      INTEGER,
  success_count       INTEGER,
  failed_count        INTEGER,
  excluded_count      INTEGER,
  exclude_reasons_json TEXT,
  date_start          TEXT,
  date_end            TEXT,
  notes               TEXT,
  http_events_json    TEXT,
  fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_coverage_summaries_kind
  ON coverage_summaries(universe, data_kind, market, fetched_at);
