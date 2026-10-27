"""Dataset loading and the bridge to the `swebench` package (image names, eval scripts, grading).

Imports of `datasets` / `swebench` are lazy so the rest of the package (and its
tests) work without them.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .config import RunConfig, select_instances
from .runner import InstanceSpec, NoTestOutput


def load_rows(cfg: RunConfig) -> list[dict]:
    from datasets import load_dataset

    return [dict(r) for r in load_dataset(cfg.dataset.name, split=cfg.dataset.split)]


def build_specs(cfg: RunConfig, rows: list[dict]) -> tuple[dict[str, InstanceSpec], dict]:
    """InstanceSpecs for the selected subset, plus swebench TestSpecs keyed by id."""
    from swebench.harness.test_spec.test_spec import make_test_spec

    by_id = {r["instance_id"]: r for r in rows}
    ids = select_instances([r["instance_id"] for r in rows], cfg.dataset.subset)
    specs: dict[str, InstanceSpec] = {}
    test_specs: dict = {}
    for i in ids:
        ts = make_test_spec(by_id[i], namespace=cfg.k8s.instance_image_namespace)
        test_specs[i] = ts
        specs[i] = InstanceSpec(
            instance_id=i,
            problem_statement=by_id[i]["problem_statement"],
            image=ts.instance_image_key,
            eval_script=ts.eval_script,
            gold_patch=by_id[i].get("patch", ""),
        )
    return specs, test_specs


def make_grader(test_specs: dict, model_name: str):
    """Grade an evaluation log with SWE-bench's own log parsers and report logic."""
    from swebench.harness.constants import START_TEST_OUTPUT
    from swebench.harness.grading import get_eval_report

    def grade(instance_id: str, patch: str, log: str) -> tuple[bool, dict]:
        if START_TEST_OUTPUT not in log:
            raise NoTestOutput(instance_id)
        pred = {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }
        # get_eval_report reads the log from a path.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test_output.txt"
            path.write_text(log)
            report = get_eval_report(
                test_spec=test_specs[instance_id],
                prediction=pred,
                test_log_path=str(path),
                include_tests_status=True,
            )
        entry = report[instance_id]
        return bool(entry.get("resolved")), json.loads(json.dumps(report, default=str))

    return grade
