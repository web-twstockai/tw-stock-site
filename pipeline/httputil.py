"""友善限流、重試、封鎖偵測。"""
from __future__ import annotations

import contextlib
import logging
import os
import random
import signal
import threading
import time
from typing import Any

import requests

from pipeline import config

log = logging.getLogger("pipeline.http")


# 單次 HTTP 嘗試的「總時間」上限（秒，含重導向與讀 body）。requests 的 timeout 只管每次 socket 讀取，
# 慢速回應或重導迴圈可以無限拖長（2026-11-17 mopsov ajax_t163sb20 307 後閒置 >10 分鐘）。
# 以 SIGALRM 強制中斷（只在主執行緒生效；非主執行緒退回只靠 timeout）。可用環境變數 PIPELINE_HTTP_DEADLINE 調整，0＝關閉。
HTTP_DEADLINE = float(os.environ.get("PIPELINE_HTTP_DEADLINE", "150"))
MAX_REDIRECTS = 5


class DeadlineExceeded(requests.Timeout):
    """單次嘗試超過總時間上限；屬 RequestException，沿用既有重試流程。"""


_warned_no_alarm = False


@contextlib.contextmanager
def _hard_deadline(seconds: float, what: str):
    """單次嘗試的總時間上限（SIGALRM）。會保存並還原外層已存在的 ITIMER_REAL 計時器與 handler。"""
    global _warned_no_alarm
    if seconds <= 0:
        yield
        return
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        if not _warned_no_alarm:   # 沒有 SIGALRM（非主執行緒／Windows）：只剩 requests 的 timeout，沒有總時間上限
            log.warning("總時間上限未啟用（非主執行緒或沒有 SIGALRM）：%s", what)
            _warned_no_alarm = True
        yield
        return
    outer_left, outer_interval = signal.getitimer(signal.ITIMER_REAL)
    eff = min(seconds, outer_left) if outer_left > 0 else seconds

    def _handler(signum, frame):
        raise DeadlineExceeded(f"total wall-clock deadline {eff:.0f}s exceeded: {what}")

    old = signal.signal(signal.SIGALRM, _handler)
    t0 = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, eff)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        if outer_left > 0:   # 還原外層計時器：剩餘時間扣掉已用；若已到期就立刻觸發
            signal.setitimer(signal.ITIMER_REAL, max(outer_left - (time.monotonic() - t0), 0.001), outer_interval)


class FetchError(Exception):
    def __init__(self, msg: str, *, blocked: bool = False, status: int | None = None):
        super().__init__(msg)
        self.blocked = blocked
        self.status = status


class RateClient:
    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0
        self.session = requests.Session()
        self.session.max_redirects = MAX_REDIRECTS
        self.session.headers.update({"User-Agent": config.USER_AGENT})

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        need = self.interval + random.uniform(0, 0.4)
        if elapsed < need:
            time.sleep(need - elapsed)

    def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = config.MAX_RETRIES,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", (20, 60))
        deadline = float(kwargs.pop("deadline", HTTP_DEADLINE))
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            self._wait()
            try:
                with _hard_deadline(deadline, f"{method} {url}"):
                    resp = self.session.request(method, url, **kwargs)
                self._last = time.monotonic()
                # 307/302 without final body: treat as transient (WAF/session), not permanent block
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    raise FetchError(
                        f"redirect/http={resp.status_code} loc={loc} url={url}",
                        blocked=False,
                        status=resp.status_code,
                    )
                text_head = resp.text[:300] if resp.content else ""
                # 200 擋頁／安全頁視為失敗（非成功 JSON／表）；記入呼叫端 source_status
                if (
                    resp.status_code in (429, 403)
                    or "安全性考量" in text_head
                    or "無法呈現" in text_head
                    or "FOR SECURITY REASONS" in text_head.upper()
                ):
                    raise FetchError(
                        f"blocked/http={resp.status_code} url={url}",
                        blocked=True,
                        status=resp.status_code,
                    )
                if resp.status_code >= 500:
                    raise FetchError(f"server {resp.status_code}", status=resp.status_code)
                return resp
            except FetchError as e:
                last_err = e
                sleep = (config.BACKOFF_BASE ** attempt) + random.uniform(0, 1)
                log.warning("attempt %s/%s %s; sleep %.1fs", attempt, retries, e, sleep)
                time.sleep(sleep)
                if e.blocked:
                    self.interval = min(self.interval * 1.5, 30)
            except requests.RequestException as e:
                last_err = e
                sleep = (config.BACKOFF_BASE ** attempt) + random.uniform(0, 1)
                log.warning("attempt %s/%s net %s; sleep %.1fs", attempt, retries, e, sleep)
                time.sleep(sleep)
        raise FetchError(f"failed after retries: {last_err}")


twse = RateClient(config.TWSE_INTERVAL)
tpex = RateClient(config.TPEX_INTERVAL)
mops = RateClient(config.MOPS_INTERVAL)


def warm_mops(page: str = "https://mopsov.twse.com.tw/mops/web/t05st01") -> None:
    """GET 表單頁以取得 jcsession，降低 ajax 307。"""
    try:
        mops.request("GET", page, retries=2)
    except FetchError as e:
        log.warning("warm_mops failed: %s", e)
