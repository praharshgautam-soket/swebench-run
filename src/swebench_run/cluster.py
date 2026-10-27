"""Running one pod to completion on Kubernetes, and classifying how it ended.

`Cluster` is the seam the runner talks to; tests substitute a fake. `KubeCluster`
is the real implementation on the official Python client (imported lazily, so
nothing else needs a kubeconfig).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

# Container waiting reasons that mean the image will not arrive on its own.
PULL_FAILURES = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}


@dataclass
class PodResult:
    # "completed": the pod ran its command to the end (success or not); the log
    #              decides what happened. "infra": the cluster, not the task, failed.
    kind: str
    reason: str = ""
    logs: str = ""


class Cluster(Protocol):
    def run_pod(
        self, configmap: dict[str, Any], pod: dict[str, Any], start_timeout: int, run_timeout: int
    ) -> PodResult: ...


def classify_pod(pod: Any, pending_for_sec: float, start_timeout: int) -> PodResult | None:
    """Look at a pod object; return a result if it has ended or is hopeless, else None.

    `pod` is a kubernetes V1Pod (or anything shaped like one).
    """
    status = pod.status
    phase = status.phase

    statuses = list(status.init_container_statuses or []) + list(status.container_statuses or [])

    for cs in statuses:
        term = cs.state.terminated if cs.state else None
        if term and term.reason == "OOMKilled":
            return PodResult("infra", "container OOMKilled")

    if status.reason == "Evicted":
        return PodResult("infra", f"pod evicted: {status.message or ''}".strip())
    if status.reason in ("NodeLost", "Shutdown", "NodeAffinity"):
        return PodResult("infra", f"pod {status.reason}")

    if phase == "Succeeded":
        return PodResult("completed")
    if phase == "Failed":
        # An init container failing (e.g. bundle copy) never reached the agent.
        for cs in status.init_container_statuses or []:
            term = cs.state.terminated if cs.state else None
            if term and term.exit_code != 0:
                return PodResult("infra", f"init container failed: {term.reason or term.exit_code}")
        return PodResult("completed")

    if phase == "Pending" and pending_for_sec > start_timeout:
        for cs in statuses:
            wait = cs.state.waiting if cs.state else None
            if wait and wait.reason in PULL_FAILURES:
                return PodResult("infra", f"image pull failed: {wait.reason}")
        for cond in status.conditions or []:
            if cond.type == "PodScheduled" and cond.status == "False":
                return PodResult("infra", f"unschedulable: {cond.message or cond.reason}")
        return PodResult("infra", f"pod still Pending after {start_timeout}s")
    return None


class KubeCluster:
    def __init__(self, namespace: str, poll_sec: float = 5.0):
        from kubernetes import client, config

        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()
        self.core = client.CoreV1Api()
        self.client = client
        self.ns = namespace
        self.poll_sec = poll_sec

    def ensure_secret(self, name: str, key: str, value: str) -> None:
        body = self.client.V1Secret(
            metadata=self.client.V1ObjectMeta(name=name, namespace=self.ns),
            string_data={key: value},
        )
        try:
            self.core.create_namespaced_secret(self.ns, body)
        except self.client.ApiException as e:
            if e.status != 409:
                raise
            self.core.replace_namespaced_secret(name, self.ns, body)

    def ensure_namespace(self) -> None:
        body = self.client.V1Namespace(metadata=self.client.V1ObjectMeta(name=self.ns))
        try:
            self.core.create_namespace(body)
        except self.client.ApiException as e:
            if e.status != 409:
                raise

    def run_pod(self, configmap, pod, start_timeout, run_timeout) -> PodResult:
        name = pod["metadata"]["name"]
        cm_name = configmap["metadata"]["name"]
        self._delete_leftovers(name, cm_name)
        try:
            self.core.create_namespaced_config_map(self.ns, configmap)
            self.core.create_namespaced_pod(self.ns, pod)
            return self._wait(name, start_timeout, run_timeout)
        except self.client.ApiException as e:
            return PodResult("infra", f"kubernetes API error {e.status}: {e.reason}")
        finally:
            self._delete_leftovers(name, cm_name)

    def _wait(self, name: str, start_timeout: int, run_timeout: int) -> PodResult:
        began = time.monotonic()
        while True:
            elapsed = time.monotonic() - began
            try:
                obj = self.core.read_namespaced_pod(name, self.ns)
            except self.client.ApiException as e:
                if e.status == 404:
                    return PodResult("infra", "pod disappeared")
                time.sleep(self.poll_sec)
                continue
            result = classify_pod(obj, elapsed, start_timeout)
            if result is not None:
                if result.kind == "completed":
                    result.logs = self._logs(name)
                return result
            if elapsed > run_timeout + start_timeout:
                return PodResult("infra", "controller deadline exceeded")
            time.sleep(self.poll_sec)

    def _logs(self, name: str) -> str:
        try:
            return self.core.read_namespaced_pod_log(name, self.ns, container=None)
        except self.client.ApiException:
            return ""

    def _delete_leftovers(self, pod: str, cm: str) -> None:
        for fn, obj in ((self.core.delete_namespaced_pod, pod),
                        (self.core.delete_namespaced_config_map, cm)):
            try:
                fn(obj, self.ns, grace_period_seconds=0) if fn is self.core.delete_namespaced_pod \
                    else fn(obj, self.ns)
            except self.client.ApiException as e:
                if e.status != 404:
                    raise

    def apply_daemonset(self, ds: dict[str, Any]) -> None:
        apps = self.client.AppsV1Api()
        name = ds["metadata"]["name"]
        try:
            apps.create_namespaced_daemon_set(self.ns, ds)
        except self.client.ApiException as e:
            if e.status != 409:
                raise
            apps.replace_namespaced_daemon_set(name, self.ns, ds)
