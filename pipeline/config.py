"""路徑、樣本清單與限流設定。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "twstock.db"
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
TPEX_INTERVAL = 3.5
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
CHECKPOINT_ALL = LOG_DIR / "backfill_all_checkpoint.json"
COVERAGE_ALL_JSON = LOG_DIR / "coverage_all.json"
