import json

import pytest

from swebench_run.config import RunConfig
from swebench_run.manifests import (
    instance_key,
    meter_objects,
    meter_url,
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


def test_image_mode_copies_bundle_from_image(cfg):
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    init = pod["spec"]["initContainers"][0]
    assert init["image"] == "reg.example/loop-bundle:0.5.0"
    assert init["command"][:2] == ["sh", "-c"] and "/opt/harness" in init["command"][2]


def test_fetch_mode_needs_no_registry(cfg):
    cfg.k8s.harness_image = ""
    cm, pod = inference_objects(cfg, "a__b-1", IMG, "PROMPT")
    init = pod["spec"]["initContainers"][0]
    assert init["image"] == cfg.k8s.fetch_image
    script = init["command"][2]
    assert "loop-x86_64-unknown-linux-gnu.tar.gz" in script
    assert "sha256sum -c" in script and "--strip-components=1" in script
    # The scripts travel in the same ConfigMap the pod mounts at /task.
    assert {"problem.md", "entrypoint.sh", "run-agent.sh"} <= set(cm["data"])
    assert "/harness/bin/loop" in cm["data"]["run-agent.sh"]
    mounts = {m["name"] for m in init["volumeMounts"]}
    assert mounts == {"harness", "task"}


def test_fetch_mode_does_not_leak_scripts_in_image_mode(cfg):
    cm, _ = inference_objects(cfg, "a__b-1", IMG, "p")
    assert set(cm["data"]) == {"problem.md"}


def test_harness_without_download_or_image_is_an_error(cfg, monkeypatch):
    import dataclasses

    from swebench_run import harnesses

    cfg.k8s.harness_image = ""
    bare = dataclasses.replace(harnesses.REGISTRY["loop"], release_url=None, release_template=None)
    monkeypatch.setitem(harnesses.REGISTRY, "loop", bare)
    with pytest.raises(ValueError, match="no download URL"):
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


def test_pods_have_no_gpu_request_and_small_footprint(cfg):
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    res = pod["spec"]["containers"][0]["resources"]
    assert "nvidia.com/gpu" not in json.dumps(res)
    assert res["requests"]["cpu"] == "1" and res["limits"]["memory"] == "6Gi"


def test_allowed_nodes_and_priority_class_apply_to_every_pod(cfg):
    cfg.k8s.allowed_nodes = ["node-a", "node-b"]
    cfg.k8s.priority_class = "swebench-low"
    _, inf = inference_objects(cfg, "a__b-1", IMG, "p")
    _, ev = eval_objects(cfg, "a__b-1", IMG, "diff", "s")
    for pod in (inf, ev):
        term = pod["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"][0]
        assert term["key"] == "kubernetes.io/hostname"
        assert term["values"] == ["node-a", "node-b"]
        assert pod["spec"]["priorityClassName"] == "swebench-low"


def test_no_affinity_or_priority_by_default(cfg):
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    assert "affinity" not in pod["spec"] and "priorityClassName" not in pod["spec"]


def test_every_container_declares_all_quota_resources(cfg):
    """A ResourceQuota on cpu/memory/ephemeral-storage rejects pods whose containers omit any."""
    needed = {"cpu", "memory", "ephemeral-storage"}
    for mode_image in ("reg/bundle:1", ""):
        cfg.k8s.harness_image = mode_image
        _, inf = inference_objects(cfg, "a__b-1", IMG, "p")
        _, ev = eval_objects(cfg, "a__b-1", IMG, "diff", "s")
        for pod in (inf, ev):
            spec = pod["spec"]
            for c in spec.get("initContainers", []) + spec["containers"]:
                for kind in ("requests", "limits"):
                    assert needed <= set(c["resources"][kind]), (c["name"], kind)


def test_pods_use_the_meter_and_never_see_the_real_key(cfg):
    cfg.meter.enabled = True
    _, pod = inference_objects(cfg, "django__django-11099", IMG, "p")
    env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
    key = instance_key("django__django-11099")
    assert env["PROVIDER_BASE_URL"]["value"] == (
        f"http://sbr-meter.bench.svc.cluster.local:8080/i/{key}"
    )
    ref = env["LLM_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "swebench-meter", "key": "meter-token"}
    assert "swebench-api-key" not in json.dumps(pod)


def test_meter_disabled_leaves_pods_talking_to_the_provider(cfg):
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
    assert env["PROVIDER_BASE_URL"]["value"] == "https://openrouter.ai/api/v1"


def test_meter_objects_shape_and_guards(cfg):
    cfg.meter.enabled = True
    cfg.k8s.allowed_nodes = ["node-a"]
    cfg.k8s.priority_class = "swebench-low"
    cfg.provider.input_price_per_mtok, cfg.provider.output_price_per_mtok = 0.3, 1.2
    cfg.limits.max_cost_usd = 5.0
    cm, dep, svc = meter_objects(cfg)
    assert "serve_forever" in cm["data"]["meter_server.py"]
    pod = dep["spec"]["template"]["spec"]
    c = pod["containers"][0]
    env = {e["name"]: e for e in c["env"]}
    assert env["UPSTREAM_BASE_URL"]["value"] == cfg.provider.base_url
    assert env["UPSTREAM_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "swebench-meter", "key": "upstream-key"}
    assert env["BUDGET_USD"]["value"] == "5.0" and env["PRICE_IN"]["value"] == "0.3"
    assert pod["priorityClassName"] == "swebench-low" and "affinity" in pod
    assert pod["restartPolicy"] == "Always"
    assert {"cpu", "memory", "ephemeral-storage"} <= set(c["resources"]["requests"]) & set(c["resources"]["limits"])
    assert svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"]
    assert "SUPERSECRET" not in json.dumps([cm, dep, svc])


def test_no_budget_env_without_prices(cfg):
    cfg.meter.enabled = True
    cfg.limits.max_cost_usd = 5.0  # a cap without prices cannot be enforced by the proxy
    _, dep, _ = meter_objects(cfg)
    names = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "BUDGET_USD" not in names


def _render_loop_json(think):
    """Run the Loop script's two heredocs through bash with a fake environment; return both JSON files."""
    import os
    import subprocess
    import tempfile
    from importlib import resources as res

    text = res.files("swebench_run").joinpath("bundles/loop/run-agent.sh").read_text()
    head = text[: text.index("ARGS=(")]
    with tempfile.TemporaryDirectory() as d:
        script = head.replace("/tmp/loop-home", d + "/home").replace("/out/trajectory", d + "/traj")
        env = {"PATH": os.environ["PATH"], "LLM_API_KEY": "x", "MODEL": "GLM-5.3",
               "PROVIDER_BASE_URL": "http://p/v1"}
        if think is not None:
            env["THINKING_LEVEL"] = think
        subprocess.run(["bash", "-c", script], env=env, check=True)
        return (json.load(open(d + "/home/settings.json")), json.load(open(d + "/home/models.json")))


def test_loop_keeps_thinking_off_by_default():
    settings, models = _render_loop_json(None)
    assert settings["defaultThinkingLevel"] == "off"
    assert "reasoning" not in models["providers"][0]["models"][0]


def test_loop_thinking_level_turns_on_reasoning_and_the_level():
    settings, models = _render_loop_json("medium")
    assert settings["defaultThinkingLevel"] == "medium"
    assert models["providers"][0]["models"][0] == {"id": "GLM-5.3", "reasoning": True}
    assert settings["responseHeaderTimeoutMs"] == 600000  # earlier fixes kept


def _fetch_script(cfg):
    cfg.k8s.harness_image = ""
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    return pod["spec"]["initContainers"][0]["command"][2]


def test_loop_downloads_0_7_0_unless_a_version_is_chosen(cfg):
    assert "/releases/download/v0.7.0/loop-x86_64-unknown-linux-gnu.tar.gz" in _fetch_script(cfg)


def test_loop_version_option_picks_the_release(cfg):
    cfg.harness.options = {"version": "v0.6.0"}
    assert "/releases/download/v0.6.0/" in _fetch_script(cfg)


@pytest.mark.parametrize("bad", ["0.7.0", "v0.7", 'v0.7.0"; rm -rf /', "latest", "v0.7.0/../x"])
def test_loop_version_must_look_like_a_release_tag(cfg, bad):
    cfg.harness.options = {"version": bad}
    with pytest.raises(ValueError, match="not like v0.7.0"):
        _fetch_script(cfg)


def test_loop_run_script_can_turn_compaction_off_and_leaves_it_alone_otherwise():
    from importlib import resources

    text = resources.files("swebench_run").joinpath("bundles/loop/run-agent.sh").read_text()
    assert '"${COMPACTION:-default}" = "off"' in text and '"compaction":{"enabled":false}' in text
