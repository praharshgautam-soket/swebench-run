import base64
import io
import json
import tarfile

import pytest

from swebench_run.cluster import PodResult
from swebench_run.config import RunConfig
from swebench_run.contract import BEGIN, END
from swebench_run.runner import InstanceSpec, NoTestOutput, Runner
from swebench_run.state import Store


def result_log(patch: str = "diff --git a/f b/f\n+x\n", status: str = "ok", usage: dict | None = None) -> str:
    files = {
        "patch.diff": patch.encode(),
        "meta.json": json.dumps({"status": status}).encode(),
    }
    if usage is not None:
        files["usage.json"] = json.dumps(usage).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return f"agent output\n{BEGIN}\n{base64.b64encode(buf.getvalue()).decode()}\n{END}\n"


class FakeCluster:
    """Scripted pod outcomes per (stage, instance); records every pod it runs."""

    def __init__(self, inference=None, evaluation=None):
        self.inference = inference or {}
        self.evaluation = evaluation or {}
        self.calls: list[tuple[str, str]] = []

    def run_pod(self, configmap, pod, start_timeout, run_timeout):
        stage = pod["metadata"]["labels"]["swebench-run/stage"]
        iid = pod["metadata"]["annotations"]["swebench-run/instance-id"]
        self.calls.append((stage, iid))
        script = self.inference if stage == "inference" else self.evaluation
        outcomes = script[iid]
        outcome = outcomes.pop(0) if isinstance(outcomes, list) and len(outcomes) > 1 else (
            outcomes[0] if isinstance(outcomes, list) else outcomes
        )
        return outcome


def ok_eval(resolved: bool):
    return PodResult("completed", logs=f"SBR_EVAL_BEGIN\nRESOLVED={resolved}\nSBR_EVAL_END\n")


def fake_grader(instance_id, patch, log):
    if "RESOLVED=" not in log:
        raise NoTestOutput()
    return "RESOLVED=True" in log, {instance_id: {"resolved": "RESOLVED=True" in log}}


@pytest.fixture
def cfg(tmp_path):
    return RunConfig.from_dict(
        {
            "run_id": "t1",
            "output_dir": str(tmp_path),
            "workers": 2,
            "max_retries": 2,
            "provider": {
                "kind": "openrouter",
                "model": "m",
                "input_price_per_mtok": 1.0,
                "output_price_per_mtok": 2.0,
            },
            "k8s": {"harness_image": "reg/loop-bundle:1"},
        }
    )


def make_runner(cfg, cluster, ids, sleeps=None):
    specs = {i: InstanceSpec(i, f"problem {i}", f"swebench/img-{i}:latest", "#!/bin/bash\n") for i in ids}
    store = Store(cfg.run_dir)
    return Runner(
        cfg, store, cluster, specs, fake_grader,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        log=lambda m: None,
    ), store
