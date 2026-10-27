"""swebench-run command line.

    swebench-run setup                 interactive questions -> run.yaml
    swebench-run create-secret  -c run.yaml   copy the API key from your env into a K8s Secret
    swebench-run build-bundle   -c run.yaml   print the docker commands for the harness image
    swebench-run prepull        -c run.yaml   DaemonSet that pre-pulls the instance images
    swebench-run validate       -c run.yaml   gold patches must resolve; empty patches must not
    swebench-run run            -c run.yaml   inference + evaluation (resumes automatically)
    swebench-run infer|eval     -c run.yaml   one stage only
    swebench-run report         -c run.yaml   rebuild report files from stored results
"""
from __future__ import annotations

import argparse
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
    harness_image = ask("Harness bundle image (push it first; see build-bundle)", "")
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


def cmd_build_bundle(args: argparse.Namespace) -> int:
    cfg = load(args)
    image = cfg.k8s.harness_image or "<registry>/loop-bundle:0.5.0"
    print(
        "Run from the repo root (Docker needed on a machine of your choice):\n\n"
        f"  docker build --platform linux/amd64 -f bundles/{cfg.harness.name}/Dockerfile -t {image} .\n"
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
    runner = Runner(
        cfg, store, KubeCluster(cfg.k8s.namespace), specs,
        make_grader(test_specs, f"{cfg.harness.name}__{cfg.provider.model}"),
    )
    return runner, store, list(specs)


def _run_stages(args: argparse.Namespace, stages: tuple[str, ...]) -> int:
    from .report import write_report

    cfg = load(args)
    runner, store, ids = _runner(cfg)
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


def cmd_report(args: argparse.Namespace) -> int:
    from .dataset import build_specs, load_rows
    from .report import write_report
    from .state import Store

    cfg = load(args)
    specs, _ = build_specs(cfg, load_rows(cfg))
    report = write_report(cfg, Store(cfg.run_dir), total_instances=len(specs))
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
    sp = add("prepull", cmd_prepull)
    sp.add_argument("--dry-run", action="store_true", help="print the DaemonSet instead of applying it")
    sp = add("validate", cmd_validate)
    sp.add_argument("-n", type=int, default=3, help="how many instances to check")
    for name, fn in (("run", cmd_run), ("infer", cmd_infer), ("eval", cmd_eval)):
        sp = add(name, fn)
        sp.add_argument("--retry-infra", action="store_true",
                        help="give instances that ended in infra_error a fresh set of attempts")
    add("report", cmd_report)

    args = p.parse_args(argv)
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
