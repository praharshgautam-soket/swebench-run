import http.client
import http.server
import importlib.util
import json
import socketserver
import threading
import time
from importlib import resources

import pytest


def load_meter():
    path = resources.files("swebench_run").joinpath("assets/meter_server.py")
    spec = importlib.util.spec_from_file_location("meter_server", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


meter_server = load_meter()


class FakeUpstream:
    """Stands in for the model API. Records requests; behaviour is chosen by path."""

    def __init__(self):
        self.requests = []
        self.flaky_left = 2
        self.in_flight = 0
        self.max_in_flight = 0
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _read(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), None))
                self._json(200, {"data": [{"id": "GLM-5.3"}]})

            def _json(self, status, obj, extra=()):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self._read()
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                if self.path.endswith("/flaky"):
                    with outer.lock:
                        left, outer.flaky_left = outer.flaky_left, max(0, outer.flaky_left - 1)
                    if left > 0:
                        return self._json(429, {"error": "slow down"}, [("Retry-After", "0")])
                if self.path.endswith("/down"):
                    return self._json(500, {"error": "boom"})
                if self.path.endswith("/slow"):
                    with outer.lock:
                        outer.in_flight += 1
                        outer.max_in_flight = max(outer.max_in_flight, outer.in_flight)
                    time.sleep(0.25)
                    with outer.lock:
                        outer.in_flight -= 1
                if self.path.endswith("/cut"):
                    # a chunked event stream that the server drops before the final chunk
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    part = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                    self.wfile.flush()
                    if (body.get("stream_options") or {}).get("include_usage"):
                        self.wfile.write(
                            b'data: {"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":10}}\n\n'
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.close_connection = True
                    return
                return self._json(
                    200,
                    {"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                               "prompt_tokens_details": {"cached_tokens": 40}}},
                )

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    u = FakeUpstream()
    yield u
    u.close()


def start_proxy(upstream, **env):
    base = {
        "UPSTREAM_BASE_URL": f"http://127.0.0.1:{upstream.port}/v1",
        "UPSTREAM_API_KEY": "REALKEY",
        "METER_TOKEN": "tok",
        "BACKOFF_BASE_SEC": "0.01",
        "MAX_RETRIES": "3",
    }
    base.update(env)
    cfg = meter_server.Config(env=base)
    srv = meter_server.serve(cfg, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
    return cfg, srv, srv.server_address[1]


@pytest.fixture
def proxy(upstream):
    cfg, srv, port = start_proxy(upstream)
    yield cfg, port
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, token="tok"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def usage(port, key="k1"):
    return json.loads(call(port, "GET", f"/admin/tok/usage/{key}", token=None)[1])


CHAT = {"model": "GLM-5.3", "messages": [{"role": "user", "content": "x"}]}


def test_counts_tokens_and_swaps_in_the_real_key(upstream, proxy):
    _, port = proxy
    status, data = call(port, "POST", "/i/k1/chat/completions", CHAT)
    assert status == 200 and json.loads(data)["choices"]
    u = usage(port)
    assert (u["calls"], u["ok_calls"], u["prompt_tokens"], u["completion_tokens"], u["cached_tokens"]) == (1, 1, 100, 20, 40)
    method, path, headers, _ = upstream.requests[-1]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer REALKEY"  # not the meter token


def test_streaming_gets_usage_option_injected_and_counted(upstream, proxy):
    _, port = proxy
    status, data = call(port, "POST", "/i/k1/chat/completions", {**CHAT, "stream": True})
    assert status == 200 and b"[DONE]" in data and b'"content":"hi"' in data
    assert upstream.requests[-1][3]["stream_options"] == {"include_usage": True}
    u = usage(port)
    assert (u["prompt_tokens"], u["completion_tokens"]) == (50, 10)


def test_existing_stream_options_are_kept(upstream, proxy):
    _, port = proxy
    call(port, "POST", "/i/k1/chat/completions", {**CHAT, "stream": True, "stream_options": {"x": 1}})
    assert upstream.requests[-1][3]["stream_options"] == {"x": 1, "include_usage": True}


def test_wrong_token_rejected_and_never_forwarded(upstream, proxy):
    _, port = proxy
    assert call(port, "POST", "/i/k1/chat/completions", CHAT, token="nope")[0] == 401
    assert call(port, "POST", "/i/k1/chat/completions", CHAT, token=None)[0] == 401
    assert upstream.requests == []


def test_models_listing_is_proxied_but_not_counted_as_a_call(upstream, proxy):
    _, port = proxy
    status, data = call(port, "GET", "/i/k1/models")
    assert status == 200 and b"GLM-5.3" in data
    assert usage(port)["calls"] == 0


def test_retries_on_429_then_succeeds(upstream, proxy):
    _, port = proxy
    status, _ = call(port, "POST", "/i/k1/flaky", CHAT)
    assert status == 200
    u = usage(port)
    assert u["retries"] == 2 and u["ok_calls"] == 1 and u["errors"] == 0
    assert len([r for r in upstream.requests if r[1].endswith("/flaky")]) == 3


def test_persistent_500_is_returned_after_retries_and_counted_as_error(upstream, proxy):
    _, port = proxy
    status, _ = call(port, "POST", "/i/k1/down", CHAT)
    assert status == 500
    u = usage(port)
    assert u["errors"] == 1 and u["retries"] == 3 and u["ok_calls"] == 0


def test_unreachable_upstream_gives_502(upstream):
    cfg, srv, port = start_proxy(upstream, UPSTREAM_BASE_URL="http://127.0.0.1:9/v1", MAX_RETRIES="1")
    try:
        assert call(port, "POST", "/i/k1/chat/completions", CHAT)[0] == 502
        assert usage(port)["errors"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_admin_needs_the_token_and_reset_clears_one_instance(upstream, proxy):
    _, port = proxy
    call(port, "POST", "/i/k1/chat/completions", CHAT)
    call(port, "POST", "/i/k2/chat/completions", CHAT)
    assert call(port, "GET", "/admin/wrong/usage/k1", token=None)[0] == 404
    assert call(port, "POST", "/admin/tok/reset/k1", token=None)[0] == 200
    assert usage(port, "k1")["calls"] == 0 and usage(port, "k2")["calls"] == 1
    snap = json.loads(call(port, "GET", "/admin/tok/all", token=None)[1])
    assert set(snap["instances"]) == {"k2"} and "REALKEY" not in json.dumps(snap)


def test_budget_stops_further_calls_with_402(upstream):
    # 100 prompt + 20 completion tokens at $1000 / $1000 per Mtok = $0.12 per call.
    cfg, srv, port = start_proxy(upstream, PRICE_IN="1000", PRICE_OUT="1000", BUDGET_USD="0.1")
    try:
        assert call(port, "POST", "/i/k1/chat/completions", CHAT)[0] == 200
        status, data = call(port, "POST", "/i/k1/chat/completions", CHAT)
        assert status == 402 and b"budget" in data
        assert len([r for r in upstream.requests if r[0] == "POST"]) == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_concurrency_cap_limits_simultaneous_upstream_calls(upstream):
    cfg, srv, port = start_proxy(upstream, MAX_CONCURRENCY="1")
    try:
        threads = [threading.Thread(target=call, args=(port, "POST", "/i/k1/slow", CHAT)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert upstream.max_in_flight == 1
        assert usage(port)["ok_calls"] == 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_routes_404(upstream, proxy):
    _, port = proxy
    assert call(port, "GET", "/nothing")[0] == 404
    assert call(port, "GET", "/healthz")[0] == 200


def test_a_stream_the_upstream_cuts_short_is_counted_as_truncated_not_ok(upstream, proxy):
    cfg, port = proxy
    with pytest.raises(http.client.IncompleteRead):
        call(port, "POST", "/i/k1/v1/cut", {**CHAT, "stream": True})
    u = usage(port)
    assert u["calls"] == 1 and u["ok_calls"] == 0 and u["errors"] == 1 and u["truncated"] == 1


def test_a_complete_stream_is_not_truncated(upstream, proxy):
    cfg, port = proxy
    status, _ = call(port, "POST", "/i/k1/v1/chat/completions", {**CHAT, "stream": True})
    assert status == 200
    assert usage(port)["truncated"] == 0 and usage(port)["ok_calls"] == 1
