"""限流處理：302→/errors、429 視為限流，不追轉址、不重試、整個主機熔斷；以本機假伺服器驗證。"""
import http.server
import socketserver
import threading
import time

import pytest

from pipeline import httputil
from pipeline.db import connect, init_db, touch_source
from pipeline.httputil import FetchError, RateClient, RateLimited

HITS: dict[str, int] = {}


class _H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        HITS[p] = HITS.get(p, 0) + 1
        if p == "/to-errors":
            self.send_response(302); self.send_header("Location", "/errors"); self.send_header("Content-Length", "0"); self.end_headers()
        elif p == "/errors":
            self.send_response(302); self.send_header("Location", "/errors"); self.send_header("Content-Length", "0"); self.end_headers()
        elif p == "/429":
            self.send_response(429); self.send_header("Content-Length", "0"); self.end_headers()
        elif p == "/redir-ok":
            self.send_response(302); self.send_header("Location", "/ok"); self.send_header("Content-Length", "0"); self.end_headers()
        elif p == "/loop":
            self.send_response(302); self.send_header("Location", "/loop"); self.send_header("Content-Length", "0"); self.end_headers()
        else:
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")


class _S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture()
def server():
    HITS.clear()
    s = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def _client():
    return RateClient(0.0)


def test_errors_redirect_is_rate_limit_not_followed_not_retried(server):
    c = _client()
    with pytest.raises(RateLimited) as ei:
        c.request("GET", server + "/to-errors", retries=3)
    assert HITS.get("/to-errors") == 1, "不得重試"
    assert HITS.get("/errors", 0) == 0, "不得追 /errors 轉址"
    assert str(ei.value).startswith("rate_limited") and ei.value.blocked and isinstance(ei.value, FetchError)
    assert 1.9 <= ei.value.retry_after_s <= 2.1 and not ei.value.circuit_open


def test_host_circuit_breaker_blocks_all_clients_without_network(server):
    a, b = _client(), _client()
    with pytest.raises(RateLimited):
        a.request("GET", server + "/to-errors")
    before = dict(HITS)
    for client in (a, b):   # 同主機（含另一個 RateClient、不同路徑）都被擋
        with pytest.raises(RateLimited) as ei:
            client.request("GET", server + "/ok")
        assert ei.value.circuit_open and ei.value.retry_after_s > 0
    assert HITS == before, "熔斷期間不得送出任何請求"


def test_resumes_after_backoff_and_backoff_doubles(server, monkeypatch):
    c = _client()
    with pytest.raises(RateLimited):
        c.request("GET", server + "/to-errors")          # 第 1 次：2 秒
    time.sleep(2.15)
    assert c.request("GET", server + "/ok").status_code == 200   # 期滿放行並解除
    assert httputil.breaker_left("127.0.0.1") == 0
    with pytest.raises(RateLimited):
        c.request("GET", server + "/to-errors")          # 解除後再限流：重新從 2 秒起算
    time.sleep(2.15)
    with pytest.raises(RateLimited) as ei:
        c.request("GET", server + "/to-errors")          # 期滿後連續第 2 次限流（未成功過）：加倍 → 4 秒
    assert 3.9 <= ei.value.retry_after_s <= 4.1


def test_429_is_rate_limit(server):
    with pytest.raises(RateLimited) as ei:
        _client().request("GET", server + "/429", retries=3)
    assert HITS.get("/429") == 1 and ei.value.status == 429


def test_normal_redirect_still_followed_and_loop_capped(server):
    c = _client()
    assert c.request("GET", server + "/redir-ok").status_code == 200
    with pytest.raises(FetchError) as ei:
        c.request("GET", server + "/loop", retries=1)
    assert not isinstance(ei.value, RateLimited)
    assert HITS["/loop"] == httputil.MAX_REDIRECTS + 1


def test_is_errors_location():
    f = httputil._is_errors_location
    assert f("/errors") and f("https://www.tpex.org.tw/errors") and f("https://www.tpex.org.tw/errors/")
    assert not f("/ok") and not f(None) and not f("https://www.tpex.org.tw/zh-tw/errors-help.html")


def test_source_status_marks_rate_limited_and_clears_on_success(server):
    conn = connect()
    init_db(conn)
    try:
        c = _client()
        try:
            c.request("GET", server + "/to-errors")
        except RateLimited as e:
            touch_source(conn, "src_x", url="u", ok=False, http_status=e.status, error=str(e))
        row = conn.execute("SELECT ok, notes, last_error FROM source_status WHERE source_id='src_x'").fetchone()
        assert row[0] == 0 and row[1].startswith("rate_limited") and "限流" in row[1] and row[2].startswith("rate_limited")
        touch_source(conn, "src_x", url="u", ok=True, http_status=200)
        assert conn.execute("SELECT notes FROM source_status WHERE source_id='src_x'").fetchone()[0] is None
    finally:
        conn.close()
