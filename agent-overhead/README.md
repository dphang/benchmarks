# agent-overhead

How much does a coding agent cost *on its own* — the CPU and memory it spends per
turn before the model even replies? This measures each agent's own code path
(parsing the response, running its tool, rebuilding context), with the model
factored out, so what's left is the agent's intrinsic overhead.

![Coding agent overhead: CPU and memory per turn](overhead.png)

## How it works

A coding agent's turn is *model latency* + *tool work* + *the agent's own loop*. The
first runs on separate hardware; the last is what this benchmark isolates. We replace
the model with an **instant mock** (`stressmock.py`) that answers in ~0 ms, then drive
each agent through the **same 40-turn session** where every turn reads a growing file
(a realistic long, high-context session). With model time removed, what we measure is
the agent itself:

- **CPU per turn** — process-tree CPU (user+sys) ÷ turns.
- **Peak memory** — max resident set of the whole agent process tree over the run.

Nothing is instrumented or rebuilt — CPU and RSS are read from the OS (`/usr/bin/time`,
`/proc`, `ps`) from outside each process, identically for every runtime.

The mock is the interesting part: it speaks OpenAI chat / Anthropic messages / OpenAI
Responses, inspects each agent's advertised tools and calls a safe read tool so the
agent actually executes and comes back, and it handles the quirks real agents need to
run headless — streaming-chunk envelopes, structured-output (`response_format`)
side-calls, conversation compaction/summary requests, and terminal/completion tools —
so agents complete a clean loop instead of erroring or looping.

## Results

### Desktop (Apple silicon, macOS) — 10 reps × 40 turns, latest versions, Oct 2026

| agent | runtime | CPU ms/turn | peak RSS (MB) |
|---|---|--:|--:|
| forge | Rust | 11.5 | 105 |
| pi | Node | 12.2 | 193 |
| crush | Go | 19.3 | 188 |
| claude | Node | 20.7 | 369 |
| goose | Rust | 26.9 | 183 |
| qwen | Node | 44.5 | 263 |
| codex | Rust | 46.9 | 150 |
| cline | Node | 56.5 | 858 |
| opencode | Bun | 61.3 | 578 |
| aider\* | Python | 126.0 | 256 |
| kilo | Node | 176.8 | 1182 |

\* aider has no tool-call loop (edit-block), so its number is per-*message*, not the
same tool-loop turn as the others.

### Raspberry Pi 5 (4 GB, aarch64) — 3 reps × 40 turns

See [`results/raspberry-pi-5.txt`](results/raspberry-pi-5.txt). Headline: native
(Rust/Go) agents hold their desktop numbers on the Pi; interpreted/VM agents (Node/
Bun) degrade more, because their per-turn cost is engine work (JIT, GC, JSON) bound by
CPU speed and memory bandwidth — both far weaker on a small board. kilo's tree-sitter
WASM is 3.3× worse on ARM (~0.6 s/turn).

## What it shows

- **The spread is large and it tracks runtime architecture.** Native-compiled agents
  (Rust/Go) are lean; VM/interpreted agents carry their engine's weight. Per-turn CPU
  ranges ~11→177 ms and memory ~105 MB→1.2 GB across the field.
- **Per-turn cost tracks the current tool-output size, not accumulated history** — it
  stays flat across a long session (an agent that compacts keeps it flat), so this is
  a stable overhead measurement, not a function of how long you've been running.
- **The gap widens on weak hardware.** On a Raspberry Pi the native agents are
  unchanged while the JS/WASM ones degrade, so "which agent is leanest" is not stable
  across platforms for interpreted runtimes.

## Caveats

This is an **overhead** benchmark, not end-to-end task time. It deliberately excludes
the model (which dominates wall-clock) and uses a light, uniform tool workload (reading
one file per turn), so it does not capture the CPU of heavy tools an agent shells out
to (compilers, test suites) on a real task. The relative *ordering* has been
cross-checked against a real-model run and holds; absolute numbers would be higher for
everyone on a heavier task.

## Running it

Needs `python3` and the agents installed (on `PATH` or at known paths; see the configs
at the top of `runner.py`). The mock is model-free, so no API keys or credits are used.

```sh
# Linux (CPU via /proc), one agent at a time, with a RAM guard for small devices:
python3 runner.py stressmock.py goose crush pi kilo --reps 3 --turns 40 --blob-kb 50
```

`runner.py` samples the process tree every ~80 ms and kills an agent if free memory
drops below `--min-free-mb` (default 150), so it is safe to run on a 4 GB board. The
desktop numbers above were produced by an equivalent macOS runner using
`/usr/bin/time -l`; `runner.py` reproduces the same methodology on Linux.
