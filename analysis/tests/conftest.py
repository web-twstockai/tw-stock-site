"""pytest 共用設定：所有測試只使用「測試用合成資料」（欄位對齊 docs/schema.md）。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import synthetic_data  # noqa: E402


def _read(name):
    return pd.read_csv(synthetic_data.FIXTURE_DIR / f"{name}.csv", dtype={"symbol": str}, keep_default_na=True)


@pytest.fixture(scope="session")
def T():
    """合成四表＋覆蓋起日（CSV fixture）。"""
    return {n: _read(n) for n in ("daily_prices", "corporate_actions", "trading_calendar", "securities",
                                  "actions_coverage", "trading_halts")}


@pytest.fixture(scope="session")
def all_symbols(T):
    return sorted(set(T["daily_prices"]["symbol"]) | set(T["securities"]["symbol"]))


@pytest.fixture(scope="session")
def synth_db(tmp_path_factory):
    return synthetic_data.build_sqlite(tmp_path_factory.mktemp("db") / "synthetic.db")
