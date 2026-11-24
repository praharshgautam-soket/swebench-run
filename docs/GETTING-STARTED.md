# Getting started

From a fresh clone to a graded SWE-bench Lite run. Allow an hour for the first 13-task test and
7 to 10 hours for a full 300-task run with 8 workers.

## 0. What you need

- **A Kubernetes cluster you can create pods in**, with `kubectl` already pointing at it
  (`kubectl get nodes` works). No cluster? Use one VM and k3s: [VM-SETUP.md](VM-SETUP.md).
- **x86_64 nodes.** The prebuilt SWE-bench images are amd64.
- **Capacity:** each task pod asks for 1 CPU (up to 2), 2 GiB (up to 6) and 10 GiB of disk (up to
  30). Eight at once is about 8 to 16 cores and 16 to 48 GiB. The images are large: plan on
  roughly 500 GB of disk on the nodes that pull them (an estimate, not measured).
- **Outbound internet from the nodes** to Docker Hub (images), GitHub (the Loop release) and your
  model API. DeepSeek also needs the npm registry once, for the cache pod.
- **A model API key** for an OpenAI-compatible endpoint. The examples use Soket's GLM-5.3.
- **Python 3.10 or newer** on the machine you run the commands from.

## 1. Install

```bash
git clone <this repo> && cd swebench-run
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q            # optional: 164 tests, no cluster needed
```

## 2. Pick or write a config

One YAML file describes a run. Start from one of these in [`configs/`](../configs):

| File | Use it for |
|---|---|
| `smoke-13.yaml` | The first test: Loop on 13 tasks, 2 workers. |
| `loop-full.yaml` | Loop on all 300 tasks, 8 workers. |
| `deepseek-full.yaml` | DeepSeek Harness on all 300 tasks. |
| `single-vm.yaml` | The same as `loop-full.yaml`, sized for a single 16 vCPU VM. |

Things you will probably change: `run_id` (a new name per run; results go to
`results/<run_id>/`), `provider` (`kind`, `model`, and `base_url` for your own endpoint),
`workers`, and for a shared cluster `k8s.allowed_nodes` and `k8s.priority_class`.

To use your own endpoint:

```yaml
provider:
  kind: openai-compatible
  base_url: https://your-endpoint.example/v1
  api_key_env: MY_API_KEY
  model: your-model-name
```

## 3. Store the key, start the proxy

```bash
export SOKET_API_KEY=...                          # the variable your config names; never on a command line
swebench-run create-secret -c configs/smoke-13.yaml
swebench-run meter-up      -c configs/smoke-13.yaml
```

`create-secret` copies the key into a Kubernetes Secret (agent pods read it from there, never from a
file). `meter-up` starts the small metering proxy that counts tokens and retries rate limits. Keep it
running between runs; `meter-down` removes it.

## 4. Check the model answers

A run against a model that is down turns into failed tasks, so test it first:

```bash
python scripts/probe_thinking.py --base https://api.soket.ai/v1 --model GLM-5.3 --only none
```

You want an `ok` line. `HTTP 502 model unavailable` means the model backend is down: wait.
`HTTP 401` or `403` means the key was rejected.

## 5. Validate the setup (optional, recommended)

```bash
swebench-run validate -c configs/smoke-13.yaml -n 3
```

Runs three tasks with the *gold* patch and an empty patch. The gold patches must resolve and the
empty ones must not. If this passes, the images, the grader and the cluster are working,
independent of any agent.

## 6. Run

```bash
swebench-run run -c configs/smoke-13.yaml
```

Run it in `tmux`, or detach it so it survives your terminal closing:

```bash
setsid nohup swebench-run run -c configs/loop-full.yaml > full.log 2>&1 < /dev/null & disown
```

Watch progress from any shell:

```bash
swebench-run status -c configs/loop-full.yaml
```

It shows counts per outcome, token totals and an estimated time left. Stopping and restarting is
safe: finished tasks are kept and skipped.

Optionally pre-pull the images first (they are several GB each), which moves the wait out of the
timed part: `swebench-run prepull -c configs/loop-full.yaml`.

## 7. Read the results

`results/<run_id>/` holds:

| File | What it is |
|---|---|
| `report.md`, `report.html`, `report.json` | The summary, with a per-provider split if the run mixed providers. |
| `predictions.jsonl` | The patches, in the official SWE-bench format. |
| `instances/<task>/state.json` | Status, steps, tokens and timing for one task. |
| `instances/<task>/patch.diff`, `agent.log`, `eval.log` | The patch, the agent's output, the grader's output. |

Outcomes:

| Status | Meaning |
|---|---|
| `resolved` | The patch passes the official tests. |
| `unresolved` | A patch was made and graded, and the tests failed. |
| `empty_patch` | The agent finished but changed nothing, so nothing was graded. |
| `timeout` | The agent used up its time (default 30 minutes). A patch left behind is still graded. |
| `agent_error` | The agent crashed. |
| `infra_error` | The cluster or the model API failed. Retried automatically; never counted as unresolved. |

## 8. Fix what failed

```bash
swebench-run run  -c configs/loop-full.yaml --retry-infra    # retry infrastructure errors
swebench-run redo -c configs/loop-full.yaml --status agent_error          # lists what it would delete
swebench-run redo -c configs/loop-full.yaml --status agent_error --yes    # deletes those results
swebench-run run  -c configs/loop-full.yaml                               # redoes only those
```

## 9. Compare two runs

```bash
python scripts/compare_runs.py <run_a> <run_b>       # outcome counts, per repo, who solved what
```

`scripts/make_report.py` builds an HTML report with charts; edit the `RUNS` list at the top for your
run ids.

## 10. Cross-check with the official grader (optional)

`predictions.jsonl` can be graded by the SWE-bench team's own service with `sb-cli`
(`pip install sb-cli`, then `sb-cli gen-api-key` and `sb-cli submit swe-bench_lite test ...`). This
uploads your patches and your email to their service. The docs there show a JSON list, so convert the
`.jsonl` first. Check `sb-cli --help` for the current options.

## When something goes wrong

| Symptom | Likely cause |
|---|---|
| Many `agent_error`, `502` in the logs | The model API is down or cutting streams. Wait, then `redo --status agent_error`. |
| Pods stuck `Pending` | Quota or node capacity; `kubectl -n swebench describe pod <name>`. |
| `403 exceeded quota` at start | Too many workers for the namespace quota; lower `workers`. |
| `ImagePullBackOff` | The node cannot reach Docker Hub, or the disk is full. |
| `status` ETA looks wrong right after a restart | It ignores gaps over 20 minutes; give it a few finished tasks. |
| "No LLM provider connected" in a Loop log | The provider is missing from Loop's `settings.json`; the run script writes it, so check the harness version. |
