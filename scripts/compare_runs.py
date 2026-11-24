"""Compare two runs of the same tasks: status counts, per-repo resolved counts, and the
tasks only one of them solved.

    python scripts/compare_runs.py RUN_A RUN_B [--results DIR]   (DIR defaults to results)

Reads <results>/<run_id>/instances/*/state.json. Tasks a run has not finished yet count as
"pending" for that run, so the totals always add up to the size of the larger run.
"""
import argparse
import glob
import json
import os
from collections import Counter

STATUSES = ["resolved", "unresolved", "empty_patch", "patch_failed", "agent_error", "timeout", "infra_error", "pending"]


def load_run(results, run):
    out = {}
    for p in glob.glob(os.path.join(results, run, "instances", "*", "state.json")):
        s = json.load(open(p))
        status = s["eval"]["status"] or s["inference"]["status"] or "pending"
        if status == "ok":  # inference done, not graded yet
            status = "pending"
        out[s["instance_id"]] = {"status": status, "steps": s.get("steps"), "patch": s.get("patch_bytes")}
    return out


def repo_of(iid):
    return iid.rsplit("-", 1)[0].replace("__", "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--results", default="results")
    args = ap.parse_args()
    A, B = load_run(args.results, args.a), load_run(args.results, args.b)
    ids = sorted(set(A) | set(B))
    total = len(ids)

    print(f"## Status counts over {total} tasks\n")
    print(f"| status | {args.a} | {args.b} |\n|---|---:|---:|")
    ca = Counter(A.get(i, {"status": "pending"})["status"] for i in ids)
    cb = Counter(B.get(i, {"status": "pending"})["status"] for i in ids)
    for st in STATUSES:
        if ca[st] or cb[st]:
            print(f"| {st} | {ca[st]} ({100 * ca[st] / total:.1f}%) | {cb[st]} ({100 * cb[st] / total:.1f}%) |")

    print("\n## Resolved by repo\n")
    print(f"| repo | tasks | {args.a} | {args.b} |\n|---|---:|---:|---:|")
    repos = sorted({repo_of(i) for i in ids})
    for r in repos:
        rid = [i for i in ids if repo_of(i) == r]
        ra = sum(A.get(i, {}).get("status") == "resolved" for i in rid)
        rb = sum(B.get(i, {}).get("status") == "resolved" for i in rid)
        print(f"| {r} | {len(rid)} | {ra} | {rb} |")

    done = [i for i in ids if A.get(i, {}).get("status", "pending") != "pending"]
    both = [i for i in done if A[i]["status"] == "resolved" and B.get(i, {}).get("status") == "resolved"]
    only_b = [i for i in done if A[i]["status"] != "resolved" and B.get(i, {}).get("status") == "resolved"]
    only_a = [i for i in done if A[i]["status"] == "resolved" and B.get(i, {}).get("status") != "resolved"]
    print(f"\nOf the {len(done)} tasks {args.a} has finished: both solved {len(both)}, "
          f"only {args.b} {len(only_b)}, only {args.a} {len(only_a)}")
    print(f"\nWhy {args.a} missed the {len(only_b)} that only {args.b} solved: "
          + ", ".join(f"{k} {v}" for k, v in Counter(A[i]["status"] for i in only_b).most_common()))


if __name__ == "__main__":
    main()
