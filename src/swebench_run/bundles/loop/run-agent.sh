#!/usr/bin/env bash
# Loop (https://github.com/soketlabs/loop) in headless --print mode.
#  - full-access: --print has nobody to approve tool calls, and the pod is the sandbox
#  - --no-web (the default; set the harness option web: "on" to leave Loop's web tools on):
#    keeps the agent from looking up the upstream fix
#  - a throwaway config dir with a models.json pinning the model, so a fresh
#    container never depends on a cached model catalog
# Loop has no step-limit flag; MAX_STEPS is not enforced for this harness.
set -euo pipefail

export LOOP_CODING_AGENT_DIR=/tmp/loop-home
mkdir -p "$LOOP_CODING_AGENT_DIR" /out/trajectory

export BENCH_API_KEY="$LLM_API_KEY"

# Loop only counts a provider as "connected" if it is in settings.json (or a built-in
# preset has its key in the environment); models.json alone is not enough, and --print
# then stops with "No LLM provider connected". The same id in models.json adds the
# pinned model and the key variable, so write both.
# responseHeaderTimeoutMs: Loop gives up if the model has not started answering within 60 s
# (its default). A busy API under many parallel agents can take longer, and the whole run
# then ends as agent_error. Wait up to 10 minutes; the pod's own timeout still applies.
# Thinking: Loop declares a custom model as non-reasoning and its default thinking level is
# "off", so by default the model is NOT asked to think. Set the harness option thinking_level
# (minimal|low|medium|high|xhigh|max) to turn it on; unset or "off" keeps the old behaviour.
THINK="${THINKING_LEVEL:-off}"
if [ "$THINK" = "off" ]; then
  REASONING_JSON=""
else
  REASONING_JSON=',"reasoning":true'
fi
# Compaction: Loop 0.7.0 compacts a long conversation automatically by default, earlier
# releases do not. The harness option compaction: "off" turns it off, so runs on different
# versions can be compared; anything else leaves the release's own default.
COMPACT_JSON=""
if [ "${COMPACTION:-default}" = "off" ]; then COMPACT_JSON=',"compaction":{"enabled":false}'; fi
cat >"$LOOP_CODING_AGENT_DIR/settings.json" <<EOF
{"responseHeaderTimeoutMs":600000,"defaultThinkingLevel":"$THINK","providers":[{"id":"bench","name":"bench","baseUrl":"$PROVIDER_BASE_URL"}]$COMPACT_JSON}
EOF
cat >"$LOOP_CODING_AGENT_DIR/models.json" <<EOF
{"providers":[{"id":"bench","baseUrl":"$PROVIDER_BASE_URL","apiKeyEnv":["BENCH_API_KEY"],"models":[{"id":"$MODEL"$REASONING_JSON}]}]}
EOF

ARGS=(--cwd /testbed --provider bench --model "$MODEL"
      --permission-mode full-access --no-context-files)
if [ "${WEB:-off}" != "on" ]; then ARGS+=(--no-web); fi

set +e
/harness/bin/loop "${ARGS[@]}" --print "$(cat /task/problem.md)" >/out/final.txt
RC=$?
set -e

# Loop's session database is the trajectory. Its format is Loop-internal.
cp -r "$LOOP_CODING_AGENT_DIR"/sessions* /out/trajectory/ 2>/dev/null || true
exit "$RC"
