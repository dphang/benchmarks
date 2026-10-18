#!/usr/bin/env python3
"""Token meter for fair efficiency numbers across harnesses.

    python3 rally-3d/meter.py [--port 4100] [--upstream http://localhost:4000] [--log rally-3d/meter.jsonl]

Sits in front of the LiteLLM proxy. A client uses a base URL with a run label
as the first path segment (http://localhost:4100/<label>/v1/...); the meter
strips the label, forwards the request unchanged (except asking OpenAI-style
streams to include usage), and records the provider-reported usage of every
response under that label: OpenAI chat (JSON or SSE), Anthropic Messages
(message_start / message_delta) and OpenAI Responses (response.completed).
Every harness is counted by the same code, from the same numbers the
provider bills.
"""

import argparse
import os
import http.client
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()


def generated_chars(obj):
    """Characters of output in one streamed chunk (OpenAI-style)."""
    n = 0
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or {}
        n += len(d.get("content") or "") + len(d.get("reasoning_content") or d.get("reasoning") or "")
        for tc in d.get("tool_calls") or []:
            n += len((tc.get("function") or {}).get("arguments") or "")
    return n


def usage_from(obj, acc):
    """Fold any usage found in a JSON object (or SSE event) into acc."""
    if not isinstance(obj, dict):
        return
    for u in (obj.get("usage"), (obj.get("message") or {}).get("usage") if isinstance(obj.get("message"), dict) else None,
              (obj.get("response") or {}).get("usage") if isinstance(obj.get("response"), dict) else None):
        if not isinstance(u, dict):
            continue
        inp = u.get("prompt_tokens", u.get("input_tokens"))
        # Anthropic's input_tokens excludes cache reads/writes; OpenAI's prompt_tokens includes them.
        if "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u:
            inp = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        out = u.get("completion_tokens", u.get("output_tokens"))
        # Anthropic streams report input at message_start and output at message_delta.
        if inp:
            acc["input"] = max(acc["input"], int(inp))
        if out:
            acc["output"] = max(acc["output"], int(out))
        cached = u.get("prompt_tokens_details", {}) if isinstance(u.get("prompt_tokens_details"), dict) else {}
        c = cached.get("cached_tokens") or u.get("cache_read_input_tokens")
        if c:
            acc["cached"] = max(acc["cached"], int(c))


class Handler(BaseHTTPRequestHandler):
    upstream = None
    log_path = None
    dump_dir = None
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # a client closing its connection, often after cancelling a call

    def log_message(self, *a):
        pass

    def _forward(self):
        parts = self.path.lstrip("/").split("/", 1)
        label = urllib.parse.unquote(parts[0]) if len(parts) == 2 else "unlabelled"
        path = "/" + (parts[1] if len(parts) == 2 else parts[0])
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if body and path.split("?")[0].endswith("/chat/completions"):
            try:
                req = json.loads(body)
                if req.get("stream"):
                    req.setdefault("stream_options", {})["include_usage"] = True
                    body = json.dumps(req).encode()
            except ValueError:
                pass
        u = urllib.parse.urlparse(self.upstream)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "accept-encoding")}
        headers["Content-Length"] = str(len(body))
        conn.request(self.command, path, body=body, headers=headers)
        resp = conn.getresponse()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        acc = {"input": 0, "output": 0, "cached": 0}
        buf = b""
        streaming = "text/event-stream" in (resp.getheader("Content-Type") or "")
        whole = b""
        raw = []
        streamed = 0  # characters of generated output seen, for cancelled calls
        aborted = False
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            if self.dump_dir:
                raw.append(chunk)
            if streaming:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line.startswith(b"data:"):
                        try:
                            obj = json.loads(line[5:].strip())
                            usage_from(obj, acc)
                            streamed += generated_chars(obj)
                        except ValueError:
                            pass
            else:
                whole += chunk
            try:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            except OSError:
                # The client cancelled mid-stream (e.g. a reasoning loop was cut
                # short). The provider still billed what it generated.
                aborted = True
                break
        if not aborted:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                aborted = True
        if aborted and not acc["output"]:
            acc["output"] = streamed // 4
            acc["input"] = len(body) // 4
            acc["estimated"] = True
        if not streaming:
            try:
                usage_from(json.loads(whole), acc)
            except ValueError:
                pass
        conn.close()
        if path.split("?")[0].rstrip("/").endswith(("completions", "messages", "responses")):
            rec = {"t": time.time(), "label": label, "path": path.split("?")[0], "status": resp.status, **acc}
            if aborted:
                rec["aborted"] = True
            with LOCK, open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            if self.dump_dir:
                # Request and raw response per call, for looking at what a model actually produced.
                stem = os.path.join(self.dump_dir, f"{label.replace('/', '_')}-{rec['t']:.3f}")
                with open(stem + ".req.json", "wb") as f:
                    f.write(body)
                with open(stem + ".resp", "wb") as f:
                    f.write(b"".join(raw))

    do_POST = _forward
    do_GET = _forward


def totals(log_path):
    """label -> {requests, input, output, cached}."""
    out = {}
    try:
        lines = open(log_path).read().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        r = json.loads(line)
        t = out.setdefault(r["label"], {"requests": 0, "input": 0, "output": 0, "cached": 0})
        t["requests"] += 1
        for k in ("input", "output", "cached"):
            t[k] += r.get(k, 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4100)
    ap.add_argument("--upstream", default="http://localhost:4000")
    ap.add_argument("--log", default="rally-3d/meter.jsonl")
    ap.add_argument("--dump", help="directory to save each call's request and raw response")
    a = ap.parse_args()
    if a.dump:
        os.makedirs(a.dump, exist_ok=True)
    Handler.dump_dir = a.dump
    Handler.upstream = a.upstream
    Handler.log_path = a.log
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
