#!/usr/bin/env bash
# Evaluation runner. Runs in a FRESH pod from the instance image; the agent's pod
# is never reused. /eval is a ConfigMap: patch.diff and eval.sh (SWE-bench's own
# eval script for this instance). Patch application mirrors the official harness.
set -u
cd /testbed
git config --global --add safe.directory /testbed >/dev/null 2>&1 || true

cp /eval/patch.diff /tmp/patch.diff

APPLIED=0
for CMD in "git apply --verbose" "git apply --verbose --reject" "patch --batch --fuzz=5 -p1 -i"; do
  if $CMD /tmp/patch.diff; then APPLIED=1; break; fi
  git checkout -- . ; git clean -fd >/dev/null 2>&1
done
if [ "$APPLIED" -ne 1 ] && git apply --check --reverse /tmp/patch.diff >/dev/null 2>&1; then
  APPLIED=1   # a failed chain can still leave the patch fully applied
fi
if [ "$APPLIED" -ne 1 ]; then
  echo "SBR_PATCH_APPLY_FAILED"
  exit 0
fi

echo "SBR_EVAL_BEGIN"
timeout -k 15 "${EVAL_TIMEOUT_SEC:-1800}" bash /eval/eval.sh
RC=$?
if [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; then
  echo "SBR_EVAL_TIMEOUT"
fi
echo "SBR_EVAL_END"
exit 0
