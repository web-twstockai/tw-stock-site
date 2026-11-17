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
from urllib.parse import urljoin, urlparse

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


class RateLimited(FetchError):
    """對方限流（302 轉到 /errors、HTTP 429）或本機主機熔斷中。不重試、不追轉址；呼叫端應停止該來源並記為「限流」。
    訊息以 `rate_limited` 開頭，`db.touch_source` 會據此把 source_status.notes 寫成「限流」。"""

    def __init__(self, msg: str, *, host: str = "", retry_after_s: float = 0.0, circuit_open: bool = False, status: int | None = None):
        super().__init__(msg, blocked=True, status=status)
        self.host = host
        self.retry_after_s = retry_after_s
        self.circuit_open = circuit_open


# 限流後的主機層級熔斷：同一主機在退避期間不再送出任何請求（所有 RateClient 共用）。
# 退避秒數預設 900（15 分鐘），連續再限流時加倍，上限 RATE_LIMIT_BACKOFF_MAX（預設 3600）。環境變數可調。
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_breakers: dict[str, dict[str, float]] = {}


def reset_breakers() -> None:
    _breakers.clear()


def breaker_left(host: str) -> float:
    b = _breakers.get(host)
    return max(0.0, b["until"] - time.monotonic()) if b else 0.0


def _trip_breaker(host: str) -> float:
    base = _env_float("PIPELINE_RATE_LIMIT_BACKOFF", 900.0)
    cap = max(base, _env_float("PIPELINE_RATE_LIMIT_BACKOFF_MAX", 3600.0))
    prev = _breakers.get(host)
    n = (int(prev["count"]) + 1) if prev else 1
    wait = min(base * (2 ** (n - 1)), cap)
    _breakers[host] = {"until": time.monotonic() + wait, "count": n}
    return wait


def _is_errors_location(loc: str | None) -> bool:
    if not loc:
        return False
    path = urlparse(loc).path.rstrip("/").lower()
    return path.endswith("/errors") or path == "/errors"


# 請求計數（每次實際送出的 HTTP 嘗試，含重試；依主機）。作業摘要用 runstats 取差值。
STATS: dict[str, int] = {}


_SINGLE_SHOT = 0


@contextlib.contextmanager
def single_shot():
    """區間／大查詢端點用：區塊內每個請求只送 1 次（不重試、失敗後不 sleep）。
    2026-11-17 TWSE 區間端點被 WAF 擋頁，預設 3 次重試會把一次擋頁放大成 3 個請求；改用本模式後一次擋頁＝1 個請求。"""
    global _SINGLE_SHOT
    _SINGLE_SHOT += 1
    try:
        yield
    finally:
        _SINGLE_SHOT -= 1


class _NoErrorsRedirectSession(requests.Session):
    """遇到「轉到 /errors」的 3xx 不追轉址（直接回傳該 3xx 回應，由 RateClient 判為限流）。"""

    def get_redirect_target(self, resp):  # type: ignore[override]
        target = super().get_redirect_target(resp)
        if target and _is_errors_location(urljoin(resp.url, target)):
            return None
        return target


class RateClient:
    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0
        self.session = _NoErrorsRedirectSession()
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
        if _SINGLE_SHOT:
            retries = 1
        last_err: Exception | None = None
        host = urlparse(url).hostname or ""
        for attempt in range(1, retries + 1):
            left = breaker_left(host)
            if left > 0:   # 主機熔斷中：不送請求
                raise RateLimited(
                    f"rate_limited(限流): 主機 {host} 熔斷中，剩餘 {left:.0f} 秒；未送出請求 url={url}",
                    host=host, retry_after_s=left, circuit_open=True,
                )
            self._wait()
            STATS[host] = STATS.get(host, 0) + 1
            try:
                with _hard_deadline(deadline, f"{method} {url}"):
                    resp = self.session.request(method, url, **kwargs)
                self._last = time.monotonic()
                # 限流：3xx 轉到 /errors（櫃買中心／Cloudflare）或 429 → 不重試、熔斷整個主機
                loc = resp.headers.get("Location")
                if (resp.status_code in (301, 302, 303, 307, 308) and _is_errors_location(urljoin(url, loc) if loc else None)) or resp.status_code == 429:
                    wait = _trip_breaker(host)
                    log.error("rate limited http=%s loc=%s url=%s → 主機 %s 熔斷 %.0f 秒", resp.status_code, loc, url, host, wait)
                    raise RateLimited(
                        f"rate_limited(限流): http={resp.status_code} loc={loc} url={url}；主機 {host} 暫停 {wait:.0f} 秒",
                        host=host, retry_after_s=wait, status=resp.status_code,
                    )
                # 307/302 without final body: treat as transient (WAF/session), not permanent block
                if resp.status_code in (301, 302, 303, 307, 308):
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
                if host in _breakers and breaker_left(host) == 0:   # 熔斷期滿後首個成功請求：解除
                    _breakers.pop(host, None)
                return resp
            except RateLimited:
                raise
            except FetchError as e:
                last_err = e
                sleep = (config.BACKOFF_BASE ** attempt) + random.uniform(0, 1)
                log.warning("attempt %s/%s %s; sleep %.1fs", attempt, retries, e, sleep)
                if attempt < retries:
                    time.sleep(sleep)
                if e.blocked:
                    self.interval = min(self.interval * 1.5, 30)
            except requests.RequestException as e:
                last_err = e
                sleep = (config.BACKOFF_BASE ** attempt) + random.uniform(0, 1)
                log.warning("attempt %s/%s net %s; sleep %.1fs", attempt, retries, e, sleep)
                if attempt < retries:
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
