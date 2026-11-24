from types import SimpleNamespace as NS

from swebench_run.cluster import classify_pod


def pod(phase="Pending", reason=None, containers=(), inits=(), conditions=()):
    return NS(
        status=NS(
            phase=phase,
            reason=reason,
            message=None,
            container_statuses=list(containers),
            init_container_statuses=list(inits),
            conditions=list(conditions),
        )
    )


def cs(waiting=None, terminated=None):
    return NS(state=NS(waiting=waiting and NS(reason=waiting), terminated=terminated))


def term(reason=None, exit_code=0):
    return NS(reason=reason, exit_code=exit_code)


def test_succeeded_and_failed_are_completed_so_logs_decide():
    assert classify_pod(pod("Succeeded"), 0, 60).kind == "completed"
    assert classify_pod(pod("Failed"), 0, 60).kind == "completed"


def test_running_pod_is_left_alone():
    assert classify_pod(pod("Running"), 100, 60) is None


def test_oomkilled_is_infra():
    p = pod("Failed", containers=[cs(terminated=term("OOMKilled", 137))])
    assert classify_pod(p, 0, 60).kind == "infra"


def test_eviction_is_infra():
    assert classify_pod(pod("Failed", reason="Evicted"), 0, 60).kind == "infra"


def test_failed_init_container_is_infra_not_an_agent_failure():
    p = pod("Failed", inits=[cs(terminated=term("Error", 1))])
    r = classify_pod(p, 0, 60)
    assert r.kind == "infra" and "init container" in r.reason


def test_image_pull_gets_the_full_start_timeout_before_giving_up():
    p = pod("Pending", containers=[cs(waiting="ImagePullBackOff")])
    assert classify_pod(p, 30, 1800) is None  # multi-GB pulls take a while
    r = classify_pod(p, 1801, 1800)
    assert r.kind == "infra" and "ImagePullBackOff" in r.reason


def test_unschedulable_after_timeout():
    cond = NS(type="PodScheduled", status="False", message="0/3 nodes: insufficient cpu", reason="Unschedulable")
    r = classify_pod(pod("Pending", conditions=[cond]), 2000, 1800)
    assert r.kind == "infra" and "unschedulable" in r.reason


def _dep(gen=2, observed=2, want=1, updated=1, replicas=1, ready=1):
    return NS(
        metadata=NS(generation=gen),
        spec=NS(replicas=want),
        status=NS(observed_generation=observed, updated_replicas=updated, replicas=replicas, ready_replicas=ready),
    )


def test_rollout_is_done_only_when_new_pods_replaced_the_old_ones():
    from swebench_run.cluster import deployment_rolled_out

    assert deployment_rolled_out(_dep())
    # old pod still serving while the new one starts: 2 pods, only 1 updated
    assert not deployment_rolled_out(_dep(updated=1, replicas=2, ready=1))
    assert not deployment_rolled_out(_dep(updated=0, replicas=1, ready=1))  # nothing replaced yet
    assert not deployment_rolled_out(_dep(observed=1, gen=2))  # controller hasn't seen the change
    assert not deployment_rolled_out(_dep(ready=0))
