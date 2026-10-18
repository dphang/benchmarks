#!/bin/bash
# Run the benchmark proxy: rally-3d/proxy.sh [upstream-model]
#   rally-3d/proxy.sh deepseek/deepseek-flash
#   rally-3d/proxy.sh mistral/codestral-latest
#   rally-3d/proxy.sh openrouter/qwen/qwen3.5-9b
# The key is read from ~/.config/rally-3d-bench/<provider>_key (or BENCH_UPSTREAM_KEY).
# Port: BENCH_PROXY_PORT (default 4000), to run two providers side by side.
# Spending limits: use a prepaid provider account, and rally-3d/run.py
# --min-balance (DeepSeek) to stop before it runs dry. LiteLLM's own budget
# needs a database, so it isn't used here.
set -e
export BENCH_UPSTREAM="${1:-deepseek/deepseek-flash}"
provider="${BENCH_UPSTREAM%%/*}"
export BENCH_UPSTREAM_KEY="${BENCH_UPSTREAM_KEY:-$(cat ~/.config/rally-3d-bench/${provider}_key)}"
# USD per token (input, output), so the proxy log shows spend.
case "$BENCH_UPSTREAM" in
  deepseek/deepseek-flash) cost_in=0.00000015; cost_out=0.0000006 ;;
  deepseek/deepseek-v4-pro) cost_in=0.00000066; cost_out=0.00000198 ;;
  openrouter/qwen/qwen3.5-9b) cost_in=0.0000001; cost_out=0.00000015 ;;
  *) cost_in=0; cost_out=0 ;;
esac
cfg="$(mktemp -t bench-litellm).yaml"
sed -e "s/__COST_IN__/$cost_in/" -e "s/__COST_OUT__/$cost_out/" "$(dirname "$0")/litellm.yaml" > "$cfg"
exec litellm --config "$cfg" --port "${BENCH_PROXY_PORT:-4000}"
