"""Running one pod to completion on Kubernetes, and classifying how it ended.

`Cluster` is the seam the runner talks to; tests substitute a fake. `KubeCluster`
is the real implementation on the official Python client (imported lazily, so
nothing else needs a kubeconfig).
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

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


TRANSIENT_STATUSES = {409, 429, 500, 502, 503, 504}


def is_transient_api_error(e: Exception) -> bool:
    """Conflicts and brief server trouble. Many workers creating pods at once update the
    namespace's ResourceQuota together, which Kubernetes answers with 409 (or, when the
    conflict is reported as an admission failure, a 403 that mentions the quota)."""
    status = getattr(e, "status", None)
    if status in TRANSIENT_STATUSES:
        return True
    return status == 403 and "quota" in api_message(e).lower()


def retry_transient(fn: Callable[[], Any], tries: int = 8, sleep: Callable[[float], None] = time.sleep) -> Any:
    """Call `fn`, retrying transient Kubernetes API errors with short jittered backoff."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - only API errors with a transient status are retried
            if attempt == tries - 1 or not is_transient_api_error(e):
                raise
            sleep(min(0.3 * 2**attempt, 5.0) + random.random() * 0.5)


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


def deployment_rolled_out(dep: Any) -> bool:
    """True only when the *new* pods have fully replaced the old ones.

    Ready-count alone is not enough: during a rollout the old pod stays ready and keeps
    serving (with the old settings) until the new one is up and the old one is gone.
    """
    want = dep.spec.replicas or 1
    st = dep.status
    return (
        (st.observed_generation or 0) >= (dep.metadata.generation or 0)
        and (st.updated_replicas or 0) == want
        and (st.replicas or 0) == want
        and (st.ready_replicas or 0) == want
    )


def api_message(e: Exception) -> str:
    """The human-readable message Kubernetes put in an API error (no request data, no secrets)."""
    import json

    try:
        msg = json.loads(getattr(e, "body", "") or "{}").get("message", "")
    except ValueError:
        msg = ""
    return msg[:400]


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
        # Creating pods one at a time (it takes ~100 ms) avoids most quota update conflicts.
        self._create_lock = threading.Lock()

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
            with self._create_lock:
                retry_transient(lambda: self.core.create_namespaced_config_map(self.ns, configmap))
                retry_transient(lambda: self.core.create_namespaced_pod(self.ns, pod))
            return self._wait(name, start_timeout, run_timeout)
        except self.client.ApiException as e:
            return PodResult("infra", f"kubernetes API error {e.status}: {e.reason}: {api_message(e)}")
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

    # -- metering proxy ---------------------------------------------------

    def read_secret_value(self, name: str, key: str) -> str | None:
        import base64

        try:
            sec = self.core.read_namespaced_secret(name, self.ns)
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise
        raw = (sec.data or {}).get(key)
        return base64.b64decode(raw).decode() if raw else None

    def meter_client(self, name: str, port: int, secret_name: str):
        from .meter import KubeMeterClient

        token = self.read_secret_value(secret_name, "meter-token")
        if not token:
            raise RuntimeError(f"secret {secret_name!r} has no meter-token; run `swebench-run meter-up` first")
        return KubeMeterClient(self.client.ApiClient(), self.ns, name, port, token)

    def meter_up(self, configmap: dict, deployment: dict, service: dict,
                 secret_name: str, upstream_key: str) -> None:
        """Create or update the proxy. An existing meter token is kept, so running pods keep working."""
        import secrets

        token = self.read_secret_value(secret_name, "meter-token") or secrets.token_urlsafe(24)
        body = self.client.V1Secret(
            metadata=self.client.V1ObjectMeta(name=secret_name, namespace=self.ns),
            string_data={"upstream-key": upstream_key, "meter-token": token},
        )
        try:
            self.core.create_namespaced_secret(self.ns, body)
        except self.client.ApiException as e:
            if e.status != 409:
                raise
            self.core.replace_namespaced_secret(secret_name, self.ns, body)

        apps = self.client.AppsV1Api()
        for create, replace, name, obj in (
            (self.core.create_namespaced_config_map, self.core.replace_namespaced_config_map,
             configmap["metadata"]["name"], configmap),
            (apps.create_namespaced_deployment, apps.replace_namespaced_deployment,
             deployment["metadata"]["name"], deployment),
            (self.core.create_namespaced_service, self.core.patch_namespaced_service,
             service["metadata"]["name"], service),
        ):
            try:
                create(self.ns, obj)
            except self.client.ApiException as e:
                if e.status != 409:
                    raise
                replace(name, self.ns, obj)

    def apply_workload(self, configmap: dict, deployment: dict, service: dict) -> None:
        """Create or replace a ConfigMap, Deployment and Service (used by the agent cache pod)."""
        apps = self.client.AppsV1Api()
        for create, replace, name, obj in (
            (self.core.create_namespaced_config_map, self.core.replace_namespaced_config_map,
             configmap["metadata"]["name"], configmap),
            (apps.create_namespaced_deployment, apps.replace_namespaced_deployment,
             deployment["metadata"]["name"], deployment),
            (self.core.create_namespaced_service, self.core.patch_namespaced_service,
             service["metadata"]["name"], service),
        ):
            try:
                create(self.ns, obj)
            except self.client.ApiException as e:
                if e.status != 409:
                    raise
                replace(name, self.ns, obj)

    def wait_deployment_ready(self, name: str, timeout_sec: int = 300) -> bool:
        apps = self.client.AppsV1Api()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            dep = apps.read_namespaced_deployment_status(name, self.ns)
            if deployment_rolled_out(dep):
                return True
            time.sleep(self.poll_sec)
        return False

    def meter_down(self, name: str) -> None:
        """Remove the proxy's deployment, service and configmap (not the secret)."""
        apps = self.client.AppsV1Api()
        for fn in (lambda: apps.delete_namespaced_deployment(name, self.ns),
                   lambda: self.core.delete_namespaced_service(name, self.ns),
                   lambda: self.core.delete_namespaced_config_map(name, self.ns)):
            try:
                fn()
            except self.client.ApiException as e:
                if e.status != 404:
                    raise
