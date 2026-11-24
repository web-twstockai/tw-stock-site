-- 籌碼資料（2026-11-24，pipeline_chips.patch）：三大法人、融資融券、集保股權分散。
-- 由 pipeline/db.py init_db() 在 migrate() 之後執行（CREATE ... IF NOT EXISTS，可重跑）。
-- 共通：每列保存 source_table、source（URL）、所屬日期、announced_at（來源無公告時間＝NULL）、fetched_at、unit、version／is_current／checksum。
-- 數值照官方原值保存，不換算單位、不推算缺欄（官方沒有的欄位＝NULL）。

-- 三大法人個股買賣超（TWSE T86／TPEx insti/dailyTrade）。單位：股（兩市場官方皆為股數）。
CREATE TABLE IF NOT EXISTS institutional_trades (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL CHECK (market IN ('TWSE','TPEx')),
  trade_date TEXT NOT NULL,                 -- 所屬交易日 YYYY-MM-DD
  foreign_ex_dealer_buy INTEGER,            -- 外陸資買進股數（不含外資自營商）
  foreign_ex_dealer_sell INTEGER,
  foreign_ex_dealer_net INTEGER,
  foreign_dealer_buy INTEGER,               -- 外資自營商
  foreign_dealer_sell INTEGER,
  foreign_dealer_net INTEGER,
  foreign_total_buy INTEGER,                -- 外資及陸資合計（只有 TPEx 有此欄；TWSE＝NULL，不推算）
  foreign_total_sell INTEGER,
  foreign_total_net INTEGER,
  trust_buy INTEGER,                        -- 投信
  trust_sell INTEGER,
  trust_net INTEGER,
  dealer_self_buy INTEGER,                  -- 自營商（自行買賣）
  dealer_self_sell INTEGER,
  dealer_self_net INTEGER,
  dealer_hedge_buy INTEGER,                 -- 自營商（避險）
  dealer_hedge_sell INTEGER,
  dealer_hedge_net INTEGER,
  dealer_total_buy INTEGER,                 -- 自營商合計買進／賣出（只有 TPEx 有；TWSE＝NULL）
  dealer_total_sell INTEGER,
  dealer_net INTEGER,                       -- 自營商買賣超股數（合計；兩市場都有）
  total_net INTEGER,                        -- 三大法人買賣超股數（官方合計）
  fields_sig TEXT NOT NULL,                 -- 官方欄位版本：'T86_19'（2017-12-18 起 19 欄）／'TPEX_INSTI_24'
  source_table TEXT NOT NULL,               -- 'T86'／'insti_dailyTrade'
  source TEXT NOT NULL,                     -- 請求 URL
  announced_at TEXT,                        -- 來源無公告時間 → NULL
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '股',
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY (symbol, trade_date, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_institutional_trades_current
  ON institutional_trades(symbol, trade_date) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS ix_institutional_trades_date ON institutional_trades(trade_date, market);

-- 融資融券餘額（TWSE MI_MARGN 個股彙總／TPEx margin/balance）。單位：TWSE「交易單位」、TPEx「張」（官方欄名，未換算成股）。
CREATE TABLE IF NOT EXISTS margin_balances (
  symbol TEXT NOT NULL,
  market TEXT NOT NULL CHECK (market IN ('TWSE','TPEx')),
  trade_date TEXT NOT NULL,
  margin_buy INTEGER,                       -- 融資買進（TPEx 資買）
  margin_sell INTEGER,                      -- 融資賣出（TPEx 資賣）
  margin_cash_repay INTEGER,                -- 現金償還（TPEx 現償）
  margin_prev_balance INTEGER,              -- 前日餘額（TPEx 前資餘額）
  margin_balance INTEGER,                   -- 今日餘額（TPEx 資餘額）
  margin_limit INTEGER,                     -- TWSE「次一營業日限額」／TPEx「資限額」（口徑不同，見 limit_basis）
  margin_finco INTEGER,                     -- TPEx「資屬證金」（TWSE 無 → NULL）
  margin_usage_pct REAL,                    -- TPEx「資使用率(%)」（TWSE 無 → NULL）
  short_buy INTEGER,                        -- 融券買進（回補；TPEx 券買）
  short_sell INTEGER,                       -- 融券賣出（TPEx 券賣）
  short_stock_repay INTEGER,                -- 現券償還（TPEx 券償）
  short_prev_balance INTEGER,               -- 前日餘額（TPEx 前券餘額）
  short_balance INTEGER,                    -- 今日餘額（TPEx 券餘額）
  short_limit INTEGER,                      -- TWSE「次一營業日限額」／TPEx「券限額」
  short_finco INTEGER,                      -- TPEx「券屬證金」
  short_usage_pct REAL,                     -- TPEx「券使用率(%)」
  offset_qty INTEGER,                       -- 資券互抵（TWSE）／資券相抵(張)（TPEx）
  note_flags TEXT,                          -- TWSE「註記」（O 停止融資、X 停止融券、@ 融資分配、% 融券分配、! 停止買賣）／TPEx「備註」；原文去空白
  limit_basis TEXT,                         -- 'twse_next_day_limit'／'tpex_limit'
  fields_sig TEXT NOT NULL,                 -- 'MI_MARGN_16'／'TPEX_MARGIN_20'
  source_table TEXT NOT NULL,               -- 'MI_MARGN'／'margin_balance'
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL,                       -- '交易單位'（TWSE）／'張'（TPEx）；使用率為 %
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY (symbol, trade_date, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_margin_balances_current
  ON margin_balances(symbol, trade_date) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS ix_margin_balances_date ON margin_balances(trade_date, market);

-- 集保戶股權分散表（TDCC），每代號每週一列，分級原值寬表保存。
-- 級距 1–15 為持股分級（1：1–999 股 … 15：1,000,001 股以上）、16＝差異數調整、17＝合計（opendata 1-5 的編號）。
-- qryStock（單檔歷史頁）沒有「差異數調整」列，其「合計」存在 *_17，*_16＝NULL。
-- 「千張大戶」＝級距 15（1,000,001 股以上＝超過 1,000 張）；由分析端自行計算，pipeline 不存衍生值。
CREATE TABLE IF NOT EXISTS shareholding_distribution (
  symbol TEXT NOT NULL,
  market TEXT,                              -- 依當時證券清單（universe）標示；TDCC 本身不分市場
  data_date TEXT NOT NULL,                  -- 集保資料日（每週最後營業日）YYYY-MM-DD
  level_scheme TEXT NOT NULL DEFAULT 'tdcc_17',
  holders_1 INTEGER, holders_2 INTEGER, holders_3 INTEGER, holders_4 INTEGER, holders_5 INTEGER,
  holders_6 INTEGER, holders_7 INTEGER, holders_8 INTEGER, holders_9 INTEGER, holders_10 INTEGER,
  holders_11 INTEGER, holders_12 INTEGER, holders_13 INTEGER, holders_14 INTEGER, holders_15 INTEGER,
  holders_16 INTEGER, holders_17 INTEGER,
  shares_1 INTEGER, shares_2 INTEGER, shares_3 INTEGER, shares_4 INTEGER, shares_5 INTEGER,
  shares_6 INTEGER, shares_7 INTEGER, shares_8 INTEGER, shares_9 INTEGER, shares_10 INTEGER,
  shares_11 INTEGER, shares_12 INTEGER, shares_13 INTEGER, shares_14 INTEGER, shares_15 INTEGER,
  shares_16 INTEGER, shares_17 INTEGER,
  pct_1 REAL, pct_2 REAL, pct_3 REAL, pct_4 REAL, pct_5 REAL,
  pct_6 REAL, pct_7 REAL, pct_8 REAL, pct_9 REAL, pct_10 REAL,
  pct_11 REAL, pct_12 REAL, pct_13 REAL, pct_14 REAL, pct_15 REAL,
  pct_16 REAL, pct_17 REAL,
  source_table TEXT NOT NULL,               -- 'tdcc_od_1-5'（opendata 整批最新一週）／'tdcc_qryStock'（單檔歷史）
  source TEXT NOT NULL,
  announced_at TEXT,
  announced_at_note TEXT,
  fetched_at TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'holders=人;shares=股;pct=%',
  version INTEGER NOT NULL DEFAULT 1,
  is_current INTEGER NOT NULL DEFAULT 1,
  checksum TEXT,
  PRIMARY KEY (symbol, data_date, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shareholding_distribution_current
  ON shareholding_distribution(symbol, data_date) WHERE is_current=1;
CREATE INDEX IF NOT EXISTS ix_shareholding_distribution_date ON shareholding_distribution(data_date);

-- 每個（資料集, 市場, 日期）的抓取結果；chips_backfill／evening 的檢查點。
CREATE TABLE IF NOT EXISTS chips_fetch_status (
  dataset TEXT NOT NULL,                    -- 'twse_t86'／'tpex_insti'／'twse_margin'／'tpex_margin'／'tdcc_dist'
  market TEXT NOT NULL,                     -- 'TWSE'／'TPEx'／'ALL'（TDCC）
  data_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','no_data','fields_changed','blocked','error','資料不足')),
  http_status INTEGER,
  page_rows INTEGER,                        -- 回應中的列數（過濾前）
  stored_rows INTEGER,                      -- 寫入（universe 內）列數
  identity_fail INTEGER,                    -- 恆等式（合計＝各項加總）不符的列數
  fields_sig TEXT,
  source_table TEXT,
  source TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  fetched_at TEXT NOT NULL,
  error TEXT,
  notes TEXT,
  PRIMARY KEY (dataset, market, data_date)
);

-- 區間層級的覆蓋（每次 chips_backfill／evening 結束時由 chips_fetch_status 重算；非版本化的摘要表）。
CREATE TABLE IF NOT EXISTS chips_coverage (
  dataset TEXT NOT NULL,
  market TEXT NOT NULL,
  range_start TEXT NOT NULL,
  range_end TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','partial','資料不足','未測')),
  expected_days INTEGER,                    -- 區間內交易日（TDCC＝週）數
  ok_days INTEGER,
  basis TEXT,
  notes TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (dataset, market, range_start, status)
);
