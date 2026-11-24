"""Pure builders for the Kubernetes objects a run needs (plain dicts, no client).

Inference pod:  instance image + an init container that puts the harness bundle into an
                emptyDir (from a bundle image, or by downloading the agent), so the huge
                instance images are never rebuilt.
Evaluation pod: a fresh pod from the same instance image; patch and eval script
                arrive via ConfigMap.
The API key is only ever referenced through secretKeyRef, never written out.
"""
from __future__ import annotations

import hashlib
from importlib import resources
from typing import Any

from .config import RunConfig
from .harnesses import get_harness

# ConfigMap objects are capped at 1 MiB in total.
CONFIGMAP_LIMIT_BYTES = 900_000
# Grace on top of the agent timeout for scheduling, pulls and teardown.
DEADLINE_GRACE_SEC = 300


def slug(run_id: str, instance_id: str, stage: str) -> str:
    """DNS-safe, collision-free object name (instance ids contain `__` and caps)."""
    h = hashlib.sha1(instance_id.encode()).hexdigest()[:10]
    return f"sbr-{run_id}-{stage}-{h}"[:63].rstrip("-")


def instance_key(instance_id: str) -> str:
    """Short stable id for an instance; used in labels and in the meter's URL path."""
    return hashlib.sha1(instance_id.encode()).hexdigest()[:10]


def labels(cfg: RunConfig, instance_id: str, stage: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": "swebench-run",
        "swebench-run/run": cfg.run_id,
        "swebench-run/stage": stage,
        "swebench-run/instance": instance_key(instance_id),
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


INIT_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "100Mi"},
    "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "1Gi"},
}


def _pod_common(cfg: RunConfig) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "nodeSelector": dict(cfg.k8s.node_selector),
    }
    if cfg.k8s.tolerations:
        spec["tolerations"] = cfg.k8s.tolerations
    if cfg.k8s.allowed_nodes:
        spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": list(cfg.k8s.allowed_nodes),
                                }
                            ]
                        }
                    ]
                }
            }
        }
    if cfg.k8s.priority_class:
        spec["priorityClassName"] = cfg.k8s.priority_class
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

    if cfg.meter.enabled:
        # Agents never see the real key: they get the proxy's address and its own token.
        base_url = meter_url(cfg, instance_id)
        key_ref = {"name": cfg.meter.secret_name, "key": "meter-token"}
    else:
        base_url = cfg.provider.base_url
        key_ref = {"name": cfg.k8s.secret_name, "key": cfg.k8s.secret_key}

    env: list[dict[str, Any]] = [
        {"name": "INSTANCE_ID", "value": instance_id},
        {"name": "MODEL", "value": cfg.provider.model},
        {"name": "PROVIDER_BASE_URL", "value": base_url},
        {"name": "MAX_STEPS", "value": str(cfg.limits.max_steps)},
        {"name": "TIMEOUT_SEC", "value": str(cfg.limits.timeout_sec)},
        {"name": "LLM_API_KEY", "valueFrom": {"secretKeyRef": key_ref}},
    ]
    env += [{"name": k, "value": v} for k, v in harness.extra_env(cfg).items()]

    task_data = {"problem.md": prompt}
    task_mounts = [{"name": "harness", "mountPath": "/harness"}]
    if cfg.k8s.harness_image:
        init = {
            "name": "harness",
            "image": cfg.k8s.harness_image,
            "imagePullPolicy": cfg.k8s.image_pull_policy,
            "command": ["sh", "-c", "cp -a /opt/harness/. /harness/"],
            "volumeMounts": task_mounts,
            "resources": INIT_RESOURCES,
        }
    elif harness.can_fetch():
        # Fetch mode: no registry. The scripts ride along in the task ConfigMap.
        task_data.update(harness.scripts())
        fetch_image, fetch_cmd, fetch_resources = harness.fetch_init(cfg)
        init = {
            "name": "harness",
            "image": fetch_image,
            "imagePullPolicy": cfg.k8s.image_pull_policy,
            "command": ["sh", "-c", fetch_cmd],
            "volumeMounts": task_mounts + [{"name": "task", "mountPath": "/task", "readOnly": True}],
            "resources": fetch_resources,
        }
    else:
        raise ValueError(
            f"harness {harness.name!r} has no download URL; set k8s.harness_image to an image "
            "holding the bundle (see src/swebench_run/bundles/<name>/Dockerfile)"
        )

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
            "initContainers": [init],
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
    return configmap(name, ns, lbls, task_data), pod


def meter_url(cfg: RunConfig, instance_id: str) -> str:
    """Base URL an agent should use: the proxy, tagged with the instance key."""
    m = cfg.meter
    return f"http://{m.name}.{cfg.k8s.namespace}.svc.cluster.local:{m.port}/i/{instance_key(instance_id)}"


def meter_script_text() -> str:
    return resources.files("swebench_run").joinpath("assets/meter_server.py").read_text()


def meter_objects(cfg: RunConfig) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(configmap, deployment, service) for the metering proxy."""
    m, ns = cfg.meter, cfg.k8s.namespace
    lbls = {"app.kubernetes.io/managed-by": "swebench-run", "app": m.name}
    secret = lambda key: {"valueFrom": {"secretKeyRef": {"name": m.secret_name, "key": key}}}  # noqa: E731
    env: list[dict[str, Any]] = [
        {"name": "UPSTREAM_BASE_URL", "value": cfg.provider.base_url},
        {"name": "UPSTREAM_API_KEY", **secret("upstream-key")},
        {"name": "METER_TOKEN", **secret("meter-token")},
        {"name": "PORT", "value": str(m.port)},
        {"name": "MAX_CONCURRENCY", "value": str(m.max_concurrency)},
        {"name": "MAX_RETRIES", "value": str(m.max_retries)},
    ]
    p = cfg.provider
    if p.input_price_per_mtok is not None and p.output_price_per_mtok is not None:
        env += [{"name": "PRICE_IN", "value": str(p.input_price_per_mtok)},
                {"name": "PRICE_OUT", "value": str(p.output_price_per_mtok)}]
        if cfg.limits.max_cost_usd is not None:
            env.append({"name": "BUDGET_USD", "value": str(cfg.limits.max_cost_usd)})
    cm = configmap(m.name, ns, lbls, {"meter_server.py": meter_script_text()})
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": m.name, "namespace": ns, "labels": lbls},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": m.name}},
            "template": {
                "metadata": {"labels": lbls},
                "spec": {
                    **_pod_common(cfg),
                    "restartPolicy": "Always",
                    "containers": [
                        {
                            "name": "meter",
                            "image": m.image,
                            "imagePullPolicy": cfg.k8s.image_pull_policy,
                            "command": ["python", "/app/meter_server.py"],
                            "env": env,
                            "ports": [{"containerPort": m.port}],
                            "readinessProbe": {"httpGet": {"path": "/healthz", "port": m.port},
                                               "periodSeconds": 5},
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "100Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi", "ephemeral-storage": "1Gi"},
                            },
                            "volumeMounts": [{"name": "app", "mountPath": "/app", "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "app", "configMap": {"name": m.name}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": m.name, "namespace": ns, "labels": lbls},
        "spec": {"selector": {"app": m.name},
                 "ports": [{"port": m.port, "targetPort": m.port}]},
    }
    return cm, deployment, service


def cache_objects(cfg: RunConfig) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(configmap, deployment, service) for the agent cache pod of a cache-mode harness."""
    harness = get_harness(cfg.harness.name)
    if not harness.cache:
        raise ValueError(f"harness {harness.name!r} does not use a cache pod")
    c, ns = harness.cache, cfg.k8s.namespace
    assets = resources.files("swebench_run").joinpath("assets")
    lbls = {"app.kubernetes.io/managed-by": "swebench-run", "app": c.service}
    cm = configmap(
        c.service,
        ns,
        lbls,
        {"cache.sh": assets.joinpath("dsh_cache.sh").read_text(),
         "cache_serve.js": assets.joinpath("cache_serve.js").read_text()},
    )
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": c.service, "namespace": ns, "labels": lbls},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": c.service}},
            "template": {
                "metadata": {"labels": lbls},
                "spec": {
                    **_pod_common(cfg),
                    "restartPolicy": "Always",
                    "containers": [
                        {
                            "name": "cache",
                            "image": c.image,
                            "imagePullPolicy": cfg.k8s.image_pull_policy,
                            "command": ["sh", "/app/cache.sh"],
                            "env": [
                                {"name": "PACKAGE", "value": c.package},
                                {"name": "VERSION", "value": c.version},
                                {"name": "PORT", "value": str(c.port)},
                            ],
                            "ports": [{"containerPort": c.port}],
                            # Ready only once the tarball and its checksum exist.
                            "readinessProbe": {"httpGet": {"path": "/healthz", "port": c.port},
                                               "periodSeconds": 10, "failureThreshold": 3},
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
                                "limits": {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "6Gi"},
                            },
                            "volumeMounts": [{"name": "app", "mountPath": "/app", "readOnly": True}],
                        }
                    ],
                    "volumes": [{"name": "app", "configMap": {"name": c.service}}],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": c.service, "namespace": ns, "labels": lbls},
        "spec": {"selector": {"app": c.service}, "ports": [{"port": c.port, "targetPort": c.port}]},
    }
    return cm, deployment, service


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
