#!/bin/bash
# Grading runs in the agent's workspace (cwd). Zero means solved.
HERE="$(cd "$(dirname "$0")" && pwd)"
cmp -s "$HERE/files/test/shade.test.js" test/shade.test.js || { echo "test/shade.test.js was modified"; exit 1; }
node --test 2>&1 | tail -30
[ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
T="$(mktemp -d)"
cp "$HERE/hidden/check.mjs" "$HERE/hidden/expected.json" "$T/"
node "$T/check.mjs"
r=$?
rm -rf "$T"
exit $r
