#!/usr/bin/env bash
# DeepSeek Harness (dsh) 0.2.1-rc.5, one-shot `headless` profile.
#  - DSH_PERMISSION_MODE=danger-full-access: no approvals, no sandbox limits; the pod is the sandbox
#  - the model goes through an OpenAI-compatible gateway (our proxy) declared in the llm-pi-ai row,
#    and the default-model row points at it
#  - telemetry rows are disabled: by default dsh exports session data to a DeepSeek collector
#  - the bash tool's per-command timeout is raised from 60 s to 10 minutes (test suites run longer)
# dsh has no step-limit flag; MAX_STEPS is not enforced for this harness.
set -euo pipefail

export HOME=/tmp/home
export DSH_HOME=/tmp/dsh-home
mkdir -p "$HOME" "$DSH_HOME" /out/trajectory
export PATH=/harness/dsh/bin:/harness/dsh/node_modules/.bin:$PATH
export DSH_PERMISSION_MODE=danger-full-access
export BENCH_API_KEY="$LLM_API_KEY"

cat >/tmp/bench.patch.yml <<EOF
- id: agent-default-model
  config:
    provider: custom
    model: "$MODEL"
- id: llm-pi-ai
  config:
    providers:
      custom:
        apiKeyEnv: BENCH_API_KEY
        api: openai-completions
        baseURL: "$PROVIDER_BASE_URL"
        models:
          - id: "$MODEL"
- id: bash-sandbox
  config:
    timeoutMs: 600000
- id: otel
  disabled: true
- id: session-telemetry-otel
  disabled: true
EOF
# No web tools by default: with fetch on, the agent could download the real upstream fix. Same
# rule as Loop's --no-web, so the two harnesses are compared on equal terms. The harness option
# web: "on" leaves the tool enabled for a run that deliberately allows it.
if [ "${WEB:-off}" != "on" ]; then
  cat >>/tmp/bench.patch.yml <<'EOF'
- id: tool-web
  disabled: true
EOF
fi

cd /testbed
set +e
# Only flags the released rc.5 accepts: the permission flags in newer docs do not exist there.
# Full access comes from DSH_PERMISSION_MODE (above), which also turns approvals off.
dsh --profile headless --patch /tmp/bench.patch.yml "$(cat /task/problem.md)" >/out/final.txt
RC=$?
set -e

# The persisted session logs are the trajectory (JSONL).
cp -r "$DSH_HOME/sessions" /out/trajectory/ 2>/dev/null || true
exit "$RC"
