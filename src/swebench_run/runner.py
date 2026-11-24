"""The controller: runs inference and evaluation per instance, with resume.

Rules that matter:
- An instance is only re-run when a stage has never run or ended in `infra_error`
  with retries left. Anything the task itself caused (agent error, timeout, a
  failing patch) is final.
- Infra failures keep their own status; they are never recorded as "unresolved".
- Evaluation always happens in a fresh pod, never the one the agent used.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

from .cluster import Cluster
from .config import RunConfig
from .contract import extract_result, strip_result_block
from .manifests import DEADLINE_GRACE_SEC, eval_objects, inference_objects, instance_key
from .meter import MeterClient, stats_to_usage
from .prompt import build_prompt
from .state import (
    EV_INFRA,
    EV_PATCH_FAILED,
    EV_RESOLVED,
    EV_SKIPPED,
    EV_TIMEOUT,
    EV_UNRESOLVED,
    INF_AGENT_ERROR,
    INF_INFRA,
    INF_OK,
    INF_TIMEOUT,
    InstanceState,
    Store,
    now,
)


@dataclass
class InstanceSpec:
    instance_id: str
    problem_statement: str
    image: str  # full image reference
    eval_script: str
    gold_patch: str = ""


class NoTestOutput(Exception):
    """The evaluation log has no test output at all (the grader can't judge)."""


# (instance_id, patch, eval_log) -> (resolved, report). Raises NoTestOutput.
Grader = Callable[[str, str, str], tuple[bool, dict]]

_PATCH_FAILED = re.compile(r"^SBR_PATCH_APPLY_FAILED$", re.MULTILINE)
_EVAL_TIMEOUT = re.compile(r"^SBR_EVAL_TIMEOUT$", re.MULTILINE)

_CORE_FILES = {"patch.diff", "meta.json", "usage.json"}


def backoff_sec(attempt: int, base: float = 15.0, cap: float = 300.0) -> float:
    """Wait before retry number `attempt` (1-based): 15s, 30s, 60s ... capped."""
    return min(base * (2 ** (attempt - 1)), cap)


class Runner:
    def __init__(
        self,
        cfg: RunConfig,
        store: Store,
        cluster: Cluster,
        instances: dict[str, InstanceSpec],
        grader: Grader,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda m: print(m, flush=True),
        meter: MeterClient | None = None,
    ):
        self.cfg, self.store, self.cluster = cfg, store, cluster
        self.instances, self.grader = instances, grader
        self.sleep, self.log = sleep, log
        self.meter = meter

    # -- stages -----------------------------------------------------------

    def infer(self, inst: InstanceSpec) -> None:
        cfg, st = self.cfg, self.store.load(inst.instance_id)
        if self.meter and st.inference.attempts == 0 and st.needs_inference(cfg.max_retries):
            # Fresh start for this instance. Retries keep adding to the same counters, so
            # the report shows what was really spent, including attempts that failed.
            self.log(f"[{inst.instance_id}] resetting meter counters")
            self._meter_call(self.meter.reset, inst.instance_id)
        while st.needs_inference(cfg.max_retries):
            st.inference.attempts += 1
            began = time.time()
            self.log(f"[{inst.instance_id}] inference: started (attempt {st.inference.attempts})")
            status, error = self._infer_once(inst, st)
            if self.meter:
                stats = self._meter_call(self.meter.usage, inst.instance_id)
                if stats and stats.get("calls"):
                    self._record_usage(st, stats_to_usage(stats))
            st.inference.status, st.inference.error = status, error
            if status != INF_INFRA:  # an infra failure never reached the model
                st.provider, st.model = cfg.provider.kind, cfg.provider.model
            st.inference.wall_sec = round(time.time() - began, 1)
            st.inference.finished_at = now()
            self.store.save(st)
            self.log(f"[{inst.instance_id}] inference: {status}" + (f" ({error})" if error else ""))
            if status == INF_INFRA and st.inference.attempts <= cfg.max_retries:
                self.sleep(backoff_sec(st.inference.attempts))

    def _infer_once(self, inst: InstanceSpec, st: InstanceState) -> tuple[str, str | None]:
        cfg = self.cfg
        cm, pod = inference_objects(cfg, inst.instance_id, inst.image, build_prompt(inst.problem_statement))
        res = self.cluster.run_pod(
            cm, pod, cfg.k8s.pod_start_timeout_sec, cfg.limits.timeout_sec + DEADLINE_GRACE_SEC
        )
        if res.kind == "infra":
            return INF_INFRA, res.reason
        self.store.write(inst.instance_id, "agent.log", strip_result_block(res.logs))
        bundle = extract_result(res.logs)
        if bundle is None:
            return INF_INFRA, "pod finished without a result block"

        for name, data in bundle.files.items():
            if name == "patch.diff":
                self.store.write(inst.instance_id, "patch.diff", data)
            elif name not in _CORE_FILES:
                rel = name if name.startswith("trajectory/") else f"trajectory/{name}"
                self.store.write(inst.instance_id, rel, data)
        st.patch_bytes = len(bundle.files.get("patch.diff", b"").strip())
        if not self.meter:
            self._record_usage(st, bundle.usage)

        meta_status = bundle.meta.get("status")
        status = {"ok": INF_OK, "timeout": INF_TIMEOUT}.get(meta_status, INF_AGENT_ERROR)
        if status != INF_OK and self.meter is not None:
            stats = self._meter_call(self.meter.usage, inst.instance_id)
            if stats and stats.get("calls") and not stats.get("ok_calls"):
                # Every call to the model API failed (out of credit, bad key, rate limit...).
                # That is the account's problem, not the model's: retry later, never record a final agent_error.
                return INF_INFRA, "the model API rejected every call (check credit, key and limits)"
        if status == INF_AGENT_ERROR and self.meter is not None:
            stats = self._meter_call(self.meter.usage, inst.instance_id)
            if stats and stats.get("truncated"):
                # The model API cut a streamed answer short and the agent died on the broken
                # stream. That is the API's fault, not the task's: retry instead of recording
                # a final agent_error.
                return INF_INFRA, f"the model API cut {stats['truncated']} streamed answer(s) short"
        if status != INF_OK and self.meter is not None and not self._meter_healthy():
            # The agent's model calls go through the proxy; if it is down, the failure is the
            # cluster's, not the task's. Retry instead of recording a final agent_error.
            return INF_INFRA, "metering proxy unavailable during the run"
        return status, None

    def _meter_healthy(self) -> bool:
        try:
            return bool(self.meter.healthy())
        except Exception:  # noqa: BLE001
            return False

    def _meter_call(self, fn, instance_id: str):
        """Metering is advisory: a failure to reach the proxy never fails an instance."""
        try:
            return fn(instance_key(instance_id))
        except Exception as e:  # noqa: BLE001
            self.log(f"[{instance_id}] meter unavailable: {type(e).__name__}")
            return None

    def _record_usage(self, st: InstanceState, usage: dict) -> None:
        st.steps = usage.get("steps")
        st.input_tokens = usage.get("input_tokens")
        st.output_tokens = usage.get("output_tokens")
        cost = usage.get("cost_usd")
        p = self.cfg.provider
        if (
            cost is None
            and st.input_tokens is not None
            and st.output_tokens is not None
            and p.input_price_per_mtok is not None
            and p.output_price_per_mtok is not None
        ):
            cost = (
                st.input_tokens * p.input_price_per_mtok + st.output_tokens * p.output_price_per_mtok
            ) / 1_000_000
        st.cost_usd = cost

    def evaluate(self, inst: InstanceSpec, patch: str | None = None) -> None:
        """Grade the stored patch (or `patch`, e.g. a gold patch) in a fresh pod."""
        cfg, st = self.cfg, self.store.load(inst.instance_id)
        if patch is not None:
            self.store.write(inst.instance_id, "patch.diff", patch)
            st.patch_bytes = len(patch.strip())
            st.eval.status = None
            st.eval.attempts = 0
        if not st.has_patch():
            st.eval.status = EV_SKIPPED
            self.store.save(st)
            return
        while st.eval.retryable(cfg.max_retries):
            st.eval.attempts += 1
            began = time.time()
            self.log(f"[{inst.instance_id}] eval: started (attempt {st.eval.attempts})")
            status, error = self._eval_once(inst)
            st.eval.status, st.eval.error = status, error
            st.eval.wall_sec = round(time.time() - began, 1)
            st.eval.finished_at = now()
            self.store.save(st)
            self.log(f"[{inst.instance_id}] eval: {status}" + (f" ({error})" if error else ""))
            if status == EV_INFRA and st.eval.attempts <= cfg.max_retries:
                self.sleep(backoff_sec(st.eval.attempts))

    def _eval_once(self, inst: InstanceSpec) -> tuple[str, str | None]:
        cfg = self.cfg
        patch = self.store.read_text(inst.instance_id, "patch.diff")
        try:
            cm, pod = eval_objects(cfg, inst.instance_id, inst.image, patch, inst.eval_script)
        except ValueError as e:
            return EV_PATCH_FAILED, str(e)
        res = self.cluster.run_pod(
            cm, pod, cfg.k8s.pod_start_timeout_sec, cfg.limits.eval_timeout_sec + DEADLINE_GRACE_SEC
        )
        if res.kind == "infra":
            return EV_INFRA, res.reason
        self.store.write(inst.instance_id, "eval.log", res.logs)
        if _PATCH_FAILED.search(res.logs):
            return EV_PATCH_FAILED, None
        if _EVAL_TIMEOUT.search(res.logs):
            return EV_TIMEOUT, None
        try:
            resolved, report = self.grader(inst.instance_id, patch, res.logs)
        except NoTestOutput:
            return EV_INFRA, "evaluation produced no test output"

        self.store.write(inst.instance_id, "report.json", json.dumps(report, indent=2, default=str))
        return (EV_RESOLVED if resolved else EV_UNRESOLVED), None

    # -- orchestration ----------------------------------------------------

    def process(self, instance_id: str, stages: Iterable[str] = ("infer", "eval")) -> None:
        inst = self.instances[instance_id]
        stages = set(stages)
        try:
            if "infer" in stages:
                self.infer(inst)
            if "eval" in stages and self.store.load(instance_id).needs_eval(self.cfg.max_retries):
                self.evaluate(inst)
        except Exception as e:  # never let one instance take the run down
            self._record_controller_error(instance_id, stages, e)

    def _record_controller_error(self, instance_id: str, stages: set[str], err: Exception) -> None:
        st = self.store.load(instance_id)
        stage = st.eval if st.inference.status not in (None, INF_INFRA) and "eval" in stages else st.inference
        stage.status = INF_INFRA
        stage.error = f"controller exception: {type(err).__name__}: {err}"
        stage.attempts = max(stage.attempts, self.cfg.max_retries + 1)
        self.store.save(st)
        self.log(f"[{instance_id}] controller exception: {err!r}")

    def total_cost(self) -> float:
        return sum(s.cost_usd or 0.0 for s in self.store.all_states())

    def pending(self, ids: Iterable[str], stages: Iterable[str] = ("infer", "eval")) -> list[str]:
        """Instances that still have work, in the given order."""
        stages, out = set(stages), []
        for i in ids:
            st = self.store.load(i)
            mr = self.cfg.max_retries
            if ("infer" in stages and st.needs_inference(mr)) or (
                "eval" in stages and st.needs_eval(mr)
            ):
                out.append(i)
        return out

    def run(self, ids: Iterable[str], stages: Iterable[str] = ("infer", "eval")) -> None:
        todo = self.pending(ids, stages)
        cap = self.cfg.limits.max_cost_usd
        self.log(f"{len(todo)} instance(s) to process with {self.cfg.workers} worker(s)")

        def work(i: str) -> None:
            if cap is not None and self.total_cost() >= cap:
                self.log(f"[{i}] skipped: cost cap ${cap:.2f} reached")
                return
            self.process(i, stages)

        with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
            futures = [pool.submit(work, i) for i in todo]
            try:
                for f in as_completed(futures):
                    f.result()
            except KeyboardInterrupt:
                for f in futures:
                    f.cancel()
                raise


def fill_usage_from_meter(cfg: RunConfig, store: Store, meter: MeterClient, log: Callable[[str], None] = lambda m: print(m, flush=True)) -> int:
    """Copy the proxy's counters into stored results that have no usage yet.

    Lets a report be rebuilt after a run where the proxy could not be read at the time.
    Returns how many instances were filled. Never overwrites numbers already recorded.
    """
    helper = Runner(cfg, store, None, {}, lambda *a: (False, {}), log=log)  # type: ignore[arg-type]
    filled = 0
    for st in store.all_states():
        if st.input_tokens is not None:
            continue
        try:
            stats = meter.usage(instance_key(st.instance_id))
        except Exception as e:  # noqa: BLE001
            log(f"[{st.instance_id}] meter unavailable: {type(e).__name__}")
            continue
        usage = stats_to_usage(stats)
        if usage:
            helper._record_usage(st, usage)
            store.save(st)
            filled += 1
    return filled
