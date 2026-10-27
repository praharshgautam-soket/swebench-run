"""Pure builders for the Kubernetes objects a run needs (plain dicts, no client).

Inference pod:  instance image + an init container that copies the harness bundle
                into an emptyDir, so the huge instance images are never rebuilt.
Evaluation pod: a fresh pod from the same instance image; patch and eval script
                arrive via ConfigMap.
The API key is only ever referenced through secretKeyRef, never written out.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from typing import Any

from .config import RunConfig
from .harnesses import get_harness, harness_image

# ConfigMap objects are capped at 1 MiB in total.
CONFIGMAP_LIMIT_BYTES = 900_000
# Grace on top of the agent timeout for scheduling, pulls and teardown.
DEADLINE_GRACE_SEC = 300


def slug(run_id: str, instance_id: str, stage: str) -> str:
    """DNS-safe, collision-free object name (instance ids contain `__` and caps)."""
    h = hashlib.sha1(instance_id.encode()).hexdigest()[:10]
    return f"sbr-{run_id}-{stage}-{h}"[:63].rstrip("-")


def labels(cfg: RunConfig, instance_id: str, stage: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": "swebench-run",
        "swebench-run/run": cfg.run_id,
        "swebench-run/stage": stage,
        "swebench-run/instance": hashlib.sha1(instance_id.encode()).hexdigest()[:10],
    }


def _resources(cfg: RunConfig) -> dict[str, Any]:
    k = cfg.k8s
    return {
        "requests": {
            "cpu": k.cpu_request,
            "memory": k.memory_request,
            "ephemeral-storage": k.ephemeral_storage_request,
        },
        "limits": {
            "cpu": k.cpu_limit,
            "memory": k.memory_limit,
            "ephemeral-storage": k.ephemeral_storage_limit,
        },
    }


def _pod_common(cfg: RunConfig) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "nodeSelector": dict(cfg.k8s.node_selector),
    }
    if cfg.k8s.tolerations:
        spec["tolerations"] = cfg.k8s.tolerations
    if cfg.k8s.image_pull_secrets:
        spec["imagePullSecrets"] = [{"name": n} for n in cfg.k8s.image_pull_secrets]
    return spec


def configmap(name: str, ns: str, lbls: dict[str, str], data: dict[str, str]) -> dict[str, Any]:
    size = sum(len(v.encode()) for v in data.values())
    if size > CONFIGMAP_LIMIT_BYTES:
        raise ValueError(f"configmap payload is {size} bytes, over the {CONFIGMAP_LIMIT_BYTES} limit")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": ns, "labels": lbls},
        "data": data,
    }


def inference_objects(
    cfg: RunConfig, instance_id: str, image: str, prompt: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(configmap, pod) for the agent run."""
    name = slug(cfg.run_id, instance_id, "inf")
    lbls = labels(cfg, instance_id, "inference")
    ns = cfg.k8s.namespace
    harness = get_harness(cfg.harness.name)

    env: list[dict[str, Any]] = [
        {"name": "INSTANCE_ID", "value": instance_id},
        {"name": "MODEL", "value": cfg.provider.model},
        {"name": "PROVIDER_BASE_URL", "value": cfg.provider.base_url},
        {"name": "MAX_STEPS", "value": str(cfg.limits.max_steps)},
        {"name": "TIMEOUT_SEC", "value": str(cfg.limits.timeout_sec)},
        {
            "name": "LLM_API_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": cfg.k8s.secret_name, "key": cfg.k8s.secret_key}
            },
        },
    ]
    env += [{"name": k, "value": v} for k, v in harness.extra_env(cfg).items()]

    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": lbls,
            "annotations": {"swebench-run/instance-id": instance_id},
        },
        "spec": {
            **_pod_common(cfg),
            "activeDeadlineSeconds": cfg.limits.timeout_sec + DEADLINE_GRACE_SEC,
            "initContainers": [
                {
                    "name": "harness",
                    "image": harness_image(cfg),
                    "imagePullPolicy": cfg.k8s.image_pull_policy,
                    "command": ["sh", "-c", "cp -a /opt/harness/. /harness/"],
                    "volumeMounts": [{"name": "harness", "mountPath": "/harness"}],
                }
            ],
            "containers": [
                {
                    "name": "agent",
                    "image": image,
                    "imagePullPolicy": cfg.k8s.image_pull_policy,
                    "command": ["bash", "/harness/entrypoint.sh"],
                    "env": env,
                    "resources": _resources(cfg),
                    "volumeMounts": [
                        {"name": "harness", "mountPath": "/harness", "readOnly": True},
                        {"name": "task", "mountPath": "/task", "readOnly": True},
                        {"name": "out", "mountPath": "/out"},
                    ],
                }
            ],
            "volumes": [
                {"name": "harness", "emptyDir": {}},
                {"name": "task", "configMap": {"name": name}},
                {"name": "out", "emptyDir": {}},
            ],
        },
    }
    return configmap(name, ns, lbls, {"problem.md": prompt}), pod


def eval_script_text() -> str:
    return resources.files("swebench_run").joinpath("assets/eval_run.sh").read_text()


def eval_objects(
    cfg: RunConfig, instance_id: str, image: str, patch: str, eval_script: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(configmap, pod) for a fresh evaluation pod."""
    name = slug(cfg.run_id, instance_id, "eval")
    lbls = labels(cfg, instance_id, "eval")
    ns = cfg.k8s.namespace
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": lbls,
            "annotations": {"swebench-run/instance-id": instance_id},
        },
        "spec": {
            **_pod_common(cfg),
            "activeDeadlineSeconds": cfg.limits.eval_timeout_sec + DEADLINE_GRACE_SEC,
            "containers": [
                {
                    "name": "eval",
                    "image": image,
                    "imagePullPolicy": cfg.k8s.image_pull_policy,
                    "command": ["bash", "/eval/run.sh"],
                    "env": [
                        {"name": "EVAL_TIMEOUT_SEC", "value": str(cfg.limits.eval_timeout_sec)}
                    ],
                    "resources": _resources(cfg),
                    "volumeMounts": [{"name": "eval", "mountPath": "/eval", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "eval", "configMap": {"name": name, "defaultMode": 0o755}}],
        },
    }
    cm = configmap(
        name,
        ns,
        lbls,
        {"run.sh": eval_script_text(), "eval.sh": eval_script, "patch.diff": patch},
    )
    return cm, pod


def prepull_daemonset(cfg: RunConfig, images: list[str]) -> dict[str, Any]:
    """DaemonSet that pulls the given images onto every matching node.

    One init container per image runs `true`-equivalent; the kubelet pulls each
    image first. Needs a shell in the image, which SWE-bench instance images have.
    """
    inits = [
        {
            "name": f"pull-{i}",
            "image": img,
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", "true"],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
        }
        for i, img in enumerate(images)
    ]
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": f"sbr-{cfg.run_id}-prepull"[:63],
            "namespace": cfg.k8s.namespace,
            "labels": {"app.kubernetes.io/managed-by": "swebench-run"},
        },
        "spec": {
            "selector": {"matchLabels": {"swebench-run/prepull": cfg.run_id}},
            "template": {
                "metadata": {"labels": {"swebench-run/prepull": cfg.run_id}},
                "spec": {
                    **_pod_common(cfg),
                    "restartPolicy": "Always",
                    "initContainers": inits,
                    "containers": [
                        {
                            "name": "pause",
                            "image": "registry.k8s.io/pause:3.9",
                            "resources": {"requests": {"cpu": "1m", "memory": "8Mi"}},
                        }
                    ],
                },
            },
        },
    }
