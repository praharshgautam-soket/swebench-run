# swebench-run

Run [SWE-bench](https://www.swebench.com/) Lite (300 real GitHub issues) with any coding-agent
harness and model, in parallel on Kubernetes, then grade it and get a report. Built to compare
harnesses fairly: same model, same prompt, same limits, same grader.

**Supported out of the box:** [Loop](https://github.com/soketlabs/loop) (v0.7.0 by default, any
release tag works) and the DeepSeek Harness (`dsh` 0.2.1-rc.5). Adding another is one folder and one
registry line ([how](docs/CONTRACT.md)).

## What you get

- **No Docker-in-Docker.** Every task runs as its own pod from the prebuilt SWE-bench image; the
  agent works in `/testbed`. Grading runs in a *fresh* pod, using the official `swebench` parsers.
- **Resume and retries.** Finished tasks are skipped on restart. Infrastructure failures (evicted
  pod, image pull, model API down) are retried and reported as `infra_error`, never as a wrong answer.
- **A metering proxy.** Counts calls and tokens per task, retries rate limits, caps spend, and
  records cut-off model streams so they are retried instead of counted as failures.
- **Reports.** `report.json`, `report.md`, `report.html`, and `predictions.jsonl` in the official
  format, ready for `sb-cli`. Per-provider split when a run mixes model providers.
- **Safe on shared clusters.** Low-priority pods, a resource quota, optional node allow-list.

```
 your laptop / VM                         Kubernetes
 ┌──────────────┐   one pod per task   ┌───────────────────────────────────┐
 │ swebench-run │ ───────────────────▶ │ inference pod  (SWE-bench image)  │
 │ (controller) │                      │   init: fetch harness  ─▶ /harness│
 │              │ ◀── patch + log ──── │   agent edits /testbed            │
 │              │                      └───────────────┬───────────────────┘
 │              │   fresh pod, same image              │ model calls
 │              │ ───────────────────▶ ┌───────────────▼───────────────────┐
 │              │ ◀── resolved? ────── │ eval pod: apply patch, run tests  │
 └──────────────┘                      └───────────────────────────────────┘
        │                    sbr-meter proxy ─▶ model API (counts tokens, retries)
        ▼
  results/<run_id>/report.{json,md,html}, predictions.jsonl
```

## Quick start

You need a Kubernetes cluster (or one VM: [docs/VM-SETUP.md](docs/VM-SETUP.md)), `kubectl` access,
and an API key for an OpenAI-compatible model endpoint.

```bash
git clone <this repo> && cd swebench-run
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export SOKET_API_KEY=...                                 # or the variable your config names
swebench-run create-secret -c configs/smoke-13.yaml      # stores the key in a K8s Secret
swebench-run meter-up      -c configs/smoke-13.yaml      # the metering proxy
swebench-run validate      -c configs/smoke-13.yaml -n 3 # sanity check: gold patches must pass
swebench-run run           -c configs/smoke-13.yaml      # 13 tasks, about an hour
swebench-run status        -c configs/smoke-13.yaml      # progress and ETA, any time
```

A full 300-task run is `configs/loop-full.yaml` (about 7 to 10 hours with 8 workers). The step by
step guide, with what each command prints, is in [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md).

## Choosing a harness and its options

Set these under `harness:` in the run config. Quote `"on"` and `"off"`.

| Harness | Option | Values | Meaning |
|---|---|---|---|
| `loop` | `version` | a release tag, default `v0.7.0` | Which Loop release the pods download (checksummed). |
| `loop` | `web` | `"off"` (default), `"on"` | Off drops Loop's web tools, so it cannot look up the upstream fix. |
| `loop` | `thinking_level` | `off`, `minimal` … `max` | See [what we found](docs/FINDINGS.md#thinking-levels). On GLM-5.3 only on/off is real. |
| `loop` | `compaction` | `"off"` | Loop 0.7.0 compacts long conversations by default; `"off"` disables it. |
| `deepseek` | `web` | `"off"` (default), `"on"` | Off disables dsh's web tool. |

Providers: `soket`, `openrouter` or `openai-compatible` (any base URL). Keys are read from the
environment variable you name in `provider.api_key_env`, stored in a Secret, and never written to
files or put on a command line.

## Commands

| Command | What it does |
|---|---|
| `setup` | Interactive: writes a run config. |
| `create-secret` | Copies the API key from your environment into a Kubernetes Secret. |
| `meter-up / meter-status / meter-down` | The metering proxy. |
| `cache-up / cache-down` | The in-cluster package cache (DeepSeek only). |
| `prepull` | Pre-pulls the multi-GB instance images onto the nodes. |
| `validate -n N` | Runs N tasks with the gold patch: they must all resolve. |
| `run` / `infer` / `eval` | Both stages, or one at a time. Resumable. |
| `status` | Counts, tokens, ETA. `run --retry-infra` retries infrastructure errors. |
| `redo --status S [--yes]` | Deletes selected results so the next `run` redoes them. Lists first. |
| `report` | Rebuilds the reports from the saved results. |

Comparing runs and building charts: [scripts/compare_runs.py](scripts/compare_runs.py) (text tables)
and [scripts/make_report.py](scripts/make_report.py) (an HTML report with charts; edit the `RUNS`
list at its top for your run ids). [scripts/probe_thinking.py](scripts/probe_thinking.py) tests which
thinking parameters a model endpoint accepts, and doubles as a check that the model is up.

## Results from our runs

GLM-5.3, all 300 Lite tasks, no web tools, same prompt and limits ([details](docs/FINDINGS.md)):

| Harness | Resolved | Notes |
|---|---:|---|
| Loop 0.5.0 | 231 / 300 (77.0%) | 130 tasks went through OpenRouter, 170 through Soket |
| Loop 0.6.0 | 222 / 300 (74.0%) | 31 tasks lost to cut model streams; not rerun |
| DeepSeek Harness rc.5 | 282 / 300 (94.0%) | |

**Loop 0.7.0 has not been run yet.** The version option, the run script and the tests are in place,
and 0.7.0 keeps every flag the script uses, but it has not been tried on a cluster. Run
`configs/smoke-13.yaml` first.

## Known issues

- **The model API matters a lot.** During our runs Soket's GLM-5.3 backend sometimes answered
  `502 model unavailable` or cut a streamed answer in half. The proxy now records cut streams and the
  runner retries them. Check the model is up before a long run:
  `python scripts/probe_thinking.py --base <url> --model <model> --only none`.
- **"Web off" is not a network block.** It removes the harness's web tools only. The shell can still
  reach the internet, and in at least five DeepSeek tasks it did (`curl` of upstream commits). For a
  strictly offline run, add a network policy that allows only DNS and the model endpoint.
- **Loop has no step limit flag**, so `limits.max_steps` is not enforced for it.
- **Instance image names** come from `swebench` 4.x; 5.x expects columns the Lite dataset lacks, so
  the dependency is pinned to `<5`.
- **The single-VM path** ([k3s](docs/VM-SETUP.md)) is untested on a real VM.

## Develop

```bash
pytest          # 164 tests, no cluster needed
```

Layout: `src/swebench_run/` (controller), `bundles/<harness>/run-agent.sh` (the only harness-specific
file), `assets/` (the proxy and the grading script), `configs/` (examples, tested), `docs/`.
