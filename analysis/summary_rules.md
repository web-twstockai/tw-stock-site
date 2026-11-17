# 每日盤後摘要規則

最後更新：2026-11-10（Asia/Taipei，UTC+8）。依據 docs/SPEC.md 第 4、6、7 節。
新聞分類（SPEC 四類＋主題標籤）2026-11-10 負責人核可。實作：`analysis/narrative.py`（規則模板，`generation_method=rule`）。
資料表與寫入者（2026-11-10 負責人決議 G；schema 一律由資料工程建立）：

| 表 | 寫入者 | 研究分析 |
|---|---|---|
| `daily_prices`、`corporate_actions`、`corporate_actions_coverage`、`material_infos`、`monthly_revenues`、`income_statements`、`cash_flow_statements`、`trading_calendar`、`job_runs`、`index_daily`、`news_items`、`news_item_symbols`、`trading_halts` | 資料工程 | 只讀 |
| `screen_results`、`news_events`、`news_event_symbols`、`daily_summaries` | 研究分析程式 | 寫入；欄位草案見 `analysis/tables_draft.md`，由資料工程建表。`news_event_symbols` 與 `news_events` 同一交易一併寫入（每個事件版本一組列） |

## 1. 基本原則

1. **數字一律由程式從資料表讀取或計算**，AI 只負責把已計算好的數字組成文字與解釋；AI 不得自行產生、估算、四捨五入以外改動任何數字。
2. 每個數字都附「來源資料表＋欄位＋資料日期（＋來源 source／fetched_at）」，可逐一追溯。
3. **只有技術訊號、沒有對應官方公告或新聞時，只陳述數值，不寫原因**。例如可寫「2330 收盤 X 元，漲 Y%，量比 Z，符合四項條件」，不可寫「受惠 AI 需求」之類未經來源支持的說法。
4. 不宣稱勝率、不把技術訊號描述成獲利保證；摘要末尾固定附「技術條件僅為篩選結果，非投資建議」。
5. 零檔入選就寫「今日 0 檔符合條件」，並列出被排除數與主要排除原因統計，不放寬條件找替代股。
6. 缺資料寫「資料不足」、未公告寫「尚未公告」、抓取失敗寫明「來源 X 更新失敗，顯示 YYYY-MM-DD 資料」。
7. 測試用合成資料（`data_label` 標示者或代號 TEST 開頭）一律不得進入正式摘要。
8. 社群內容（含 X）只當線索，標「社群未證實訊息」，不得作為原因陳述。

## 2. 摘要內容與數字來源

| 摘要段落 | 數字／內容 | 來源資料表.欄位 | 追溯欄位 |
|---|---|---|---|
| 標頭 | 資料日期、更新狀態 | `job_runs.data_date`, `job_runs.status`, `job_runs.finished_at` | 工作紀錄 id |
| 大盤 | 加權指數收盤、漲跌點、漲跌% | `index_daily.close / change`（index_id＝TAIEX）；`change_pct` 來源無值時由研究分析計算（schema §7.5） | `index_daily.index_id, price_date, source, fetched_at` |
| 大盤（櫃買） | 櫃買指數收盤、漲跌% | `index_daily`（index_id＝TPEX） | 同上 |
| 篩選結果 | 入選檔數、各檔代號名稱、收盤、漲跌幅、成交金額、MA20、MA60、量比、各條件實際值 | `screen_results`（存 `analysis/screener.py` 的 `summary`/`conditions` 輸出，欄位見 analysis/tables_draft.md），原始值來自 `daily_prices`、`corporate_actions` | `screen_results.as_of, params_hash, params_json, version, analysis_version`；`daily_prices.source, price_date, fetched_at` |
| 排除統計 | 各排除原因代碼檔數 | `screen_results.exclusion_code` 計數 | 同上 |
| 重大訊息 | 標題、發布時間、公司、連結 | `material_infos.title / announced_at / symbol / url`（公開資訊觀測站） | `material_infos.source, announce_date, fetched_at` |
| 月營收 | 當月營收、月增率、年增率 | `monthly_revenues.revenue / mom_pct / yoy_pct`（MOPS t187ap05 等） | `monthly_revenues.revenue_month, source, fetched_at` |
| 財報 | EPS、毛利率等 | `income_statements.*`、`cash_flow_statements.*`（標合併/個別、單季/累計） | `income_statements.fiscal_year, fiscal_quarter, version, source` |
| 新聞 | 標題、媒體、時間、連結、分類 | `news_events`（研究分析寫入，第 4、5 節），成員來自 `news_items`＋`news_item_symbols` | `news_events.news_ids_json`→`news_items.news_id, url, published_at, fetched_at` |

規則：
- 摘要每段落渲染時，網頁可點開「資料來源」顯示上表追溯欄位；`screen_results` 須保存執行參數（`params_json`），重算可得到相同結果。
- 數字格式：價格 2 位小數、漲跌幅 2 位小數 %、量以「股」並可加註換算「張」、金額以「元」並可加註「億元」換算；換算由程式進行。
- 營收月份、財報季度、公告日期與股價日期分欄顯示，不混用（例如「11/09 收盤」「9 月營收（10/10 公告）」）。

## 3. 事件與個股連結規則

- 個股段落只能引用：(a) 該股同日或前一交易日盤後至當日盤後之間的官方重訊／公告；(b) 已依第 4 節分類與去重的新聞事件；且關聯須有證據（新聞內文提及代號或公司全名，或官方公告的公司代號）。
- 有關聯事件時寫法：「同日公告：〔標題〕（官方已公告事實，連結）」；不得寫成因果（避免「因…而大漲」），可寫「同日另有…公告」。
- 「可能影響」「推論」類文字一律標「AI 分析推論」，並列出依據與待確認事項。
- 無任何關聯事件時，只寫數值。

## 4. 新聞分類（依 SPEC 第 6 節四類）

| 分類 | 判定 | 摘要中的呈現 |
|---|---|---|
| 官方已公告事實 | 來源為證交所、櫃買、公開資訊觀測站、公司官方 IR／新聞稿 | 可陳述為事實，附公告連結與時間 |
| 媒體報導 | 新聞媒體（具名媒體網域）報導 | 「據〔媒體〕報導」，附連結，不當作事實 |
| 社群未證實訊息 | X、論壇、社群平台、未具名來源 | 僅列為線索，標「未證實」，不得作為原因 |
| AI分析推論 | 由 AI 根據上述資料產生的解釋 | 明確標示，附依據與待確認事項 |

另外以「主題標籤」（`news_events.topic_tags_json`）輔助，不取代上述分類：重大訊息／公司公告、營收財報、產業與總經、其他。

`news_items.category`（schema §7.6 短名）對映：官方→官方已公告事實、媒體→媒體報導、社群未證實→社群未證實訊息、AI推論→AI分析推論。事件分類取成員中證據最強者（官方 > 媒體 > 社群未證實）；AI 解釋另存 `inference_text`，不改變事件分類。

## 5. 去重設計（同事件聚合）

1. **URL 正規化**：小寫網域、去 `www.`/`m.`/`amp` 子路徑、去 fragment、移除追蹤參數（`utm_*`, `fbclid`, `gclid` 等）、排序剩餘 query、去結尾斜線；優先採用頁面 `<link rel=canonical>`。正規化 URL 相同 → 同一則。
2. **標題正規化與相似度**：去全半形差異、標點、媒體名後綴（如「｜〇〇新聞」）、空白；以字元 2-gram Jaccard 相似度（預設門檻 ≥ 0.6，存於 `news_events.dedup_params_json`）且發布時間差 ≤ 48 小時、相關公司集合有交集 → 候選同事件。
3. **同事件聚合**：以官方公告為錨（若有，`news_events.material_info_keys_json`），同公司、同事件類型（例如營收、法說、重訊主旨代碼）、時間窗 ≤ 48 小時內的媒體報導併入同一 `news_events`；事件保留所有成員 `news_items.news_id`（`news_ids_json`）與來源連結；關聯公司與證據逐筆寫入 `news_event_symbols`（以此為準；`related_symbols_json` 已移除，DECISIONS 2026-11-10 03:45 ②），`relation_evidence_json` 只存同內容快照，兩者由 `writer.write_news_event()` 同一交易寫入，顯示最早官方來源＋最多 3 則代表報導。
4. 去重門檻與時間窗為可調參數，變更記錄於 CHANGELOG（由負責人）。
5. 每則保存 `content_hash`（正規化標題＋來源網域）供重跑冪等，不重複入庫。

## 6. 追溯與稽核

- 每日摘要存檔 `daily_summaries`（欄位見 analysis/tables_draft.md）：`summary_date`, `params_hash`, `screen_ref_json`, `text`, `facts_json`（摘要中出現的每個數字及其來源表/欄位/主鍵/日期）, `news_categories_json`, `generation_method`（rule／ai／rule+ai）, `review_status`。
- 規則模板產生者 `review_status` 可為 `not_required`；含 AI 改寫者須人工核可後才能發布。
- 產生後自動檢查：摘要文字中的每個數字都能在 `facts_json` 找到；找不到即視為失敗、不發布。
- 只對新增或變更內容呼叫 AI（SPEC 第 9 節）。
