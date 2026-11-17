"""研究分析模組版本號。

`ANALYSIS_VERSION` 寫入 `screen_results.analysis_version`（以及 news_events、daily_summaries 的同名欄），
供追溯結果由哪一版程式產生。規則（2026-11-10 負責人決議）：每批程式變更完成後版本號加一，
並同步更新 `analysis/USAGE.md` 開頭的版本號與變更紀錄（`tests/test_usage_doc.py` 會檢查兩者一致）。
"""

ANALYSIS_VERSION = "analysis-0.8.0"

__all__ = ["ANALYSIS_VERSION"]
