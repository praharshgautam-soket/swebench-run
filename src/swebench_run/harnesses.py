"""Harness registry.

A harness is a *bundle*: `entrypoint.sh` (shared, bundles/common) plus
`run-agent.sh` (the only harness-specific part) and the agent itself. The
controller never imports harness code. It delivers a bundle to the pod one of three ways:

- image mode: `k8s.harness_image` names an image holding /opt/harness (needs a registry);
- release mode: an init container downloads the agent from `release_url`
  (checked against its `.sha256`);
- cache mode: the agent is too big to download per pod (hundreds of MB), so one cache pod
  installs it once and serves a checksummed tarball inside the cluster.

In the last two modes the two scripts arrive in the task ConfigMap.
Adding a harness means adding a folder under bundles/ and one entry here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources

from .config import RunConfig


class UnknownHarness(KeyError):
    pass


@dataclass(frozen=True)
class CacheSpec:
    """An npm-installed agent served from an in-cluster cache pod."""

    package: str  # npm package, e.g. "@deepseek-ai/dsh"
    version: str  # exact version: nothing floats
    service: str = "sbr-dsh-cache"
    port: int = 8081
    image: str = "node:22-bookworm-slim"

    @property
    def tarball(self) -> str:
        return f"dsh-{self.version}.tar.gz"


@dataclass(frozen=True)
class HarnessSpec:
    name: str
    description: str
    # Extra env vars for the pod, derived from config.harness.options.
    env_keys: tuple[str, ...] = field(default_factory=tuple)
    # tar.gz with one top-level directory holding the agent binary; a `.sha256`
    # file must sit next to it. None means image/cache mode only.
    release_url: str | None = None
    # Same, with a "{version}" slot filled from the run config's `version` option (or
    # `default_version`). The version is checked against VERSION_RE: it goes into a shell script.
    release_template: str | None = None
    default_version: str | None = None
    cache: CacheSpec | None = None

    def extra_env(self, cfg: RunConfig) -> dict[str, str]:
        opts = cfg.harness.options
        unknown = set(opts) - set(self.env_keys)
        if unknown:
            raise ValueError(
                f"harness {self.name!r} does not accept option(s): {', '.join(sorted(unknown))}"
            )
        # YAML reads an unquoted on/off as a boolean; hand the pod the word the user wrote.
        return {k.upper(): ("on" if v is True else "off" if v is False else str(v)) for k, v in opts.items()}

    def scripts(self) -> dict[str, str]:
        """entrypoint.sh and run-agent.sh, as shipped in the package."""
        root = resources.files("swebench_run").joinpath("bundles")
        return {
            "entrypoint.sh": root.joinpath("common/entrypoint.sh").read_text(),
            "run-agent.sh": root.joinpath(f"{self.name}/run-agent.sh").read_text(),
        }

    def resolved_release_url(self, cfg: RunConfig) -> str | None:
        """The download URL for this run: a fixed release_url, or the template for the chosen version."""
        if self.release_template:
            version = str(cfg.harness.options.get("version") or self.default_version)
            if not VERSION_RE.match(version):
                raise ValueError(f"harness version {version!r} is not like v0.7.0")
            return self.release_template.format(version=version)
        return self.release_url

    def can_fetch(self) -> bool:
        return bool(self.release_url or self.release_template or self.cache)

    def fetch_init(self, cfg: RunConfig) -> tuple[str, str, dict]:
        """(image, shell script, resources) for the init container that delivers the agent."""
        if self.cache:
            return (
                cfg.k8s.fetch_image,
                cache_fetch_script(self.cache, cfg.k8s.namespace),
                CACHE_FETCH_RESOURCES,
            )
        url = self.resolved_release_url(cfg)
        if url:
            return cfg.k8s.fetch_image, fetch_script(url), RELEASE_FETCH_RESOURCES
        raise ValueError(f"harness {self.name!r} has no download source")


VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")

# Every container declares cpu, memory and ephemeral-storage: the namespace quota requires it.
RELEASE_FETCH_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "64Mi", "ephemeral-storage": "100Mi"},
    "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "1Gi"},
}
CACHE_FETCH_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "500Mi"},
    "limits": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "3Gi"},
}


REGISTRY: dict[str, HarnessSpec] = {
    "loop": HarnessSpec(
        name="loop",
        description="Loop (soketlabs/loop), headless `--print` mode, full-access, no web tools",
        env_keys=("thinking_level", "web", "version", "compaction"),
        release_template=(
            "https://github.com/soketlabs/loop/releases/download/{version}/"
            "loop-x86_64-unknown-linux-gnu.tar.gz"
        ),
        default_version="v0.7.0",
    ),
    "deepseek": HarnessSpec(
        name="deepseek",
        description="DeepSeek Harness (dsh) 0.2.1-rc.5, `headless` profile, full access, telemetry off",
        env_keys=("web",),
        cache=CacheSpec(package="@deepseek-ai/dsh", version="0.2.1-rc.5"),
    ),
}


def get_harness(name: str) -> HarnessSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownHarness(f"unknown harness {name!r}; known: {', '.join(sorted(REGISTRY))}")


def fetch_script(release_url: str) -> str:
    """Shell run by the init container (busybox sh, curlimages/curl)."""
    return (
        "set -eu\n"
        "mkdir -p /harness/bin /tmp/dl\n"
        "cd /tmp/dl\n"
        f'curl -fsSL --retry 5 --retry-delay 5 "{release_url}" -o agent.tgz\n'
        f'curl -fsSL --retry 5 --retry-delay 5 "{release_url}.sha256" -o agent.sha256\n'
        'echo "$(cut -d\' \' -f1 agent.sha256)  agent.tgz" | sha256sum -c -\n'
        "tar -xzf agent.tgz --strip-components=1 -C /harness/bin\n"
        "chmod +x /harness/bin/*\n"
        "cp /task/entrypoint.sh /task/run-agent.sh /harness/\n"
    )


def cache_fetch_script(cache: CacheSpec, namespace: str) -> str:
    """Init-container shell: fetch the cached agent from the in-cluster cache pod, verify, unpack.

    Works in the emptyDir (/harness), not the container's own small writable layer.
    Retries for several minutes, because the cache pod may still be installing.
    """
    url = f"http://{cache.service}.{namespace}.svc.cluster.local:{cache.port}/{cache.tarball}"
    return (
        "set -eu\n"
        "mkdir -p /harness/dsh\n"
        "cd /harness\n"
        f'curl -fsS --retry 60 --retry-delay 10 --retry-connrefused --retry-all-errors "{url}" -o dsh.tgz\n'
        f'curl -fsS --retry 5 --retry-delay 5 --retry-all-errors "{url}.sha256" -o dsh.sha256\n'
        'echo "$(cut -d\' \' -f1 dsh.sha256)  dsh.tgz" | sha256sum -c -\n'
        "tar -xzf dsh.tgz -C /harness/dsh\n"
        "rm -f dsh.tgz dsh.sha256\n"
        "cp /task/entrypoint.sh /task/run-agent.sh /harness/\n"
    )
