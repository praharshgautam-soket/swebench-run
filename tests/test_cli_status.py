from swebench_run.cli import main
from swebench_run.state import EV_RESOLVED, INF_OK, InstanceState, Store


def test_status_counts_and_totals(cfg, tmp_path, capsys):
    cfg.dataset.subset.first_n = 3  # a stated size: no dataset download in a unit test
    store = Store(cfg.run_dir)
    for i, (ev, tin) in enumerate([(EV_RESOLVED, 100), (EV_RESOLVED, 50), (None, None)]):
        st = InstanceState(f"a__a-{i}", patch_bytes=5, input_tokens=tin, output_tokens=10 if tin else None, steps=3 if tin else None)
        st.inference.status = INF_OK
        st.eval.status = ev
        store.save(st)
    cfg_path = tmp_path / "run.yaml"
    cfg.save(cfg_path)
    assert main(["status", "-c", str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert "2 finished of 3" in out and "1 in progress or waiting" in out and "resolved" in out
    assert "tokens in 150" in out


def test_eta_needs_enough_data_then_estimates_from_the_finish_times():
    from swebench_run.cli import _eta_line

    def done(i, t):
        st = InstanceState(f"a__a-{i}", patch_bytes=5)
        st.inference.status, st.eval.status, st.eval.finished_at = INF_OK, EV_RESOLVED, t
        return st

    few = [done(i, 1000 + i * 60) for i in range(3)]
    assert _eta_line(few, 3) == ""
    states = [done(i, 1000 + i * 60) for i in range(11)]  # 10 gaps of 60 s: 1 task/min
    states += [InstanceState(f"b__b-{i}") for i in range(59)]  # 59 not done
    line = _eta_line(states, 11)
    assert "1.00 tasks/min" in line and "about 1.0 h left for 59 task(s)" in line


def test_tag_existing_labels_only_finished_untagged_tasks(cfg, tmp_path, capsys):
    store = Store(cfg.run_dir)
    done = InstanceState("a__a-1", patch_bytes=5)
    done.inference.status, done.eval.status = INF_OK, EV_RESOLVED
    already = InstanceState("b__b-2", patch_bytes=5, provider="openrouter", model="z-ai/glm-5.3")
    already.inference.status, already.eval.status = INF_OK, EV_RESOLVED
    waiting = InstanceState("c__c-3")
    infra = InstanceState("d__d-4")
    infra.inference.status = "infra_error"
    for s in (done, already, waiting, infra):
        store.save(s)
    cfg_path = tmp_path / "run.yaml"
    cfg.save(cfg_path)
    assert main(["tag-existing", "-c", str(cfg_path), "--provider", "soket", "--model", "GLM-5.3"]) == 0
    assert "tagged 1 finished task(s) as soket/GLM-5.3" in capsys.readouterr().out
    assert store.load("a__a-1").provider == "soket"
    assert store.load("b__b-2").provider == "openrouter"  # not overwritten
    assert store.load("c__c-3").provider is None and store.load("d__d-4").provider is None


def test_eta_ignores_long_idle_gaps_and_old_history():
    from swebench_run.cli import _eta_line

    def done(i, t):
        st = InstanceState(f"a__a-{i}", patch_bytes=5)
        st.inference.status, st.eval.status, st.eval.finished_at = INF_OK, EV_RESOLVED, t
        return st

    # 10 tasks one per minute, then a 25-hour stop, then 10 more one per minute
    t0 = 1000.0
    times = [t0 + i * 60 for i in range(10)] + [t0 + 25 * 3600 + i * 60 for i in range(10)]
    states = [done(i, t) for i, t in enumerate(times)] + [InstanceState(f"b__b-{i}") for i in range(30)]
    line = _eta_line(states, 20)
    # 19 gaps: 18 of one minute plus the 25 h stop counted as 20 min = 38 min for 19 tasks
    assert "pace 0.50 tasks/min" in line  # not 0.01, which the raw 25 h span would give
    assert "idle gaps ignored" in line


def test_status_uses_the_planned_size_so_a_fresh_run_shows_waiting_tasks(cfg, tmp_path, capsys):
    cfg.dataset.subset.first_n = 13
    store = Store(cfg.run_dir)
    st = InstanceState("a__a-0", patch_bytes=5)
    st.inference.status, st.eval.status = INF_OK, EV_RESOLVED
    store.save(st)  # only ONE task has a saved result yet
    cfg_path = tmp_path / "run.yaml"
    cfg.save(cfg_path)
    assert main(["status", "-c", str(cfg_path)]) == 0
    assert "1 finished of 13, 12 in progress or waiting" in capsys.readouterr().out


def test_planned_total_from_ids_first_n_and_never_below_saved(cfg):
    from swebench_run.cli import planned_total

    cfg.dataset.subset.instance_ids = ["a", "b", "b", "c"]
    assert planned_total(cfg, 1) == 3
    assert planned_total(cfg, 9) == 9  # never below what is already saved
    cfg.dataset.subset.instance_ids = None
    cfg.dataset.subset.first_n = 5
    assert planned_total(cfg, 2) == 5
