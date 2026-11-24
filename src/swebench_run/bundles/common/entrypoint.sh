#!/usr/bin/env bash
# Shared run-contract entrypoint. Runs inside the SWE-bench instance image;
# the bundle is copied to /harness by an init container. See docs/CONTRACT.md.
#
# In:  /task/problem.md, repo at /testbed, env MODEL PROVIDER_BASE_URL LLM_API_KEY
#      MAX_STEPS TIMEOUT_SEC INSTANCE_ID (+ harness-specific env)
# Out: one base64 tar.gz of /out between BEGIN/END marker lines on stdout.
set -u

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=/out
BEGIN="===SBR-RESULT-BEGIN==="
END="===SBR-RESULT-END==="
MAX_BLOCK_BYTES=$((6 * 1024 * 1024))   # stay inside the kubelet's log rotation size

mkdir -p "$OUT"

if [ ! -d /testbed ]; then
  echo "run-contract: /testbed does not exist in this image" >&2
  exit 2
fi
cd /testbed
git config --global --add safe.directory /testbed >/dev/null 2>&1 || true

START=$(date +%s)
timeout -k 15 "${TIMEOUT_SEC:-1800}" bash "$HARNESS_DIR/run-agent.sh" >"$OUT/agent.log" 2>&1
RC=$?
WALL=$(( $(date +%s) - START ))

case "$RC" in
  0) STATUS=ok ;;
  124|137) STATUS=timeout ;;
  *) STATUS=agent_error ;;
esac

# The patch is whatever the agent left in the working tree, minus agent state.
git add -A -- . ':(exclude).loop' >/dev/null 2>&1
git diff --cached HEAD >"$OUT/patch.diff" 2>/dev/null
PATCH_BYTES=$(wc -c <"$OUT/patch.diff" | tr -d ' ')

printf '{"status":"%s","agent_exit_code":%s,"wall_sec":%s,"patch_bytes":%s}\n' \
  "$STATUS" "$RC" "$WALL" "$PATCH_BYTES" >"$OUT/meta.json"

echo "run-contract: status=$STATUS exit=$RC wall=${WALL}s patch_bytes=$PATCH_BYTES"
echo "--- last agent output ---"
tail -n 40 "$OUT/agent.log"

pack() { tar czf - -C "$OUT" "$@" | base64 -w0; }

BLOCK=$(pack .)
if [ "${#BLOCK}" -gt "$MAX_BLOCK_BYTES" ]; then
  # Too big for the log: drop trajectory files and keep the tail of the agent log.
  tail -c 1048576 "$OUT/agent.log" >"$OUT/agent.log.tail" && mv "$OUT/agent.log.tail" "$OUT/agent.log"
  rm -rf "$OUT/trajectory"
  echo '{"trajectory_dropped":"result block exceeded log size limit"}' >"$OUT/note.json"
  BLOCK=$(pack .)
fi

echo "$BEGIN"
echo "$BLOCK"
echo "$END"
exit 0
