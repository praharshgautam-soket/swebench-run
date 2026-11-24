import json

from swebench_run.report import build_report, render_html, render_markdown, write_report
from swebench_run.state import (
    EV_INFRA,
    EV_RESOLVED,
    EV_UNRESOLVED,
    INF_AGENT_ERROR,
    INF_INFRA,
    INF_OK,
    InstanceState,
    Store,
)


def state(iid, inf, ev=None, patch=5, cost=None, wall=60.0):
    s = InstanceState(iid, patch_bytes=patch, cost_usd=cost, steps=7)
    s.inference.status, s.inference.attempts, s.inference.wall_sec = inf, 1, wall
    s.eval.status = ev
    return s


def sample():
    return [
        state("a__a-1", INF_OK, EV_RESOLVED, cost=0.5),
        state("b__b-2", INF_OK, EV_UNRESOLVED, cost=0.25),
        state("c__c-3", INF_INFRA, patch=0),
        state("d__d-4", INF_AGENT_ERROR, patch=0),
        state("e__e-5", INF_OK, EV_INFRA),
    ]


def test_headline_counts_exclude_infra_from_attempted(cfg):
    r = build_report(cfg, sample(), total_instances=10)
    assert r["headline"]["resolved"] == 1
    assert r["headline"]["total"] == 10
    assert r["headline"]["attempted"] == 3  # resolved, unresolved, agent_error
    assert r["headline"]["resolved_pct_of_total"] == 10.0
    assert r["headline"]["resolved_pct_of_attempted"] == 33.33
    assert r["counts"]["infra_error"] == 2
    assert r["counts"]["pending"] == 5  # 10 total, 5 have state


def test_cost_is_none_when_nobody_reported(cfg):
    r = build_report(cfg, [state("a__a-1", INF_OK, EV_RESOLVED)])
    assert r["totals"]["cost_usd"] is None
    assert "n/a" in render_markdown(r)


def test_cost_sums_reported_values(cfg):
    r = build_report(cfg, sample())
    assert r["totals"]["cost_usd"] == 0.75
    assert r["totals"]["cost_reported_for"] == 2


def test_empty_run_does_not_divide_by_zero(cfg):
    r = build_report(cfg, [])
    assert r["headline"]["resolved_pct_of_total"] is None
    assert r["headline"]["resolved_pct_of_attempted"] is None


def test_markdown_and_html_contain_every_instance(cfg):
    r = build_report(cfg, sample())
    md, page = render_markdown(r), render_html(r)
    for s in sample():
        assert s.instance_id in md and s.instance_id in page


def test_write_report_files_and_predictions_format(cfg):
    store = Store(cfg.run_dir)
    for s in sample():
        store.save(s)
        store.write(s.instance_id, "patch.diff", f"patch for {s.instance_id}\n" if s.patch_bytes else "")
    # An instance that never ran must not appear in predictions.
    store.save(InstanceState("z__z-9"))
    write_report(cfg, store, total_instances=6)
    for name in ("report.json", "report.md", "report.html", "predictions.jsonl"):
        assert (store.root / name).exists()
    rows = [json.loads(l) for l in (store.root / "predictions.jsonl").read_text().splitlines()]
    assert {r["instance_id"] for r in rows} == {s.instance_id for s in sample()}
    assert set(rows[0]) == {"instance_id", "model_name_or_path", "model_patch"}
    assert rows[0]["model_name_or_path"] == "loop__m"


def tagged(iid, ev, provider, model):
    s = state(iid, INF_OK, ev)
    s.provider, s.model = provider, model
    return s


def test_mixed_providers_are_split_in_the_report(cfg):
    states = [
        tagged("a__a-1", EV_RESOLVED, "soket", "GLM-5.3"),
        tagged("b__b-2", EV_UNRESOLVED, "soket", "GLM-5.3"),
        tagged("c__c-3", EV_RESOLVED, "openrouter", "z-ai/glm-5.3"),
        tagged("d__d-4", EV_RESOLVED, "openrouter", "z-ai/glm-5.3"),
        state("e__e-5", INF_OK, EV_RESOLVED),  # recorded before provider tracking
    ]
    r = build_report(cfg, states)
    assert r["mixed_providers"] is True
    assert r["by_provider"]["soket/GLM-5.3"] == {"tasks": 2, "resolved": 1, "resolved_pct": 50.0}
    assert r["by_provider"]["openrouter/z-ai/glm-5.3"]["resolved"] == 2
    assert r["by_provider"]["unrecorded"]["tasks"] == 1
    md = render_markdown(r)
    assert "mixes providers" in md and "soket/GLM-5.3" in md
    assert "mixes providers" in render_html(r)


def test_single_provider_report_has_no_mixed_banner(cfg):
    r = build_report(cfg, [tagged("a__a-1", EV_RESOLVED, "soket", "GLM-5.3")])
    assert r["mixed_providers"] is False
    assert "mixes providers" not in render_markdown(r)


def test_infra_and_pending_tasks_are_not_counted_in_provider_split(cfg):
    states = [tagged("a__a-1", EV_RESOLVED, "soket", "GLM-5.3"), state("c__c-3", INF_INFRA, patch=0)]
    assert build_report(cfg, states)["by_provider"]["soket/GLM-5.3"]["tasks"] == 1


def test_predictions_use_the_model_each_task_actually_ran_on(cfg):
    store = Store(cfg.run_dir)
    for s in (tagged("a__a-1", EV_RESOLVED, "soket", "GLM-5.3"),
              tagged("b__b-2", EV_RESOLVED, "openrouter", "z-ai/glm-5.3")):
        store.save(s)
        store.write(s.instance_id, "patch.diff", "p\n")
    write_report(cfg, store, total_instances=2)
    rows = {json.loads(l)["instance_id"]: json.loads(l)["model_name_or_path"]
            for l in (store.root / "predictions.jsonl").read_text().splitlines()}
    assert rows == {"a__a-1": "loop__GLM-5.3", "b__b-2": "loop__z-ai/glm-5.3"}
