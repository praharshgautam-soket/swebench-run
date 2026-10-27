import json

import pytest

from swebench_run.config import RunConfig
from swebench_run.manifests import (
    CONFIGMAP_LIMIT_BYTES,
    eval_objects,
    inference_objects,
    prepull_daemonset,
    slug,
)


@pytest.fixture
def cfg():
    return RunConfig.from_dict(
        {
            "run_id": "r1",
            "provider": {"kind": "openrouter", "model": "z-ai/glm-5.3-flash"},
            "k8s": {"harness_image": "reg.example/loop-bundle:0.5.0", "namespace": "bench"},
        }
    )


IMG = "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


def test_slug_is_dns_safe_and_unique():
    a = slug("r1", "django__django-11099", "inf")
    b = slug("r1", "django__django-11100", "inf")
    assert a != b and len(a) <= 63
    assert a == a.lower() and "_" not in a


def test_inference_pod_shape(cfg):
    cm, pod = inference_objects(cfg, "django__django-11099", IMG, "PROMPT")
    spec = pod["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    assert spec["initContainers"][0]["image"] == "reg.example/loop-bundle:0.5.0"
    agent = spec["containers"][0]
    assert agent["image"] == IMG
    assert agent["resources"]["requests"]["ephemeral-storage"]
    assert agent["resources"]["limits"]["memory"]
    assert cm["data"] == {"problem.md": "PROMPT"}
    assert pod["metadata"]["name"] == cm["metadata"]["name"]


def test_api_key_only_via_secret_ref(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-SUPERSECRET")
    cm, pod = inference_objects(cfg, "django__django-11099", IMG, "PROMPT")
    blob = json.dumps([cm, pod])
    assert "SUPERSECRET" not in blob
    env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
    ref = env["LLM_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "swebench-api-key", "key": "api-key"}


def test_agent_deadline_set(cfg):
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    assert pod["spec"]["activeDeadlineSeconds"] > cfg.limits.timeout_sec


def test_missing_harness_image_is_an_error(cfg):
    cfg.k8s.harness_image = ""
    with pytest.raises(ValueError, match="harness_image"):
        inference_objects(cfg, "a__b-1", IMG, "p")


def test_unknown_harness_option_rejected(cfg):
    cfg.harness.options = {"nope": 1}
    with pytest.raises(ValueError, match="does not accept"):
        inference_objects(cfg, "a__b-1", IMG, "p")


def test_eval_pod_is_separate_from_inference_pod(cfg):
    _, inf = inference_objects(cfg, "a__b-1", IMG, "p")
    cm, ev = eval_objects(cfg, "a__b-1", IMG, "diff", "#!/bin/bash\n")
    assert ev["metadata"]["name"] != inf["metadata"]["name"]
    assert "initContainers" not in ev["spec"]
    assert set(cm["data"]) == {"run.sh", "eval.sh", "patch.diff"}


def test_oversized_patch_rejected(cfg):
    with pytest.raises(ValueError, match="over the"):
        eval_objects(cfg, "a__b-1", IMG, "x" * CONFIGMAP_LIMIT_BYTES, "s")


def test_prepull_daemonset(cfg):
    ds = prepull_daemonset(cfg, [IMG, "swebench/other:latest"])
    inits = ds["spec"]["template"]["spec"]["initContainers"]
    assert [i["image"] for i in inits] == [IMG, "swebench/other:latest"]
    assert ds["spec"]["template"]["spec"]["restartPolicy"] == "Always"
