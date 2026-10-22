#!/usr/bin/env python3
"""Generic looping mock for measuring agents' per-turn overhead. Speaks both
OpenAI /v1/chat/completions and Anthropic /v1/messages (JSON or SSE). On each
request it inspects the agent's advertised tools, calls a safe read/list/shell
tool so the agent executes and comes back, and after --turns calls returns plain
text so the agent stops. The file argument varies per turn so loop-detection
doesn't trip. Logs {t, n}. Prints 'PORT <n>'."""
import http.server, socketserver, json, sys, threading, time, argparse

LOCK = threading.Lock()
STATE = {"n": 0}
LOGF = None; TURNS = 40; DUMP = None  # set from argv in __main__
PREF = ("shell", "bash", "exec", "run_command", "read_file", "read", "cat",
        "ls", "list", "glob", "grep", "find")


def tools_of(body):
    out = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if "function" in t:                       # OpenAI chat
            f = t["function"]; params = f.get("parameters") or {}
        elif "input_schema" in t:                  # Anthropic
            f = t; params = t.get("input_schema") or {}
        elif "name" in t and "parameters" in t:    # OpenAI Responses (flattened)
            f = t; params = t.get("parameters") or {}
        else:
            continue
        out.append((f.get("name", ""), params.get("properties") or {}, params.get("required") or []))
    return out


READ = ("read_file", "readfile", "read", "cat", "view_file", "open_file", "fs_read")
SHELL = ("shell", "bash", "exec", "run_command", "run_terminal", "terminal", "run")
BAD = ("image", "screenshot", "browser", "web", "fetch", "apply_patch", "edit", "write", "plan")
# Terminal/completion tools: some agents (cline's submit_and_exit) treat the run
# as unfinished until the MODEL calls one of these, so plain "done." text makes
# them loop forever. On the final turn the mock calls the agent's terminal tool.
# Matched by exact/suffix name only — bare words like "exit" would wrongly match
# unrelated tools (e.g. a plan-mode toggle) and trap the agent in a loop.
TERMINAL = ("submit_and_exit", "attempt_completion", "task_complete", "complete_task",
            "end_task", "finish_task", "mark_task_complete")


def pick(body):
    ts = [t for t in tools_of(body) if not any(b in (t[0] or "").lower() for b in BAD)]
    for prefs in (READ, SHELL):
        for name, props, req in ts:
            nl = (name or "").lower()
            if any(nl == p or nl.endswith("_" + p) or p in nl for p in prefs):
                is_shell = prefs is SHELL
                return name, props, req, is_shell
    return (ts[0][0], ts[0][1], ts[0][2], False) if ts else None


def pick_terminal(body):
    """An agent's completion tool, if it has one (so the run can end cleanly)."""
    for name, props, req in tools_of(body):
        nl = (name or "").lower()
        if any(nl == t or nl.endswith("_" + t) for t in TERMINAL):
            return name, props, req, False
    return None


def args_for(props, req, n, is_shell):
    req = req or list(props)[:1]
    a = {}
    for p in req:
        spec = props.get(p, {}); typ = spec.get("type", "string"); pl = p.lower()
        cmdish = any(k in pl for k in ("command", "cmd", "script", "input"))
        if is_shell and cmdish:
            # read a large file through the shell, so the conversation grows
            a[p] = ["cat", f"f{n}.txt"] if typ == "array" else f"cat f{n}.txt"
        elif typ in ("number", "integer"):
            a[p] = 1
        elif typ == "boolean":
            # A terminal tool's success flag (verified/success/...) must be true,
            # or the agent treats the run as failed and keeps going.
            a[p] = any(k in pl for k in ("verif", "success", "ok", "complete", "done", "pass", "valid"))
        elif typ == "array":
            # A file/path list (e.g. cline's read_files(files=[...])) must hold a
            # real path, or the agent reads nothing, makes no progress and quits.
            if any(k in pl for k in ("file", "path", "command", "cmd")):
                a[p] = [f"f{n}.txt"]
            else:
                a[p] = []
        elif any(k in pl for k in ("summary", "message", "reason", "description", "result")):
            a[p] = "Task complete."
        elif any(k in pl for k in ("query", "search", "text", "content", "prompt")):
            a[p] = "x"
        else:
            a[p] = f"f{n}.txt"
    return a


# Phrases an agent uses when it asks the model to compact/summarize the session.
# Matched against the LAST message only (the instruction), so tool descriptions
# that merely mention "summarize" don't trip it, and so it fires whether or not
# the agent leaves its tools attached (opencode keeps all 12 attached).
SUMMARY_PHRASES = ("summarize the conversation", "structured summary", "summary template",
                   "return the summary", "running summary", "conversation so far",
                   "compact the conversation", "do not continue the task",
                   "summary of the conversation", "condense the conversation")
# A filled version of opencode's template; the section headings are what its
# validator checks for, and the generic bullets suit the read-files scenario.
# Agents that want a plain summary ignore the headings, so one text serves all.
SUMMARY_TEXT = (
    "## Objective\n- Read a sequence of placeholder files (f0.txt, f1.txt, ...) in order.\n\n"
    "## Requirements\n- Read each file the user points to, in the order given.\n\n"
    "## Decisions\n- (none)\n\n"
    "## Work State\n### Completed\n- Read the files requested so far.\n"
    "### Active\n- Reading the next file in the sequence.\n### Blocked\n- (none)\n\n"
    "## Next Move\n1. Read the next file (f<n>.txt).\n\n"
    "## Relevant Files\n- `f0.txt`..`fN.txt`: the placeholder files read in order.\n\n"
    "## Important Context\n- Files hold repeated placeholder text; all reads succeeded, no errors.")


def _fill_schema(schema):
    """A minimal value satisfying a JSON schema (required fields only)."""
    if not isinstance(schema, dict):
        return "x"
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if typ == "object":
        props = schema.get("properties") or {}
        req = schema.get("required") or list(props.keys())
        return {k: _fill_schema(props.get(k, {})) for k in req}
    if typ == "array":
        items = schema.get("items")
        return [_fill_schema(items)] if isinstance(items, dict) else []
    if typ in ("number", "integer"):
        return 1
    if typ == "boolean":
        return True
    enum = schema.get("enum")
    return enum[0] if isinstance(enum, list) and enum else "x"


def structured_json(body):
    """If a request asks for structured output (response_format) and brings no
    tools — e.g. forge's conversation-title call — return a JSON string that
    satisfies the schema, so the agent accepts it instead of retrying forever."""
    if body.get("tools"):
        return None
    rf = body.get("response_format") or body.get("text", {}).get("format")
    if not isinstance(rf, dict):
        return None
    if rf.get("type") == "json_object":
        return "{}"
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema") or rf.get("schema") or {}
        return json.dumps(_fill_schema(schema))
    return None


def _last_text(body):
    """Text of the final message (the instruction), for both wire shapes."""
    for key in ("messages", "input"):
        seq = body.get(key)
        if isinstance(seq, list) and seq and isinstance(seq[-1], dict):
            c = seq[-1].get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return ""


def is_summary(body):
    """True if this is an agent's compaction/summarize sub-call (wants prose,
    not a tool-call), so the mock returns a filled summary instead of a canned
    tool-call (which otherwise makes opencode/cline abort with "no summary")."""
    return any(p in _last_text(body).lower() for p in SUMMARY_PHRASES)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        b = b'{"data":[{"id":"fake","object":"model"}]}'
        self._send(200, b, "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _sse(self, events):
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        for ev in events:
            self.wfile.write(ev.encode())

    def do_POST(self):
        ln = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(ln) if ln else b"{}"
        try: body = json.loads(raw)
        except Exception: body = {}
        with LOCK:
            STATE["n"] += 1; n = STATE["n"]
        if LOGF:
            with LOCK, open(LOGF, "a") as f:
                f.write(json.dumps({"t": time.time(), "n": n, "path": self.path}) + "\n")
        if DUMP:                                     # diagnostic: raw bodies
            import os
            os.makedirs(DUMP, exist_ok=True)
            with open(os.path.join(DUMP, f"req_{n:03d}.json"), "w") as f:
                json.dump({"path": self.path, "body": body}, f)
        done = n > TURNS
        struct = structured_json(body)
        summ = is_summary(body)
        if struct is not None:
            # A structured-output side-call (e.g. forge's title request): return
            # schema-valid JSON, don't advance/finish the task.
            tool, text = None, struct
        elif summ:
            # A summary sub-call returns prose but is NOT the task finishing, so
            # the agent consumes the summary and keeps going.
            tool, text = None, SUMMARY_TEXT
        elif done:
            # End the run: call the agent's terminal tool if it has one (cline's
            # submit_and_exit), else return plain text for agents that stop on it.
            tool, text = pick_terminal(body), "done."
        else:
            tool, text = pick(body), "done."
        stream = bool(body.get("stream"))
        pt = max(1, ln // 4)
        if "/messages" in self.path:               # Anthropic
            self._anthropic(n, tool, stream, pt, text)
        elif "/responses" in self.path:             # OpenAI Responses (codex)
            self._responses(n, tool, pt, text)
        else:                                       # OpenAI chat (default)
            self._openai(n, tool, stream, pt, text)

    def _openai(self, n, tool, stream, pt, text="done."):
        usage = {"prompt_tokens": pt, "completion_tokens": 5, "total_tokens": pt + 5}
        if tool:
            name, props, req, is_shell = tool
            call = {"id": f"call_{n}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args_for(props, req, n, is_shell))}}
            msg = {"role": "assistant", "content": None, "tool_calls": [call]}; finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": text}; finish = "stop"; call = None
        if stream:
            delta = {"role": "assistant"}
            if call:
                delta["tool_calls"] = [{"index": 0, **call}]
            else:
                delta["content"] = text
            # Strict clients (forge's aws-smithy codegen) require the full chunk
            # envelope, not just `choices`, or they fail to deserialize the stream.
            head = {"id": f"chatcmpl-{n}", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": "fake"}
            self._sse([
                f'data: {json.dumps({**head, "choices":[{"index":0,"delta":delta,"finish_reason":None}]})}\n\n',
                f'data: {json.dumps({**head, "choices":[{"index":0,"delta":{},"finish_reason":finish}],"usage":usage})}\n\n',
                "data: [DONE]\n\n"])
        else:
            self._send(200, json.dumps({"id": f"c{n}", "object": "chat.completion",
                "created": int(time.time()), "model": "fake",
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage}).encode(),
                "application/json")

    def _responses(self, n, tool, pt, text="done."):
        rid = f"resp_{n}"
        if tool:
            name, props, req, is_shell = tool
            args = json.dumps(args_for(props, req, n, is_shell))
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
                    "name": name, "arguments": args}
            done_resp = {"id": rid, "object": "response", "status": "completed",
                         "output": [item], "usage": {"input_tokens": pt, "output_tokens": 5, "total_tokens": pt + 5}}
            evs = [
                ("response.created", {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "output": []}}),
                ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": name, "arguments": ""}}),
                ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": f"fc_{n}", "output_index": 0, "delta": args}),
                ("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": f"fc_{n}", "output_index": 0, "arguments": args}),
                ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
                ("response.completed", {"type": "response.completed", "response": done_resp}),
            ]
        else:
            item = {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text}]}
            done_resp = {"id": rid, "object": "response", "status": "completed",
                         "output": [item], "usage": {"input_tokens": pt, "output_tokens": 5, "total_tokens": pt + 5}}
            evs = [
                ("response.created", {"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "output": []}}),
                ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "in_progress", "content": []}}),
                ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": f"msg_{n}", "output_index": 0, "content_index": 0, "delta": text}),
                ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
                ("response.completed", {"type": "response.completed", "response": done_resp}),
            ]
        self._sse([f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in evs])

    def _anthropic(self, n, tool, stream, pt, text="done."):
        usage = {"input_tokens": pt, "output_tokens": 5}
        if tool:
            name, props, req, is_shell = tool
            inp = args_for(props, req, n, is_shell)
            block = {"type": "tool_use", "id": f"tu_{n}", "name": name, "input": inp}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": text}; stop = "end_turn"
        if stream:
            start = {"type": "message_start", "message": {"id": f"msg{n}", "type": "message",
                     "role": "assistant", "model": "fake", "content": [], "stop_reason": None,
                     "usage": {"input_tokens": pt, "output_tokens": 1}}}
            if tool:
                cbs = {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "id": f"tu_{n}", "name": name, "input": {}}}
                delta = {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "input_json_delta", "partial_json": json.dumps(inp)}}
            else:
                cbs = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
                delta = {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": text}}
            self._sse([
                f"event: message_start\ndata: {json.dumps(start)}\n\n",
                f"event: content_block_start\ndata: {json.dumps(cbs)}\n\n",
                f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n",
                f'event: content_block_stop\ndata: {json.dumps({"type":"content_block_stop","index":0})}\n\n',
                f'event: message_delta\ndata: {json.dumps({"type":"message_delta","delta":{"stop_reason":stop},"usage":{"output_tokens":5}})}\n\n',
                f'event: message_stop\ndata: {json.dumps({"type":"message_stop"})}\n\n'])
        else:
            self._send(200, json.dumps({"id": f"msg{n}", "type": "message", "role": "assistant",
                "model": "fake", "content": [block], "stop_reason": stop, "usage": usage}).encode(),
                "application/json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--log", default=None)
    ap.add_argument("--dump", default=None, help="dir to write each request body (diagnostic)")
    a = ap.parse_args()
    TURNS = a.turns; LOGF = a.log; DUMP = a.dump
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    print("PORT", srv.server_address[1], flush=True)
    srv.serve_forever()
