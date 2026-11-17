"""研究分析寫入層：screen_results、daily_summaries、news_events（＋news_event_symbols）的冪等版本化寫入。

規則（analysis/tables_draft.md 第 5 節；負責人 2026-11-10 決議）：
- 自然鍵＋version＋is_current＋checksum。checksum 相同 → 跳過；不同 → 舊現行列 is_current=0、寫入新版
  （version＝該鍵既有最大版＋1）。**一律在同一個交易內完成**，失敗整批回滾，不刪除任何舊列。
- 表由資料工程建立；本模組不建表、不改表。只寫入目標表實際存在的欄位（PRAGMA 偵測），
  程式有值但表沒有的欄位列在 WriteReport.dropped_columns（請資料工程補欄），表缺必要欄位則拋錯。
- 時間欄：computed_at＝計算時間（ScreenResult.computed_at 等）；generated_at＝寫入時間；
  input_max_fetched_at＝計算所用輸入列 fetched_at 最大值（io_sqlite.ScreenInputs.input_max_fetched_at）。
  三者與 version／is_current 不納入 checksum（內容相同只是重算或重抓，不升版）。
- screen_run_id＝產生該批 screen_results 的 `job_runs.run_id`（schema §3.11 主鍵名為 run_id），由 pipeline.run 傳入；
  寫入 daily_summaries.screen_run_id（TEXT），screen_results 有此欄時一併寫入。不納入 checksum：內容相同的重跑
  不升版，保留第一次產生該版內容的 run_id。目標庫有 job_runs 表時檢查該 run_id 存在。
- 正式結果只接受 coverage_policy=strict；代號 TEST 開頭的合成資料由表的 CHECK 擋下（測試以暫存 SQLite 放寬）。
- 寫入 data/twstock.db（正式庫）必須明示 allow_production=True；測試一律用暫存 SQLite。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from version import ANALYSIS_VERSION

__all__ = ["WriteReport", "connect_writable", "PRODUCTION_DB", "write_screen_results", "write_daily_summary",
           "write_news_event", "screen_batch_checksum", "row_checksum", "screen_rows"]

PRODUCTION_DB = (Path(__file__).resolve().parents[1] / "data" / "twstock.db").resolve()
TZ = ZoneInfo("Asia/Taipei")
NON_CONTENT = ("computed_at", "input_max_fetched_at", "generated_at", "screen_run_id", "version", "is_current", "checksum")
PROVIDER_RULE = "研究分析規則（analysis/narrative.py）"
EVIDENCE_TYPES = ("官方公告代號", "內文代號", "公司全名")


@dataclass
class WriteReport:
    table: str
    inserted: int = 0              # 新鍵第一版
    versioned: int = 0             # 內容改變 → 升版
    skipped: int = 0               # checksum 相同
    retired: int = 0               # retire_missing=True 時，本批沒有的舊現行列改為非現行
    dropped_columns: list = field(default_factory=list)   # 程式有值、表上沒有的欄位
    versions: dict = field(default_factory=dict)          # 自然鍵 → 寫入後現行版本


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect_writable(db_path: str | Path, allow_production: bool = False) -> sqlite3.Connection:
    """開啟可寫連線（autocommit 模式，交易由本模組 BEGIN IMMEDIATE 控制）。正式庫須 allow_production=True。"""
    p = Path(db_path).resolve()
    if p == PRODUCTION_DB and not allow_production:
        raise PermissionError(f"拒絕寫入正式資料庫 {p}：須明示 allow_production=True（負責人通知後才可執行）")
    if not p.exists():
        raise FileNotFoundError(f"找不到資料庫：{p}（表由資料工程建立，本模組不建庫）")
    con = sqlite3.connect(p, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def _tx(con: sqlite3.Connection) -> Iterator[None]:
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _clean(v: Any) -> Any:
    """轉成 SQLite／JSON 可存的純量：NaN／NaT→None、numpy→Python、Timestamp→日期字串、bool→0/1。"""
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        if pd.isna(v):
            return None
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, bool):
        return int(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def row_checksum(row: Mapping[str, Any], extra: Any = None) -> str:
    """除 NON_CONTENT 外全部欄位（依鍵排序）＋extra 的 SHA-256。浮點以 repr 序列化，避免格式差異。"""
    body = {k: _clean(v) for k, v in row.items() if k not in NON_CONTENT}
    if extra is not None:
        body["__extra__"] = extra
    s = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=repr)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if not cols:
        raise ValueError(f"資料表 {table} 不存在（由資料工程建立）")
    return cols


def _upsert_versioned(con: sqlite3.Connection, table: str, key: Mapping[str, Any], row: dict, cols: set[str],
                      report: WriteReport, required: Sequence[str]) -> tuple[str, int]:
    """單列版本化寫入（呼叫端負責交易）。回傳 ('inserted'|'versioned'|'skipped', 寫入後現行版本)。"""
    miss = [c for c in required if c not in cols]
    if miss:
        raise ValueError(f"{table} 缺少必要欄位 {miss}")
    kw = " AND ".join(f"{k} = ?" for k in key)
    kv = list(key.values())
    cur = con.execute(f"SELECT version, checksum FROM {table} WHERE {kw} AND is_current = 1", kv).fetchone()
    if cur is not None and cur[1] == row["checksum"]:
        return "skipped", int(cur[0])
    maxv = con.execute(f"SELECT MAX(version) FROM {table} WHERE {kw}", kv).fetchone()[0]
    ver = int(maxv or 0) + 1
    if cur is not None:
        con.execute(f"UPDATE {table} SET is_current = 0 WHERE {kw} AND is_current = 1", kv)
    row = dict(row, version=ver, is_current=1)
    for c in row:
        if c not in cols and c not in report.dropped_columns:
            report.dropped_columns.append(c)
    use = [c for c in row if c in cols]
    con.execute(f"INSERT INTO {table} ({','.join(use)}) VALUES ({','.join('?' * len(use))})",
                [_clean(row[c]) for c in use])
    return ("versioned" if cur is not None else "inserted"), ver


def _run_id(con: sqlite3.Connection, screen_run_id: Any) -> Optional[str]:
    """驗證並正規化 screen_run_id（正整數；None＝未提供）。目標庫有 job_runs 時須存在該 run_id。"""
    if screen_run_id is None:
        return None
    if isinstance(screen_run_id, bool) or not str(screen_run_id).strip().isdigit() or int(str(screen_run_id)) <= 0:
        raise ValueError(f"screen_run_id 必須是正整數（job_runs.run_id），收到 {screen_run_id!r}")
    rid = int(str(screen_run_id))
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_runs'").fetchone():
        if con.execute("SELECT 1 FROM job_runs WHERE run_id = ?", (rid,)).fetchone() is None:
            raise ValueError(f"job_runs 沒有 run_id={rid}")
    return str(rid)


def _backfill_run_id(con, table, key: Mapping[str, Any], rid: Optional[str], cols: set[str], what: str) -> None:
    """內容未變（skipped）但現行列 screen_run_id 為空時補上；已有值不覆寫（保留第一次產生該版的 run）。"""
    if what == "skipped" and rid is not None and "screen_run_id" in cols:
        kw = " AND ".join(f"{k} = ?" for k in key)
        con.execute(f"UPDATE {table} SET screen_run_id = ? WHERE {kw} AND is_current = 1 AND screen_run_id IS NULL",
                    [rid, *key.values()])


def _count(report: WriteReport, what: str) -> None:
    setattr(report, what, getattr(report, what) + 1)


# ------------------------------------------------------------------
# screen_results
# ------------------------------------------------------------------
_COND = {"C1_TREND": "c1", "C2_BREAKOUT": "c2", "C3_VOLUME": "c3", "C4_TURNOVER": "c4"}
_SR_REQUIRED = ("as_of", "symbol", "params_hash", "params_json", "status", "c1_enabled", "c2_enabled", "c3_enabled",
                "c4_enabled", "conditions_json", "analysis_version", "generated_at", "checksum", "version",
                "is_current")


def screen_rows(result: Any, input_max_fetched_at: Optional[str]) -> list[dict]:
    """ScreenResult → screen_results 列（欄位對應見 tables_draft.md 3.1）。不含 generated_at／version。"""
    p = result.params
    if p.get("coverage_policy") != "strict":
        raise ValueError("正式結果只接受 coverage_policy=strict（gap_check 只供研究比較，不可寫入）")
    as_of = _clean(pd.Timestamp(result.as_of))
    pj = json.dumps(p, ensure_ascii=False, sort_keys=True)
    conds = {sym: g for sym, g in result.conditions.groupby("symbol", sort=False)}
    rows = []
    for s in result.summary.to_dict("records"):
        sym = str(s["symbol"])
        g = conds.get(sym)
        crow = {}
        clist = []
        if g is not None:
            for c in g.to_dict("records"):
                pre = _COND[c["condition_id"]]
                crow[f"{pre}_enabled"] = int(bool(c["enabled"]))
                passed = _clean(c["passed"])
                crow[f"{pre}_passed"] = None if (passed is None or not c["enabled"]) else int(bool(passed))
                clist.append({k: _clean(v) for k, v in c.items()})
        for pre in _COND.values():
            crow.setdefault(f"{pre}_enabled", 0)
            crow.setdefault(f"{pre}_passed", None)
        pav = _clean(s.get("prior_avg_volume_shares"))
        pv = _clean(s.get("price_version"))
        r = dict(
            as_of=as_of, symbol=sym, params_hash=result.params_hash, params_json=pj,
            coverage_policy=p["coverage_policy"], market=_clean(s.get("market")), name=_clean(s.get("name")),
            status=s["status"], exclusion_code=_clean(s.get("exclusion_code")),
            exclusion_reason=_clean(s.get("exclusion_reason")), coverage_note=_clean(s.get("coverage_note")),
            c1_actual_close_twd=_clean(s.get("close_twd")), c1_threshold_ma_short_twd=_clean(s.get("ma_short")),
            c1_threshold_ma_long_twd=_clean(s.get("ma_long")),
            c2_actual_close_twd=_clean(s.get("close_twd")), c2_threshold_prior_high_twd=_clean(s.get("prior_high_twd")),
            c3_actual_volume_shares=_clean(s.get("volume_shares")), c3_prior_avg_volume_shares=pav,
            c3_threshold_volume_shares=None if pav is None else pav * float(p["volume_multiplier"]),
            c3_volume_ratio=_clean(s.get("volume_ratio")),
            c4_actual_amount_twd=_clean(s.get("amount_twd")), c4_threshold_amount_twd=float(p["min_turnover"]),
            conditions_json=json.dumps(clist, ensure_ascii=False, sort_keys=True, default=str),
            **crow,
        )
        for c in ("close_twd", "change_pct", "change_base_twd", "source_change_twd", "change_check",
                  "change_check_diff_twd", "volume_shares", "amount_twd", "n_adj_events", "adj_events",
                  "price_source", "price_fetched_at"):
            r[c] = _clean(s.get(c))
        for c in ("n_valid_days", "window_gap_days", "window_halt_days", "window_missing_days",
                  "window_no_trade_days", "n_adj_events"):
            v = _clean(s.get(c))
            r[c] = None if v is None else int(v)
        r["window_start"] = _clean(s.get("window_start"))
        r["price_version"] = None if pv is None else int(pv)
        r["analysis_version"] = result.analysis_version
        r["computed_at"] = result.computed_at or None
        r["input_max_fetched_at"] = input_max_fetched_at
        r["checksum"] = row_checksum(r)
        rows.append(r)
    return rows


def write_screen_results(con: sqlite3.Connection, result: Any, input_max_fetched_at: Optional[str], *,
                         screen_run_id: Any = None, retire_missing: bool = False,
                         generated_at: Optional[str] = None) -> WriteReport:
    """同一 (as_of, params_hash) 整批在一個交易內寫入。retire_missing=True 時，本批沒有的舊現行列設為非現行
    （例如全市場重跑時下市股票）；預設 False，避免以 30 檔子集重跑時誤改其他代號。"""
    if result.analysis_version != ANALYSIS_VERSION:
        raise ValueError(f"結果版本 {result.analysis_version} 與目前程式 {ANALYSIS_VERSION} 不符")
    rows = screen_rows(result, input_max_fetched_at)
    rep = WriteReport("screen_results")
    gen = generated_at or _now()
    with _tx(con):
        cols = _columns(con, "screen_results")
        rid = _run_id(con, screen_run_id)
        for r in rows:
            if rid is not None and "screen_run_id" in cols:
                r = dict(r, screen_run_id=rid)
            key = {"as_of": r["as_of"], "symbol": r["symbol"], "params_hash": r["params_hash"]}
            what, ver = _upsert_versioned(con, "screen_results", key, dict(r, generated_at=gen), cols, rep,
                                          _SR_REQUIRED)
            _backfill_run_id(con, "screen_results", key, rid, cols, what)
            _count(rep, what)
            rep.versions[(r["as_of"], r["symbol"], r["params_hash"])] = ver
        if retire_missing and rows:
            keep = [r["symbol"] for r in rows]
            cur = con.execute(
                f"UPDATE screen_results SET is_current = 0 WHERE as_of = ? AND params_hash = ? AND is_current = 1 "
                f"AND symbol NOT IN ({','.join('?' * len(keep))})", [rows[0]["as_of"], rows[0]["params_hash"], *keep])
            rep.retired = cur.rowcount
    return rep


def screen_batch_checksum(con: sqlite3.Connection, as_of: str, params_hash: str) -> tuple[str, int]:
    """(as_of, params_hash) 現行列 checksum 依代號排序後再雜湊；回傳 (雜湊, 列數)。供 daily_summaries.screen_ref_json。"""
    rows = con.execute("SELECT symbol, checksum FROM screen_results WHERE as_of = ? AND params_hash = ? "
                       "AND is_current = 1 ORDER BY symbol", (as_of, params_hash)).fetchall()
    h = hashlib.sha256("\n".join(f"{s}:{c}" for s, c in rows).encode("utf-8")).hexdigest()
    return h, len(rows)


# ------------------------------------------------------------------
# daily_summaries
# ------------------------------------------------------------------
_DS_REQUIRED = ("summary_date", "summary_kind", "params_hash", "screen_ref_json", "text", "facts_json", "n_pass",
                "n_fail", "n_excluded", "generation_method", "traceability_check", "review_status",
                "analysis_version", "generated_at", "checksum", "version", "is_current")


def write_daily_summary(con: sqlite3.Connection, narrative: Any, result: Any, *, input_max_fetched_at: Optional[str],
                        news_event_refs: Sequence[Mapping[str, Any]] = (), ai_prompt_version: Optional[str] = None,
                        screen_run_id: Any = None, generated_at: Optional[str] = None) -> WriteReport:
    """每日盤後摘要。必須先寫入同批 screen_results（screen_ref_json 引用其現行列 checksum）。
    news_event_refs：[{event_id, version, category}]，依 news_events 現行版。
    screen_run_id：產生所引用 screen_results 批次的 job_runs.run_id（pipeline.run 傳入）。"""
    import narrative as N
    as_of = _clean(pd.Timestamp(result.as_of))
    rep = WriteReport("daily_summaries")
    gen = generated_at or _now()
    bad = N.untraceable_numbers(narrative.text, narrative.facts) if getattr(narrative, "facts", None) else []
    method = narrative.generation_method
    trace = "fail" if bad else "pass"
    counts = result.summary["status"].value_counts()
    cats: dict[str, int] = {}
    for e in news_event_refs:
        cats[e["category"]] = cats.get(e["category"], 0) + 1
    with _tx(con):
        cols = _columns(con, "daily_summaries")
        rid = _run_id(con, screen_run_id)
        sc, n_rows = screen_batch_checksum(con, as_of, result.params_hash)
        if n_rows == 0:
            raise ValueError(f"screen_results 沒有 {as_of}／{result.params_hash[:8]}… 的現行列：請先 write_screen_results")
        ref = json.dumps({"as_of": as_of, "params_hash": result.params_hash, "screen_checksum": sc, "n_rows": n_rows},
                         ensure_ascii=False, sort_keys=True)
        ev = json.dumps([{"event_id": e["event_id"], "version": int(e["version"])} for e in news_event_refs],
                        ensure_ascii=False)
        r = dict(summary_date=as_of, summary_kind="post_market", params_hash=result.params_hash, screen_ref_json=ref,
                 text=narrative.text, facts_json=narrative.facts_json, news_event_ids_json=ev,
                 news_categories_json=json.dumps(cats, ensure_ascii=False, sort_keys=True),
                 n_pass=int(counts.get("PASS", 0)), n_fail=int(counts.get("FAIL", 0)),
                 n_excluded=int(counts.get("EXCLUDED", 0)), generation_method=method,
                 ai_prompt_version=ai_prompt_version, traceability_check=trace,
                 untraceable_json=json.dumps(bad, ensure_ascii=False),
                 review_status="not_required" if (method == "rule" and trace == "pass") else "unreviewed",
                 analysis_version=ANALYSIS_VERSION, data_date=as_of,
                 provider=PROVIDER_RULE if method == "rule" else None, screen_run_id=rid)
        # DDL 註：checksum＝text + facts_json + screen_ref_json + news_event_ids_json 的 SHA-256
        r["checksum"] = hashlib.sha256("\x1f".join([r["text"], r["facts_json"], ref, ev]).encode("utf-8")).hexdigest()
        r.update(computed_at=_now(), input_max_fetched_at=input_max_fetched_at, generated_at=gen)
        key = {"summary_date": as_of, "summary_kind": "post_market", "params_hash": result.params_hash}
        what, ver = _upsert_versioned(con, "daily_summaries", key, r, cols, rep, _DS_REQUIRED)
        _backfill_run_id(con, "daily_summaries", key, rid, cols, what)
        _count(rep, what)
        rep.versions[tuple(key.values())] = ver
    return rep


# ------------------------------------------------------------------
# news_events ＋ news_event_symbols（同一交易）
# ------------------------------------------------------------------
_NE_REQUIRED = ("event_id", "category", "title", "first_published_at", "last_published_at", "dedup_method",
                "dedup_params_json", "analysis_version", "generated_at", "checksum", "version", "is_current")
_NES_COLS = ("event_id", "version", "symbol", "evidence_type", "evidence", "source_news_id", "source_material_key_json")
_JSON_FIELDS = ("topic_tags_json", "news_ids_json", "material_info_keys_json", "source_urls_json",
                "member_categories_json", "dedup_keys_json", "dedup_params_json", "inference_basis_json",
                "pending_checks_json")


def _norm_symbols(symbols: Iterable[Mapping[str, Any]]) -> list[dict]:
    out = []
    for s in symbols:
        d = {c: _clean(s.get(c)) for c in _NES_COLS if c not in ("event_id", "version")}
        if d["evidence_type"] not in EVIDENCE_TYPES:
            raise ValueError(f"evidence_type 必須是 {EVIDENCE_TYPES}，收到 {d['evidence_type']!r}")
        if d["source_news_id"] is None and d["source_material_key_json"] is None:
            raise ValueError(f"{d['symbol']} 無證據出處（source_news_id／source_material_key_json），不得關聯")
        if isinstance(d["source_material_key_json"], (dict, list)):
            d["source_material_key_json"] = json.dumps(d["source_material_key_json"], ensure_ascii=False, sort_keys=True)
        out.append(d)
    out.sort(key=lambda d: (str(d["symbol"]), str(d["evidence_type"])))
    return out


def write_news_event(con: sqlite3.Connection, event: Mapping[str, Any], symbols: Iterable[Mapping[str, Any]], *,
                     computed_at: Optional[str] = None, input_max_fetched_at: Optional[str] = None,
                     generated_at: Optional[str] = None) -> WriteReport:
    """寫入一個事件與其關聯代號。checksum 涵蓋事件內容與代號清單；不同則升版，新版本的代號整組重寫
    （舊版本的 news_event_symbols 列保留，對應舊版 news_events）。related_symbols_json 已移除，傳入會拋錯。"""
    if "related_symbols_json" in event or "related_symbols" in event:
        raise ValueError("related_symbols_json 已移除（DECISIONS 2026-11-10 03:45）：關聯代號請以 symbols 參數寫入 news_event_symbols")
    syms = _norm_symbols(symbols)
    r = {k: v for k, v in event.items() if k not in NON_CONTENT}
    for k in _JSON_FIELDS:
        if k in r and isinstance(r[k], (dict, list)):
            r[k] = json.dumps(r[k], ensure_ascii=False, sort_keys=True)
    r["relation_evidence_json"] = json.dumps(syms, ensure_ascii=False, sort_keys=True)   # 與 news_event_symbols 同內容快照
    r["analysis_version"] = ANALYSIS_VERSION
    r["checksum"] = row_checksum(r)
    r.update(computed_at=computed_at or _now(), input_max_fetched_at=input_max_fetched_at,
             generated_at=generated_at or _now())
    rep = WriteReport("news_events")
    with _tx(con):
        cols = _columns(con, "news_events")
        scols = _columns(con, "news_event_symbols")
        what, ver = _upsert_versioned(con, "news_events", {"event_id": r["event_id"]}, r, cols, rep, _NE_REQUIRED)
        _count(rep, what)
        rep.versions[r["event_id"]] = ver
        if what != "skipped":
            use = [c for c in _NES_COLS if c in scols]
            con.executemany(f"INSERT INTO news_event_symbols ({','.join(use)}) VALUES ({','.join('?' * len(use))})",
                            [[{"event_id": r["event_id"], "version": ver, **d}[c] for c in use] for d in syms])
    return rep
