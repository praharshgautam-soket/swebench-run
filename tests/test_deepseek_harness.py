import json
import os
import re
import subprocess
import textwrap
from importlib import resources

import pytest
import yaml

from swebench_run.config import RunConfig
from swebench_run.harnesses import REGISTRY, cache_fetch_script, get_harness
from swebench_run.manifests import cache_objects, inference_objects

IMG = "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


@pytest.fixture
def cfg():
    return RunConfig.from_dict(
        {
            "run_id": "r1",
            "harness": {"name": "deepseek"},
            "provider": {"kind": "soket", "model": "GLM-5.3"},
            "k8s": {"namespace": "bench"},
        }
    )


def test_deepseek_is_registered_and_pinned_to_rc5():
    spec = get_harness("deepseek")
    assert spec.cache and spec.cache.package == "@deepseek-ai/dsh"
    assert spec.cache.version == "0.2.1-rc.5"  # exact: nothing floats to a newer or older build
    assert spec.cache.tarball == "dsh-0.2.1-rc.5.tar.gz"
    assert "deepseek" in REGISTRY


def test_task_pod_fetches_from_the_cache_service_and_verifies_the_checksum(cfg):
    cm, pod = inference_objects(cfg, "django__django-11099", IMG, "PROMPT")
    init = pod["spec"]["initContainers"][0]
    script = init["command"][2]
    assert "http://sbr-dsh-cache.bench.svc.cluster.local:8081/dsh-0.2.1-rc.5.tar.gz" in script
    assert "sha256sum -c" in script and "--retry-connrefused" in script
    assert "/harness/dsh" in script
    assert {"problem.md", "entrypoint.sh", "run-agent.sh"} <= set(cm["data"])
    # every container declares cpu, memory and ephemeral storage, which the namespace quota requires
    for kind in ("requests", "limits"):
        assert {"cpu", "memory", "ephemeral-storage"} <= set(init["resources"][kind])


def test_agent_pod_uses_the_proxy_url_not_the_real_key(cfg):
    cfg.meter.enabled = True
    _, pod = inference_objects(cfg, "a__b-1", IMG, "p")
    env = {e["name"]: e for e in pod["spec"]["containers"][0]["env"]}
    assert env["PROVIDER_BASE_URL"]["value"].startswith("http://sbr-meter.bench.svc")
    assert env["LLM_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "meter-token"


def test_cache_pod_shape(cfg):
    cm, dep, svc = cache_objects(cfg)
    c = dep["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env == {"PACKAGE": "@deepseek-ai/dsh", "VERSION": "0.2.1-rc.5", "PORT": "8081"}
    assert c["image"] == "node:22-bookworm-slim"
    assert c["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert {"cpu", "memory", "ephemeral-storage"} <= set(c["resources"]["requests"]) & set(c["resources"]["limits"])
    assert set(cm["data"]) == {"cache.sh", "cache_serve.js"}
    assert svc["metadata"]["name"] == "sbr-dsh-cache" and svc["spec"]["ports"][0]["port"] == 8081
    assert "affinity" not in dep["spec"]["template"]["spec"]  # no node list configured here


def test_cache_pod_follows_the_node_and_priority_guards(cfg):
    cfg.k8s.allowed_nodes = ["node-a"]
    cfg.k8s.priority_class = "swebench-low"
    _, dep, _ = cache_objects(cfg)
    pod = dep["spec"]["template"]["spec"]
    assert pod["priorityClassName"] == "swebench-low" and "affinity" in pod


def test_harness_without_a_cache_cannot_make_cache_objects():
    other = RunConfig.from_dict({"provider": {"kind": "soket", "model": "m"}})  # loop
    with pytest.raises(ValueError, match="does not use a cache pod"):
        cache_objects(other)


def test_cache_script_refuses_the_wrong_version():
    script = resources.files("swebench_run").joinpath("assets/dsh_cache.sh").read_text()
    assert 'node -p "require' in script and '"$INSTALLED" != "$VERSION"' in script
    assert "exec node /app/cache_serve.js" in script


def render_patch(model="GLM-5.3", base="http://sbr-meter.bench.svc.cluster.local:8080/i/abc1234567", web="off"):
    """Run only the heredocs of run-agent.sh with values filled in, as the shell would.

    The web-tool row is appended only when the web option is not "on"."""
    text = resources.files("swebench_run").joinpath("bundles/deepseek/run-agent.sh").read_text()
    body = re.search(r"cat >/tmp/bench.patch.yml <<EOF\n(.*?)\nEOF", text, re.S).group(1)
    if web != "on":
        body += "\n" + re.search(r"cat >>/tmp/bench.patch.yml <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    return body.replace("$MODEL", model).replace("$PROVIDER_BASE_URL", base)


def test_generated_patch_is_valid_yaml_with_the_expected_rows():
    rows = {r["id"]: r for r in yaml.safe_load(render_patch())}
    assert rows["agent-default-model"]["config"] == {"provider": "custom", "model": "GLM-5.3"}
    gw = rows["llm-pi-ai"]["config"]["providers"]["custom"]
    assert gw["api"] == "openai-completions" and gw["apiKeyEnv"] == "BENCH_API_KEY"
    assert gw["baseURL"].startswith("http://sbr-meter") and gw["models"] == [{"id": "GLM-5.3"}]
    assert rows["bash-sandbox"]["config"]["timeoutMs"] == 600000
    # telemetry is off: dsh would otherwise export session data to a DeepSeek collector
    assert rows["otel"]["disabled"] is True and rows["session-telemetry-otel"]["disabled"] is True
    # the web tool can fetch the upstream fix from the internet: off, like Loop's --no-web
    assert rows["tool-web"]["disabled"] is True


def test_patch_survives_model_ids_with_slashes_and_dots():
    rows = {r["id"]: r for r in yaml.safe_load(render_patch(model="z-ai/glm-5.3"))}
    assert rows["agent-default-model"]["config"]["model"] == "z-ai/glm-5.3"


def test_run_agent_gets_full_access_from_the_env_and_uses_only_flags_rc5_accepts():
    text = resources.files("swebench_run").joinpath("bundles/deepseek/run-agent.sh").read_text()
    assert "DSH_PERMISSION_MODE=danger-full-access" in text
    cmd = next(l for l in text.splitlines() if l.startswith("dsh --profile headless"))
    # rc.5 rejected --permission-mode ("unknown option"); the task goes in as plain text
    assert "--permission-mode" not in cmd and "--approval-policy" not in cmd
    assert '"$(cat /task/problem.md)"' in cmd and "--patch /tmp/bench.patch.yml" in cmd


def test_web_tool_is_left_on_only_when_the_web_option_is_on():
    assert any(r["id"] == "tool-web" for r in yaml.safe_load(render_patch(web="off")))
    assert not any(r["id"] == "tool-web" for r in yaml.safe_load(render_patch(web="on")))
    text = resources.files("swebench_run").joinpath("bundles/deepseek/run-agent.sh").read_text()
    assert '"${WEB:-off}" != "on"' in text  # off unless a run asks for it


def test_loop_drops_its_web_tools_unless_the_web_option_is_on():
    text = resources.files("swebench_run").joinpath("bundles/loop/run-agent.sh").read_text()
    assert 'if [ "${WEB:-off}" != "on" ]; then ARGS+=(--no-web); fi' in text
    assert "--no-web --no-context-files" not in text  # no longer hard-wired


def test_both_harnesses_accept_the_web_option():
    from swebench_run.harnesses import get_harness

    for name in ("loop", "deepseek"):
        assert "web" in get_harness(name).env_keys
