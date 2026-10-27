from conftest import FakeCluster, make_runner, ok_eval, result_log

from swebench_run.cluster import PodResult


def infra(reason="node lost"):
    return PodResult("infra", reason)


def test_happy_path_resolved_and_unresolved(cfg):
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log()),
                   "b__b-2": PodResult("completed", logs=result_log())},
        evaluation={"a__a-1": ok_eval(True), "b__b-2": ok_eval(False)},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1", "b__b-2"])
    runner.run(["a__a-1", "b__b-2"])
    assert store.load("a__a-1").final_status == "resolved"
    assert store.load("b__b-2").final_status == "unresolved"
    assert "diff --git" in store.read_text("a__a-1", "patch.diff")


def test_eval_runs_in_a_separate_pod_after_inference(cfg):
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log())},
        evaluation={"a__a-1": ok_eval(True)},
    )
    runner, _ = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    assert cluster.calls == [("inference", "a__a-1"), ("eval", "a__a-1")]


def test_resume_skips_finished_instances(cfg):
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log())},
        evaluation={"a__a-1": ok_eval(True)},
    )
    runner, _ = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    first = list(cluster.calls)
    runner2, _ = make_runner(cfg, cluster, ["a__a-1"])
    runner2.run(["a__a-1"])
    assert cluster.calls == first  # nothing re-run


def test_infra_error_retried_with_backoff_then_succeeds(cfg):
    sleeps = []
    cluster = FakeCluster(
        inference={"a__a-1": [infra(), infra(), PodResult("completed", logs=result_log())]},
        evaluation={"a__a-1": ok_eval(True)},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1"], sleeps)
    runner.run(["a__a-1"])
    assert store.load("a__a-1").final_status == "resolved"
    assert store.load("a__a-1").inference.attempts == 3
    assert sleeps == [15.0, 30.0]  # exponential


def test_infra_error_exhausts_retries_and_is_not_unresolved(cfg):
    cluster = FakeCluster(inference={"a__a-1": [infra()]})
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    s = store.load("a__a-1")
    assert s.final_status == "infra_error"
    assert s.inference.attempts == cfg.max_retries + 1
    assert [c for c in cluster.calls if c[0] == "eval"] == []
    # And a later resume does not burn more attempts on it.
    n = len(cluster.calls)
    runner2, _ = make_runner(cfg, cluster, ["a__a-1"])
    runner2.run(["a__a-1"])
    assert len(cluster.calls) == n


def test_agent_error_and_timeout_with_no_patch_are_final(cfg):
    cluster = FakeCluster(
        inference={
            "a__a-1": PodResult("completed", logs=result_log(patch="", status="agent_error")),
            "b__b-2": PodResult("completed", logs=result_log(patch="", status="timeout")),
            "c__c-3": PodResult("completed", logs=result_log(patch="", status="ok")),
        }
    )
    ids = ["a__a-1", "b__b-2", "c__c-3"]
    runner, store = make_runner(cfg, cluster, ids)
    runner.run(ids)
    assert [store.load(i).final_status for i in ids] == ["agent_error", "timeout", "empty_patch"]
    assert all(c[0] == "inference" for c in cluster.calls)  # no pod wasted on empty patches


def test_timed_out_agent_with_partial_patch_is_still_graded(cfg):
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log(status="timeout"))},
        evaluation={"a__a-1": ok_eval(False)},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    assert store.load("a__a-1").final_status == "unresolved"
    assert store.load("a__a-1").inference.status == "timeout"


def test_patch_apply_failure_and_eval_timeout(cfg):
    cluster = FakeCluster(
        inference={i: PodResult("completed", logs=result_log()) for i in ("a__a-1", "b__b-2")},
        evaluation={
            "a__a-1": PodResult("completed", logs="SBR_PATCH_APPLY_FAILED\n"),
            "b__b-2": PodResult("completed", logs="SBR_EVAL_BEGIN\nSBR_EVAL_TIMEOUT\nSBR_EVAL_END\n"),
        },
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1", "b__b-2"])
    runner.run(["a__a-1", "b__b-2"])
    assert store.load("a__a-1").final_status == "patch_failed"
    assert store.load("b__b-2").final_status == "timeout"


def test_eval_without_test_output_is_infra_not_unresolved(cfg):
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log())},
        evaluation={"a__a-1": [PodResult("completed", logs="image crashed\n")]},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    assert store.load("a__a-1").final_status == "infra_error"


def test_pod_without_result_block_is_infra(cfg):
    cluster = FakeCluster(inference={"a__a-1": [PodResult("completed", logs="died early")]})
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    assert store.load("a__a-1").final_status == "infra_error"


def test_usage_and_cost_from_token_prices(cfg):
    usage = {"input_tokens": 2_000_000, "output_tokens": 500_000, "steps": 12}
    cluster = FakeCluster(
        inference={"a__a-1": PodResult("completed", logs=result_log(usage=usage))},
        evaluation={"a__a-1": ok_eval(True)},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.run(["a__a-1"])
    s = store.load("a__a-1")
    assert s.steps == 12 and s.cost_usd == 3.0  # 2M*$1 + 0.5M*$2 per Mtok


def test_cost_cap_stops_new_work(cfg):
    cfg.limits.max_cost_usd = 1.0
    cfg.workers = 1
    usage = {"cost_usd": 1.5}
    ids = ["a__a-1", "b__b-2"]
    cluster = FakeCluster(
        inference={i: PodResult("completed", logs=result_log(usage=usage)) for i in ids},
        evaluation={i: ok_eval(True) for i in ids},
    )
    runner, store = make_runner(cfg, cluster, ids)
    runner.run(ids)
    assert store.load("a__a-1").final_status == "resolved"
    assert store.load("b__b-2").final_status == "pending"


def test_unexpected_exception_does_not_kill_run(cfg):
    class Boom(FakeCluster):
        def run_pod(self, configmap, pod, *a):
            if pod["metadata"]["annotations"]["swebench-run/instance-id"] == "a__a-1":
                raise RuntimeError("boom")
            return super().run_pod(configmap, pod, *a)

    cluster = Boom(
        inference={"b__b-2": PodResult("completed", logs=result_log())},
        evaluation={"b__b-2": ok_eval(True)},
    )
    runner, store = make_runner(cfg, cluster, ["a__a-1", "b__b-2"])
    runner.run(["a__a-1", "b__b-2"])
    assert store.load("a__a-1").final_status == "infra_error"
    assert store.load("b__b-2").final_status == "resolved"


def test_gold_patch_evaluation_path(cfg):
    cluster = FakeCluster(evaluation={"a__a-1": ok_eval(True)})
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.evaluate(runner.instances["a__a-1"], patch="diff --git a/x b/x\n+gold\n")
    assert store.load("a__a-1").eval.status == "resolved"
    assert [c[0] for c in cluster.calls] == ["eval"]


def test_empty_patch_is_never_graded_as_resolved(cfg):
    cluster = FakeCluster()
    runner, store = make_runner(cfg, cluster, ["a__a-1"])
    runner.evaluate(runner.instances["a__a-1"], patch="")
    assert store.load("a__a-1").final_status != "resolved"
    assert cluster.calls == []
