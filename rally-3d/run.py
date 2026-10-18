#!/usr/bin/env python3
"""Benchmark coding agents on small, automatically-verified tasks.

Each task lives in rally-3d/tasks/<name>/ with:
  prompt.txt  - the instruction given to the agent
  files/      - the starting repository
  check.sh    - exit 0 if the task was solved (runs in the work dir;
                $OUTPUT_FILE points to the agent's captured output)
  setup.sh    - optional setup hook, run before the agent timer
  env         - optional KEY=VALUE lines exported to check.sh

Usage:
  rally-3d/run.py --agents opencode,claude,codex,goose,aider,qwen,crush --model bench-model [--tasks a,b] [--repeat N]
"""

import argparse
from contextlib import contextmanager, nullcontext
import signal
import fnmatch
import getpass
import hashlib
import json
import math
import random
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
# Work dirs live outside this repo so agents can't wander into the sources.
# Not /tmp: a restart wipes it mid-run.
WORK = Path(os.environ.get("BENCH_WORK", Path.home() / ".local/share/rally-3d-bench/work"))
RESULTS = ROOT / "results"
# Some agents are installed with npm into an isolated prefix (override with BENCH_NPM_BIN).
NPM_BIN = Path(os.environ.get("BENCH_NPM_BIN", Path.home() / ".local/share/rally-3d-bench/agents/node_modules/.bin"))
AGENT_INSTALLS = [NPM_BIN.parent.parent, Path.home() / ".local/share/uv/tools/aider-chat"]


def home(agent):
    """A fresh, empty config home per agent: nobody gets the user's personal
    settings, plugins, MCP servers or memories."""
    d = WORK / ".homes" / agent
    d.mkdir(parents=True, exist_ok=True)
    return d


def source_files(wd):
    """Non-test source files (Aider needs the files it may edit named up front)."""
    out = []
    for p in sorted(wd.rglob("*")):
        rel = p.relative_to(wd).as_posix()
        if p.is_file() and not rel.startswith((".git/", "target/", ".venv/")) and not TEST_RE.search(rel if "/" in rel else p.name) \
                and p.suffix in (".py", ".go", ".rs", ".js", ".ts", ".toml", ".mod", ".json", ".md", ".csv", ".txt") and p.stat().st_size < 200_000:
            out.append(rel)
    return out


# --api: every agent talks to one OpenAI/Anthropic-compatible proxy (rally-3d/proxy.sh)
# instead of the local Ollama server.
API = None
API_KEY = "sk-bench-local"
# --meter: route each run through rally-3d/meter.py under its own label, so every
# agent's token use is counted the same way.
METER = None
METER_LOG = None


def hosted_cmd(agent, model, prompt, ctx, wd):
    """Commands for a hosted model behind the LiteLLM proxy at API."""
    v1 = f"{API}/v1"
    if agent == "opencode":
        cfg = home("opencode") / "opencode.json"
        cfg.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench", "options": {"baseURL": v1, "apiKey": API_KEY},
                                   "models": {model: {"name": model, "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
        }))
        return ["opencode", "run", "--standalone", "--auto", "-m", f"bench/{model}", prompt], {
            "OPENCODE_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(home("opencode") / "xdg"), "XDG_DATA_HOME": str(home("opencode") / "data")}
    if agent == "claude":
        env = {"ANTHROPIC_BASE_URL": API, "ANTHROPIC_AUTH_TOKEN": API_KEY, "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": model,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
               "CLAUDE_CODE_SUBAGENT_MODEL": model, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CONFIG_DIR": str(home("claude"))}
        return ["claude", "-p", "--model", model, "--dangerously-skip-permissions", "--output-format", "json", prompt], env
    if agent == "codex":
        (home("codex") / "config.toml").write_text(
            f'model_provider = "bench"\n[model_providers.bench]\nname = "bench"\nbase_url = "{v1}"\nenv_key = "BENCH_API_KEY"\nwire_api = "responses"\n')
        return ["codex", "exec", "-m", model, "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", prompt], {
            "CODEX_HOME": str(home("codex")), "BENCH_API_KEY": API_KEY}
    if agent == "aider":
        files = source_files(wd) if wd else []
        return ["aider", "--model", f"openai/{model}", "--yes-always", "--no-auto-commits", "--no-check-update", "--analytics-disable",
                "--no-show-model-warnings", "--no-pretty", "--no-stream", "--no-gitignore", "--message", prompt, *files], {
            "OPENAI_API_BASE": v1, "OPENAI_API_KEY": API_KEY, "AIDER_HOME": str(home("aider"))}
    if agent == "goose":
        return ["goose", "run", "--no-session", "--quiet", "-t", prompt], {
            "GOOSE_PROVIDER": "openai", "GOOSE_MODEL": model, "OPENAI_HOST": API, "OPENAI_API_KEY": API_KEY, "GOOSE_MODE": "auto",
            "GOOSE_DISABLE_KEYRING": "1", "XDG_CONFIG_HOME": str(home("goose") / "xdg"), "XDG_DATA_HOME": str(home("goose") / "data"),
            "XDG_STATE_HOME": str(home("goose") / "state")}
    nb = NPM_BIN
    if agent == "pi":
        d = home("pi")
        (d / "models.json").write_text(json.dumps({"providers": {"bench": {
            "baseUrl": v1, "api": "openai-completions", "apiKey": API_KEY,
            "models": [{"id": model, "contextWindow": ctx}]}}}))
        return [str(nb / "pi"), "-p", "--no-session", "--provider", "bench", "--model", model, prompt], {"PI_CODING_AGENT_DIR": str(d)}
    if agent == "crush":
        xdg = home("crush") / "xdg"
        (xdg / "crush").mkdir(parents=True, exist_ok=True)
        (xdg / "crush" / "crush.json").write_text(json.dumps({
            "providers": {"bench": {"type": "openai", "base_url": v1, "api_key": API_KEY,
                                    "models": [{"id": model, "name": model, "context_window": ctx, "default_max_tokens": 8192}]}},
            "models": {"large": {"model": model, "provider": "bench"}, "small": {"model": model, "provider": "bench"}},
            "options": {"disable_metrics": True}}))
        tmp = home("crush") / "tmp"
        tmp.mkdir(exist_ok=True)
        return [str(nb / "crush"), "run", "--quiet", "-D", str(home("crush") / "data"), "-m", f"bench/{model}", prompt], {
            "XDG_CONFIG_HOME": str(xdg), "XDG_DATA_HOME": str(home("crush") / "data"), "TMPDIR": str(tmp), "CRUSH_DISABLE_METRICS": "1"}
    if agent == "qwen":
        h = home("qwen")
        (h / ".qwen").mkdir(exist_ok=True)
        (h / ".qwen" / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "openai"}}, "usageStatisticsEnabled": False}))
        return [str(nb / "qwen"), "--yolo", "-m", model, prompt], {
            "HOME": str(h), "OPENAI_API_KEY": API_KEY, "OPENAI_BASE_URL": v1, "OPENAI_MODEL": model}
    if agent == "forge":
        h = home("forge")
        return [str(nb / "forge"), "-p", prompt], {
            "HOME": str(h), "OPENAI_API_KEY": API_KEY, "OPENAI_URL": v1, "FORGE_MODEL": model}
    if agent == "kilo":
        cfg = home("kilo") / "kilo.json"
        cfg.write_text(json.dumps({
            "$schema": "https://app.kilo.ai/config.json",
            "provider": {"bench": {"npm": "@ai-sdk/openai-compatible", "name": "Bench", "options": {"baseURL": v1, "apiKey": API_KEY},
                                   "models": {model: {"name": model, "tools": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
        }))
        return [str(nb / "kilo"), "run", "--auto", "-m", f"bench/{model}", prompt], {
            "KILO_CONFIG": str(cfg), "OPENCODE_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(home("kilo") / "xdg"), "XDG_DATA_HOME": str(home("kilo") / "data")}
    raise SystemExit(f"agent {agent} has no hosted (--api) configuration")


def agent_cmd(agent, model, prompt, ctx, wd=None):
    if API:
        return hosted_cmd(agent, model, prompt, ctx, wd)
    if agent == "opencode":
        cfg = home("opencode") / "opencode.json"
        cfg.write_text(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "name": "Ollama",
                                    "options": {"baseURL": "http://localhost:11434/v1"},
                                    "models": {model: {"name": model, "tools": True, "reasoning": True}}}},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
        }))
        return ["opencode", "run", "--standalone", "--auto", "-m", f"ollama/{model}", prompt], {
            "OPENCODE_CONFIG": str(cfg), "XDG_CONFIG_HOME": str(home("opencode") / "xdg"), "XDG_DATA_HOME": str(home("opencode") / "data")}
    if agent == "claude":
        env = {
            "ANTHROPIC_BASE_URL": "http://localhost:11434",
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CONFIG_DIR": str(home("claude")),
        }
        return ["claude", "-p", "--model", model, "--dangerously-skip-permissions", "--output-format", "json", prompt], env
    if agent == "codex":
        # Codex's local mode looks the model up by exact tag (and tries to pull it otherwise).
        return ["codex", "exec", "--oss", "--local-provider", "ollama", "-m", model if ":" in model else f"{model}:latest",
                "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", prompt], {"CODEX_HOME": str(home("codex"))}
    if agent == "aider":
        settings = home("aider") / "model-settings.yml"
        settings.write_text(f"- name: ollama_chat/{model}\n  extra_params:\n    num_ctx: {ctx}\n")
        files = source_files(wd) if wd else []
        return ["aider", "--model", f"ollama_chat/{model}", "--model-settings-file", str(settings), "--yes-always",
                "--no-auto-commits", "--no-check-update", "--analytics-disable", "--no-show-model-warnings",
                "--no-pretty", "--no-stream", "--no-gitignore", "--message", prompt, *files], {
            "OLLAMA_API_BASE": "http://127.0.0.1:11434", "AIDER_HOME": str(home("aider"))}
    if agent == "goose":
        return ["goose", "run", "--no-session", "--quiet", "-t", prompt], {
            "GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": model, "OLLAMA_HOST": "http://localhost:11434",
            "GOOSE_MODE": "auto", "GOOSE_DISABLE_KEYRING": "1", "XDG_CONFIG_HOME": str(home("goose") / "xdg"),
            "XDG_DATA_HOME": str(home("goose") / "data"), "XDG_STATE_HOME": str(home("goose") / "state")}
    raise SystemExit(f"unknown agent {agent}")


TEST_RE = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$|^tests?/)")


def test_files(wd):
    """Hashes of test files (to detect an agent editing or deleting tests)."""
    out = {}
    for p in wd.rglob("*"):
        rel = p.relative_to(wd).as_posix()
        if p.is_file() and not rel.startswith((".git/", ".venv/")) and "node_modules" not in rel and TEST_RE.search(rel if "/" in rel else p.name):
            out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


OUT_OF_BUDGET = re.compile(r"402 Payment Required|Insufficient Balance|budget exhausted|BudgetExceeded|Budget has been exceeded|exceeded your current quota|insufficient_quota", re.I)


def metered(label):
    """Provider-reported usage for one run, from the meter log (None without --meter)."""
    if not label:
        return None
    time.sleep(0.5)  # let the meter finish writing the last response
    from meter import totals
    return totals(METER_LOG).get(label, {"requests": 0, "input": 0, "output": 0, "cached": 0})


# --------------------------------------------------------------------- tiers
# Tasks in three difficulty bands. The core band is small, self-contained katas
# that a strong model solves nearly every time (a ceiling check, not a
# differentiator); hard is real GitHub issues (SWE-bench Lite) where models
# actually diverge. A good suite mixes the two so it can show both "does it work
# at all" and "how much better is it". "beyond" is coding with an honesty twist:
# hidden-parameter tests that catch hardcoding and faked "done", simulations and
# solvers, and impossible tasks (trap-imp-*) where the pass is saying so.
# "showcase" (data analysis, root-causing, docs/config work) produces answers
# rather than code: it is opt-in and never part of the headline coding score.
TIERS = ["core", "medium", "hard", "beyond", "showcase"]
CODING_TIERS = ["core", "medium", "hard", "beyond"]
BEYOND = ("coding-", "math-", "physics-", "trap-imp-")
SHOWCASE = ("data-", "analysis-", "nontrad-")
# Price per token (USD), matching proxy.sh, so an estimate needs no live call.
# DeepSeek V4.1 Flash / V4 Pro are listed at off-peak rates (peak is 2x).
PRICES = {
    "deepseek/deepseek-flash": (0.15e-6, 0.60e-6),
    "deepseek/deepseek-v4-pro": (0.66e-6, 1.98e-6),
    "openrouter/qwen/qwen3.5-9b": (0.10e-6, 0.15e-6),
}


def tier_of(task):
    """Difficulty band from the task name; anything hand-written is 'medium'."""
    if task.startswith("swe-"):
        return "hard"
    if task.startswith(SHOWCASE):
        return "showcase"
    if task.startswith(BEYOND):
        return "beyond"
    if task.startswith(("poly-", "trap-")):
        return "core"
    return "medium"


def select_tasks(tiers, patterns):
    """Tasks in the requested tiers, narrowed by comma-separated name globs."""
    tasks = sorted(p.name for p in TASKS.iterdir() if p.is_dir())
    tasks = [t for t in tasks if tier_of(t) in tiers]
    if patterns:
        tasks = [t for t in tasks if any(fnmatch.fnmatchcase(t, p) for p in patterns.split(","))]
    return tasks


def hms(secs):
    """Seconds as H:MM:SS, with hours past 24 (a full suite can run for days)."""
    secs = int(secs)
    return f"{secs // 3600}:{secs % 3600 // 60:02}:{secs % 60:02}"


def estimate(agents, tiers, repeat, price, refs):
    """Project cost and wall time for a run, from recorded results.

    Token counts are model-agnostic, so they are averaged per agent per tier
    from older results and then priced with `price` for whatever model you are
    about to run. Seconds come from the same runs. An agent or tier with no
    history falls back to the cross-agent mean for that tier.
    """
    import statistics as st

    seen = {}  # (agent, tier) -> [[in, out], ...]
    secs = {}  # (agent, tier) -> [seconds]
    for f in refs:
        for r in json.load(open(f)).get("results", []):
            u = r.get("usage") or {}
            key = (r["agent"], tier_of(r["task"]))
            seen.setdefault(key, []).append([u.get("input", 0), u.get("output", 0)])
            secs.setdefault(key, []).append(r["seconds"])

    def mean(pairs):
        return (st.mean([p[0] for p in pairs]), st.mean([p[1] for p in pairs])) if pairs else None

    def usage_avg(t):
        """Cross-agent mean (input, output) for a tier, or None if unseen."""
        return mean([x for (a, tt), v in seen.items() if tt == t for x in v])

    def secs_avg(t):
        vals = [x for (a, tt), v in secs.items() if tt == t for x in v]
        return st.mean(vals) if vals else 0.0

    counts = {t: sum(1 for x in select_tasks([t], None)) for t in tiers}
    rows = []
    for a in agents:
        tin = tout = tsec = 0.0
        for t in tiers:
            m = mean(seen.get((a, t), [])) or usage_avg(t)
            s = st.mean(secs[(a, t)]) if (a, t) in secs else secs_avg(t)
            n = counts[t] * repeat
            if m:
                tin += m[0] * n
                tout += m[1] * n
            tsec += s * n
        rows.append((a, tin, tout, tin * price[0] + tout * price[1], tsec))
    return counts, rows


# --offline: agents may reach only localhost (the model proxy), and can't read
# anything that holds answers: task folders (hidden tests, gold data), cached
# upstream clones (their history has the fix), other runs, this repo.
OFFLINE = False


def require_offline_platform():
    if sys.platform == "darwin":
        return
    if sys.platform.startswith("linux") and shutil.which("bwrap") and shutil.which("socat"):
        return
    sys.exit("--offline needs macOS (sandbox-exec), or Linux with bubblewrap and socat installed")


def offline_hidden():
    """What agents may not read: the bench's own data (other agents' and shards'
    workspaces, past runs), and package caches that could hold a newer release
    with the fix."""
    return [ROOT.parent, Path.home() / ".cache/rally-3d-bench", Path.home() / ".local/share/rally-3d-bench", WORK,
            Path.home() / "Library/Caches/pip", Path.home() / ".cache/pip", Path.home() / ".cache/uv",
            Path.home() / "Library/Caches/uv"]


def python_package_dirs():
    """Installed-package directories (Linux) outside any task venv: a newer pytest
    or sympy there already contains the fix. The agents' own installs stay."""
    import glob
    import site
    pats = ["/usr/lib/python3*/site-packages", "/usr/lib/python3*/dist-packages", "/usr/lib/python3/dist-packages",
            "/usr/local/lib/python3*/site-packages", "/usr/local/lib/python3*/dist-packages",
            str(Path.home() / ".local/lib/python3*/site-packages"),
            str(Path.home() / ".local/share/uv/python/*/lib/python3*/site-packages"),
            str(Path.home() / ".pyenv/versions/*/lib/python3*/site-packages"),
            "/opt/hostedtoolcache/Python/*/*/lib/python3*/site-packages"]
    found = {Path(p).resolve() for pat in pats for p in glob.glob(pat)}
    found |= {Path(p).resolve() for p in [*site.getsitepackages(), site.getusersitepackages()] if Path(p).is_dir()}
    keep = [Path(p).resolve() for p in AGENT_INSTALLS]
    return sorted(p for p in found if p.is_dir() and not any(p == k or k in p.parents for k in keep))


def offline_linux(wd, agent_home, cmd, ports, sock_dir, executable=None):
    """The Linux counterpart of offline_profile, with bubblewrap: no network
    except the listed localhost ports (bridged in over Unix sockets), hidden
    paths covered by empty mounts, the workspace and config home re-exposed."""
    # Own PID namespace: when the agent exits, everything it (and the bridges) left behind goes too.
    args = ["bwrap", "--die-with-parent", "--unshare-net", "--unshare-pid", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for p in [*offline_hidden(), *python_package_dirs()]:
        if Path(p).exists():
            args += ["--tmpfs", str(Path(p).resolve())]
    # Later mounts win: what the agent needs, even under hidden paths.
    for p in [*AGENT_INSTALLS, wd, run_tmp(wd), agent_home, sock_dir]:
        if Path(p).exists():
            args += ["--bind", str(Path(p).resolve()), str(Path(p).resolve())]
    if executable is not None:
        args += ["--ro-bind", str(executable), str(executable)]
    bridges = "".join(f"socat TCP-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:{sock_dir}/{port}.sock </dev/null >/dev/null 2>&1 & " for port in ports)
    return args + ["--chdir", str(Path(wd).resolve()), "--", "sh", "-c", bridges + 'sleep 0.3; exec "$@"', "sh", *cmd]


def offline_ports():
    """Localhost ports agents talk to: the proxy and meter, or Ollama."""
    from urllib.parse import urlparse
    urls = [u for u in (API, METER) if u] or ["http://localhost:11434"]
    return sorted({urlparse(u).port or 80 for u in urls})


def run_tmp(wd):
    """The run's private TMPDIR, next to its workspace."""
    wd = Path(wd)
    return wd.with_name(wd.name + ".tmp")


def offline_profile(wd, agent_home, executable=None):
    q = lambda p: json.dumps(str(Path(p).resolve()), ensure_ascii=False)
    # The bench's own data (other agents' and shards' workspaces, past runs),
    # and package caches that could hold a newer release with the fix.
    hidden = offline_hidden()
    rules = ["(version 1)", "(allow default)",
             '(deny network-outbound (remote ip "*:*"))', '(allow network-outbound (remote ip "localhost:*"))',
             "(allow network-outbound (remote unix-socket))"]
    rules += [f'(deny file-read-data file-write* (subpath {q(p)}))' for p in hidden]
    # Every installed Python package outside the task's own venv: a newer
    # pytest or sympy elsewhere on the machine already contains the fix.
    rules.append('(deny file-read-data file-write* (regex #"/(site|dist)-packages(/|$)"))')
    # The agents' own installs (npm prefix, Aider's uv tool venv), read-only.
    rules += [f'(allow file-read* file-read-data (subpath {q(p)}))' for p in AGENT_INSTALLS]
    # Later rules win, but only over a rule for the same operation: an allow of
    # file-read* doesn't undo a deny of file-read-data, so name it too. Only
    # the current workspace and config home are writable.
    rules += [f'(allow file-read* file-read-data file-write* (subpath {q(p)}))' for p in (wd, run_tmp(wd), agent_home)]
    # getcwd needs to list each parent directory (macOS 26); listing a hidden
    # directory shows names only, not what's in the files.
    parents = {a for p in (wd, agent_home) for a in Path(p).resolve().parents}
    rules += [f'(allow file-read-data (literal {q(a)}))' for a in sorted(parents)]
    if executable is not None:
        rules.append(f'(allow file-read* file-read-data (literal {q(executable)}))')
    return "\n".join(rules)


@contextmanager
def offline(agent, cmd, wd):
    require_offline_platform()
    agent_home = home(agent)
    executable = None
    if sys.platform == "darwin":
        yield ["sandbox-exec", "-p", offline_profile(wd, agent_home, executable), *cmd]
    else:
        # Each allowed port gets a Unix socket outside, bridged to localhost.
        import tempfile
        sock_dir = Path(tempfile.mkdtemp(prefix="bench-net-"))
        ports = offline_ports()
        bridges = [subprocess.Popen(["socat", f"UNIX-LISTEN:{sock_dir}/{port}.sock,fork,unlink-early", f"TCP:127.0.0.1:{port}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for port in ports]
        try:
            time.sleep(0.2)
            yield offline_linux(wd, agent_home, cmd, ports, sock_dir, executable)
        finally:
            for b in bridges:
                b.kill()
            shutil.rmtree(sock_dir, ignore_errors=True)


def kill_process_group(p):
    """Clean up children still in the original group, not detached sessions."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_leftovers(wd):
    """Kill processes still working in `wd` after the agent is gone: test
    runs an agent started in their own process group outlive a SIGKILL of the
    agent's group, and an orphaned test suite can run for hours and load the
    machine for every later task."""
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "pn"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return
    pid, root = None, str(wd)
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid and pid != os.getpid() and (line[1:] == root or line[1:].startswith(root + os.sep)):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def reap(p, grace=10):
    """Stop a run that overran its deadline and reap it — but never wait on a
    process that refuses to die.

    A process blocked in a kernel call (a stuck network read, say) does not
    become reapable the moment it is sent SIGKILL. Waiting it out without a
    bound once recorded 49 minutes against a 5-minute limit and stalled every
    later task in the shard.
    """
    kill_process_group(p)
    if p.poll() is None:
        try:
            p.kill()
        except (ProcessLookupError, PermissionError):
            pass
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass  # leave it; it is already detached and will be reaped by init


def run_check(cmd, wd, env, timeout=300):
    """Grade a task, with a deadline and cleanup of its original process group."""
    p = subprocess.Popen(cmd, cwd=wd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)
    except subprocess.TimeoutExpired:
        kill_process_group(p)
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return subprocess.CompletedProcess(cmd, 124, out, (err or "") + f"\n[check timed out after {timeout}s]")
    finally:
        reap(p)


def deepseek_balance():
    """Remaining USD balance on the DeepSeek account (None if unknown)."""
    import urllib.request
    try:
        key = (Path.home() / ".config/rally-3d-bench/deepseek_key").read_text().strip()
        req = urllib.request.Request("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
        return sum(float(b["total_balance"]) for b in data["balance_infos"] if b["currency"] == "USD")
    except Exception:
        return None


def prepare_task(tdir, wd):
    """Set up the repository before starting the agent's clock."""
    hook = tdir / "setup.sh"
    # Setup occasionally fails transiently (e.g. ensurepip), so retry once from a clean copy.
    for attempt in range(2):
        shutil.rmtree(wd, ignore_errors=True)
        shutil.copytree(tdir / "files", wd)
        if not hook.exists():
            return
        result = run_check(["nice", "-n", "19", "bash", str(hook)], wd, dict(os.environ), 600)
        if not result.returncode:
            return
    raise RuntimeError(f"Setup failed for {tdir.name}: {result.stdout[-2000:]}{result.stderr[-2000:]}")


def run_task(agent, task, model, ctx, timeout, rep):
    if OFFLINE:
        require_offline_platform()
    tdir = TASKS / task
    prompt = (tdir / "prompt.txt").read_text().strip()
    wd = WORK / agent / f"{task}-{rep}"
    if wd.exists():
        shutil.rmtree(wd)
    prepare_task(tdir, wd)
    subprocess.run(["git", "init", "-q"], cwd=wd)
    subprocess.run(["git", "add", "-A"], cwd=wd)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=bench", "commit", "-qm", "init"], cwd=wd)
    tests_before = test_files(wd)
    global API, METER, METER_LOG
    label = None
    if METER:
        import uuid
        label = f"{agent}~{task}~{rep}~{uuid.uuid4().hex[:8]}"
        saved_api, API = API, f"{METER}/{label}"
    cmd, extra_env = agent_cmd(agent, model, prompt, ctx, wd)
    if METER:
        API = saved_api
        if agent == "goose":
            # Goose takes a host plus a separate path.
            extra_env["OPENAI_HOST"] = METER
            extra_env["OPENAI_BASE_PATH"] = f"{label}/v1/chat/completions"
    env = dict(os.environ, **extra_env)
    if (wd / ".venv/bin").exists():
        env["VIRTUAL_ENV"] = str(wd / ".venv")
        env["PATH"] = str(wd / ".venv/bin") + os.pathsep + env["PATH"]
    env["PWD"] = str(wd)  # some agents resolve their project from $PWD, not the process cwd
    # A private temp dir per run: pytest keeps `pytest-of-<user>` under it, and
    # one shared between concurrent and leftover runs breaks and stalls tests.
    tmp = run_tmp(wd)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    env["TMPDIR"] = str(tmp)
    if agent == "claude":
        env.pop("ANTHROPIC_API_KEY", None)
    if API:
        # Hosted runs must not leak real provider keys from the environment.
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY"):
            if k not in extra_env:
                env.pop(k, None)
    out_file = WORK / agent / f"{task}-{rep}.out"
    t0 = time.time()
    timed_out = False
    with offline(agent, cmd, wd) if OFFLINE else nullcontext(cmd) as cmd, open(out_file, "w") as f:
        # Detached children that create their own process group are not covered.
        p = subprocess.Popen(cmd, cwd=wd, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = -1
            timed_out = True
        # Stop the clock at the deadline, before cleanup. Reaping a process that
        # is slow to die is not the task's fault and must not be billed to it.
        elapsed = time.time() - t0
        reap(p)
        kill_leftovers(wd)
        # Let an agent's own cleanup after a kill (put-back guards)
        # land before the diff is taken.
        time.sleep(1)
    ended = time.time()
    # Before the check runs: it may apply the hidden tests to the workspace.
    patch = workspace_diff(wd)
    check_env = dict(os.environ, OUTPUT_FILE=str(out_file))
    envf = tdir / "env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                check_env[k.strip()] = v.strip()
    after = test_files(wd)
    chk = run_check(["bash", str(tdir / "check.sh")], wd, check_env, timeout=600 if task.startswith("swe-") else 300)
    passed = chk.returncode == 0
    tampered = sorted(f for f, h in tests_before.items() if after.get(f) != h)
    # The agent finished normally (believing it was done) but the task isn't solved.
    false_done = code == 0 and not timed_out and not passed
    tokens = None
    if agent == "claude":
        try:
            last = [l for l in out_file.read_text().splitlines() if l.strip().startswith("{")][-1]
            j = json.loads(last)
            u = j.get("usage", {})
            tokens = {"in": u.get("input_tokens"), "out": u.get("output_tokens")}
        except Exception:
            pass
    try:
        sessions = session_files(home(agent), t0)
    except Exception:
        sessions = []
    evidence = keep_evidence(agent, task, rep, out_file, patch, chk, sessions)
    # SWE-bench workspaces (repo plus venv) are hundreds of MB each and the
    # evidence is kept: free the space unless asked to keep them.
    if ARTIFACTS is not None and "error" not in evidence and not os.environ.get("BENCH_KEEP_WORK"):
        shutil.rmtree(wd, ignore_errors=True)
    subprocess.run(["chmod", "-R", "u+rwx", str(tmp)], capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "agent": agent, "task": task, "rep": rep, "passed": passed, "seconds": round(elapsed, 1),
        "exit_code": code, "timed_out": timed_out, "check_output": chk.stdout[-800:] + chk.stderr[-400:],
        "tokens": tokens, "tampered": tampered, "false_done": false_done,
        "usage": metered(label),
        # Matches this run's lines in the meter log (every model call it made).
        "meter_label": label,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended)),
        "evidence": evidence,
    }


# Evidence kept next to each result file (results/<stem>/), so a skeptical
# reader can check every number instead of trusting the table: what ran
# (versions, binary and task hashes, the sandbox profile), what each agent did
# (its full output and the change it made) and what the grader printed.
ARTIFACTS = None

VERSION_CMDS = {
    "claude": ["claude", "--version"], "codex": ["codex", "--version"], "opencode": ["opencode", "--version"],
    "goose": ["goose", "--version"], "aider": ["aider", "--version"],
    "pi": [str(NPM_BIN / "pi"), "--version"], "crush": [str(NPM_BIN / "crush"), "--version"],
    "qwen": [str(NPM_BIN / "qwen"), "--version"], "kilo": [str(NPM_BIN / "kilo"), "--version"],
    "forge": [str(NPM_BIN / "forge"), "--version"],
}


def first_line(cmd, timeout=60):
    """A command's first line of output, or why it couldn't run."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        lines = (r.stdout.strip() or r.stderr.strip()).splitlines()
        return lines[0][:200] if lines else f"no output (exit {r.returncode})"
    except Exception as e:
        return f"unavailable: {e}"[:200]


def task_digest(task):
    """Hash of a task's definition (prompt, setup, check, test patch, starter files)."""
    h = hashlib.sha256()
    # Bytecode left by a local check run isn't part of the task.
    for f in sorted(p for p in (TASKS / task).rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(TASKS / task)).encode() + b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


def run_meta(args, tasks):
    """Everything needed to rerun this exactly, or to spot what differed."""
    import platform
    git = lambda *a: first_line(["git", "-C", str(ROOT.parent), *a])
    agents = args.agents.split(",")
    versions = {}
    for a in agents:
        if a in VERSION_CMDS:
            versions[a] = first_line(VERSION_CMDS[a])
    meta = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": [Path(sys.argv[0]).name, *sys.argv[1:]],
        "bench_commit": git("rev-parse", "HEAD"),
        # Uncommitted changes to the benchmark itself (tasks, runner, grader) at run time.
        "bench_dirty": bool(subprocess.run(["git", "-C", str(ROOT.parent), "status", "--porcelain", "--", ROOT.name, f":(exclude){ROOT.name}/results"],
                                           capture_output=True, text=True).stdout.strip()),
        "agent_versions": versions,
        "model_alias": args.model,
        # What the proxy actually sends the alias to (e.g. deepseek/deepseek-flash).
        "upstream_model": upstream_model(args.model) if API else None,
        "proxy": first_line(["litellm", "--version"]) if API else None,
        "platform": {"system": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(), "python": platform.python_version()},
        "offline_sandbox": OFFLINE,
        "timeout_s": args.timeout,
        "seed": args.seed,
        "repeat": args.repeat,
        "tasks": {t: task_digest(t) for t in tasks},
    }
    if OFFLINE:
        meta["offline_profile_sha256"] = hashlib.sha256(offline_profile(WORK / "PROFILE-WD", WORK / "PROFILE-HOME").encode()).hexdigest()
    return meta


def upstream_model(alias):
    """Ask the LiteLLM proxy which provider model the alias is routed to."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{API}/model/info", headers={"Authorization": f"Bearer {API_KEY}"})
        data = json.load(urllib.request.urlopen(req, timeout=15))
        return next((m["litellm_params"]["model"] for m in data.get("data", []) if m.get("model_name") == alias), None)
    except Exception as e:
        return f"unavailable: {e}"[:200]


def workspace_diff(wd, exclude=()):
    """The change an agent made, new files included, without touching the repo's
    own index. Diffs against the workspace's first commit (the bench's "init"),
    so an agent that commits its own work still shows all of it."""
    index = Path(wd) / ".git" / "bench-diff-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    git = lambda *a: subprocess.run(["git", *a], cwd=wd, env=env, capture_output=True, timeout=300)
    try:
        roots = git("rev-list", "--max-parents=0", "HEAD").stdout.split()
        if len(roots) != 1:
            return b"diff unavailable: the workspace's history no longer starts at the bench's init commit\n"
        base = roots[0].decode()
        for step in (("read-tree", base), ("add", "-A", "--", ".")):
            r = git(*step)
            if r.returncode:
                return f"diff unavailable: git {step[0]} failed: {r.stderr.decode(errors='replace')[:300]}\n".encode()
        # Build and test output isn't the agent's change (no task ships a .gitignore).
        noise = ["target", "node_modules", ".venv", "**/__pycache__", "**/*.pyc", ".pytest_cache", "**/.pytest_cache", ".mypy_cache", ".ruff_cache"]
        r = git("diff", "--cached", "--binary", base, "--", ".", *[f":(exclude){f}" for f in [*noise, *exclude]])
        return r.stdout if r.returncode == 0 else f"diff unavailable: {r.stderr.decode(errors='replace')[:300]}\n".encode()
    except Exception as e:
        return f"diff unavailable: {e}\n".encode()
    finally:
        index.unlink(missing_ok=True)


def session_files(agent_home, since):
    """The agent's own step-by-step transcripts written during this run (their
    sessions, Claude Code and Codex JSONL, ...): whatever session-like files in
    its home changed since the run began. Runs in a shard are sequential, so
    they're this run's."""
    found = []
    for p in Path(agent_home).rglob("*"):
        try:
            st = p.stat()
        except OSError:
            continue
        session = p.suffix == ".jsonl" or (p.suffix == ".json" and "sessions" in p.parts)
        if p.is_file() and session and st.st_mtime >= since and st.st_size < 20_000_000:
            found.append(p)
    return sorted(found)


def redact(data):
    """Published results show `~` for the home directory and no user name
    (pytest's temp dirs carry it), for str or bytes."""
    home, user = str(Path.home()), getpass.getuser()
    if isinstance(data, bytes):
        return data.replace(home.encode(), b"~").replace(f"pytest-of-{user}".encode(), b"pytest-of-user")
    return data.replace(home, "~").replace(f"pytest-of-{user}", "pytest-of-user")


def keep_evidence(agent, task, rep, out_file, patch, chk, sessions=()):
    """Copy a run's transcript, diff and full grader output next to the results."""
    if ARTIFACTS is None:
        return {}
    try:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        stem = f"{agent}~{task}~{rep}"
        files = {
            "transcript": (ARTIFACTS / f"{stem}.out", Path(out_file).read_bytes()),
            "patch": (ARTIFACTS / f"{stem}.patch", redact(patch)),
            "check": (ARTIFACTS / f"{stem}.check.txt", (chk.stdout + "\n--- stderr ---\n" + chk.stderr).encode(errors="replace")),
        }
        for i, src in enumerate(sessions):
            files[f"session{i}"] = (ARTIFACTS / f"{stem}.session{i}{src.suffix}", src.read_bytes())
        out = {}
        for k, (path, data) in files.items():
            path.write_bytes(data)
            out[k] = {"file": f"{ARTIFACTS.name}/{path.name}", "sha256": hashlib.sha256(data).hexdigest()}
        return out
    except Exception as e:
        return {"error": str(e)[:200]}


def wilson(k, n, z=1.96):
    """95% confidence interval for a pass rate (Wilson score)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def sign_test(wins, losses):
    """Exact two-sided sign test (McNemar's test for paired pass/fail): the
    chance of a split at least this lopsided if both agents were equally good."""
    n, k = wins + losses, min(wins, losses)
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired(results, ref):
    """Each agent against `ref` on the tasks both ran: per task, compare pass
    rates (over repeats) and count who did better."""
    rate = {}
    for r in results:
        rate.setdefault((r["agent"], r["task"]), []).append(bool(r["passed"]))
    rows = []
    for a in dict.fromkeys(r["agent"] for r in results):
        if a == ref:
            continue
        common = sorted(t for (x, t) in rate if x == a and (ref, t) in rate)
        mean = lambda xs: sum(xs) / len(xs)
        better = sum(mean(rate[(ref, t)]) > mean(rate[(a, t)]) for t in common)
        worse = sum(mean(rate[(ref, t)]) < mean(rate[(a, t)]) for t in common)
        rows.append((a, len(common), better, worse, sign_test(better, worse)))
    return rows


def report(files, ref=None):
    by_key = {}
    model = ctx = upstream = None
    for f in files:
        d = json.loads(Path(f).read_text())
        model, ctx = d.get("model"), d.get("ctx")
        upstream = (d.get("meta") or {}).get("upstream_model") or upstream
        # A run recorded twice (overlapping files) counts once.
        for r in d["results"]:
            by_key[(r["agent"], r["task"], r.get("rep", 0))] = r
    results = list(by_key.values())
    agents = list(dict.fromkeys(r["agent"] for r in results))
    ref = ref or (agents[0] if agents else "")
    tasks = sorted(set(r["task"] for r in results))
    served = f"`{upstream}` (as `{model}`) through one proxy" if upstream else f"`{model}`, same server"
    out = [f"Model: {served} for all agents · context {ctx}\n"]
    # Totals only over tasks every agent ran, so the rates share a denominator.
    complete = [t for t in tasks if all(any(r["agent"] == a and r["task"] == t for r in results) for a in agents)]
    left_out = sum(r["task"] not in complete for r in results)
    if left_out:
        out.append(f"Totals cover the {len(complete)} tasks every agent ran; {left_out} runs on other tasks appear only in the per-task table.\n")
    # Showcase tasks (answers, not code) never count toward the coding score.
    showcase = [t for t in complete if tier_of(t) == "showcase"]
    complete = [t for t in complete if t not in showcase]

    def totals(subset):
        out.append("| agent | solved | 95% CI | false \"done\" | edited existing tests | median time | total time | output tokens |")
        out.append("|---|---|---|---|---|---|---|---|")
        for a in agents:
            rs = [r for r in results if r["agent"] == a and r["task"] in subset]
            p = sum(r["passed"] for r in rs)
            tt = sum(r["seconds"] for r in rs)
            # Prefer the proxy meter's count (the same measurement for every agent)
            # over what an agent reports about itself.
            toks = [(r.get("usage") or {}).get("output") or (r.get("tokens") or {}).get("out") for r in rs]
            tok = f"{sum(toks):,}" if toks and all(t is not None for t in toks) else "n/a"
            fd = sum(bool(r.get("false_done")) for r in rs)
            tp = sum(bool(r.get("tampered")) for r in rs)
            lo, hi = wilson(p, len(rs))
            med = sorted(r["seconds"] for r in rs)[len(rs) // 2] if rs else 0
            out.append(f"| {a} | **{p}/{len(rs)}** ({100 * p / max(1, len(rs)):.0f}%) | {100 * lo:.0f}–{100 * hi:.0f}% | {fd} | {tp} | {med:.0f}s | {tt / 60:.1f} min | {tok} |")

    if complete or not showcase:
        totals(complete)
    if showcase:
        out.append(f"\nShowcase tier ({len(showcase)} tasks: data, analysis and docs/config answers), not part of the coding score above:\n")
        totals(showcase)
    if any(t.startswith("swe-") for t in complete):
        out.append("\n*False \"done\"*: the agent finished normally but the task failed. *Edited existing tests*: changed a test file the task "
                   "started with. On SWE-bench tasks that's usually an added regression test, and the grader restores the original "
                   "tests before checking, so it can't change a result; it signals cheating only on the test-weakening tasks.")
    out.append("\n| task | " + " | ".join(agents) + " |")
    out.append("|---|" + "---|" * len(agents))
    for t in tasks:
        row = [t]
        for a in agents:
            rs = [r for r in results if r["agent"] == a and r["task"] == t]
            if not rs:
                row.append("–")
                continue
            cells = []
            for r in rs:
                mark = "✅" if r["passed"] else ("⏱" if r["timed_out"] else "❌")
                if r.get("tampered"):
                    mark += "✂"
                cells.append(f"{mark} {r['seconds']:.0f}s")
            row.append(" / ".join(cells))
        out.append("| " + " | ".join(row) + " |")
    if ref in agents and len(agents) > 1:
        out.append(f"\nPaired against **{ref}** on the coding tasks both ran (sign test, two-sided; p ≥ 0.05 means the difference could be chance):\n")
        out.append(f"| agent | tasks | {ref} better | {ref} worse | p |")
        out.append("|---|---|---|---|---|")
        for a, n, better, worse, p in paired([r for r in results if tier_of(r["task"]) != "showcase"], ref):
            out.append(f"| {a} | {n} | {better} | {worse} | {p:.2f}{'' if p < 0.05 else ' (not significant)'} |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="opencode,claude,codex,goose,aider,qwen,crush")
    ap.add_argument("--model", default="bench-model")
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--tier", default=",".join(CODING_TIERS),
                    help="bands: core,medium,hard,beyond (the default, all coding) and the opt-in showcase")
    ap.add_argument("--estimate", action="store_true", help="print projected cost/time and exit; runs nothing")
    ap.add_argument("--estimate-ref", nargs="+", help="result JSONs to average tokens from (default: rally-3d/results/*.json)")
    ap.add_argument("--timeout", type=int, default=None, help="agent seconds (default 1800 for swe-*, 900 otherwise)")
    ap.add_argument("--meter", help="token meter URL (rally-3d/meter.py), e.g. http://localhost:4100")
    ap.add_argument("--meter-log", default=str(ROOT / "meter.jsonl"), help="the meter's log file")
    ap.add_argument("--min-balance", type=float, help="DeepSeek: stop when the account balance (USD) drops below this")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--api", help="use a hosted model through this proxy (e.g. http://localhost:4000, see rally-3d/proxy.sh)")
    ap.add_argument("--offline", action="store_true", help="agents can reach only localhost (macOS); no web lookups")
    ap.add_argument("--sanity", action="store_true", help="verify every check fails on the untouched task")
    ap.add_argument("--report", nargs="+", help="merge result JSON files into a markdown report")
    ap.add_argument("--report-ref", help="agent the report compares the others against (default: the first agent in the files)")
    args = ap.parse_args()
    global API, METER, METER_LOG, OFFLINE
    API = args.api
    METER, METER_LOG = args.meter, args.meter_log
    OFFLINE = args.offline
    if args.report:
        print(report(args.report, args.report_ref))
        return
    tiers = [t.strip() for t in args.tier.split(",") if t.strip()]
    bad = [t for t in tiers if t not in TIERS]
    if bad:
        sys.exit(f"unknown tier(s): {', '.join(bad)} (known: {', '.join(TIERS)})")
    if args.estimate:
        refs = args.estimate_ref or sorted(str(p) for p in RESULTS.glob("*.json"))
        refs = [r for r in refs if Path(r).exists()]
        if not refs:
            sys.exit("no result files to estimate from; pass --estimate-ref")
        price = PRICES.get(args.model)
        if not price:
            sys.exit(f"no price for {args.model}; add it to PRICES or pass --model with a known one")
        agents = args.agents.split(",")
        counts, rows = estimate(agents, tiers, args.repeat, price, refs)
        print(f"projected from {len(refs)} result file(s); model {args.model} @ ${price[0]*1e6:.3f}/${price[1]*1e6:.3f} per M tok")
        print("tiers: " + ", ".join(f"{t}={counts[t]}" for t in tiers)
              + f"  x{args.repeat} reps x {len(agents)} agents")
        print(f"\n{'agent':16}{'runs':>6}{'input':>12}{'output':>10}{'cost':>10}{'wall (1 shard)':>16}{'wall (2 shards)':>17}")
        per = {t: counts[t] * args.repeat for t in tiers}
        total_runs = sum(per.values())
        grand = 0.0
        for a, tin, tout, c, tsec in rows:
            grand += c
            print(f"{a:16}{total_runs:>6}{tin:>12,.0f}{tout:>10,.0f}{'$'+format(c, '.2f'):>10}"
                  f"{hms(tsec):>16}{hms(tsec / 2):>17}")
        print(f"\nTOTAL projected cost: ${grand:.2f}   (wall time assumes agents run serially; shards divide it)")
        return
    if OFFLINE:
        require_offline_platform()
    tasks = select_tasks(tiers, args.tasks)
    if args.sanity:
        ok = True
        for t in tasks:
            wd = WORK / "sanity" / t
            if wd.exists():
                shutil.rmtree(wd)
            prepare_task(TASKS / t, wd)
            empty = WORK / "sanity" / f"{t}.out"
            empty.write_text("")
            env = dict(os.environ, OUTPUT_FILE=str(empty))
            envf = TASKS / t / "env"
            if envf.exists():
                for line in envf.read_text().splitlines():
                    k, v = line.split("=", 1)
                    env[k] = v
            r = run_check(["bash", str(TASKS / t / "check.sh")], wd, env, timeout=600)
            status = "FAILS (good)" if r.returncode != 0 else "PASSES (bad: check is too weak)"
            ok &= r.returncode != 0
            print(f"{t:22} {status}")
        sys.exit(0 if ok else 1)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # pid in the name: parallel shards started in the same second must not share a file.
    res_file = RESULTS / f"{stamp}-{os.getpid()}.json"
    global ARTIFACTS
    ARTIFACTS = RESULTS / res_file.stem
    try:
        meta = run_meta(args, tasks)
    except Exception as e:  # never lose a run over its bookkeeping
        meta = {"error": str(e)[:300]}
    results = []
    # Interleave: each task runs every agent back to back in a shuffled order,
    # so model-cache warmth and machine load don't favor one agent.
    rng = random.Random(args.seed)
    plan = []
    for rep in range(args.repeat):
        order = tasks[:]
        rng.shuffle(order)
        for t in order:
            agents = args.agents.split(",")
            rng.shuffle(agents)
            plan.extend((a, t, rep) for a in agents)
    for agent, t, rep in plan:
        if args.min_balance is not None:
            bal = deepseek_balance()
            if bal is not None and bal < args.min_balance:
                print(f"\nStopped: DeepSeek balance ${bal:.2f} is below --min-balance ${args.min_balance:.2f}. "
                      f"{len(results)} completed runs kept in {res_file}.", flush=True)
                sys.exit(2)
        r = run_task(agent, t, args.model, args.ctx, args.timeout if args.timeout is not None else (1800 if t.startswith("swe-") else 900), rep)
        results.append(r)
        print(f"{agent:14} {t:22} {'PASS' if r['passed'] else 'FAIL'} {r['seconds']:7.1f}s"
              f"{' (timeout)' if r['timed_out'] else ''}", flush=True)
        res_file.write_text(redact(json.dumps({"model": args.model, "ctx": args.ctx, "offline": OFFLINE, "meta": meta, "results": results}, indent=2)))
        # Out of credit or quota: every later run would fail instantly, so stop
        # rather than record them as failures.
        out = (WORK / agent / f"{t}-{rep}.out").read_text(errors="ignore")[-20000:]
        if not r["passed"] and OUT_OF_BUDGET.search(out):
            results.pop()
            res_file.write_text(redact(json.dumps({"model": args.model, "ctx": args.ctx, "offline": OFFLINE, "meta": meta, "results": results, "aborted": "out of budget"}, indent=2)))
            print(f"\nStopped: the model provider reports no budget left ({OUT_OF_BUDGET.search(out).group(0)!r}). "
                  f"{len(results)} completed runs kept in {res_file}.", flush=True)
            sys.exit(2)
    # Summary
    print("\n| agent | passed | total time | mean time |")
    print("|---|---|---|---|")
    for agent in args.agents.split(","):
        rs = [r for r in results if r["agent"] == agent]
        p = sum(r["passed"] for r in rs)
        tt = sum(r["seconds"] for r in rs)
        print(f"| {agent} | {p}/{len(rs)} | {tt:.0f}s | {tt / max(1, len(rs)):.0f}s |")
    print(f"\nresults: {res_file}")


if __name__ == "__main__":
    main()
