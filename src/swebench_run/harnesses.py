"""Harness registry.

A harness is a *bundle*: an image holding `/opt/harness/entrypoint.sh` (shared,
from bundles/common) plus `/opt/harness/run-agent.sh` (the only harness-specific
part). The controller never imports harness code; it only needs the image name
and any extra env vars the bundle wants. Adding a harness means adding a folder
under `bundles/` and, if it needs extra env, one entry here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import RunConfig


class UnknownHarness(KeyError):
    pass


@dataclass(frozen=True)
class HarnessSpec:
    name: str
    description: str
    # Extra env vars for the pod, derived from config.harness.options.
    env_keys: tuple[str, ...] = field(default_factory=tuple)

    def extra_env(self, cfg: RunConfig) -> dict[str, str]:
        opts = cfg.harness.options
        unknown = set(opts) - set(self.env_keys)
        if unknown:
            raise ValueError(
                f"harness {self.name!r} does not accept option(s): {', '.join(sorted(unknown))}"
            )
        return {k.upper(): str(v) for k, v in opts.items()}


REGISTRY: dict[str, HarnessSpec] = {
    "loop": HarnessSpec(
        name="loop",
        description="Loop (soketlabs/loop), headless `--print` mode, full-access, no web tools",
        env_keys=("thinking_level",),
    ),
}


def get_harness(name: str) -> HarnessSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownHarness(f"unknown harness {name!r}; known: {', '.join(sorted(REGISTRY))}")


def harness_image(cfg: RunConfig) -> str:
    image = cfg.k8s.harness_image
    if not image:
        raise ValueError(
            "k8s.harness_image is not set. Build the bundle with "
            "`swebench-run build-bundle` and push it to a registry the cluster can pull from."
        )
    return image
