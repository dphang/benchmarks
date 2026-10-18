# rally-3d

A small benchmark that takes a real 3D feature from [Rally](https://rally.danielphang.com)
— the LiDAR court view that shades pickleball courts from the real sun — and asks a
coding agent to rebuild it.

Rally shows each venue as a 3D scene built from LiDAR (ground, tree crowns,
buildings, courts) and traces how much of the courts the trees and buildings shade
through the day. That feature is turned into two tasks.

## The two tasks

Same feature, two ways of asking for it:

| task | what the agent gets | graded on |
|---|---|---|
| **`rally-3d-shade`** ("port") | the exact browser trace + sun math as reference code, plus a written spec | 36 hidden `(venue, time)` cases, exact |
| **`rally-3d-sun`** ("physics") | **no code** — the physics written up in prose | behaviour: shadow when it should be, none when it shouldn't, morning/evening vs midday, correct sun |

Both use the same three venues, baked from the LiDAR height field, and build the
same thing — `shadeAt(scene, date)` returning the share of the courts in shadow at
an instant (or `null` after dark).

Each task dir under `rally-3d/tasks/` holds the starting `files/`, the `prompt.txt`,
the hidden grader `hidden/`, a `ref/` reference solution and `wrong/` variants the
grader must reject.

## Running it

Needs `python3`, `node` (the graders are `.mjs`), [`litellm`](https://docs.litellm.ai/)
for the proxy, and the agent CLIs you want to test on `PATH`. The proxy reads its
upstream key from `BENCH_UPSTREAM_KEY` (or `~/.config/rally-3d-bench/<provider>_key`).

```sh
# upstream provider key
export BENCH_UPSTREAM_KEY=...

# models: strong on :4000, weak on :4001 (litellm), meters in front of each
BENCH_PROXY_PORT=4000 ./rally-3d/proxy.sh deepseek/deepseek-flash &
BENCH_PROXY_PORT=4001 ./rally-3d/proxy.sh openrouter/qwen/qwen3.5-9b &
python3 rally-3d/meter.py --port 4102 --upstream http://localhost:4000 --log rally-3d/meter-strong.jsonl &
python3 rally-3d/meter.py --port 4101 --upstream http://localhost:4001 --log rally-3d/meter-weak.jsonl &

# validate the tasks offline (untouched fails, reference passes, wrongs fail)
python3 rally-3d/validate_tasks.py 'rally-3d-shade,rally-3d-sun'

# run
BENCH_WORK="$PWD/rally-3d/work" python3 rally-3d/run.py \
  --api http://localhost:4000 --meter http://localhost:4102 --meter-log rally-3d/meter-strong.jsonl \
  --model bench-model --agents opencode,claude,codex,goose,aider,qwen,crush \
  --tasks rally-3d-shade,rally-3d-sun --timeout 600 --seed 11
```

Each run drops its patch, output and check log in `rally-3d/results/<run-id>/`.

## Results

Seven harnesses, two models: a strong hosted one (DeepSeek V4.1 Flash, the
`deepseek-flash` snapshot of 2026-10-17) and a small open one (Qwen3.5 9B). One run
per cell, one seed (`--seed 11`), 10-minute budget. `PASS` = every hidden check for
that task.

Cost is derived from each run's recorded token usage, priced with the runner's table
(DeepSeek V4.1 Flash, off-peak: $0.15/M input, $0.003/M cached, $0.60/M output;
Qwen3.5 9B: $0.10/M in, $0.15/M out, no cache discount). `input` is the total prompt
tokens and `cached` the portion the provider billed as a cache read; codex never
caches. DeepSeek's peak hours are 2×, so a run during peak roughly doubles the
strong-model bill.

### DeepSeek V4.1 Flash — the strong model

| harness | task | result | time | input | cached | output | cost |
|---|---|---|---:|---:|---:|---:|---:|
| claude | rally-3d-shade | PASS | 26s | 151,043 | 128,512 | 4,527 | $0.0065 |
| crush | rally-3d-shade | PASS | 28s | 259,332 | 242,560 | 3,484 | $0.0053 |
| codex | rally-3d-shade | PASS | 29s | 107,703 | 0 | 6,310 | $0.0199 |
| aider | rally-3d-shade | PASS | 29s | 101,080 | 2,816 | 5,923 | $0.0183 |
| qwen | rally-3d-shade | PASS | 38s | 160,997 | 132,480 | 6,564 | $0.0086 |
| goose | rally-3d-shade | PASS | 46s | 177,072 | 169,472 | 7,798 | $0.0063 |
| opencode | rally-3d-shade | PASS | 54s | 192,807 | 182,272 | 9,527 | $0.0078 |
| opencode | rally-3d-sun | PASS | 36s | 370,191 | 329,472 | 5,425 | $0.0104 |
| aider | rally-3d-sun | PASS | 103s | 99,439 | 2,432 | 23,046 | $0.0284 |
| claude | rally-3d-sun | PASS | 106s | 395,890 | 371,328 | 20,828 | $0.0173 |
| qwen | rally-3d-sun | PASS | 106s | 500,048 | 438,016 | 20,326 | $0.0228 |
| crush | rally-3d-sun | PASS | 132s | 297,389 | 249,856 | 27,822 | $0.0246 |
| codex | rally-3d-sun | PASS | 142s | 529,518 | 0 | 27,684 | $0.0960 |
| goose | rally-3d-sun | PASS | 199s | 859,077 | 844,672 | 39,756 | $0.0285 |
| **all** | | **14/14** | | **4,201,586** | **3,093,888** | **209,020** | **$0.3008** |

### Qwen3.5 9B — the small model

| harness | task | result | time | input | cached | output | cost |
|---|---|---|---:|---:|---:|---:|---:|
| codex | rally-3d-shade | PASS | 37s | 179,551 | 0 | 3,394 | $0.0185 |
| claude | rally-3d-shade | PASS | 185s | 1,749,291 | 0 | 15,383 | $0.1772 |
| opencode | rally-3d-shade | PASS | 189s | 1,479,351 | 0 | 24,002 | $0.1515 |
| aider | rally-3d-shade | PASS | 259s | 184,050 | 0 | 40,082 | $0.0244 |
| qwen | rally-3d-shade | PASS | 384s | 163,154 | 101,904 | 37,254 | $0.0219 |
| crush | rally-3d-shade | **FAIL** | 16s | 34,663 | 0 | 794 | $0.0036 |
| goose | rally-3d-shade | **FAIL** timeout | 600s | 4,453,079 | 3,852,816 | 31,587 | $0.4500 |
| codex | rally-3d-sun | **FAIL** | 7s | 8,951 | 0 | 317 | $0.0009 |
| crush | rally-3d-sun | **FAIL** | 10s | 15,402 | 0 | 379 | $0.0016 |
| goose | rally-3d-sun | **FAIL** | 12s | 7,002 | 0 | 1,395 | $0.0009 |
| qwen | rally-3d-sun | **FAIL** | 63s | 122,740 | 81,920 | 1,557 | $0.0125 |
| aider | rally-3d-sun | **FAIL** | 66s | 0 | 0 | 0 | $0.0000 |
| claude | rally-3d-sun | **FAIL** timeout | 600s | 3,245,462 | 0 | 26,020 | $0.3284 |
| opencode | rally-3d-sun | **FAIL** timeout | 600s | 6,949,052 | 0 | 53,427 | $0.7029 |
| **all** | | **5/14** | | **18,591,748** | **4,036,640** | **235,591** | **$1.8945** |

What it shows:

- **A fully-specified port is a near-universal pass.** All seven harnesses land the
  port on the strong model (26–54s). On the small model five still do; crush fails in
  16s and goose burns the full 10 minutes and 4.5M input tokens without finishing.
- **Asking for the reasoning instead of the code is a cliff.** On the strong model the
  physics task costs ~3× the time (26–54s → 36–199s) but all seven pass. On the small
  model it is unsolvable — 0/7, and the two harnesses that sit out the full 10-minute
  budget (opencode 6.9M input tokens, claude 3.2M) dominate the bill.
- **Caching, not speed, decides the bill.** codex never caches, so its one physics
  cell ($0.096) costs more than all seven port cells combined ($0.073); claude and
  crush cache hardest and are the cheapest on the port.
- **Failing is the expensive part.** The small model spent ~$1.05 of its $1.89 failing
  the physics task; opencode's single timed-out cell ($0.70, 6.9M input tokens) costs
  more than the entire passing strong-model run ($0.30) twice over.
- **Harness barely moves the port; it moves the physics.** Every strong port cell sits
  in a 28s band (26–54s), while the physics cells span 36–199s.

## Caveats

One run per cell, one seed. This is a demo, not a leaderboard — treat the timings as
indicative and the pass/fail as "did this config get it done at all".
