#!/usr/bin/env python3
"""Metering proxy for an OpenAI-compatible API. Standard library only, so it runs in a
stock `python` image with no pip step and no registry.

Agent pods call  http://<svc>:<port>/i/<instance-key>/<path>  with the *meter token* as
their API key. The proxy checks the token, swaps in the real upstream key, forwards the
request to UPSTREAM_BASE_URL/<path>, streams the answer back, and counts what the API
reports: calls, tokens in/out, cached tokens, errors, retries.

It also gives the run three things the agents don't:
- a cap on concurrent upstream calls (MAX_CONCURRENCY),
- exponential backoff on 429 / 5xx / connection errors (only before any byte has gone back),
- a hard budget stop (BUDGET_USD with PRICE_IN / PRICE_OUT per million tokens): once spent,
  further calls get HTTP 402.

Admin endpoints live under /admin/<METER_TOKEN>/ :
    GET  usage/<key>   counters for one instance
    GET  all           counters for every instance, plus total spend
    POST reset/<key>   zero one instance's counters
Request and response bodies are never stored or logged, and the real key is never returned.
"""
from __future__ import annotations

import hmac
import http.client
import http.server
import json
import os
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
}
MAX_JSON_BYTES = 64 * 1024 * 1024


def new_stats() -> dict:
    return {
        "calls": 0, "ok_calls": 0, "errors": 0, "retries": 0, "truncated": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
        "wall_sec": 0.0, "first_ts": None, "last_ts": None,
    }


class Meter:
    """Thread-safe counters per instance key, plus total spend for the budget stop."""

    def __init__(self, price_in: float = 0.0, price_out: float = 0.0, budget: float | None = None):
        self.lock = threading.Lock()
        self.stats: dict[str, dict] = {}
        self.price_in, self.price_out, self.budget = price_in, price_out, budget
        self.spent = 0.0

    def record(self, key: str, *, ok: bool, prompt: int, completion: int, cached: int,
               wall: float, retries: int, truncated: bool = False) -> None:
        now = time.time()
        with self.lock:
            s = self.stats.setdefault(key, new_stats())
            s["calls"] += 1
            s["ok_calls"] += 1 if ok else 0
            s["errors"] += 0 if ok else 1
            s["truncated"] += 1 if truncated else 0
            s["retries"] += retries
            s["prompt_tokens"] += prompt
            s["completion_tokens"] += completion
            s["cached_tokens"] += cached
            s["wall_sec"] = round(s["wall_sec"] + wall, 3)
            s["first_ts"] = s["first_ts"] or now
            s["last_ts"] = now
            self.spent += (prompt * self.price_in + completion * self.price_out) / 1_000_000

    def get(self, key: str) -> dict:
        with self.lock:
            return dict(self.stats.get(key) or new_stats())

    def reset(self, key: str) -> None:
        with self.lock:
            # Spend already happened; the budget keeps counting it.
            self.stats.pop(key, None)

    def over_budget(self) -> bool:
        with self.lock:
            return self.budget is not None and self.spent >= self.budget

    def snapshot(self) -> dict:
        with self.lock:
            return {"spent_usd": round(self.spent, 6), "instances": {k: dict(v) for k, v in self.stats.items()}}


def extract_usage(obj: object) -> tuple[int, int, int] | None:
    """(prompt, completion, cached) from a response/chunk object, if it carries usage."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    prompt = u.get("prompt_tokens", u.get("input_tokens"))
    completion = u.get("completion_tokens", u.get("output_tokens"))
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    return prompt, completion, cached if isinstance(cached, int) else 0


class Config:
    def __init__(self, env=os.environ):
        self.upstream = env["UPSTREAM_BASE_URL"].rstrip("/")
        self.upstream_key = env.get("UPSTREAM_API_KEY", "")
        self.token = env["METER_TOKEN"]
        self.max_concurrency = int(env.get("MAX_CONCURRENCY", "8"))
        self.max_retries = int(env.get("MAX_RETRIES", "5"))
        self.backoff_base = float(env.get("BACKOFF_BASE_SEC", "2"))
        self.backoff_cap = float(env.get("BACKOFF_CAP_SEC", "60"))
        self.timeout = float(env.get("UPSTREAM_TIMEOUT_SEC", "900"))
        price_in = float(env.get("PRICE_IN", "0") or 0)
        price_out = float(env.get("PRICE_OUT", "0") or 0)
        budget = env.get("BUDGET_USD")
        self.meter = Meter(price_in, price_out, float(budget) if budget else None)
        self.sem = threading.BoundedSemaphore(self.max_concurrency)


def make_handler(cfg: Config):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "sbr-meter"

        def log_message(self, fmt, *args):  # no request details in logs
            pass

        # -- helpers --------------------------------------------------------
        def _send_json(self, status: int, obj: object) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            sent = self.headers.get("Authorization", "")
            return hmac.compare_digest(sent.encode(), f"Bearer {cfg.token}".encode())

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        # -- routing --------------------------------------------------------
        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                return self._send_json(200, {"ok": True})
            if path.startswith("/admin/"):
                return self._admin(path)
            if path.startswith("/i/"):
                parts = path.split("/", 3)  # ['', 'i', key, rest]
                if len(parts) == 4 and parts[2]:
                    return self._proxy(parts[2], parts[3])
            self._send_json(404, {"error": "not found"})

        def _admin(self, path: str):
            parts = path.split("/", 4)  # ['', 'admin', token, verb, arg]
            if len(parts) < 4 or not hmac.compare_digest(parts[2].encode(), cfg.token.encode()):
                return self._send_json(404, {"error": "not found"})
            verb, arg = parts[3], (parts[4] if len(parts) > 4 else "")
            if verb == "usage" and arg and self.command == "GET":
                return self._send_json(200, cfg.meter.get(arg))
            if verb == "all" and self.command == "GET":
                return self._send_json(200, cfg.meter.snapshot())
            if verb == "reset" and arg and self.command == "POST":
                cfg.meter.reset(arg)
                return self._send_json(200, {"ok": True})
            self._send_json(404, {"error": "not found"})

        # -- the proxy ------------------------------------------------------
        def _proxy(self, key: str, rest: str):
            if not self._authorized():
                return self._send_json(401, {"error": {"message": "bad meter token"}})
            body = self._body()
            is_post = self.command == "POST"
            if is_post and cfg.meter.over_budget():
                return self._send_json(
                    402, {"error": {"message": "budget reached; the run's spending cap stopped this call"}}
                )
            body = self._with_usage_option(body, rest) if is_post else body
            started = time.monotonic()
            with cfg.sem:
                resp, conn, retries = self._connect_with_retries(rest, body)
                if resp is None:
                    if is_post:
                        cfg.meter.record(key, ok=False, prompt=0, completion=0, cached=0,
                                         wall=time.monotonic() - started, retries=retries)
                    return self._send_json(502, {"error": {"message": "upstream unreachable"}})
                try:
                    prompt, completion, cached, truncated = self._relay(resp)
                finally:
                    conn.close()
            if is_post:
                cfg.meter.record(key, ok=200 <= resp.status < 300 and not truncated, prompt=prompt,
                                 completion=completion, cached=cached,
                                 wall=time.monotonic() - started, retries=retries,
                                 truncated=truncated)
            if truncated:
                # The answer was cut short upstream and the client already got a partial body.
                # Close the connection so it sees a broken stream rather than waiting on a hang.
                self.close_connection = True

        @staticmethod
        def _with_usage_option(body: bytes, rest: str) -> bytes:
            """Ask the API to report usage on streamed answers (it only does so on request)."""
            if not rest.rstrip("/").endswith("chat/completions") or not body:
                return body
            try:
                obj = json.loads(body)
            except ValueError:
                return body
            if isinstance(obj, dict) and obj.get("stream"):
                opts = obj.get("stream_options")
                opts = dict(opts) if isinstance(opts, dict) else {}
                opts["include_usage"] = True
                obj["stream_options"] = opts
                return json.dumps(obj).encode()
            return body

        def _connect_with_retries(self, rest: str, body: bytes):
            url = urllib.parse.urlsplit(f"{cfg.upstream}/{rest}")
            target = url.path + (f"?{url.query}" if url.query else "")
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | {"authorization"}}
            headers["Accept-Encoding"] = "identity"
            if cfg.upstream_key:
                headers["Authorization"] = f"Bearer {cfg.upstream_key}"
            if body or self.command == "POST":
                headers["Content-Length"] = str(len(body))
            retries = 0
            for attempt in range(cfg.max_retries + 1):
                conn = None
                try:
                    if url.scheme == "https":
                        conn = http.client.HTTPSConnection(url.netloc, timeout=cfg.timeout,
                                                           context=ssl.create_default_context())
                    else:
                        conn = http.client.HTTPConnection(url.netloc, timeout=cfg.timeout)
                    conn.request(self.command, target, body=body or None, headers=headers)
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException):
                    if conn is not None:
                        conn.close()
                    resp = None
                if resp is not None and not (resp.status == 429 or resp.status >= 500):
                    return resp, conn, retries
                if attempt == cfg.max_retries:
                    if resp is not None:
                        return resp, conn, retries  # hand the final error back as is
                    return None, None, retries
                wait = min(cfg.backoff_base * (2 ** attempt), cfg.backoff_cap)
                if resp is not None:
                    ra = resp.getheader("Retry-After")
                    if ra and ra.isdigit():
                        wait = min(float(ra), cfg.backoff_cap)
                    resp.read()
                    conn.close()
                retries += 1
                time.sleep(wait)
            return None, None, retries

        def _relay(self, resp) -> tuple[int, int, int, bool]:
            """Send the upstream answer back; return (prompt, completion, cached, truncated).

            truncated is True when the upstream connection ended before its answer was complete
            (a cut stream). The tokens seen before the cut are still counted."""
            ctype = (resp.getheader("Content-Type") or "").lower()
            streaming = "text/event-stream" in ctype
            usage = None
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if streaming:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                buf = b""
                truncated = False
                while True:
                    try:
                        chunk = resp.read1(8192)
                    except (http.client.HTTPException, ValueError, OSError):
                        # The upstream closed mid-answer (an empty chunk-size line surfaces as a
                        # ValueError). Do not send the terminating chunk: the answer is incomplete.
                        truncated = True
                        break
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        usage = self._sse_usage(line) or usage
                usage = self._sse_usage(buf) or usage
                if not truncated:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                if truncated:
                    return (*(usage if usage else (0, 0, 0)), True)
            else:
                data = resp.read(MAX_JSON_BYTES)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    usage = extract_usage(json.loads(data))
                except ValueError:
                    usage = None
            return (*(usage if usage else (0, 0, 0)), False)

        @staticmethod
        def _sse_usage(line: bytes):
            line = line.strip()
            if not line.startswith(b"data:"):
                return None
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                return None
            try:
                return extract_usage(json.loads(payload))
            except ValueError:
                return None

    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(cfg: Config, host: str = "0.0.0.0", port: int = 8080) -> Server:
    return Server((host, port), make_handler(cfg))


def main() -> int:
    cfg = Config()
    port = int(os.environ.get("PORT", "8080"))
    print(f"sbr-meter listening on :{port}, upstream {cfg.upstream}, "
          f"max_concurrency={cfg.max_concurrency}, budget={cfg.meter.budget}", flush=True)
    serve(cfg, port=port).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
