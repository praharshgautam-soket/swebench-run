import pytest

from swebench_run.config import ConfigError, RunConfig, Subset, select_instances


def base(**over):
    d = {"provider": {"kind": "openrouter", "model": "z-ai/glm-5.3-flash"}}
    d.update(over)
    return d


def test_openrouter_defaults_filled_in():
    cfg = RunConfig.from_dict(base())
    assert cfg.provider.base_url == "https://openrouter.ai/api/v1"
    assert cfg.provider.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.dataset.name == "princeton-nlp/SWE-bench_Lite"
    assert cfg.dataset.split == "test"
    assert cfg.k8s.node_selector == {"kubernetes.io/arch": "amd64"}


def test_yaml_roundtrip(tmp_path):
    cfg = RunConfig.from_dict(base(workers=8, limits={"timeout_sec": 600, "max_cost_usd": 12.5}))
    path = tmp_path / "run.yaml"
    cfg.save(path)
    again = RunConfig.load(path)
    assert again.to_dict() == cfg.to_dict()
    assert again.limits.max_cost_usd == 12.5


def test_unknown_key_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        RunConfig.from_dict(base(wokers=3))
    with pytest.raises(ConfigError, match="unknown key"):
        RunConfig.from_dict(base(limits={"timeout": 5}))


def test_soket_needs_explicit_base_url():
    with pytest.raises(ConfigError, match="base_url"):
        RunConfig.from_dict({"provider": {"kind": "soket", "model": "m"}})
    cfg = RunConfig.from_dict(
        {"provider": {"kind": "soket", "model": "m", "base_url": "https://example.test/v1"}}
    )
    assert cfg.provider.api_key_env == "SOKET_API_KEY"


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"workers": 0}, "workers"),
        ({"run_id": "Bad_ID"}, "run_id"),
        ({"limits": {"timeout_sec": 0}}, "limits"),
        ({"limits": {"max_cost_usd": -1}}, "max_cost_usd"),
        ({"k8s": {"namespace": "Not_Valid"}}, "namespace"),
        ({"dataset": {"subset": {"first_n": 3, "instance_ids": ["a"]}}}, "not both"),
        ({"dataset": {"subset": {"first_n": 0}}}, "first_n"),
        ({"max_retries": -1}, "max_retries"),
    ],
)
def test_validation_errors(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        RunConfig.from_dict(base(**patch))


def test_model_with_quote_rejected():
    with pytest.raises(ConfigError, match="quotes"):
        RunConfig.from_dict({"provider": {"kind": "openrouter", "model": 'a"b'}})


def test_missing_model_rejected():
    with pytest.raises(ConfigError, match="model"):
        RunConfig.from_dict({"provider": {"kind": "openrouter"}})


def test_select_instances():
    ids = ["a", "b", "c", "d"]
    assert select_instances(ids, Subset()) == ids
    assert select_instances(ids, Subset(first_n=2)) == ["a", "b"]
    assert select_instances(ids, Subset(instance_ids=["c", "a", "c"])) == ["c", "a"]
    with pytest.raises(ConfigError, match="unknown instance"):
        select_instances(ids, Subset(instance_ids=["zzz"]))
