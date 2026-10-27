# swebench-run

Run SWE-bench (Lite by default) with a pluggable agent harness and model provider, in parallel on
Kubernetes, then grade and report. CPU nodes only: the model runs remotely over an OpenAI-compatible API.

- **No Docker-in-Docker.** Each instance is its own Pod from the prebuilt SWE-bench instance image.
  The agent works at `/testbed` in that pod.
- **Two stages, rerunnable alone.** Inference writes `predictions.jsonl`; evaluation applies the patch
  in a **fresh** pod from the same image and grades it with SWE-bench's own log parsers.
- **Resume.** Finished instances are skipped. Infra failures (evicted, image pull, OOM, no result)
  are retried with backoff up to `max_retries`, and are reported as `infra_error`, never as `unresolved`.
- **Harnesses are bundles, not code.** See [docs/CONTRACT.md](docs/CONTRACT.md).

## Quick start

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
swebench-run setup                      # writes run.yaml
export OPENROUTER_API_KEY=...           # whatever provider.api_key_env names
swebench-run create-secret -c run.yaml  # key goes into a K8s Secret, never into files
swebench-run build-bundle  -c run.yaml  # prints the docker build/push commands
swebench-run prepull       -c run.yaml   # optional: pre-pull multi-GB images onto the nodes
swebench-run validate      -c run.yaml -n 3   # gold patches must resolve, empty must not
swebench-run run           -c run.yaml
```

Results land in `<output_dir>/<run_id>/`: `report.json`, `report.md`, `report.html`,
`predictions.jsonl` (official format, usable with sb-cli) and `instances/<id>/` with the patch,
agent log, eval log and trajectory.

## Known limits

- Loop (v0.5.0) has no step-limit flag, and its stock build reports no tokens or cost. Those show as
  `n/a`; `limits.max_cost_usd` can only act on harnesses that report usage.
- Rate limiting: model calls happen inside the pods, so 429/5xx backoff is the harness's own. The
  controller's lever is `workers`.
- Instance image names come from `swebench` 4.x (`swebench` Docker Hub namespace).

## Tests

```bash
pytest
```
