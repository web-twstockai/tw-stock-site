"""PIPELINE_SKIP_ISIN 的保護（M1／M2）與 _hard_deadline 還原外層計時器（M3）。執行：.venv/bin/python -m pytest pipeline/tests -q"""
import signal
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline import db as pdb, httputil  # noqa: E402
from pipeline.fetchers import securities  # noqa: E402


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb.init_db(conn)
    return conn


def _add(conn, n_twse, n_tpex, is_common=1, is_current=1):
    i = conn.execute("select count(*) from securities").fetchone()[0]
    for market, n in (("TWSE", n_twse), ("TPEx", n_tpex)):
        for _ in range(n):
            i += 1
            conn.execute(
                "INSERT INTO securities(symbol,market,name,cfi_code,is_common,data_date,source,fetched_at,is_current) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"{i:04d}", market, f"n{i}", "ESVUFR", is_common, "2026-11-16", "t", "2026-11-17T00:00:00+08:00", is_current),
            )
    conn.commit()


def test_m1_empty_securities_cannot_skip():
    conn = _mem()
    ok, counts = securities.should_skip_isin(conn)
    assert ok is False and counts == {"TWSE": 0, "TPEx": 0}


def test_m1_below_threshold_cannot_skip():
    conn = _mem()
    _add(conn, 899, 700)
    assert securities.should_skip_isin(conn)[0] is False
    conn2 = _mem()
    _add(conn2, 900, 699)
    assert securities.should_skip_isin(conn2)[0] is False


def test_m1_enough_can_skip_and_noncurrent_or_noncommon_not_counted():
    conn = _mem()
    _add(conn, 900, 700)
    assert securities.should_skip_isin(conn)[0] is True
    conn2 = _mem()
    _add(conn2, 500, 400)
    _add(conn2, 500, 400, is_common=0)
    _add(conn2, 500, 400, is_current=0)
    assert securities.should_skip_isin(conn2)[0] is False


def test_m1_run_job_does_not_succeed_silently_with_zero_stocks(monkeypatch, tmp_path):
    """整合：空 DB＋PIPELINE_SKIP_ISIN=1 → run_job 必須呼叫 isin 抓取（這裡用假的抓取記錄呼叫），不可略過。"""
    from pipeline import run as prun, config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    monkeypatch.setenv("PIPELINE_SKIP_ISIN", "1")
    called = []

    def fake_fetch(conn):
        called.append(1)
        raise RuntimeError("stop here (test)")
    monkeypatch.setattr(prun.securities, "fetch_and_store", fake_fetch)
    for m in ("fetch_and_store",):
        monkeypatch.setattr(prun.calendar, m, lambda conn: None)
    monkeypatch.setattr(prun, "assert_no_other_pipeline", lambda: None)
    monkeypatch.setattr(prun, "setup_logging", lambda: None)
    # 讓後面的步驟不要真的連網：securities 失敗後各步驟會各自嘗試，這裡只關心「有沒有呼叫 isin 抓取」
    for mod in ("prices", "corporate_actions", "halts", "indices", "revenue", "financials", "material"):
        monkeypatch.setattr(prun, mod, type("X", (), {"__getattr__": lambda self, k: (lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no net")))})())
    try:
        prun.run_job("daily", "sample30")
    except Exception:
        pass
    assert called, "空清單下仍略過了 isin（M1 失敗）"


def test_m2_note_skipped_keeps_ok_and_last_success():
    conn = _mem()
    for sid in ("isin_twse", "isin_tpex"):
        conn.execute("INSERT INTO source_status(source_id,last_success_at,last_attempt_at,ok,updated_at,last_error) VALUES(?,?,?,?,?,?)",
                     (sid, "2026-11-17T10:03:44+08:00", "2026-11-17T10:03:44+08:00", 1, "u", None))
    conn.commit()
    securities.note_isin_skipped(conn, "雲端略過 isin")
    for r in conn.execute("select * from source_status"):
        assert r["ok"] == 1 and r["last_success_at"] == "2026-11-17T10:03:44+08:00" and r["updated_at"] == "u"
        assert r["last_attempt_at"] != "2026-11-17T10:03:44+08:00" and r["notes"].startswith("skipped")


def test_m3_hard_deadline_restores_outer_timer_and_handler():
    fired = []
    old = signal.signal(signal.SIGALRM, lambda s, f: fired.append(1))
    try:
        signal.setitimer(signal.ITIMER_REAL, 5.0)
        with httputil._hard_deadline(2, "x"):
            time.sleep(0.2)
        left, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 4.0 < left <= 5.0, left
        assert signal.getsignal(signal.SIGALRM) is not None
        signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_m3_deadline_fires_and_no_timer_left():
    with pytest.raises(httputil.DeadlineExceeded):
        with httputil._hard_deadline(0.3, "x"):
            time.sleep(2)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
