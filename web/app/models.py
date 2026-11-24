"""網站顯示用的資料結構。

所有要顯示在畫面上的數字一律包成 Val，帶來源、資料日期與狀態，
讓模板能在每個數字旁顯示「來源／資料日期」與明確的狀態標籤。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 狀態代碼 → 顯示文字（UI 必須明確呈現，不可留白或補造）
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient"      # 資料不足
STATUS_NOT_ANNOUNCED = "not_announced"    # 尚未公告（僅供設計端狀態說明沿用；網站程式不再產生）
STATUS_FINANCIALS_MISSING = "financials_missing"  # 財報：本站資料庫尚無此期
STATUS_ERROR = "error"                    # 讀取失敗（附 note）
STATUS_NOT_APPLICABLE = "not_applicable"  # 不適用（需附說明）
STATUS_REVENUE_MISSING = "revenue_missing"  # 月營收：本站資料庫尚無此月（不判斷是否已公告）
STATUS_SOURCE_DOWN = "source_down"        # 來源故障（需附上次成功日期）

# ---- 月營收缺值文字（DECISIONS 2026-11-10 04:36：全站月營收不用「尚未公告」）----
REVENUE_MISSING_LABEL = "本站資料庫尚無此月營收"
REVENUE_DEADLINE_NOTE = "法定期限為次月 10 日前；本站只反映已入庫資料，不判斷公司是否已公告"
FINANCIALS_MISSING_LABEL = "本站資料庫尚無此期財報"
FINANCIALS_DEADLINE_NOTE = "法定期限：Q1 5/15、Q2 8/14、Q3 11/14、年報次年 3/31；本站只反映已入庫資料，不判斷公司是否已公告"
INDEX_CHANGE_PCT_MISSING_NOTE = "index_daily.change_pct 為空（來源未提供）；網站不自行計算百分比"

STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_INSUFFICIENT: "資料不足",
    STATUS_NOT_ANNOUNCED: "尚未公告",
    STATUS_REVENUE_MISSING: REVENUE_MISSING_LABEL,
    STATUS_FINANCIALS_MISSING: FINANCIALS_MISSING_LABEL,
    STATUS_ERROR: "讀取失敗",
    STATUS_NOT_APPLICABLE: "不適用",
    STATUS_SOURCE_DOWN: "來源故障",
    # 篩選中心／產業分類（HUB_HANDOFF §5.6；2026-11-24）
    "rate_pending": "計算中",          # 5 日勝率：上線初期，歷史勝率回補中（不顯示任何比例）
    "rate_few": "樣本不足",            # 5 日勝率：未達 N ≥ 100 且 D ≥ 30（M5、M8 季頻結構性不足）
    "mode_na": "尚未提供",             # 篩選模式：研究分析模組尚未提供計算
    "mode_error": "無法計算",
    "insufficient_data": "資料不足",   # 篩選模式：可評估比例未達 95%
    "halted": "停牌",
    "no_trade": "無成交",
    "no_data": "資料不足",
}

TEST_LABEL = "測試資料"

# 事件分類（SPEC §6）：四類標籤
EVENT_CATEGORIES = {
    "official": "官方已公告事實",
    "media": "媒體報導",
    "social": "社群未證實訊息",
    "ai": "AI分析推論",
}


@dataclass
class Val:
    """一個可追溯的數值。value 為 None 時必須有非 ok 的 status。"""

    value: Optional[float]
    unit: str = ""                 # 股、新台幣元、%、元/股、倍…
    source: str = ""               # 來源名稱（例：證交所 STOCK_DAY_ALL）
    as_of: str = ""                # 資料所屬日期（股價日期／營收月份／財報季度／公告日期）
    status: str = STATUS_OK
    note: str = ""                 # 不適用原因、故障說明等
    last_success: str = ""         # 來源故障時的上次成功日期
    is_test: bool = False
    digits: int = 2
    href: str = ""                 # 點數字開啟的來源位置（/source?table=…、個股頁錨點、選股列、資料狀態錨點）
    ref: Optional[dict] = None     # 追溯資訊：{"table": 來源資料表, "key": "symbol=2330, price_date=…"}

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, "")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.value is not None


# 選股參數名稱＝analysis/USAGE.md 的 ScreenParams 名稱
BASIC_PARAMS = ["ma_short", "ma_long", "breakout_lookback", "volume_lookback", "volume_multiplier", "min_turnover"]
TOGGLE_PARAMS = ["enable_trend", "enable_breakout", "enable_volume", "enable_turnover"]
# 資料品質類（進階設定）：數值、字串、布林
ADVANCED_NUM_PARAMS = ["max_gap_days", "max_daily_move", "legacy_max_daily_move", "min_listing_days",
                       "new_listing_no_limit_days", "adj_prev_close_tolerance"]
# coverage_policy：USAGE.md §3「不放 UI；正式結果固定 strict」→ 網站不接受此參數（一律用 analysis 預設 strict）
ADVANCED_STR_PARAMS: list[str] = []
ADVANCED_BOOL_PARAMS = ["derive_share_factor", "check_unexplained_gap"]
INT_PARAMS = {"ma_short", "ma_long", "breakout_lookback", "volume_lookback", "max_gap_days", "min_listing_days",
              "new_listing_no_limit_days"}
PARAM_LABELS = {
    "ma_short": "短均線天數", "ma_long": "長均線天數", "breakout_lookback": "突破回看日數",
    "volume_lookback": "均量回看日數", "volume_multiplier": "量能倍數", "min_turnover": "成交金額門檻",
    "enable_trend": "條件1 趨勢", "enable_breakout": "條件2 突破", "enable_volume": "條件3 量能",
    "enable_turnover": "條件4 成交金額", "max_gap_days": "視窗內最多停牌／無成交日數",
    "max_daily_move": "單日漲跌幅上限（2015-06-01 起）", "legacy_max_daily_move": "單日漲跌幅上限（2015-06-01 前）",
    "min_listing_days": "最低上市櫃交易日數", "new_listing_no_limit_days": "新上市無漲跌幅限制日數",
    "adj_prev_close_tolerance": "除權息前收容差", "coverage_policy": "覆蓋政策",
    "derive_share_factor": "推導股數調整因子", "check_unexplained_gap": "檢查無法解釋的跳空",
}


@dataclass
class ScreenParams:
    """網站端只「收集」使用者輸入，不夾值、不修正；驗證一律交給 analysis 的 ScreenParams。

    values：要傳給 analysis ScreenParams 的關鍵字參數（只含使用者有提供的；其餘用 analysis 預設）。
    raw：使用者輸入的原字串（回填表單，讓使用者看到自己輸入的值）。
    """

    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    sort: str = "turnover"
    order: str = "desc"

    def key(self) -> tuple:
        return tuple(sorted((k, repr(v)) for k, v in self.values.items()))


@dataclass
class Condition:
    key: str
    label: str          # 條件文字（含實際參數）
    passed: Optional[bool]  # None = 無法判斷（資料不足）
    actual: str         # 實際值說明
    enabled: bool = True


@dataclass
class ScreenRow:
    code: str
    name: str
    market: str
    trade_date: str
    close: Val
    change_pct: Val
    turnover: Val
    ma_short: Val
    ma_long: Val
    vol_ratio: Val
    conditions: list[Condition] = field(default_factory=list)
    change_check: str = ""   # analysis summary.change_check（ok／mismatch／…）；只標記，不改值
    extra: list = field(default_factory=list)   # analysis-0.9.0 新欄（screen_extra_fields() 的輸出）


@dataclass
class Excluded:
    code: str
    name: str
    reason: str
    excl_code: str = ""
    label: str = ""          # 排除碼中文
    detail: str = ""         # analysis exclusion_reason（英文代碼已換中文）
    extra: list = field(default_factory=list)
    raw: str = ""            # analysis exclusion_reason 原文（放 title）


@dataclass
class ScreenResult:  # noqa: E302
    params: ScreenParams
    trade_date: str
    engine: str                 # 計算引擎名稱（測試假資料／analysis/screener.py）
    is_test: bool
    rows: list[ScreenRow] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    universe_count: int = 0
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    error: str = ""             # 參數錯誤或資料無法讀取（與「零檔」不同狀態）
    non_common: list[dict] = field(default_factory=list)  # securities_excluded（非普通股排除名單）


# ---- 量與金額口徑（集中於此；選股頁、個股頁、首頁、CSV、匯出共用）----
# 2026-11-17 負責人指示（取代 2026-11-10 03:45「上櫃不含鉅額，已知限制」）：口徑一律依 daily_prices.volume_basis
# （逐日實際值）顯示，不用市場或來源網址推斷；缺欄或空值＝「口徑不明」。
VOLUME_BASIS_TITLE = "量與金額口徑"
VOLUME_BASIS_PENDING = ""          # 舊名保留相容
VOLUME_BASIS_DECISION = "負責人 2026-11-17 指示；docs/indicators.md §9"
VOLUME_BASIS_STATEMENT = ("上櫃以櫃買中心 dailyQuotes 總量（含鉅額）為主；取不到時暫用 tradingStock，標「不含鉅額」。"
                          "上市以證交所 STOCK_DAY 總量（含鉅額）。上櫃是否含盤後定價、零股待查證。")
VOLUME_BASIS = {   # 個股頁只列該市場的一句（內容與 VOLUME_BASIS_STATEMENT 相同，不另加假設）
    "TWSE": "上市以證交所 STOCK_DAY 總量（含鉅額）",
    "TPEx": "上櫃以櫃買中心 dailyQuotes 總量（含鉅額）為主；取不到時暫用 tradingStock，標「不含鉅額」；是否含盤後定價、零股待查證",
}
VOLUME_BASIS_UNKNOWN = "口徑不明"


def volume_basis_note(market: str | None = None) -> str:
    """market=None → 完整說明（選股頁、CSV）；TWSE／TPEx → 只該市場那一句（個股頁）；其他 → 完整說明。
    這只是「來源政策」說明；每一列實際是哪種口徑看 volume_basis（basis_counts_text／basis_segments）。"""
    if market in VOLUME_BASIS:
        return f"{VOLUME_BASIS_TITLE}：{VOLUME_BASIS[market]}。"
    return f"{VOLUME_BASIS_TITLE}：{VOLUME_BASIS_STATEMENT}"


# ---- 來源端點名稱（只用來「顯示來源名稱」，不再推斷口徑）----
# 2026-11-17 起移除「依來源網址推斷口徑」：volume_basis 缺欄或空值一律「口徑不明」，避免與上面的說明矛盾。
VOLUME_BASIS_BY_SOURCE = [
    ("twse.com.tw/exchangeReport/STOCK_DAY?", "TWSE STOCK_DAY"),
    ("twse.com.tw/exchangeReport/MI_INDEX", "TWSE MI_INDEX"),
    ("tpex.org.tw/www/zh-tw/afterTrading/tradingStock", "TPEx tradingStock"),
    ("tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes", "TPEx dailyQuotes"),
]
SOURCE_OTHER_LABEL = "其他來源"
VOLUME_BASIS_NOTE = volume_basis_note()


def volume_basis_policy(market: str | None = None) -> str:
    """個股頁逐日口徑下方的小字：只寫來源政策，不重複「量與金額口徑：」標題。"""
    return "來源政策：" + (VOLUME_BASIS[market] + "。" if market in VOLUME_BASIS else VOLUME_BASIS_STATEMENT)

# 首頁「成交金額合計」（design-r2 §4-7）：範圍＝追蹤範圍內普通股；口徑看當日各列 volume_basis（basis_counts_text 附在後面）
TURNOVER_TOTAL_NOTE = "只含追蹤範圍內的上市櫃普通股（不含 ETF、權證、特別股、TDR）"

# 官方「X 無比價」（design-r2 §4-3；review-r1 建議 pipeline 存 change_sign='X'）：不算平盤，首頁另列一類
NO_COMPARE_SIGN = "X"
NO_COMPARE_LABEL = "無比價"
NO_COMPARE_NO_COLUMN_NOTE = "daily_prices 沒有 change_sign 欄位，無法判斷無比價（不是 0 檔）；平盤家數可能含無比價"
NO_COMPARE_ALL_NULL_NOTE = "當日 change_sign 全為空值，無法判斷無比價（不是 0 檔）；平盤家數可能含無比價"
NO_COMPARE_NOT_STORED_NOTE = ("資料庫尚未保存官方「X 無比價」符號（資料工程修正中）：無比價家數未知（不是 0 檔），"
                              "這類股票目前會被算成平盤")
FLAT_MAYBE_NOCOMPARE_NOTE = "可能含無比價（官方 X 符號尚未入庫）"


def source_label(source: str | None) -> tuple[str, bool]:
    """daily_prices.source → (端點名稱, 是否已知端點)。未知 → (「其他來源」, False)；原值由呼叫端放 title。"""
    s = source or ""
    for pat, label in VOLUME_BASIS_BY_SOURCE:
        if pat in s:
            return label, True
    return SOURCE_OTHER_LABEL, False


def source_basis(source: str | None) -> tuple[str, str, bool]:
    """舊介面相容：(端點名稱, 口徑, 是否已知)。口徑不再由來源推斷，一律「口徑不明」（要看 volume_basis）。"""
    label, known = source_label(source)
    return label, VOLUME_BASIS_UNKNOWN, False


def basis_segments(rows: list[tuple], has_column: bool) -> list[dict]:
    """[(日期, source, volume_basis)]（依日期排序）→ 依（端點, 口徑）連續分段：
    [{label, basis, known, state, start, end, days, raw_sources}]。口徑只看 volume_basis（缺欄／空值＝口徑不明）。"""
    segs: list[dict] = []
    for d, src, vb in sorted(rows, key=lambda x: x[0]):
        label, _ = source_label(src)
        b = volume_basis_label(vb, has_column)
        key = (label, b["label"])
        if segs and (segs[-1]["label"], segs[-1]["basis"]) == key:
            segs[-1]["end"] = d
            segs[-1]["days"] += 1
            if src and src not in segs[-1]["raw_sources"] and len(segs[-1]["raw_sources"]) < 3:
                segs[-1]["raw_sources"].append(src)
        else:
            segs.append({"label": label, "basis": b["label"], "known": b["state"] != "basis-unknown", "state": b["state"],
                         "note": b["note"], "start": d, "end": d, "days": 1, "raw_sources": [src] if src else []})
    return segs


def source_basis_segments(dated_sources: list[tuple]) -> list[dict]:
    """舊介面相容：[(日期, source)] 或 [(日期, source, volume_basis)]。只有兩欄時視為缺 volume_basis（口徑不明）。"""
    rows = [(x[0], x[1], x[2] if len(x) > 2 else None) for x in dated_sources]
    has = any(len(x) > 2 for x in dated_sources)
    return basis_segments(rows, has)


def basis_counts(rows: list[dict]) -> dict:
    """當日各列（已限追蹤範圍、有收盤價）→ {has_column, incl, excl, unknown, n}。"""
    has = bool(rows) and "volume_basis" in rows[0]
    out = {"has_column": has, "incl": 0, "excl": 0, "unknown": 0, "n": len(rows)}
    for r in rows:
        v = r.get("volume_basis") if has else None
        v = v.strip() if isinstance(v, str) else v
        out["incl" if v == "incl_block" else "excl" if v == "excl_block" else "unknown"] += 1
    return out


def basis_counts_text(c: dict | None, date: str = "") -> str:
    """實際口徑分布一句（依 volume_basis）；缺欄 →「口徑不明（資料庫尚無量額口徑欄位）」。"""
    head = f"{date} 實際口徑（依各列逐日標記）" if date else "實際口徑（依各列逐日標記）"
    if not c or not c.get("n"):
        return ""
    if not c["has_column"]:
        return f"{head}：{c['n']} 檔皆為{VOLUME_BASIS_UNKNOWN}（資料庫尚無量額口徑欄位）"
    parts = [f"{VOLUME_BASIS_LABELS['incl_block']} {c['incl']} 檔", f"{VOLUME_BASIS_LABELS['excl_block']} {c['excl']} 檔",
             f"{VOLUME_BASIS_UNKNOWN} {c['unknown']} 檔"]
    return f"{head}：" + "、".join(parts)

# ---- change_series／screener 的 change_check（語意依 analysis/USAGE.md、docs/indicators.md 1.1）----
# 自算 change_pct 為正式值；來源（官方）漲跌只做交叉檢查，不一致只標記、不覆蓋
CHANGE_CHECK_LABELS = {
    "ok": "與官方漲跌一致",
    "mismatch": "與官方漲跌不一致",
    "no_source": "官方未提供漲跌",
    "source_not_comparable": "官方漲跌不比較（符號 X）",
    "source_sign_conflict": "官方漲跌正負號矛盾",
    "after_no_trade": "前一日無成交，改用官方參考價比對",        # analysis-0.9.0（DECISIONS 2026-11-10 12:05 ②）
}
CHANGE_CHECK_NOTES = {
    "ok": "自算漲跌與官方帶號漲跌差異在容許誤差內。",
    "mismatch": "自算漲跌與官方帶號漲跌差異超過容許誤差；網站顯示的漲跌幅仍為自算正式值，僅標記、不覆蓋。",
    "no_source": "來源沒有漲跌欄位值，無法交叉檢查。",
    "source_not_comparable": "來源漲跌符號為 X（不比價等），不做比較。",
    "source_sign_conflict": "來源 change_sign 與數值正負矛盾，不做比較。",
    "after_no_trade": ("前一交易日整股無成交：官方漲跌以參考價計，自算漲跌以前一有效收盤為基準；有官方參考價時依參考價比對，"
                       "沒有參考價或官方標無比價時不比對。不算不一致；網站顯示的漲跌幅仍為自算正式值。"),
}
CHANGE_CHECK_STATE = {"ok": "ok", "mismatch": "mismatch", "no_source": "insufficient",
                      "source_not_comparable": "na", "source_sign_conflict": "mismatch", "after_no_trade": "na"}

# ---- 排除代碼的網站端備援說明（analysis.EXCLUSION_CODES 有提供時一律以 analysis 為準）----
# EXCL_SUSPENDED（確認停牌）目前 analysis 尚未提供說明；EXCL_NO_ROW_SUSPENDED_OR_MISSING 用 analysis 原文
EXCLUSION_FALLBACK = {
    "EXCL_SUSPENDED": "確認停牌（有官方停牌資料確認評估日停止買賣）",
    "EXCL_NO_ROW_SUSPENDED_OR_MISSING": "停牌或資料缺漏，待確認（評估日本股無資料列，尚無官方停牌資料可確認）",
    # analysis-0.9.0 新碼；analysis 仍是舊版時不會出現，但 CSV／統計遇到時仍顯示中文（docs/indicators.md §9.3）
    "EXCL_VOLUME_BASIS": "量額口徑非總量（不含鉅額或口徑不明）",
    # analysis-0.9.2 新碼（docs/indicators.md §8.7）：無法評估，不算資料不齊、不入 95% 分母
    "EXCL_WINDOW_NO_TRADE": "無法評估（無成交）：計算視窗內官方無成交的交易日過多（流動性不足，不算資料不齊）",
    "EXCL_WINDOW_SUSPENDED": "無法評估（停止買賣）：計算視窗內官方停牌或減資／變更面額停止買賣的交易日過多（不算資料不齊）",
}

# 「無法評估」（analysis screener.NOT_EVALUABLE_CODES；§8.7）：自 95% 覆蓋率分母移除。analysis 舊版沒有此常數時用這份
NOT_EVALUABLE_FALLBACK = frozenset({"EXCL_NEW_LISTING", "EXCL_SUSPENDED", "EXCL_NO_TRADE",
                                    "EXCL_WINDOW_NO_TRADE", "EXCL_WINDOW_SUSPENDED"})
# 無法評估的類別（畫面短標）；未列在這裡的無法評估代碼顯示「其他」
NOT_EVALUABLE_KINDS = {"EXCL_NO_TRADE": "無成交", "EXCL_WINDOW_NO_TRADE": "無成交",
                       "EXCL_SUSPENDED": "停止買賣", "EXCL_WINDOW_SUSPENDED": "停止買賣", "EXCL_NEW_LISTING": "上市未滿"}
NOT_EVALUABLE_KIND_ORDER = ("無成交", "停止買賣", "上市未滿", "其他")
SUSPEND_CODES = ("EXCL_SUSPENDED", "EXCL_WINDOW_SUSPENDED", "EXCL_NO_ROW_SUSPENDED_OR_MISSING")   # 資料狀態頁「停牌」分類

# ---- 量額口徑與鉅額（daily_prices.volume_basis／block_*；docs/indicators.md §9.4、docs/schema.md §3.4）----
# 舊庫沒有這四欄、或欄位為 NULL → 一律「口徑不明」／「未知」，不得顯示 0。
VOLUME_BASIS_LABELS = {"incl_block": "總量（含鉅額）", "excl_block": "不含鉅額"}
VOLUME_BASIS_UNKNOWN_LABEL = "口徑不明"
VOLUME_BASIS_DETAILS = {
    "incl_block": "官方總量，已含鉅額交易（上市、上櫃組成不完全相同，見下方量與金額口徑說明）",
    "excl_block": "來源 TPEx tradingStock（取不到 dailyQuotes 時暫用），不含鉅額交易",
}
VOLUME_BASIS_NO_COLUMN_NOTE = "資料庫尚無量額口徑欄位（資料工程遷移中），無法確認是否含鉅額"
VOLUME_BASIS_NULL_NOTE = "這一列的量額口徑欄位為空值，無法確認是否含鉅額"
VOLUME_BASIS_STATE = {"incl_block": "basis-total", "excl_block": "basis-excl"}
VOLUME_BASIS_BADGE = {"basis-total": "st-ok", "basis-excl": "st-derived", "basis-unknown": "st-insufficient badge-nodata"}
BLOCK_SOURCE_BADGE = {"tpex_block_day": "st-basis", "name_match": "st-derived"}

BLOCK_SOURCE_LABELS = {"tpex_block_day": "鉅額逐筆", "name_match": "名稱比對"}
BLOCK_SOURCE_NOTES = {
    "tpex_block_day": "來源 TPEx 鉅額逐筆，有證券代號",
    "name_match": "依櫃買中心歷史彙總以證券名稱比對，非代號對應，可能有誤",
}
BLOCK_UNKNOWN_LABEL = "未知"
BLOCK_NONE_LABEL = "當日無鉅額"
BLOCK_NO_COLUMN_NOTE = "資料庫尚無鉅額欄位（資料工程遷移中）"
BLOCK_NULL_NOTE = "這一列的鉅額欄位為空值（未取得），不代表當日沒有鉅額"
BLOCK_COLUMNS = ("block_shares", "block_amount_twd", "block_source", "volume_basis")


def volume_basis_label(raw, has_column: bool = True) -> dict:
    """daily_prices.volume_basis 原值 → {label, state, note, raw}。只認 incl_block／excl_block；其他值、NULL、缺欄＝口徑不明。"""
    v = raw.strip() if isinstance(raw, str) else raw
    if v in VOLUME_BASIS_LABELS:
        st = VOLUME_BASIS_STATE[v]
        return {"label": VOLUME_BASIS_LABELS[v], "state": st, "badge": VOLUME_BASIS_BADGE[st], "note": VOLUME_BASIS_DETAILS[v], "raw": v}
    if not has_column:
        note = VOLUME_BASIS_NO_COLUMN_NOTE
    elif v is None or v == "":
        note = VOLUME_BASIS_NULL_NOTE
    else:
        note = "量額口徑欄位為非預期值，視為口徑不明：" + UNKNOWN_CODE_LABEL.format(raw=v)
    return {"label": VOLUME_BASIS_UNKNOWN_LABEL, "state": "basis-unknown", "badge": VOLUME_BASIS_BADGE["basis-unknown"],
            "note": note, "raw": v}


def block_source_label(raw) -> str:
    v = raw.strip() if isinstance(raw, str) else raw
    if v is None or v == "":
        return ""
    return BLOCK_SOURCE_LABELS.get(v, UNKNOWN_CODE_LABEL.format(raw=v))


def block_info(shares, amount, source, has_column: bool = True) -> dict:
    """鉅額欄 → 顯示用 dict。NULL／缺欄＝「未知」（不顯示 0）；0＝「當日無鉅額」；>0＝股數（＋金額、來源標示）。"""
    def num(x):
        try:
            return None if x is None or x == "" else float(x)
        except (TypeError, ValueError):
            return None
    sh, am = num(shares), num(amount)
    src = block_source_label(source)
    src_badge = BLOCK_SOURCE_BADGE.get(source.strip() if isinstance(source, str) else source, "st-na")
    src_note = BLOCK_SOURCE_NOTES.get(source.strip() if isinstance(source, str) else source, "")
    if src and not src_note:
        src_note = "來源標示不在已知清單，照原始值顯示"
    if not has_column:
        return {"state": "block-unknown", "label": BLOCK_UNKNOWN_LABEL, "shares": None, "amount": None,
                "source": "", "source_badge": "", "source_note": "", "note": BLOCK_NO_COLUMN_NOTE}
    if sh is None:
        return {"state": "block-unknown", "label": BLOCK_UNKNOWN_LABEL, "shares": None, "amount": am,
                "source": src, "source_badge": src_badge, "source_note": src_note, "note": BLOCK_NULL_NOTE}
    if sh == 0:
        return {"state": "block-none", "label": BLOCK_NONE_LABEL, "shares": 0, "amount": am,
                "source": src, "source_badge": src_badge, "source_note": src_note, "note": ""}
    return {"state": "block-some", "label": f"其中鉅額 {sh:,.0f} 股", "shares": sh, "amount": am,
            "source": src, "source_badge": src_badge, "source_note": src_note,
            "note": "鉅額已含在成交量內（不扣除）" + ("；名稱比對的鉅額不得單獨作為研究結論依據" if source == "name_match" else "")}


# ---- analysis 說明文字裡的英文代碼 → 中文（只在顯示層替換；原值仍在 analysis 輸出、來源頁與 title）----
ANALYSIS_TEXT_CODES = [
    ("excl_block（不含鉅額，tradingStock）", "不含鉅額（tradingStock）"),
    ("NULL（口徑不明）", "口徑不明（欄位為空值）"),
    ("（volume_basis 為 NULL）", "（量額口徑欄位為空值）"),
    ("excl_block／NULL", "不含鉅額或口徑不明"),
    ("excl_block", "不含鉅額"),
    ("incl_block", "總量（含鉅額）"),
    ("is_no_trade 交叉檢查", "「整股無成交」旗標交叉檢查"),
    ("is_no_trade＝1", "「整股無成交」旗標為是"),
    ("is_no_trade＝0", "「整股無成交」旗標為否"),
    ("官方漲跌標 X", "官方漲跌標無比價"),
    ("trading_halts 或 daily_prices.is_halted", "停牌表或行情表停牌旗標"),
    ("daily_prices.is_halted＝1", "行情表停牌旗標為是"),
    ("trading_halts 有停牌紀錄", "停牌表有停牌紀錄"),
    ("不在普通股清單（securities）中", "不在普通股清單中"),
    ("不在 securities", "不在普通股清單"),
    ("corporate_actions", "除權息／減資事件表"),
]
# 可留在可見文字的英文：官方端點名稱、市場代碼、均線名稱、官方「X」符號、證券類別縮寫
LATIN_ALLOWED = {"dailyQuotes", "tradingStock", "STOCK_DAY", "MI_INDEX", "TWSE", "TPEx", "TPEX", "X", "ETF", "ETN", "TDR",
                 "CSV", "CFI"}
_LATIN_RE = __import__("re").compile(r"[A-Za-z][A-Za-z0-9_.]*")
_OTHER_RE = __import__("re").compile(r"其他資料說明（代碼：[^）]*）")
_VB_ODD_RE = __import__("re").compile(r"(\S+)（非允許值，視為口徑不明）")
_COV_RE = __import__("re").compile(r"\b(?:coverage_[a-z_]+|window_before_coverage_start)(?::[A-Za-z0-9_\-]+)*")


def latin_residue(text: str) -> list[str]:
    """可見文字中不在允許清單的英文詞（忽略「其他資料說明（代碼：…）」裡的代碼與 MA20 之類均線名）。"""
    t = _OTHER_RE.sub("", text or "")
    return [w for w in _LATIN_RE.findall(t) if w not in LATIN_ALLOWED and not __import__("re").fullmatch(r"MA\d*", w)]


def analysis_text_zh(text) -> str:
    """把 analysis 說明（exclusion_reason、data_note、排除碼說明）中的已知英文代碼換成中文；其餘文字原樣保留。
    要保證畫面不出英文，請用 zh_text()（換不完時改成備援格式）。"""
    if not isinstance(text, str) or not text:
        return ""
    text = _VB_ODD_RE.sub(lambda m: UNKNOWN_CODE_LABEL.format(raw=m.group(1)) + "（視為口徑不明）", text)
    for a, b in ANALYSIS_TEXT_CODES:
        text = text.replace(a, b)
    text = _COV_RE.sub(lambda m: _coverage_detail(m.group(0)), text)
    kind_re = __import__("re").compile(r"\b(" + "|".join(sorted(ADJ_KIND_LABELS, key=len, reverse=True)) + r")\b")
    text = kind_re.sub(lambda m: ADJ_KIND_LABELS[m.group(1)], text)
    return text


UNKNOWN_TEXT_LABEL = "其他資料說明（原文見提示）"   # 整句英文、沒有代碼時的固定字樣（原文放 title）
_CODE_FORMAT_RE = __import__("re").compile(r"^[A-Z0-9_]+$")


def is_code_format(v) -> bool:
    """原文整體是不是「代碼格式」（大寫英數與底線）。"""
    return isinstance(v, str) and bool(_CODE_FORMAT_RE.match(v.strip()))


def zh_text(text, code: str = "") -> dict:
    """顯示用：{text, title, known}，用於 data_note、排除原因、選股狀態說明等自由文字。
    替換後仍有英文詞時，整段改成備援文字，原文放 title：
    - 呼叫端給的代碼（如排除碼）或原文整體符合 ^[A-Z0-9_]+$ →「其他資料說明（代碼：X）」
    - 其他（整句英文、混雜文字）→ 固定「其他資料說明（原文見提示）」，不再取第一個英文詞當代碼"""
    raw = text if isinstance(text, str) else ""
    t = analysis_text_zh(raw)
    if not latin_residue(t):
        return {"text": t, "title": "", "known": True}
    c = code if is_code_format(code) else (raw.strip() if is_code_format(raw) else "")
    return {"text": UNKNOWN_CODE_LABEL.format(raw=c) if c else UNKNOWN_TEXT_LABEL, "title": raw, "known": False}


def change_check_label(code) -> str:
    if not code:
        return "—"
    return CHANGE_CHECK_LABELS.get(code) or UNKNOWN_CODE_LABEL.format(raw=code)


# screen_results／summary 新欄（analysis-0.9.0）中文欄名；值為 NULL 時顯示的文字（不出英文代碼）
SCREEN_EXTRA_FIELDS = {
    "data_note": "資料說明",
    "volume_basis": "評估日量額口徑",
    "window_volume_basis_days": "均量窗口非總量日數",
    "window_bad_data_days": "觀察窗資料異常日數",
}
SCREEN_EXTRA_OLD_ANALYSIS = "目前分析版本未提供"   # analysis 舊版、輸出沒有該欄
SCREEN_EXTRA_NOT_COMPUTED = "未算到"                 # 有該欄但為 NULL（例如提早排除、沒算到窗口）
VOLUME_BASIS_STATUS_LABELS = {
    "volume_basis_column_missing": "資料庫尚無量額口徑欄位：本次選股未檢查量額口徑（研究模式照算，正式寫入會被拒絕）",
}



# ---- 月營收年增率：當月營收為負的註記（第十三批；年增率數字照 analysis rev_yoy-v1，不改成不適用）----
REVENUE_NEGATIVE_NOTE = "當月營收為負"
REVENUE_NEGATIVE_DETAIL = "當月營收為負，年增率依 analysis 定義照算，解讀時請一併看營收金額"

# ---- source_status.notes 代碼 → 中文（第十三批）----
# 代碼保留英文（資料工程寫入）；網站只在顯示層對映。來源：data/twstock.db 現有值＋pipeline/fetchers/*.py 的 touch_source(notes=…)。
# 注意：任何譯文都不可含「尚未公告」字串（F-001 條件 3）。
SOURCE_NOTE_CODES = {
    "not_yet_due": "尚未到公告期限",                       # revenue.py：資料月未結束，或公告期內 0 列（ok=1，不算故障）
    "partial_pending": "公告期內，官方彙總表目前只列出部分公司（不算故障）",  # revenue.py：公告期內頁面列數 < 50
    "source_down": "公告期限後仍抓不到資料（來源故障）",       # revenue.py：期限後 0 列（ok=0）
    "empty_30d": "近 30 日沒有新資料",                     # material.py：重大訊息近 30 日無新增列
}
# 帶參數的代碼（key=value；多個以 ; 分隔）
SOURCE_NOTE_PARAMS = {
    "fmtqik_days": "證交所 FMTQIK 交易日 {v} 日",            # calendar.py
    "st41_days": "櫃買 ST41 交易日 {v} 日",                  # calendar.py
    "date": "資料期間 {v}",                                  # corporate_actions.py（tpex_exright／revivt／pvChgRslt）
    "rows": "{v} 列",                                        # corporate_actions.py
}
SOURCE_NOTE_UNKNOWN = "其他資料說明（代碼：{raw}）"   # 與 UNKNOWN_CODE_LABEL 同格式


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def source_note_label(raw) -> dict:
    """source_status.notes → {"code", "label", "detail", "raw", "known"}。
    - 「code」或「code: 英文說明」：code 查 SOURCE_NOTE_CODES（說明保留在 detail，只放 tooltip）
    - 「k=v;k=v」：逐項查 SOURCE_NOTE_PARAMS（date 的 ~ 改成「～」）
    - 已是中文的來源頁標題（corporate_actions.py 以頁面標題寫入）：原樣顯示
    - 其他 → 「其他資料說明（代碼：原始代碼）」"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"code": "", "label": "", "detail": "", "raw": "", "known": True}
    s = str(raw).strip()
    head, _, detail = s.partition(":")
    head = head.strip()
    if head in SOURCE_NOTE_CODES:
        return {"code": head, "label": SOURCE_NOTE_CODES[head], "detail": detail.strip(), "raw": s, "known": True}
    parts = [p.strip() for p in s.split(";") if p.strip()]
    if parts and all("=" in p for p in parts):
        out = []
        for p in parts:
            k, _, v = p.partition("=")
            if k.strip() not in SOURCE_NOTE_PARAMS:
                out = None
                break
            out.append(SOURCE_NOTE_PARAMS[k.strip()].format(v=v.strip().replace("~", "～")))
        if out:
            return {"code": ";".join(p.split("=")[0] for p in parts), "label": "，".join(out), "detail": "",
                    "raw": s, "known": True}
    if _has_cjk(s):
        return {"code": "", "label": s, "detail": "", "raw": s, "known": True}
    return {"code": s, "label": SOURCE_NOTE_UNKNOWN.format(raw=s), "detail": "", "raw": s, "known": False}


# ---- analysis 不可用代碼 → 中文（第十五批；K 線退回原始價、均線不可用的說明）----
# 來源：analysis/series.py（_boundary、ma_series 的 ma_N_note）、analysis/adjust.py（Coverage.status、_segment_for、
# coverage_boundary、resolve_action 的 _fail 代碼）。原始代碼另放 raw 欄位，不在畫面主文出現。
ADJ_MARKET_LABELS = {"TWSE": "上市", "TPEx": "上櫃", "未知市場": "未知市場"}
ADJ_KIND_LABELS = {"cash_dividend": "現金股利", "stock_dividend": "股票股利", "ex_both": "現金及股票股利",
                   "capital_reduction": "減資", "par_value_change": "變更面額", "stock_split": "股票分割"}
UNAVAILABLE_CODE_LABELS = {
    "EXCL_ADJ_COVERAGE_GAP": "除權息／減資資料未完整涵蓋",           # series._boundary、screener
    "EXCL_ADJ_FACTOR_MISSING": "除權息／減資事件缺調整因子",         # adjust.resolve_action
    "EXCL_ADJ_FACTOR_INCONSISTENT": "除權息／減資事件調整因子不合理",  # adjust.resolve_action
    "EXCL_UNEXPLAINED_GAP": "還原後仍有無法解釋的跳空",             # series._boundary
}
# 後綴細節（EXCL_ADJ_COVERAGE_GAP 的 unavailable_reason；多個以 ; 分隔）
COVERAGE_DETAIL_LABELS = {
    "coverage_table_missing": "覆蓋表（corporate_actions_coverage）不存在",
    "coverage_table_empty": "覆蓋表（corporate_actions_coverage）沒有任何資料",
    "coverage_missing": "{market}{kind}沒有覆蓋紀錄",                       # coverage_missing:市場:類別
    "window_before_coverage_start": "{market}{kind}覆蓋起日為 {date}，更早的期間未涵蓋",  # …:市場:類別:日期
    "coverage_end_before_as_of": "{market}{kind}覆蓋只到 {date}，未涵蓋資料基準日",      # …:市場:類別:日期
    "coverage_hole": "{market}{kind}在 {date} 有覆蓋缺口",                  # …:市場:類別:日期
}
MA_NOTE_LABELS = {   # series.ma_series 的 ma_N_note
    "INSUFFICIENT_HISTORY": "有效交易日不足，尚無此均線",
    "ADJ_UNAVAILABLE": "還原價不可用，不計算均線",
    "NO_TRADE": "當日無成交或無資料列",
}
UNKNOWN_CODE_LABEL = "其他資料說明（代碼：{raw}）"   # 全站未知代碼備援格式（2026-11-17 統一）


def _coverage_detail(part: str) -> str:
    bits = part.split(":")
    key = bits[0]
    if key not in COVERAGE_DETAIL_LABELS:
        return UNKNOWN_CODE_LABEL.format(raw=part)
    tpl = COVERAGE_DETAIL_LABELS[key]
    if "{" not in tpl:
        return tpl if len(bits) == 1 else UNKNOWN_CODE_LABEL.format(raw=part)
    m = ADJ_MARKET_LABELS.get(bits[1], bits[1]) if len(bits) > 1 else ""
    k = ADJ_KIND_LABELS.get(bits[2], UNKNOWN_CODE_LABEL.format(raw=bits[2])) if len(bits) > 2 else ""
    d = bits[3] if len(bits) > 3 else ""
    if ("{date}" in tpl and not d) or len(bits) < 3:
        return UNKNOWN_CODE_LABEL.format(raw=part)
    return tpl.format(market=m, kind=f" {k} " if k else "", date=d).replace("  ", " ").strip()


def _event_detail(detail: str) -> str:
    """adjust.resolve_action 的 detail（已是中文，開頭為「YYYY-MM-DD 類型代碼 …」）→ 類型代碼換中文。"""
    import re as _re
    m = _re.match(r"^(\d{4}-\d{2}-\d{2})\s+([a-z_]+)\s*(.*)$", detail or "")
    if m and m.group(2) in ADJ_KIND_LABELS:
        return f"{m.group(1)} {ADJ_KIND_LABELS[m.group(2)]} {m.group(3)}".strip()
    return detail


def unavailable_label(code, reason="") -> dict:
    """analysis 的 unavailable_code＋unavailable_reason → {"label", "code", "raw", "known"}。
    label 為中文；raw＝「代碼：原始說明」（放 title／API raw 欄位）；未知代碼 →「其他資料說明（代碼：原始代碼）」。"""
    code = (code or "").strip() if isinstance(code, str) else ""
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    raw = f"{code}：{reason}" if code and reason else (code or reason)
    if not code and not reason:
        return {"label": "", "code": "", "raw": "", "known": True}
    if code and code not in UNAVAILABLE_CODE_LABELS:
        return {"label": UNKNOWN_CODE_LABEL.format(raw=code), "code": code, "raw": raw, "known": False}
    head = UNAVAILABLE_CODE_LABELS.get(code, "")
    known = True
    if code == "EXCL_ADJ_COVERAGE_GAP" or (not code and reason.split(":")[0] in COVERAGE_DETAIL_LABELS):
        parts = [_coverage_detail(p.strip()) for p in reason.split(";") if p.strip()]
        known = not any(p.startswith("其他資料說明（") for p in parts)
        detail = "；".join(parts)
    else:
        detail = _event_detail(reason)
    label = f"{head}（{detail}）" if head and detail else (head or detail)
    return {"label": label, "code": code, "raw": raw, "known": known}


def ma_note_label(note) -> dict:
    n = note.strip() if isinstance(note, str) else ""
    if not n:
        return {"label": "", "code": "", "raw": "", "known": True}
    if n in MA_NOTE_LABELS:
        return {"label": MA_NOTE_LABELS[n], "code": n, "raw": n, "known": True}
    if n.isupper() and " " not in n:
        return {"label": UNKNOWN_CODE_LABEL.format(raw=n), "code": n, "raw": n, "known": False}
    return {"label": n, "code": "", "raw": n, "known": True}   # 已是中文說明


def screen_extra_fields(rec: dict, columns, vb_status: str = "ok") -> list[dict]:
    """analysis summary 一列 → 新欄顯示清單 [{key, label, text, state}]（中文、不出英文代碼）。
    舊版 analysis（輸出沒有該欄）→「目前分析版本未提供」；欄值 NULL →「未算到」（data_note 為 NULL 時不列）。"""
    import math as _m

    def na(v):
        return v is None or (isinstance(v, float) and _m.isnan(v)) or v == ""
    out = []
    for key, label in SCREEN_EXTRA_FIELDS.items():
        if key not in columns:
            out.append({"key": key, "label": label, "text": SCREEN_EXTRA_OLD_ANALYSIS, "state": "old-analysis"})
            continue
        v = rec.get(key)
        if key == "data_note":
            if not na(v):
                z = zh_text(str(v))
                out.append({"key": key, "label": label, "text": z["text"], "title": z["title"], "state": "note"})
            continue
        if key == "volume_basis":
            if na(v) and vb_status == "volume_basis_column_missing":
                out.append({"key": key, "label": label, "text": VOLUME_BASIS_UNKNOWN_LABEL + "（資料庫尚無量額口徑欄位）",
                            "state": "basis-unknown"})
            elif na(v):
                out.append({"key": key, "label": label, "text": VOLUME_BASIS_UNKNOWN_LABEL + "（欄位為空值或未算到評估日）",
                            "state": "basis-unknown"})
            else:
                b = volume_basis_label(v)
                out.append({"key": key, "label": label, "text": b["label"], "state": b["state"]})
            continue
        if na(v):
            out.append({"key": key, "label": label, "text": SCREEN_EXTRA_NOT_COMPUTED, "state": "na"})
        else:
            try:
                out.append({"key": key, "label": label, "text": f"{int(float(v))} 日", "state": "ok" if int(float(v)) == 0 else "warn"})
            except (TypeError, ValueError):
                out.append({"key": key, "label": label, "text": UNKNOWN_CODE_LABEL.format(raw=v), "title": str(v), "state": "na"})
    for e in out:
        e.setdefault("title", "")
    return out


def screen_extra_text(extra: list) -> str:
    return "；".join(f"{e['label']}：{e['text']}" for e in extra)
