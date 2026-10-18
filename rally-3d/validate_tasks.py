#!/usr/bin/env python3
"""Validate tasks that ship reference solutions, without running any agent.

A task may keep, next to files/ and check.sh:
  ref/            - an overlay that solves it: copied over the prepared workspace
  wrong/<name>/   - overlays that must NOT pass (guessed, hallucinated or faked work)
In an overlay, `_OUTPUT.txt` is not copied: it becomes the agent's captured
output ($OUTPUT_FILE), and `_DELETE` lists workspace paths to remove first.

Each task is checked three ways: untouched (must fail), ref (must pass) and
every wrong variant (must fail). Check time is reported, since checks should
stay well under a minute.

Usage: python3 rally-3d/validate_tasks.py ['data-*,math-*']
"""
import fnmatch
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import run


def overlay(src, wd):
    """Apply a solution overlay; returns the captured-output text."""
    out = ""
    dele = src / "_DELETE"
    if dele.exists():
        for rel in dele.read_text().split():
            p = wd / rel
            shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if rel.as_posix() in ("_OUTPUT.txt", "_DELETE"):
            if rel.as_posix() == "_OUTPUT.txt":
                out = p.read_text()
            continue
        dst = wd / rel
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    return out


def grade(task, src, root):
    """Prepare the task like run_task does, apply an overlay, and run check.sh."""
    tdir = run.TASKS / task
    wd = root / task
    run.prepare_task(tdir, wd)
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=b@b", "-c", "user.name=bench", "commit", "-qm", "init"]):
        run.subprocess.run(["git", *args], cwd=wd, capture_output=True)
    out = overlay(src, wd) if src else ""
    of = root / f"{task}.out"
    of.write_text(out)
    env = dict(os.environ, OUTPUT_FILE=str(of))
    envf = tdir / "env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    t0 = time.time()
    r = run.run_check(["bash", str(tdir / "check.sh")], wd, env, timeout=300)
    return r, time.time() - t0


def main():
    pats = sys.argv[1].split(",") if len(sys.argv) > 1 else ["*"]
    tasks = sorted(p.name for p in run.TASKS.iterdir()
                   if (p / "ref").is_dir() and any(fnmatch.fnmatchcase(p.name, x) for x in pats))
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for t in tasks:
            tdir = run.TASKS / t
            cases = [("untouched", None, False), ("ref", tdir / "ref", True)]
            cases += [(f"wrong/{w.name}", w, False) for w in sorted((tdir / "wrong").glob("*")) if w.is_dir()]
            for label, src, want in cases:
                r, secs = grade(t, src, root / label.replace("/", "_"))
                ok = (r.returncode == 0) == want
                bad += not ok
                print(f"{t:34} {label:28} {'ok ' if ok else 'BAD'} exit={r.returncode:<3} {secs:5.1f}s", flush=True)
                if not ok or secs > 60:
                    print("    " + (r.stdout[-600:] + r.stderr[-600:]).replace("\n", "\n    "))
    print(f"\n{len(tasks)} tasks, {bad} unexpected result(s)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
