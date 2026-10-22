#!/usr/bin/env python3
"""Agent-overhead benchmark runner — self-contained and cross-platform.

Drives each coding agent through the same N-turn session against the instant mock
model (stressmock.py), with the model's time excluded, and reports the agent's own
per-turn CPU and peak memory. Works on macOS and Linux:

  - CPU per turn: macOS uses `/usr/bin/time -l` (exact process+children rusage);
    Linux reads `/proc/<pid>/stat` (utime+stime, sampled). Both are read from
    outside the process — nothing is instrumented. The two are close but not
    identical, so compare within a platform, not across.
  - Peak memory: the max resident set of the whole process tree, sampled via `ps`.
  - A RAM guard (Linux) kills an agent if free memory drops below --min-free-mb,
    so it is safe to run on a small board (Raspberry Pi etc.). Agents run one at a
    time.

    python3 runner.py stressmock.py [agents...] --reps 10 --turns 40 --blob-kb 50

Each agent's binary is looked up on PATH by its name; override with an env var,
e.g. BENCH_FORGE_BIN=/path/to/forge.
"""
import argparse, json, os, re, shutil, statistics, subprocess, sys, tempfile, threading, time
from collections import defaultdict
from pathlib import Path

MOCK = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "stressmock.py"
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    del sys.argv[1]
IS_MAC = sys.platform == "darwin"
TIME_BIN = "/usr/bin/time"
PROMPT = "Read each file I point you to, in order."
AGENTS = ["forge", "pi", "crush", "claude", "goose", "qwen", "codex", "cline", "opencode", "aider", "kilo"]


def bin_for(agent):
    return os.environ.get(f"BENCH_{agent.upper()}_BIN", agent)


# ---------- measurement helpers ----------
def mem_available_mb():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) // 1024
    except Exception:
        return 1 << 30  # not Linux (e.g. macOS): guard effectively off
    return 1 << 30


def read_ticks(pid):  # Linux: utime+stime in clock ticks (usually 10 ms each)
    try:
        d = open(f"/proc/{pid}/stat").read()
        f = d[d.rfind(")") + 2:].split()
        return int(f[11]) + int(f[12])
    except Exception:
        return 0


def sample_tree(root, seen):
    """Record each tree pid's max CPU ticks (Linux) into `seen`; return tree RSS KB."""
    try:
        out = subprocess.check_output(["ps", "-o", "pid=,ppid=,rss=", "-A"], text=True)
    except Exception:
        return 0
    kids, rss = defaultdict(list), {}
    for ln in out.splitlines():
        q = ln.split()
        if len(q) < 3:
            continue
        try:
            pid_, pp_ = int(q[0]), int(q[1]); rss[pid_] = int(q[2])
        except ValueError:
            continue
        kids[pp_].append(pid_)
    st, total = [root], 0
    while st:
        x = st.pop()
        if x in rss:
            if not IS_MAC:
                seen[x] = max(seen.get(x, 0), read_ticks(x))
            total += rss[x]
        st += kids.get(x, [])
    return total


def parse_time_l(err):  # macOS /usr/bin/time -l → user+sys seconds
    m = re.search(r"([\d.]+)\s+real\s+([\d.]+)\s+user\s+([\d.]+)\s+sys", err)
    return (float(m.group(2)) + float(m.group(3))) if m else None


def start_mock(turns, log):
    p = subprocess.Popen([sys.executable, MOCK, "--turns", str(turns), "--log", str(log)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    t0 = time.time()
    while time.time() - t0 < 8:
        ln = p.stdout.readline()
        if ln.startswith("PORT"):
            return p, ln.split()[1]
    p.kill()
    return None, None


# ---------- per-agent launch configs (mirror of a hosted-model bench) ----------
def cfg(agent, wd, api, home):
    """Returns (cmd, env, stdin_data). api is the mock base URL; v1 = api + /v1."""
    v1 = f"{api}/v1"
    b = bin_for(agent)
    if agent == "forge":
        h = home / "fh"; (h / ".forge").mkdir(parents=True, exist_ok=True)
        (h / ".forge" / ".forge.toml").write_text(
            f'[[providers]]\nid = "bench"\nurl = "{v1}/chat/completions"\nresponse_type = "OpenAI"\n'
            f'models = [{{ id = "fake", name = "fake", context_length = 200000, tools_supported = true }}]\n'
            f'\n[session]\nprovider_id = "bench"\nmodel_id = "fake"\n')
        (h / ".forge" / ".credentials.json").write_text(json.dumps([{"id": "bench", "auth_details": {"api_key": "x"}}]))
        return ([b, "-p", PROMPT], {"HOME": str(h), "TERM": "xterm-256color"}, None)
    if agent == "pi":
        d = home / "pi"; d.mkdir(parents=True, exist_ok=True)
        (d / "models.json").write_text(json.dumps({"providers": {"bench": {
            "baseUrl": v1, "api": "openai-completions", "apiKey": "x",
            "models": [{"id": "fake", "contextWindow": 200000}]}}}))
        return ([b, "-p", "--no-session", "--provider", "bench", "--model", "fake", PROMPT],
                {"PI_CODING_AGENT_DIR": str(d)}, None)
    if agent == "crush":
        xdg = home / "crush/xdg"; (xdg / "crush").mkdir(parents=True, exist_ok=True)
        (xdg / "crush" / "crush.json").write_text(json.dumps({
            "providers": {"bench": {"type": "openai", "base_url": v1, "api_key": "x",
                "models": [{"id": "fake", "name": "fake", "context_window": 200000, "default_max_tokens": 8192}]}},
            "models": {"large": {"model": "fake", "provider": "bench"}, "small": {"model": "fake", "provider": "bench"}},
            "options": {"disable_metrics": True}}))
        tmp = home / "crush/tmp"; tmp.mkdir(parents=True, exist_ok=True)
        return ([b, "run", "--quiet", "-D", str(home / "crush/data"), "-m", "bench/fake", PROMPT],
                {"XDG_CONFIG_HOME": str(xdg), "XDG_DATA_HOME": str(home / "crush/data"),
                 "TMPDIR": str(tmp), "CRUSH_DISABLE_METRICS": "1"}, None)
    if agent == "claude":
        return ([b, "-p", "--model", "fake", "--dangerously-skip-permissions", "--output-format", "json", PROMPT],
                {"ANTHROPIC_BASE_URL": api, "ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "",
                 "ANTHROPIC_MODEL": "fake", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fake",
                 "ANTHROPIC_DEFAULT_SONNET_MODEL": "fake", "ANTHROPIC_DEFAULT_OPUS_MODEL": "fake",
                 "CLAUDE_CODE_SUBAGENT_MODEL": "fake", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                 "CLAUDE_CONFIG_DIR": str(home / "cc")}, None)
    if agent == "goose":
        return ([b, "run", "--no-session", "--quiet", "-t", PROMPT],
                {"GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "fake", "OPENAI_HOST": api, "OPENAI_API_KEY": "x",
                 "GOOSE_MODE": "auto", "GOOSE_DISABLE_KEYRING": "1",
                 "XDG_CONFIG_HOME": str(home / "g/c"), "XDG_DATA_HOME": str(home / "g/d"),
                 "XDG_STATE_HOME": str(home / "g/s")}, None)
    if agent == "qwen":
        h = home / "qwen"; (h / ".qwen").mkdir(parents=True, exist_ok=True)
        (h / ".qwen" / "settings.json").write_text(json.dumps(
            {"security": {"auth": {"selectedType": "openai"}}, "usageStatisticsEnabled": False}))
        return ([b, "--yolo", "-m", "fake", PROMPT],
                {"HOME": str(h), "OPENAI_API_KEY": "x", "OPENAI_BASE_URL": v1, "OPENAI_MODEL": "fake"}, None)
    if agent == "codex":
        h = home / "codex"; h.mkdir(parents=True, exist_ok=True)
        (h / "config.toml").write_text(
            f'model_provider = "bench"\n[model_providers.bench]\nname = "bench"\nbase_url = "{v1}"\n'
            f'env_key = "BENCH_API_KEY"\nwire_api = "responses"\n')
        return ([b, "exec", "-m", "fake", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", PROMPT],
                {"CODEX_HOME": str(h), "BENCH_API_KEY": "x"}, None)
    if agent == "cline":
        d = home / "cline"; (d / "settings").mkdir(parents=True, exist_ok=True)
        (d / "settings" / "providers.json").write_text(json.dumps({
            "version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
            "providers": {"openai-compatible": {
                "settings": {"provider": "openai-compatible", "apiKey": "x", "model": "fake", "baseUrl": v1},
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tokenSource": "manual"}}}))
        return ([b, "--yolo", "--json", "-P", "openai-compatible", "-m", "fake", "-k", "x",
                 "--config", str(d), "--data-dir", str(d), PROMPT], {"CLINE_DIR": str(d)}, None)
    if agent == "opencode":
        d = home / "oc"; d.mkdir(parents=True, exist_ok=True)
        c = d / "opencode.json"
        c.write_text(json.dumps({"$schema": "https://opencode.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench",
                "options": {"baseURL": v1, "apiKey": "x"}, "models": {"fake": {"name": "fake", "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"}}))
        return ([b, "run", "--standalone", "--auto", "-m", "bench/fake", PROMPT],
                {"OPENCODE_CONFIG": str(c), "XDG_CONFIG_HOME": str(d / "xc"), "XDG_DATA_HOME": str(d / "xd")}, None)
    if agent == "kilo":
        d = home / "kilo"; d.mkdir(parents=True, exist_ok=True)
        c = d / "kilo.json"
        c.write_text(json.dumps({"$schema": "https://app.kilo.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench",
                "options": {"baseURL": v1, "apiKey": "x"}, "models": {"fake": {"name": "fake", "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"}}))
        return ([b, "run", "--auto", "-m", "bench/fake", PROMPT],
                {"KILO_CONFIG": str(c), "OPENCODE_CONFIG": str(c),
                 "XDG_CONFIG_HOME": str(d / "xc"), "XDG_DATA_HOME": str(d / "xd")}, None)
    if agent == "aider":
        # no tool loop (edit-block); drive the stdin REPL, one request per line.
        return ([b, "--model", "openai/fake", "--yes-always", "--no-auto-commits", "--no-check-update",
                 "--no-show-model-warnings", "--no-pretty", "--no-stream", "--no-gitignore", "--no-analytics"],
                {"OPENAI_API_BASE": v1, "OPENAI_API_KEY": "x", "AIDER_HOME": str(home / "ah")}, "STDIN")
    raise SystemExit(f"no config for agent {agent}")


def _drain(mfd):
    while True:
        try:
            if not os.read(mfd, 65536):
                break
        except OSError:
            break


def one_run(agent, turns, blob_kb, min_free):
    home = Path(tempfile.mkdtemp(prefix=f"ao-{agent}-"))
    wd = home / "work"; wd.mkdir()
    blob = ("lorem ipsum dolor sit amet " * 40 + "\n")
    for i in range(turns + 20):
        (wd / f"f{i}.txt").write_text(f"file {i}\n" + blob * blob_kb)
    log = home / "m.jsonl"; log.write_text("")
    mock, port = start_mock(turns, log)
    if not port:
        shutil.rmtree(home, ignore_errors=True); return None
    api = f"http://127.0.0.1:{port}"
    try:
        cmd, extra, stdin_flag = cfg(agent, wd, api, home)
    except SystemExit:
        mock.kill(); shutil.rmtree(home, ignore_errors=True); return None
    if agent == "aider":
        subprocess.run(["git", "init", "-q"], cwd=wd)
    env = dict(os.environ, **extra); env["PWD"] = str(wd)
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if k not in extra:
            env.pop(k, None)
    env.update(extra)
    stdin_data = ("".join(f"read file f{i}.txt\n" for i in range(turns)) + "/exit\n") if stdin_flag == "STDIN" else None

    errf = home / "time.err"
    use_pty = agent == "forge"          # rustyline needs a controlling terminal
    tcmd = ([TIME_BIN, "-l"] + cmd) if IS_MAC else cmd
    mfd = sfd = None
    if use_pty:
        import pty, fcntl, termios
        mfd, sfd = pty.openpty()
    with open(errf, "w") as ef:
        if use_pty:
            def _ctty():
                os.setsid(); fcntl.ioctl(sfd, termios.TIOCSCTTY, 0)
            p = subprocess.Popen(tcmd, cwd=wd, env=env, stdin=sfd, stdout=sfd, stderr=ef, preexec_fn=_ctty)
            os.close(sfd)
            threading.Thread(target=lambda: _drain(mfd), daemon=True).start()
        else:
            p = subprocess.Popen(tcmd, cwd=wd, env=env, stdout=subprocess.DEVNULL, stderr=ef,
                                 stdin=(subprocess.PIPE if stdin_data else subprocess.DEVNULL))
            if stdin_data:
                try:
                    p.stdin.write(stdin_data.encode()); p.stdin.close()
                except Exception:
                    pass
        seen, peak, guard = {}, [0], [False]

        def poll():
            while p.poll() is None:
                r = sample_tree(p.pid, seen)
                if r > peak[0]:
                    peak[0] = r
                if mem_available_mb() < min_free:
                    guard[0] = True; p.kill(); break
                time.sleep(0.07)

        th = threading.Thread(target=poll); th.start()
        try:
            p.wait(timeout=300)
        except subprocess.TimeoutExpired:
            p.kill()
        th.join()
    if mfd is not None:
        try:
            os.close(mfd)
        except OSError:
            pass
    mock.kill()

    ts = [json.loads(l)["t"] for l in log.read_text().splitlines() if l.strip()]
    n = len(ts)
    if IS_MAC:
        cs = parse_time_l(errf.read_text()); cpu = (cs * 1000 / n) if (cs and n) else None
    else:
        cpu = (sum(seen.values()) * 10.0 / n) if n else None   # ticks(10ms) → ms/turn
    shutil.rmtree(home, ignore_errors=True)
    return {"turns": n, "cpu": cpu, "rss": peak[0] / 1024, "guard": guard[0]}


def agg(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n/a"
    m = statistics.median(vals); mean = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{m:7.1f} | {mean:7.1f}±{sd:4.1f} ({(sd / mean * 100 if mean else 0):3.0f}%)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agents", nargs="*", default=AGENTS)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--blob-kb", type=int, default=50)
    ap.add_argument("--min-free-mb", type=int, default=150, help="Linux: kill an agent if free RAM drops below this")
    a = ap.parse_args()
    cpu_src = "/usr/bin/time -l" if IS_MAC else "/proc utime+stime"
    print(f"agent-overhead: {a.turns} turns, {a.blob_kb} KB/turn, {a.reps} reps; CPU via {cpu_src}; model excluded\n")
    print(f"{'agent':10} {'turns':>6} {'reps':>4}  {'CPU ms/turn':>28}  {'peak memory MB':>28}  note")
    print("-" * 94)
    for agent in a.agents:
        runs = [r for r in (one_run(agent, a.turns, a.blob_kb, a.min_free_mb) for _ in range(a.reps)) if r]
        if not runs:
            print(f"{agent:10} {'—':>6}  did not run"); continue
        t = statistics.median(r["turns"] for r in runs)
        note = "RAM-GUARD" if any(r["guard"] for r in runs) else ("quit early" if t < a.turns * 0.8 else "")
        print(f"{agent:10} {t:6.0f} {len(runs):4d}  {agg([r['cpu'] for r in runs]):>28}  "
              f"{agg([r['rss'] for r in runs]):>28}  {note}", flush=True)
    print("\nEach cell: median | mean±sd (CV%). aider is per-message (edit-block, no tool loop).")


if __name__ == "__main__":
    main()
