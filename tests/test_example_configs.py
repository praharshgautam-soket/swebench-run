"""The example configs in configs/ must load, so the README never ships a broken one."""
from pathlib import Path

import pytest

from swebench_run.config import RunConfig
from swebench_run.harnesses import get_harness

CONFIGS = sorted((Path(__file__).resolve().parent.parent / "configs").glob("*.yaml"))


def test_there_are_example_configs():
    assert len(CONFIGS) >= 4


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.name)
def test_example_config_loads_and_its_harness_options_are_accepted(path):
    cfg = RunConfig.load(path)
    assert cfg.run_id and cfg.provider.model
    get_harness(cfg.harness.name).extra_env(cfg)  # raises on an unknown option


def test_unquoted_yaml_on_off_reach_the_pod_as_the_words_on_and_off():
    import yaml

    opts = yaml.safe_load("web: on\nthinking_level: off\ncompaction: off")  # YAML turns these into booleans
    assert opts == {"web": True, "thinking_level": False, "compaction": False}
    cfg = RunConfig.from_dict({"provider": {"kind": "soket", "model": "m"}, "harness": {"name": "loop", "options": opts}})
    assert get_harness("loop").extra_env(cfg) == {"WEB": "on", "THINKING_LEVEL": "off", "COMPACTION": "off"}
