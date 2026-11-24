"""Per-instance state on disk, and the rules that decide what still needs work.

Layout under `<output_dir>/<run_id>/instances/<instance_id>/`:
    state.json   this module's record
    patch.diff   the agent's patch
    agent.log    agent output
    eval.log     test output from the evaluation pod
    trajectory/  whatever the harness wrote
    usage.json   optional token / cost numbers from the harness

Infra failures are their own status and are never folded into "unresolved".
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Inference stage outcomes.
INF_OK = "ok"
INF_AGENT_ERROR = "agent_error"
INF_TIMEOUT = "timeout"
INF_INFRA = "infra_error"

# Evaluation stage outcomes.
EV_RESOLVED = "resolved"
EV_UNRESOLVED = "unresolved"
EV_PATCH_FAILED = "patch_failed"
EV_TIMEOUT = "timeout"
EV_INFRA = "infra_error"
EV_SKIPPED = "skipped"  # empty patch: nothing to evaluate

# Final per-instance statuses shown in the report.
FINAL_STATUSES = (
    "resolved",
    "unresolved",
    "empty_patch",
    "patch_failed",
    "agent_error",
    "timeout",
    "infra_error",
    "pending",
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class StageRecord:
    status: str | None = None
    attempts: int = 0
    error: str | None = None
    wall_sec: float | None = None
    finished_at: float | None = None

    def retryable(self, max_retries: int) -> bool:
        """Never run, or failed on infra with attempts left (1 try + max_retries)."""
        if self.status is None:
            return True
        return self.status in (INF_INFRA, EV_INFRA) and self.attempts <= max_retries


@dataclass
class InstanceState:
    instance_id: str
    inference: StageRecord = field(default_factory=StageRecord)
    eval: StageRecord = field(default_factory=StageRecord)
    patch_bytes: int = 0
    steps: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    # Which provider/model produced the agent's patch. None for results recorded before
    # this was tracked (see `swebench-run tag-existing`).
    provider: str | None = None
    model: str | None = None

    # -- decisions --------------------------------------------------------

    def has_patch(self) -> bool:
        return self.patch_bytes > 0

    def needs_inference(self, max_retries: int) -> bool:
        return self.inference.retryable(max_retries)

    def needs_eval(self, max_retries: int) -> bool:
        """Inference finished for real (not infra) and there is a patch to grade."""
        if self.inference.status in (None, INF_INFRA):
            return False
        if not self.has_patch():
            return False
        return self.eval.retryable(max_retries)

    @property
    def final_status(self) -> str:
        inf, ev = self.inference.status, self.eval.status
        if inf is None:
            return "pending"
        if inf == INF_INFRA:
            return "infra_error"
        if not self.has_patch():
            return {INF_AGENT_ERROR: "agent_error", INF_TIMEOUT: "timeout"}.get(inf, "empty_patch")
        if ev is None:
            return "pending"
        if ev == EV_INFRA:
            return "infra_error"
        return ev

    # -- (de)serialisation ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InstanceState":
        return cls(
            instance_id=d["instance_id"],
            inference=StageRecord(**d.get("inference", {})),
            eval=StageRecord(**d.get("eval", {})),
            patch_bytes=d.get("patch_bytes", 0),
            steps=d.get("steps"),
            input_tokens=d.get("input_tokens"),
            output_tokens=d.get("output_tokens"),
            cost_usd=d.get("cost_usd"),
            provider=d.get("provider"),
            model=d.get("model"),
        )


class Store:
    """Filesystem-backed results store. One thread owns one instance at a time."""

    def __init__(self, run_dir: str | Path):
        self.root = Path(run_dir)
        self.instances_dir = self.root / "instances"
        self.instances_dir.mkdir(parents=True, exist_ok=True)

    def instance_dir(self, instance_id: str) -> Path:
        if not _SAFE_ID.match(instance_id):
            raise ValueError(f"unsafe instance id: {instance_id!r}")
        return self.instances_dir / instance_id

    def load(self, instance_id: str) -> InstanceState:
        path = self.instance_dir(instance_id) / "state.json"
        if not path.exists():
            return InstanceState(instance_id)
        try:
            return InstanceState.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, KeyError, TypeError):
            # A torn or foreign file is treated as "not done"; it gets rewritten.
            return InstanceState(instance_id)

    def save(self, state: InstanceState) -> None:
        d = self.instance_dir(state.instance_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".state.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(state.to_dict(), indent=2))
        os.replace(tmp, d / "state.json")

    def write(self, instance_id: str, name: str, data: bytes | str) -> Path:
        d = self.instance_dir(instance_id)
        d.mkdir(parents=True, exist_ok=True)
        target = (d / name).resolve()
        if d.resolve() not in target.parents:
            raise ValueError(f"artifact path escapes the instance dir: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        return target

    def read_text(self, instance_id: str, name: str) -> str:
        path = self.instance_dir(instance_id) / name
        return path.read_text() if path.exists() else ""

    def all_states(self) -> list[InstanceState]:
        out = []
        for d in sorted(self.instances_dir.iterdir()):
            if (d / "state.json").exists():
                out.append(self.load(d.name))
        return out


def now() -> float:
    return time.time()
