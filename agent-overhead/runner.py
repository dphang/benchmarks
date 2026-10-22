#!/usr/bin/env python3
"""Self-contained Linux/ARM agent overhead benchmark for a Raspberry Pi.

Same methodology as bench/cputurn.py (instant mock model, N turns reading a
growing file, process-tree CPU + peak RSS) but Linux-native (no /usr/bin/time
-l; uses the ps-tree sampler for both metrics) and with a HARD RAM GUARD: if the
Pi's available memory falls below --min-free-mb, the agent is killed so the
board never OOMs. Agents run one at a time.

    python3 pi_bench.py <mock.py> [agents...] --reps 3 --turns 30 --blob-kb 20

Reports per-agent: turns completed, CPU ms/turn (ps-tree), peak tree RSS (MB),
and whether the RAM guard tripped.
"""
import argparse, json, os, shutil, statistics, subprocess, sys, tempfile, threading, time
from collections import defaultdict
from pathlib import Path

MOCK = sys.argv[1] if len(sys.argv) > 1 else "stressmock.py"
if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
    del sys.argv[1]
NB = os.path.expanduser("~/.local/node-agents/node_modules/.bin")


def mem_available_mb():
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) // 1024
    except Exception:
        return 99999
    return 99999


def read_ticks(pid):
    """utime+stime for a pid in clock ticks (CLK_TCK=100 → 10 ms each), parsed
    past the comm field so a process name with spaces/parens can't break it."""
    try:
        d = open(f"/proc/{pid}/stat").read()
        f = d[d.rfind(")") + 2:].split()
        return int(f[11]) + int(f[12])  # utime + stime
    except Exception:
        return 0


def sample_tree(root, seen):
    """Record each tree pid's max CPU ticks into `seen`; return tree RSS (KB)."""
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
            pid_, pp_ = int(q[0]), int(q[1])
            rss[pid_] = int(q[2])
        except ValueError:
            continue
        kids[pp_].append(pid_)
    st, total_rss = [root], 0
    while st:
        x = st.pop()
        if x in rss:
            seen[x] = max(seen.get(x, 0), read_ticks(x))
            total_rss += rss[x]
        st += kids.get(x, [])
    return total_rss


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


PROMPT = "Read each file I point you to, in order."


def cfg(agent, wd, api, home):
    """(cmd, env, stdin_data) per agent."""
    if agent == "goose":
        return (["goose", "run", "--no-session", "--quiet", "-t", PROMPT],
                {"GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "fake", "OPENAI_HOST": api,
                 "OPENAI_API_KEY": "x", "GOOSE_MODE": "auto", "GOOSE_DISABLE_KEYRING": "1",
                 "XDG_CONFIG_HOME": str(home / "x/c"), "XDG_DATA_HOME": str(home / "x/d"),
                 "XDG_STATE_HOME": str(home / "x/s")}, None)
    if agent == "opencode":
        cfgdir = home / "oc"; cfgdir.mkdir(parents=True, exist_ok=True)
        c = cfgdir / "opencode.json"
        c.write_text(json.dumps({"$schema": "https://opencode.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench",
                "options": {"baseURL": f"{api}/v1", "apiKey": "x"}, "models": {"fake": {"name": "fake", "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"}}))
        return (["opencode", "run", "--standalone", "--auto", "-m", "bench/fake", PROMPT],
                {"OPENCODE_CONFIG": str(c), "XDG_CONFIG_HOME": str(cfgdir / "xc"), "XDG_DATA_HOME": str(cfgdir / "xd")}, None)
    if agent == "aider":
        env = {"OPENAI_API_BASE": f"{api}/v1", "OPENAI_API_KEY": "x", "AIDER_HOME": str(home / "ah")}
        cmd = ["aider", "--model", "openai/fake", "--yes-always", "--no-auto-commits",
               "--no-check-update", "--no-show-model-warnings", "--no-pretty", "--no-stream",
               "--no-gitignore", "--no-analytics"]
        return (cmd, env, "STDIN")
    if agent == "pi":
        d = home / "pi"; d.mkdir(parents=True, exist_ok=True)
        (d / "models.json").write_text(json.dumps({"providers": {"bench": {
            "baseUrl": f"{api}/v1", "api": "openai-completions", "apiKey": "x",
            "models": [{"id": "fake", "contextWindow": 200000}]}}}))
        return ([f"{NB}/pi", "-p", "--no-session", "--provider", "bench", "--model", "fake", PROMPT],
                {"PI_CODING_AGENT_DIR": str(d)}, None)
    if agent == "crush":
        xdg = home / "crush/xdg"; (xdg / "crush").mkdir(parents=True, exist_ok=True)
        (xdg / "crush" / "crush.json").write_text(json.dumps({
            "providers": {"bench": {"type": "openai", "base_url": f"{api}/v1", "api_key": "x",
                "models": [{"id": "fake", "name": "fake", "context_window": 200000, "default_max_tokens": 8192}]}},
            "models": {"large": {"model": "fake", "provider": "bench"}, "small": {"model": "fake", "provider": "bench"}},
            "options": {"disable_metrics": True}}))
        tmp = home / "crush/tmp"; tmp.mkdir(parents=True, exist_ok=True)
        return ([f"{NB}/crush", "run", "--quiet", "-D", str(home / "crush/data"), "-m", "bench/fake", PROMPT],
                {"XDG_CONFIG_HOME": str(xdg), "XDG_DATA_HOME": str(home / "crush/data"),
                 "TMPDIR": str(tmp), "CRUSH_DISABLE_METRICS": "1"}, None)
    if agent == "kilo":
        c = home / "kilo/kilo.json"; c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text(json.dumps({"$schema": "https://app.kilo.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench",
                "options": {"baseURL": f"{api}/v1", "apiKey": "x"}, "models": {"fake": {"name": "fake", "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"}}))
        return ([f"{NB}/kilo", "run", "--auto", "-m", "bench/fake", PROMPT],
                {"KILO_CONFIG": str(c), "OPENCODE_CONFIG": str(c),
                 "XDG_CONFIG_HOME": str(home / "kilo/xdg"), "XDG_DATA_HOME": str(home / "kilo/data")}, None)
    raise SystemExit(f"unknown agent {agent}")


def one_run(agent, turns, blob_kb, min_free):
    home = Path(tempfile.mkdtemp(prefix=f"pib-{agent}-"))
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
    env = dict(os.environ, **extra); env["PWD"] = str(wd); env["HOME"] = os.path.expanduser("~")
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if k not in extra:
            env.pop(k, None)
    env.update(extra)
    stdin_data = "".join(f"read file f{i}.txt\n" for i in range(turns)) + "/exit\n" if stdin_flag == "STDIN" else None
    p = subprocess.Popen(cmd, cwd=wd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            if mem_available_mb() < min_free:        # protect the Pi
                guard[0] = True
                p.kill()
                break
            time.sleep(0.08)

    th = threading.Thread(target=poll); th.start()
    try:
        p.wait(timeout=300)
    except subprocess.TimeoutExpired:
        p.kill()
    th.join()
    mock.kill()
    ts = [json.loads(l)["t"] for l in log.read_text().splitlines() if l.strip()]
    n = len(ts)
    cpu_tree = sum(seen.values())
    shutil.rmtree(home, ignore_errors=True)
    if n < 2:
        return {"turns": n, "cpu": None, "rss": peak[0] / 1024, "guard": guard[0]}
    return {"turns": n, "cpu": cpu_tree * 10.0 / n, "rss": peak[0] / 1024, "guard": guard[0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agents", nargs="*", default=["goose"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--blob-kb", type=int, default=50)
    ap.add_argument("--min-free-mb", type=int, default=150, help="kill an agent if Pi free RAM drops below this")
    a = ap.parse_args()
    print(f"PI BENCH: {a.turns} turns, {a.blob_kb} KB/turn, {a.reps} reps; RAM guard < {a.min_free_mb} MB free")
    print(f"start free: {mem_available_mb()} MB\n")
    print(f"{'agent':10} {'turns':>6} {'CPU ms/turn':>14} {'peak RSS MB':>14}  note")
    print("-" * 60)
    for agent in a.agents:
        runs = [r for r in (one_run(agent, a.turns, a.blob_kb, a.min_free_mb) for _ in range(a.reps)) if r]
        if not runs:
            print(f"{agent:10} {'—':>6}  did not run"); continue
        t = statistics.median(r["turns"] for r in runs)
        cpus = [r["cpu"] for r in runs if r["cpu"] is not None]
        cpu = f"{statistics.median(cpus):.1f}" if cpus else "n/a"
        rss = statistics.median(r["rss"] for r in runs)
        note = "RAM-GUARD TRIPPED" if any(r["guard"] for r in runs) else ("quit early" if t < a.turns * 0.8 else "")
        print(f"{agent:10} {t:6.0f} {cpu:>14} {rss:14.0f}  {note}", flush=True)
    print(f"\nend free: {mem_available_mb()} MB")


if __name__ == "__main__":
    main()
