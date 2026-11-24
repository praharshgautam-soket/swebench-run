"""swebench-run command line.

    swebench-run setup                 interactive questions -> run.yaml
    swebench-run create-secret  -c run.yaml   copy the API key from your env into a K8s Secret
    swebench-run build-bundle   -c run.yaml   print the docker commands for the harness image
    swebench-run meter-up       -c run.yaml   deploy the metering proxy (tokens, steps, cost cap)
    swebench-run meter-status   -c run.yaml   token and call counts per instance so far
    swebench-run meter-down     -c run.yaml   remove the metering proxy
    swebench-run cache-up       -c run.yaml   start the agent cache pod (deepseek harness; installs it once)
    swebench-run cache-down     -c run.yaml   remove the agent cache pod
    swebench-run prepull        -c run.yaml   DaemonSet that pre-pulls the instance images
    swebench-run validate       -c run.yaml   gold patches must resolve; empty patches must not
    swebench-run run            -c run.yaml   inference + evaluation (resumes automatically)
    swebench-run infer|eval     -c run.yaml   one stage only
    swebench-run report         -c run.yaml   rebuild report files from stored results
    swebench-run status         -c run.yaml   progress so far (no cluster access needed)
    swebench-run tag-existing   -c run.yaml --provider P --model M   label tasks finished before provider tracking
    swebench-run redo           -c run.yaml --provider P --status S [--grep RE] [--yes]   delete results to rerun them
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .config import PROVIDER_PRESETS, ConfigError, RunConfig, Subset


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_int(prompt: str, default: int) -> int:
    while True:
        try:
            return int(ask(prompt, str(default)))
        except ValueError:
            print("  please enter a whole number")


def ask_float_or_none(prompt: str) -> float | None:
    while True:
        raw = ask(prompt, "none")
        if raw.lower() in ("none", "", "no"):
            return None
        try:
            return float(raw)
        except ValueError:
            print("  please enter a number or 'none'")


def cmd_setup(args: argparse.Namespace) -> int:
    print("swebench-run setup (answers are saved to a YAML file; the API key is never asked for)\n")
    kinds = sorted(PROVIDER_PRESETS)
    kind = ask(f"Model provider {kinds}", "openrouter")
    base_url, key_env = PROVIDER_PRESETS.get(kind, (None, None))
    if base_url is None:
        base_url = ask("OpenAI-compatible base URL (include /v1)")
    else:
        base_url = ask("Base URL", base_url)
    key_env = ask("Local env var holding the API key", key_env or "LLM_API_KEY")
    model = ask("Model ID", "z-ai/glm-5.3-flash")
    harness = ask("Harness", "loop")

    dataset = ask("Dataset", "princeton-nlp/SWE-bench_Lite")
    split = ask("Split", "test")
    subset = Subset()
    mode = ask("Subset: 'all', 'first' N, or 'ids'", "all")
    if mode == "first":
        subset.first_n = ask_int("  How many instances", 10)
    elif mode == "ids":
        subset.instance_ids = [i.strip() for i in ask("  Instance ids (comma separated)").split(",") if i.strip()]

    workers = ask_int("Parallel workers", 4)
    max_steps = ask_int("Max agent steps (only enforced by harnesses that support it)", 100)
    timeout = ask_int("Timeout per instance, seconds", 1800)
    max_cost = ask_float_or_none("Max cost in USD (needs the harness to report usage)")
    namespace = ask("Kubernetes namespace", "swebench")
    harness_image = ask("Harness bundle image (leave empty to download the agent at pod start)", "")
    output_dir = ask("Output directory", "results")
    run_id = ask("Run id", "run-001")
    out = ask("Save config to", "run.yaml")

    data = {
        "run_id": run_id,
        "provider": {"kind": kind, "base_url": base_url, "api_key_env": key_env, "model": model},
        "harness": {"name": harness},
        "dataset": {
            "name": dataset,
            "split": split,
            "subset": {"instance_ids": subset.instance_ids, "first_n": subset.first_n},
        },
        "workers": workers,
        "limits": {"max_steps": max_steps, "timeout_sec": timeout, "max_cost_usd": max_cost},
        "k8s": {"namespace": namespace, "harness_image": harness_image},
        "output_dir": output_dir,
    }
    try:
        cfg = RunConfig.from_dict(data)
    except ConfigError as e:
        print(f"invalid answers: {e}", file=sys.stderr)
        return 2
    cfg.save(out)
    print(f"\nSaved {out}. Next: swebench-run create-secret -c {out}, then validate, then run.")
    return 0


def load(args: argparse.Namespace) -> RunConfig:
    return RunConfig.load(args.config)


def cmd_create_secret(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster

    cfg = load(args)
    value = os.environ.get(cfg.provider.api_key_env)
    if not value:
        print(f"env var {cfg.provider.api_key_env} is not set in this shell", file=sys.stderr)
        return 2
    cluster = KubeCluster(cfg.k8s.namespace)
    cluster.ensure_namespace()
    cluster.ensure_secret(cfg.k8s.secret_name, cfg.k8s.secret_key, value)
    print(f"secret {cfg.k8s.namespace}/{cfg.k8s.secret_name} written (value not shown)")
    return 0


def cmd_meter_up(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster
    from .manifests import meter_objects

    cfg = load(args)
    if not cfg.meter.enabled:
        print("meter.enabled is false in this config; set it to true first", file=sys.stderr)
        return 2
    value = os.environ.get(cfg.provider.api_key_env)
    if not value:
        print(f"env var {cfg.provider.api_key_env} is not set in this shell", file=sys.stderr)
        return 2
    cluster = KubeCluster(cfg.k8s.namespace)
    cluster.ensure_namespace()
    cm, dep, svc = meter_objects(cfg)
    cluster.meter_up(cm, dep, svc, cfg.meter.secret_name, value)
    print(f"meter {cfg.k8s.namespace}/{cfg.meter.name} applied; waiting for it to be ready...")
    if not cluster.wait_deployment_ready(cfg.meter.name):
        print("not ready after 5 minutes; check: kubectl -n %s describe pod -l app=%s"
              % (cfg.k8s.namespace, cfg.meter.name), file=sys.stderr)
        return 1
    print("meter is ready")
    return 0


def cmd_cache_up(args: argparse.Namespace) -> int:
    """Start the agent cache pod for harnesses that need one (installs the pinned agent once)."""
    from .cluster import KubeCluster
    from .harnesses import get_harness
    from .manifests import cache_objects

    cfg = load(args)
    spec = get_harness(cfg.harness.name)
    if not spec.cache:
        print(f"harness {spec.name!r} needs no cache pod")
        return 0
    cm, dep, svc = cache_objects(cfg)
    cluster = KubeCluster(cfg.k8s.namespace)
    cluster.ensure_namespace()
    cluster.apply_workload(cm, dep, svc)
    print(f"cache {spec.cache.service} applied; installing {spec.cache.package}@{spec.cache.version} "
          "(first start takes about 3-5 minutes)...")
    if not cluster.wait_deployment_ready(spec.cache.service, timeout_sec=900):
        print("not ready after 15 minutes; check: kubectl -n %s logs deploy/%s"
              % (cfg.k8s.namespace, spec.cache.service), file=sys.stderr)
        return 1
    print("cache is ready")
    return 0


def cmd_cache_down(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster
    from .harnesses import get_harness

    cfg = load(args)
    spec = get_harness(cfg.harness.name)
    if not spec.cache:
        print(f"harness {spec.name!r} has no cache pod")
        return 0
    KubeCluster(cfg.k8s.namespace).meter_down(spec.cache.service)
    print(f"cache {spec.cache.service} removed")
    return 0


def cmd_meter_down(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster

    cfg = load(args)
    KubeCluster(cfg.k8s.namespace).meter_down(cfg.meter.name)
    print(f"meter {cfg.meter.name} removed (its secret {cfg.meter.secret_name} was kept)")
    return 0


def cmd_meter_status(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster

    cfg = load(args)
    cluster = KubeCluster(cfg.k8s.namespace)
    client = cluster.meter_client(cfg.meter.name, cfg.meter.port, cfg.meter.secret_name)
    snap = json.loads(client._call("GET", "all"))
    print(f"total spent (from configured prices): ${snap['spent_usd']}")
    print(f"{'instance key':<14}{'calls':>7}{'errors':>8}{'retries':>9}{'tokens in':>12}{'tokens out':>12}{'api s':>9}{'s/call':>8}")
    total_calls, total_wall = 0, 0.0
    for key, st in sorted(snap["instances"].items()):
        per = st["wall_sec"] / st["calls"] if st["calls"] else 0.0
        total_calls += st["calls"]
        total_wall += st["wall_sec"]
        print(f"{key:<14}{st['calls']:>7}{st['errors']:>8}{st['retries']:>9}"
              f"{st['prompt_tokens']:>12}{st['completion_tokens']:>12}{st['wall_sec']:>9.0f}{per:>8.1f}")
    if total_calls:
        print(f"average time the model API took per call: {total_wall / total_calls:.1f} s over {total_calls} calls")
    return 0


def cmd_build_bundle(args: argparse.Namespace) -> int:
    cfg = load(args)
    image = cfg.k8s.harness_image or "<registry>/loop-bundle:0.7.0"
    print(
        "Optional: only needed if you want an image instead of the default download at pod start.\n"
        "Run from the repo root (Docker needed on a machine of your choice):\n\n"
        f"  docker build --platform linux/amd64 -f src/swebench_run/bundles/{cfg.harness.name}/Dockerfile -t {image} .\n"
        f"  docker push {image}\n\n"
        "then set k8s.harness_image in the config to that image."
    )
    return 0


def cmd_prepull(args: argparse.Namespace) -> int:
    from .cluster import KubeCluster
    from .dataset import build_specs, load_rows
    from .manifests import prepull_daemonset

    cfg = load(args)
    specs, _ = build_specs(cfg, load_rows(cfg))
    images = sorted({s.image for s in specs.values()})
    ds = prepull_daemonset(cfg, images)
    if args.dry_run:
        print(yaml.safe_dump(ds, sort_keys=False))
        return 0
    KubeCluster(cfg.k8s.namespace).apply_daemonset(ds)
    print(f"DaemonSet {ds['metadata']['name']} applied for {len(images)} image(s)")
    return 0


def _runner(cfg: RunConfig):
    from .cluster import KubeCluster
    from .dataset import build_specs, load_rows, make_grader
    from .runner import Runner
    from .state import Store

    specs, test_specs = build_specs(cfg, load_rows(cfg))
    store = Store(cfg.run_dir)
    cluster = KubeCluster(cfg.k8s.namespace)
    meter = cluster.meter_client(cfg.meter.name, cfg.meter.port, cfg.meter.secret_name) if cfg.meter.enabled else None
    runner = Runner(
        cfg, store, cluster, specs,
        make_grader(test_specs, f"{cfg.harness.name}__{cfg.provider.model}"),
        meter=meter,
    )
    return runner, store, list(specs)


def _run_stages(args: argparse.Namespace, stages: tuple[str, ...]) -> int:
    from .report import write_report

    cfg = load(args)
    print(f"run {cfg.run_id}: loading the dataset and preparing tasks...", flush=True)
    runner, store, ids = _runner(cfg)
    print(f"prepared {len(ids)} task(s); metering {'on' if cfg.meter.enabled else 'off'}", flush=True)
    if args.retry_infra:
        for i in ids:
            st = store.load(i)
            for rec in (st.inference, st.eval):
                if rec.status in ("infra_error",):
                    rec.attempts = 0
            store.save(st)
    try:
        runner.run(ids, stages)
    finally:
        report = write_report(cfg, store, total_instances=len(ids))
        h = report["headline"]
        print(f"\nresolved {h['resolved']}/{h['total']}; report in {store.root}")
    return 0


def cmd_run(args):
    return _run_stages(args, ("infer", "eval"))


def cmd_infer(args):
    return _run_stages(args, ("infer",))


def cmd_eval(args):
    return _run_stages(args, ("eval",))


def _eta_line(states, done: int, total: int | None = None) -> str:
    """Pace and time left, from the most recent finishes. '' until there is enough data.

    Gaps longer than 20 minutes between consecutive finishes are treated as 20 minutes: a
    paused or stopped run must not drag the estimate down. Only the last 40 finishes count,
    so the number follows the current speed (for example after switching provider).
    """
    graded = [s for s in states if s.final_status not in ("pending", "infra_error")]
    times = sorted((s.eval.finished_at or s.inference.finished_at) for s in graded
                   if (s.eval.finished_at or s.inference.finished_at))[-40:]
    if len(times) < 5 or times[-1] <= times[0]:
        return ""
    gap_cap = 20 * 60
    active = sum(min(b - a, gap_cap) for a, b in zip(times, times[1:]))
    if active <= 0:
        return ""
    per_min = (len(times) - 1) / (active / 60)
    left = (total if total is not None else len(states)) - done
    hours = left / per_min / 60
    return f"pace {per_min:.2f} tasks/min (recent, idle gaps ignored); about {hours:.1f} h left for {left} task(s) (a rough guess)"


def cmd_tag_existing(args: argparse.Namespace) -> int:
    """Label results recorded before provider tracking, e.g. the tasks already done on Soket."""
    from .state import Store

    cfg = load(args)
    store = Store(cfg.run_dir)
    n = 0
    for st in store.all_states():
        if st.inference.status not in (None, "infra_error") and st.provider is None:
            st.provider, st.model = args.provider, args.model
            store.save(st)
            n += 1
    print(f"tagged {n} finished task(s) as {args.provider}/{args.model}")
    return 0


def cmd_redo(args: argparse.Namespace) -> int:
    """Throw away selected results so the next `run` does those tasks again. Lists first."""
    import re
    import shutil

    from .state import Store

    cfg = load(args)
    store = Store(cfg.run_dir)
    pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    if not args.status and not pattern:
        print("give --status and/or --grep, or this would select nothing", file=sys.stderr)
        return 2
    hits = []
    for st in store.all_states():
        if args.provider and st.provider != args.provider:
            continue
        if args.status and st.final_status == args.status:
            hits.append((st.instance_id, f"status {st.final_status}"))
        elif pattern and pattern.search(store.read_text(st.instance_id, "agent.log")):
            hits.append((st.instance_id, "log matches"))
    for iid, why in hits:
        print(f"  {iid}  ({why})")
    print(f"{len(hits)} task(s) selected" + (f" from provider {args.provider}" if args.provider else ""))
    if not args.yes:
        print("dry run: nothing deleted. Add --yes to delete these results so `run` redoes them.")
        return 0
    for iid, _ in hits:
        shutil.rmtree(store.instance_dir(iid), ignore_errors=True)
    print(f"deleted {len(hits)} result folder(s); start `swebench-run run -c {args.config}` to redo them")
    return 0


def planned_total(cfg: RunConfig, saved: int) -> int:
    """How many tasks this run is meant to do: from the subset in the config, else the dataset."""
    sub = cfg.dataset.subset
    if sub.instance_ids:
        return max(saved, len(set(sub.instance_ids)))
    if sub.first_n:
        return max(saved, sub.first_n)
    try:  # whole dataset: its size (cached locally after the first load)
        from .dataset import load_rows

        return max(saved, len(load_rows(cfg)))
    except Exception:  # noqa: BLE001 - offline: fall back to what is saved
        return saved


def cmd_status(args: argparse.Namespace) -> int:
    """Progress from the stored results only; needs no cluster access."""
    from .state import FINAL_STATUSES, Store

    cfg = load(args)
    states = Store(cfg.run_dir).all_states()
    counts = {s: 0 for s in FINAL_STATUSES}
    for st in states:
        counts[st.final_status] += 1
    done = sum(n for k, n in counts.items() if k != "pending")
    # A task only gets a saved result when a stage ends, so the number of saved files undercounts
    # a run that has just started. Take the planned size from the config when it states one.
    total = planned_total(cfg, len(states))
    waiting = max(0, total - done)
    print(f"run {cfg.run_id}: {done} finished of {total}, {waiting} in progress or waiting")
    for k, n in counts.items():
        if n and k != "pending":
            print(f"  {k:<14}{n}")
    eta = _eta_line(states, done, total)
    if eta:
        print(eta)
    tin = sum(s.input_tokens or 0 for s in states)
    tout = sum(s.output_tokens or 0 for s in states)
    steps = sum(s.steps or 0 for s in states)
    print(f"tokens in {tin:,}  out {tout:,}  steps {steps:,}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .dataset import build_specs, load_rows
    from .report import write_report
    from .state import Store

    cfg = load(args)
    specs, _ = build_specs(cfg, load_rows(cfg))
    store = Store(cfg.run_dir)
    if cfg.meter.enabled:
        from .cluster import KubeCluster
        from .runner import fill_usage_from_meter

        client = KubeCluster(cfg.k8s.namespace).meter_client(cfg.meter.name, cfg.meter.port, cfg.meter.secret_name)
        n = fill_usage_from_meter(cfg, store, client)
        print(f"usage filled from the meter for {n} instance(s)")
    report = write_report(cfg, store, total_instances=len(specs))
    print(f"resolved {report['headline']['resolved']}/{report['headline']['total']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Evaluation correctness check: gold patches resolve, empty patches never do."""
    cfg = load(args)
    cfg.run_id = f"{cfg.run_id}-validate"[:41]
    runner, store, ids = _runner(cfg)
    ids = ids[: args.n]
    bad = 0
    for i in ids:
        spec = runner.instances[i]
        runner.evaluate(spec, patch=spec.gold_patch)
        gold = store.load(i).eval.status
        runner.evaluate(spec, patch="")
        empty = store.load(i).final_status
        ok = gold == "resolved" and empty != "resolved"
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {i}: gold -> {gold}, empty -> {empty}")
    print(f"\n{len(ids) - bad}/{len(ids)} evaluation checks passed")
    return 1 if bad else 0


def _enable_stack_dump() -> None:
    """`kill -USR1 <pid>` prints every thread's stack to stderr: the way to see why a run is quiet."""
    import faulthandler
    import signal

    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True)
        except (OSError, ValueError, AttributeError):  # e.g. stderr without a file descriptor
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="swebench-run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, config=True, **kw):
        sp = sub.add_parser(name, **kw)
        if config:
            sp.add_argument("-c", "--config", default="run.yaml")
        sp.set_defaults(fn=fn)
        return sp

    add("setup", cmd_setup, config=False, help="interactive setup")
    add("create-secret", cmd_create_secret)
    add("build-bundle", cmd_build_bundle)
    add("meter-up", cmd_meter_up)
    add("meter-down", cmd_meter_down)
    add("cache-up", cmd_cache_up)
    add("cache-down", cmd_cache_down)
    add("meter-status", cmd_meter_status)
    sp = add("prepull", cmd_prepull)
    sp.add_argument("--dry-run", action="store_true", help="print the DaemonSet instead of applying it")
    sp = add("validate", cmd_validate)
    sp.add_argument("-n", type=int, default=3, help="how many instances to check")
    for name, fn in (("run", cmd_run), ("infer", cmd_infer), ("eval", cmd_eval)):
        sp = add(name, fn)
        sp.add_argument("--retry-infra", action="store_true",
                        help="give instances that ended in infra_error a fresh set of attempts")
    add("report", cmd_report)
    add("status", cmd_status)
    sp = add("redo", cmd_redo, help="delete selected results so they run again (lists first)")
    sp.add_argument("--provider", help="only tasks that ran on this provider, e.g. openrouter")
    sp.add_argument("--status", choices=["resolved", "unresolved", "empty_patch", "patch_failed", "agent_error", "timeout", "infra_error"])
    sp.add_argument("--grep", help="also select tasks whose agent log matches this pattern (case-insensitive)")
    sp.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    sp = add("tag-existing", cmd_tag_existing)
    sp.add_argument("--provider", required=True)
    sp.add_argument("--model", required=True)

    args = p.parse_args(argv)
    _enable_stack_dump()
    try:
        return args.fn(args)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; progress is saved, rerun the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
