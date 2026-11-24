# Harness run contract

A harness is a *bundle*, not controller code. The controller mounts the bundle into
the SWE-bench instance pod and calls the same entrypoint for every harness.

## Bundle layout (copied to `/harness` by an init container)

Two delivery modes: **image mode** (`k8s.harness_image`, needs a registry) or **fetch mode**
(default when it is empty): the init container downloads the agent from the harness's release URL,
verifies its `.sha256`, and the two scripts come from the pod's ConfigMap. Fetch mode needs pods to reach the
internet.

```
/opt/harness/entrypoint.sh   shared, from src/swebench_run/bundles/common (timeout, patch capture, packaging)
/opt/harness/run-agent.sh    the only harness-specific file
/opt/harness/bin/...         the agent itself
```

## What `run-agent.sh` receives

| Input | Where |
|---|---|
| Full prompt (issue + instructions) | `/task/problem.md` |
| Repository at the base commit | `/testbed` (also the working directory) |
| `MODEL`, `PROVIDER_BASE_URL` | env |
| `LLM_API_KEY` | env, from a Kubernetes Secret |
| `MAX_STEPS`, `TIMEOUT_SEC`, `INSTANCE_ID` | env |
| harness-specific options | env, upper-cased from `harness.options` |

It must edit files in `/testbed`, exit 0 on a normal finish, and may write:

- `/out/usage.json`: `{"input_tokens", "output_tokens", "cost_usd", "steps"}`, every key optional.
  Missing numbers show as `n/a` in the report; they are never replaced by zeros.
- `/out/trajectory/...`: anything useful for debugging. Linked from the report.

Do not commit. Do not print the API key.

## What the controller gets back

`entrypoint.sh` computes the patch (`git add -A; git diff --cached HEAD`, excluding `.loop`),
writes `/out/meta.json` (`status`: `ok` | `timeout` | `agent_error`) and prints `/out` as a
base64 tar.gz between `===SBR-RESULT-BEGIN===` and `===SBR-RESULT-END===` at the end of the
pod log. If the block would exceed ~6 MiB, trajectory files are dropped (and `note.json`
says so), because the kubelet rotates logs. A pod that ends without a block is an
**infra error** and is retried.

## Limits the controller enforces for every harness

- Wall clock: `timeout` inside the pod, plus `activeDeadlineSeconds` on the pod.
- Step limit: passed as `MAX_STEPS`; honoured only if the harness supports it.
- Cost cap: checked between instances from reported usage; unenforceable for
  harnesses that report none.

## Adding a harness

1. `src/swebench_run/bundles/<name>/run-agent.sh`, plus a `Dockerfile` if you want an image.
2. One entry in `src/swebench_run/harnesses.py` (name and the option keys it accepts).
3. Set `harness.name` and `k8s.harness_image` in the run config. Nothing else changes.
