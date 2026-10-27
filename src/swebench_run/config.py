"""Run configuration: one YAML file describes a whole run.

API keys never live here. `provider.api_key_env` names the *local* env var that
`swebench-run create-secret` copies into a Kubernetes Secret; pods read the key
from that Secret.
"""
from __future__ import annotations

import dataclasses
import re
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


# kind -> (default base_url, default api-key env var). A None base_url means the
# user must supply one; Soket's gateway URL is deliberately not guessed.
PROVIDER_PRESETS: dict[str, tuple[str | None, str | None]] = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai-compatible": (None, None),
    "soket": (None, "SOKET_API_KEY"),
}

_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
# The Loop entrypoint writes these into a JSON file with plain shell
# substitution, so quotes and backslashes would corrupt it.
_SHELL_JSON_UNSAFE = re.compile(r'["\\\s]')


@dataclass
class ProviderConfig:
    kind: str = "openrouter"
    base_url: str = ""
    api_key_env: str = ""
    model: str = ""
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None


@dataclass
class HarnessConfig:
    name: str = "loop"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Subset:
    instance_ids: list[str] | None = None
    first_n: int | None = None


@dataclass
class DatasetConfig:
    name: str = "princeton-nlp/SWE-bench_Lite"
    split: str = "test"
    subset: Subset = field(default_factory=Subset)


@dataclass
class Limits:
    max_steps: int = 100
    timeout_sec: int = 1800
    eval_timeout_sec: int = 1800
    max_cost_usd: float | None = None


@dataclass
class K8sConfig:
    namespace: str = "swebench"
    secret_name: str = "swebench-api-key"
    secret_key: str = "api-key"
    node_selector: dict[str, str] = field(
        default_factory=lambda: {"kubernetes.io/arch": "amd64"}
    )
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    image_pull_secrets: list[str] = field(default_factory=list)
    image_pull_policy: str = "IfNotPresent"
    # Docker Hub namespace of the prebuilt instance images.
    instance_image_namespace: str = "swebench"
    # Image holding the harness bundle; defaults to <name>-bundle, set by the user.
    harness_image: str = ""
    cpu_request: str = "2"
    cpu_limit: str = "4"
    memory_request: str = "4Gi"
    memory_limit: str = "8Gi"
    ephemeral_storage_request: str = "10Gi"
    ephemeral_storage_limit: str = "30Gi"
    # Instance images are multi-GB; allow a long Pending phase for the pull.
    pod_start_timeout_sec: int = 1800


@dataclass
class RunConfig:
    run_id: str = "run-001"
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    workers: int = 4
    limits: Limits = field(default_factory=Limits)
    k8s: K8sConfig = field(default_factory=K8sConfig)
    output_dir: str = "results"
    # Extra attempts after the first, for infra errors only.
    max_retries: int = 2

    @property
    def run_dir(self) -> Path:
        return Path(self.output_dir) / self.run_id

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunConfig":
        cfg = _build(cls, data or {}, "")
        cfg.apply_provider_defaults()
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "RunConfig":
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))

    # -- rules ------------------------------------------------------------

    def apply_provider_defaults(self) -> None:
        preset = PROVIDER_PRESETS.get(self.provider.kind)
        if preset is None:
            return
        base_url, key_env = preset
        if not self.provider.base_url and base_url:
            self.provider.base_url = base_url
        if not self.provider.api_key_env and key_env:
            self.provider.api_key_env = key_env

    def validate(self) -> None:
        p = self.provider
        if p.kind not in PROVIDER_PRESETS:
            raise ConfigError(
                f"provider.kind must be one of {sorted(PROVIDER_PRESETS)}, got {p.kind!r}"
            )
        if not p.model:
            raise ConfigError("provider.model is required")
        if not p.base_url.startswith(("http://", "https://")):
            raise ConfigError("provider.base_url must be an http(s) URL")
        if not p.api_key_env:
            raise ConfigError("provider.api_key_env is required")
        for name, value in (("provider.base_url", p.base_url), ("provider.model", p.model)):
            if _SHELL_JSON_UNSAFE.search(value):
                raise ConfigError(f"{name} must not contain quotes, backslashes or spaces")
        if not _RUN_ID.match(self.run_id):
            raise ConfigError("run_id must be lowercase letters, digits and dashes (max 41 chars)")
        if not _DNS_LABEL.match(self.k8s.namespace):
            raise ConfigError(f"k8s.namespace {self.k8s.namespace!r} is not a valid DNS label")
        if self.workers < 1:
            raise ConfigError("workers must be >= 1")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be >= 0")
        lim = self.limits
        if lim.timeout_sec <= 0 or lim.eval_timeout_sec <= 0 or lim.max_steps <= 0:
            raise ConfigError("limits.max_steps / timeout_sec / eval_timeout_sec must be > 0")
        if lim.max_cost_usd is not None and lim.max_cost_usd <= 0:
            raise ConfigError("limits.max_cost_usd must be > 0 when set")
        sub = self.dataset.subset
        if sub.instance_ids and sub.first_n:
            raise ConfigError("dataset.subset: set instance_ids or first_n, not both")
        if sub.first_n is not None and sub.first_n < 1:
            raise ConfigError("dataset.subset.first_n must be >= 1")


def select_instances(all_ids: list[str], subset: Subset) -> list[str]:
    """Apply the optional subset to the dataset's instance ids (order preserved)."""
    if subset.instance_ids:
        known = set(all_ids)
        missing = [i for i in subset.instance_ids if i not in known]
        if missing:
            raise ConfigError(f"unknown instance ids: {', '.join(missing[:5])}")
        return list(dict.fromkeys(subset.instance_ids))
    if subset.first_n:
        return all_ids[: subset.first_n]
    return list(all_ids)


def _build(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'} must be a mapping")
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {path or 'config'}: {', '.join(sorted(unknown))}")
    kwargs = {}
    for name, value in data.items():
        hint = hints[name]
        if dataclasses.is_dataclass(hint):
            kwargs[name] = _build(hint, value or {}, f"{path}{name}")
        else:
            kwargs[name] = value
    return cls(**kwargs)
