#!/usr/bin/env bash
# Loop (https://github.com/soketlabs/loop) in headless --print mode.
#  - full-access: --print has nobody to approve tool calls, and the pod is the sandbox
#  - --no-web: keep the agent from looking up the upstream fix
#  - a throwaway config dir with a models.json pinning the model, so a fresh
#    container never depends on a cached model catalog
# Loop has no step-limit flag; MAX_STEPS is not enforced for this harness.
set -euo pipefail

export LOOP_CODING_AGENT_DIR=/tmp/loop-home
mkdir -p "$LOOP_CODING_AGENT_DIR" /out/trajectory

export BENCH_API_KEY="$LLM_API_KEY"

cat >"$LOOP_CODING_AGENT_DIR/models.json" <<EOF
{"providers":[{"id":"bench","baseUrl":"$PROVIDER_BASE_URL","apiKeyEnv":["BENCH_API_KEY"],"models":[{"id":"$MODEL"}]}]}
EOF

ARGS=(--cwd /testbed --provider bench --model "$MODEL"
      --permission-mode full-access --no-web --no-context-files)

set +e
/harness/bin/loop "${ARGS[@]}" --print "$(cat /task/problem.md)" >/out/final.txt
RC=$?
set -e

# Loop's session database is the trajectory. Its format is Loop-internal.
cp -r "$LOOP_CODING_AGENT_DIR"/sessions* /out/trajectory/ 2>/dev/null || true
exit "$RC"
