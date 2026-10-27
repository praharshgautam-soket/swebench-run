import pytest

from swebench_run.state import (
    EV_INFRA,
    EV_RESOLVED,
    EV_SKIPPED,
    EV_UNRESOLVED,
    INF_AGENT_ERROR,
    INF_INFRA,
    INF_OK,
    INF_TIMEOUT,
    InstanceState,
    Store,
)


def st(inf=None, ev=None, patch=10, inf_attempts=1, ev_attempts=1):
    s = InstanceState("x__y-1", patch_bytes=patch)
    s.inference.status, s.inference.attempts = inf, inf_attempts if inf else 0
    s.eval.status, s.eval.attempts = ev, ev_attempts if ev else 0
    return s


def test_fresh_instance_needs_inference_only():
    s = st()
    assert s.needs_inference(2) and not s.needs_eval(2)
    assert s.final_status == "pending"


def test_finished_instance_is_skipped_on_resume():
    s = st(INF_OK, EV_RESOLVED)
    assert not s.needs_inference(2) and not s.needs_eval(2)
    assert s.final_status == "resolved"


def test_infra_error_retried_until_attempts_exhausted():
    # max_retries=2 means 3 tries in total.
    assert st(INF_INFRA, inf_attempts=1).needs_inference(2)
    assert st(INF_INFRA, inf_attempts=2).needs_inference(2)
    assert not st(INF_INFRA, inf_attempts=3).needs_inference(2)
    assert st(INF_INFRA, inf_attempts=3).final_status == "infra_error"


def test_infra_never_becomes_unresolved():
    s = st(INF_OK, EV_INFRA, ev_attempts=3)
    assert s.final_status == "infra_error"
    assert not s.needs_eval(2)
    assert s.final_status != "unresolved"


def test_task_caused_failures_are_final_not_retried():
    for status in (INF_AGENT_ERROR, INF_TIMEOUT):
        s = st(status, patch=0)
        assert not s.needs_inference(5)
    assert st(INF_OK, EV_UNRESOLVED).final_status == "unresolved"


def test_empty_patch_statuses():
    assert st(INF_OK, patch=0).final_status == "empty_patch"
    assert st(INF_AGENT_ERROR, patch=0).final_status == "agent_error"
    assert st(INF_TIMEOUT, patch=0).final_status == "timeout"
    assert not st(INF_OK, patch=0).needs_eval(2)


def test_eval_waits_for_real_inference_result():
    assert not st(INF_INFRA).needs_eval(2)
    assert st(INF_TIMEOUT).needs_eval(2)  # a partial patch is still graded
    assert st(INF_OK, EV_SKIPPED).final_status == "skipped"


def test_store_roundtrip_and_atomic_overwrite(tmp_path):
    store = Store(tmp_path)
    s = store.load("django__django-1")
    assert s.final_status == "pending"
    s.inference.status, s.cost_usd = INF_OK, 0.25
    store.save(s)
    again = Store(tmp_path).load("django__django-1")
    assert again.inference.status == INF_OK and again.cost_usd == 0.25
    assert [p.name for p in (tmp_path / "instances" / "django__django-1").iterdir()] == ["state.json"]


def test_corrupt_state_file_means_not_done(tmp_path):
    store = Store(tmp_path)
    d = store.instance_dir("a__b-1")
    d.mkdir(parents=True)
    (d / "state.json").write_text("{not json")
    assert store.load("a__b-1").final_status == "pending"


def test_store_rejects_path_escape(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        store.instance_dir("../evil")
    with pytest.raises(ValueError):
        store.write("a__b-1", "../../evil.txt", "x")
