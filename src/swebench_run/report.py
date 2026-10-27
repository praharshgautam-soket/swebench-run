"""Final report: report.json, report.md, report.html and an official-format predictions.jsonl."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .config import RunConfig
from .state import FINAL_STATUSES, InstanceState, Store

# "Attempted" = the task itself ran to a verdict. Infra errors and instances not
# yet run are excluded: the cluster failing says nothing about the model.
_NOT_ATTEMPTED = {"infra_error", "pending"}


def build_report(cfg: RunConfig, states: list[InstanceState], total_instances: int | None = None) -> dict[str, Any]:
    total = total_instances if total_instances is not None else len(states)
    counts = {s: 0 for s in FINAL_STATUSES}
    for st in states:
        counts[st.final_status] += 1
    # Instances in the dataset subset that have no state yet are pending too.
    counts["pending"] += max(0, total - len(states))

    attempted = sum(n for s, n in counts.items() if s not in _NOT_ATTEMPTED)
    resolved = counts["resolved"]

    wall = sum((st.inference.wall_sec or 0) + (st.eval.wall_sec or 0) for st in states)
    costs = [st.cost_usd for st in states if st.cost_usd is not None]

    instances = []
    for st in sorted(states, key=lambda s: s.instance_id):
        instances.append(
            {
                "instance_id": st.instance_id,
                "status": st.final_status,
                "inference_status": st.inference.status,
                "eval_status": st.eval.status,
                "error": st.eval.error or st.inference.error,
                "input_tokens": st.input_tokens,
                "output_tokens": st.output_tokens,
                "cost_usd": st.cost_usd,
                "wall_sec": round((st.inference.wall_sec or 0) + (st.eval.wall_sec or 0), 1),
                "agent_steps": st.steps,
                "attempts": {"inference": st.inference.attempts, "eval": st.eval.attempts},
                "trajectory": f"instances/{st.instance_id}/trajectory/",
                "patch": f"instances/{st.instance_id}/patch.diff",
            }
        )

    return {
        "run_id": cfg.run_id,
        "model": cfg.provider.model,
        "provider": cfg.provider.kind,
        "harness": cfg.harness.name,
        "dataset": f"{cfg.dataset.name}:{cfg.dataset.split}",
        "headline": {
            "resolved": resolved,
            "total": total,
            "attempted": attempted,
            "resolved_pct_of_total": round(100 * resolved / total, 2) if total else None,
            "resolved_pct_of_attempted": round(100 * resolved / attempted, 2) if attempted else None,
        },
        "counts": counts,
        "totals": {
            # None when no harness reported usage; never a made-up zero.
            "cost_usd": round(sum(costs), 4) if costs else None,
            "cost_reported_for": len(costs),
            "instance_seconds": round(wall, 1),
            "instances_per_hour_per_worker": (
                round(len([s for s in states if s.final_status not in _NOT_ATTEMPTED]) / (wall / 3600), 2)
                if wall
                else None
            ),
        },
        "instances": instances,
    }


def write_predictions(cfg: RunConfig, store: Store, states: list[InstanceState]) -> Path:
    """predictions.jsonl in the official format, usable with sb-cli or the SWE-bench harness."""
    path = store.root / "predictions.jsonl"
    with open(path, "w") as f:
        for st in sorted(states, key=lambda s: s.instance_id):
            if st.inference.status is None:
                continue
            f.write(
                json.dumps(
                    {
                        "instance_id": st.instance_id,
                        "model_name_or_path": f"{cfg.harness.name}__{cfg.provider.model}",
                        "model_patch": store.read_text(st.instance_id, "patch.diff"),
                    }
                )
                + "\n"
            )
    return path


def _fmt(v: Any, nd: int = 4) -> str:
    return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render_markdown(r: dict[str, Any]) -> str:
    h, t = r["headline"], r["totals"]
    lines = [
        f"# SWE-bench report: {r['run_id']}",
        "",
        f"- Harness: `{r['harness']}`  Model: `{r['model']}` via {r['provider']}",
        f"- Dataset: `{r['dataset']}`",
        "",
        f"## Resolved: {h['resolved']} / {h['total']} "
        f"({_fmt(h['resolved_pct_of_total'], 1)}% of total, "
        f"{_fmt(h['resolved_pct_of_attempted'], 1)}% of {h['attempted']} attempted)",
        "",
        "| Status | Count |",
        "|---|---|",
    ]
    lines += [f"| {s} | {n} |" for s, n in r["counts"].items() if n]
    lines += [
        "",
        f"Total cost: {_fmt(t['cost_usd'])} USD (reported for {t['cost_reported_for']} instance(s)). "
        f"Summed instance time: {t['instance_seconds']}s. "
        f"Throughput: {_fmt(t['instances_per_hour_per_worker'], 2)} instances/hour per worker.",
        "",
        "## Instances",
        "",
        "| Instance | Status | Tokens in/out | Cost USD | Wall s | Steps | Trajectory |",
        "|---|---|---|---|---|---|---|",
    ]
    for i in r["instances"]:
        lines.append(
            f"| {i['instance_id']} | {i['status']} | {_fmt(i['input_tokens'])}/{_fmt(i['output_tokens'])} "
            f"| {_fmt(i['cost_usd'])} | {i['wall_sec']} | {_fmt(i['agent_steps'])} "
            f"| [{i['trajectory']}]({i['trajectory']}) |"
        )
    return "\n".join(lines) + "\n"


def render_html(r: dict[str, Any]) -> str:
    h = r["headline"]
    e = html.escape
    rows = "\n".join(
        f"<tr class='{e(i['status'])}'><td>{e(i['instance_id'])}</td><td>{e(i['status'])}</td>"
        f"<td>{_fmt(i['input_tokens'])}/{_fmt(i['output_tokens'])}</td><td>{_fmt(i['cost_usd'])}</td>"
        f"<td>{i['wall_sec']}</td><td>{_fmt(i['agent_steps'])}</td>"
        f"<td><a href='{e(i['trajectory'])}'>trajectory</a> · <a href='{e(i['patch'])}'>patch</a></td></tr>"
        for i in r["instances"]
    )
    counts = "".join(f"<li>{e(s)}: {n}</li>" for s, n in r["counts"].items() if n)
    return f"""<!doctype html><meta charset="utf-8">
<title>SWE-bench report {e(r['run_id'])}</title>
<style>body{{font:14px system-ui;margin:2rem}}table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:4px 8px}}tr.resolved{{background:#e6f6e6}}
tr.infra_error{{background:#fff3cd}}</style>
<h1>{e(r['harness'])} · {e(r['model'])}</h1>
<h2>Resolved {h['resolved']} / {h['total']} ({_fmt(h['resolved_pct_of_total'], 1)}% of total,
{_fmt(h['resolved_pct_of_attempted'], 1)}% of {h['attempted']} attempted)</h2>
<ul>{counts}</ul>
<p>Cost: {_fmt(r['totals']['cost_usd'])} USD · instance time {r['totals']['instance_seconds']}s</p>
<table><tr><th>Instance</th><th>Status</th><th>Tokens in/out</th><th>Cost USD</th><th>Wall s</th><th>Steps</th><th>Links</th></tr>
{rows}</table>"""


def write_report(cfg: RunConfig, store: Store, total_instances: int | None = None) -> dict[str, Any]:
    states = store.all_states()
    report = build_report(cfg, states, total_instances)
    (store.root / "report.json").write_text(json.dumps(report, indent=2))
    (store.root / "report.md").write_text(render_markdown(report))
    (store.root / "report.html").write_text(render_html(report))
    write_predictions(cfg, store, states)
    return report
