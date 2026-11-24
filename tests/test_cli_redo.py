import json

from swebench_run.cli import main
from swebench_run.state import EV_RESOLVED, INF_AGENT_ERROR, INF_OK, InstanceState, Store


def make(store, iid, provider, inf, ev, patch=5, log=""):
    st = InstanceState(iid, patch_bytes=patch, provider=provider, model="m")
    st.inference.status, st.eval.status = inf, ev
    store.save(st)
    store.write(iid, "agent.log", log)


def setup(cfg, tmp_path):
    store = Store(cfg.run_dir)
    make(store, "a__a-1", "soket", INF_AGENT_ERROR, None, patch=0)                   # soket failure: keep
    make(store, "b__b-2", "openrouter", INF_AGENT_ERROR, None, patch=0)              # limit failure
    make(store, "c__c-3", "openrouter", INF_OK, "unresolved", log="loop: 402 insufficient credits")  # partial
    make(store, "d__d-4", "openrouter", INF_OK, EV_RESOLVED, log="all good")         # fine
    path = tmp_path / "run.yaml"
    cfg.save(path)
    return store, str(path)


def test_dry_run_lists_but_deletes_nothing(cfg, tmp_path, capsys):
    store, path = setup(cfg, tmp_path)
    assert main(["redo", "-c", path, "--provider", "openrouter", "--status", "agent_error"]) == 0
    out = capsys.readouterr().out
    assert "b__b-2" in out and "dry run" in out
    assert len(store.all_states()) == 4


def test_yes_deletes_only_the_selected_provider_and_matches(cfg, tmp_path, capsys):
    store, path = setup(cfg, tmp_path)
    assert main(["redo", "-c", path, "--provider", "openrouter", "--status", "agent_error",
                 "--grep", "insufficient credits|402", "--yes"]) == 0
    left = {s.instance_id for s in store.all_states()}
    assert left == {"a__a-1", "d__d-4"}  # soket task and the good openrouter task survive
    assert "deleted 2 result folder(s)" in capsys.readouterr().out


def test_refuses_to_select_everything_by_accident(cfg, tmp_path, capsys):
    store, path = setup(cfg, tmp_path)
    assert main(["redo", "-c", path, "--yes"]) == 2
    assert len(store.all_states()) == 4
