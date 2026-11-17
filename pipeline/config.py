"""路徑、樣本清單與限流設定。"""
from __future__ import annotations

from pathlib import Path

import os

ROOT = Path(__file__).resolve().parents[1]
# 正式庫路徑（唯讀／寫入都要經過這個常數；測試與副本可用環境變數 PIPELINE_DB_PATH 覆寫）
PRODUCTION_DB_PATH = ROOT / "data" / "twstock.db"
DB_PATH = Path(os.environ["PIPELINE_DB_PATH"]) if os.environ.get("PIPELINE_DB_PATH") else PRODUCTION_DB_PATH
LOG_DIR = ROOT / "logs"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"

# PROGRESS.md 第二階段 30 檔（已以 ISIN 核實市場別）
SAMPLE30_TWSE = [
    "2330", "2317", "2454", "2308", "2412", "2881", "2882", "2801", "1301", "6505",
    "2002", "1101", "1216", "2912", "2207", "2603", "3008", "2357", "3711", "9910",
]
SAMPLE30_TPEX = [
    "6488", "5347", "8299", "3105", "3293", "8069", "5483", "5274", "6547", "4105",
]
SAMPLE30 = SAMPLE30_TWSE + SAMPLE30_TPEX

# 請求間隔（秒）
TWSE_INTERVAL = 3.5
TPEX_INTERVAL = 5.0   # 櫃買中心：每次請求間隔 ≥5 秒（2026-11-17 曾因連續請求被暫時限流約 15 分鐘）
MOPS_INTERVAL = 5.5
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

USER_AGENT = "Mozilla/5.0 (compatible; TwStockResearch/1.0; +local research pipeline)"

# 目標資料深度
PRICE_TRADE_DAYS = 150
REVENUE_MONTHS = 36
FINANCIAL_QUARTERS = 8
MATERIAL_DAYS = 30
# daily 增量（歷史回補留給 evening／手動）
DAILY_PRICE_TRADE_DAYS = 5
DAILY_REVENUE_MONTHS = 2
DAILY_MATERIAL_DAYS = 7
EVENING_PRICE_TRADE_DAYS = 150  # evening 仍可補滿觀察窗

# 全市場（universe=all）目標深度；daily 增量仍用 DAILY_* 
ALL_PRICE_TRADE_DAYS = 150
ALL_REVENUE_MONTHS = 24
ALL_FINANCIAL_QUARTERS = 8
ALL_MATERIAL_DAYS = 30
# Phase 3（2026-11-17）：全市場行情回補
ALL_BACKFILL_START = "2026-03-02"          # 與 sample30 起始日一致
FULL_DAY_RATIO = 0.85                      # 該日該市場現行列數 ≥ 此比例 × 現行普通股數，視為全市場日
ALL_EVENING_REVENUE_MONTHS = 3             # all 模式 evening：只重抓最近 3 個月營收（歷史由 revenue_backfill）
ALL_EVENING_FINANCIAL_QUARTERS = 2         # all 模式 evening：只重抓最近 2 季財報（歷史由 financials_backfill）
ENRICH_PER_RUN = 20                        # all 模式 daily／evening：MOPS 配股補強每次最多幾個代號（其餘留到下次）
RANGE_BLOCK_COOLDOWN_MIN = 60              # 區間端點（TWSE／TPEx）被擋後至少等幾分鐘才可再送（存在 backfill_state，Actions 換機器也有效）
REDIRECT_STREAK_STOP = 3                   # 連續幾次 3xx／擋頁／錯誤就停止該作業（視為來源限流）
CHECKPOINT_ALL = LOG_DIR / "backfill_all_checkpoint.json"
COVERAGE_ALL_JSON = LOG_DIR / "coverage_all.json"
