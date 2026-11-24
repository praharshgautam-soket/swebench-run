"""Build one self-contained HTML comparison report (inline SVG charts, light/dark) from the
state.json files of several runs.

    python scripts/make_report.py [--out reports/comparison.html]

Runs are looked up in results/<id>/instances and results-lite/<id>/instances (the second is a
copy of just the state.json files, made with tar over ssh)."""
import argparse
import glob
import html
import json
import os
import statistics as st
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = [  # id, short label, harness line, slot colour var
    ("full1", "Loop 0.5.0", "Loop 0.5.0 · thinking off · Soket + OpenRouter", "s1"),
    ("loop06-max-full", "Loop 0.6.0", "Loop 0.6.0 · thinking 'max' (GLM default effective) · Soket", "s2"),
    ("dsh-full2", "DeepSeek", "DeepSeek Harness 0.2.1-rc.5 · web off · Soket", "s3"),
]
SMALL = [("loop-think1", "Loop 0.5.0, medium, 13 tasks"), ("loop06-max13", "Loop 0.6.0, max, 13 tasks")]
OUTCOMES = [  # key, label, css var, dark text on it
    ("resolved", "Resolved", "good", False), ("unresolved", "Unresolved", "warn", True),
    ("nopatch", "Empty / bad patch", "gray", False), ("agent_error", "Agent error", "crit", False),
    ("timeout", "Timeout", "serious", True), ("pending", "Not finished", "pend", True),
]


def load(run):
    for base in ("results", "results-lite"):
        files = glob.glob(str(ROOT / base / run / "instances" / "*" / "state.json"))
        if files:
            out = {}
            for p in files:
                s = json.load(open(p))
                status = s["eval"]["status"] or s["inference"]["status"] or "pending"
                if status == "ok" and not s.get("patch_bytes"):
                    status = "nopatch"  # the agent finished but left no change, so nothing was graded
                status = {"ok": "pending", "empty_patch": "nopatch", "patch_failed": "nopatch",
                          "infra_error": "agent_error"}.get(status, status)
                out[s["instance_id"]] = {
                    "status": status, "steps": s.get("steps"), "tin": s.get("input_tokens"),
                    "tout": s.get("output_tokens"), "wall": s["inference"].get("wall_sec"),
                    "patch": s.get("patch_bytes"), "provider": s.get("provider"),
                }
            return out
    return None


def esc(x):
    return html.escape(str(x))


def repo_of(i):
    return i.rsplit("-", 1)[0].replace("__", "/")


def pct(a, b):
    return 100 * a / b if b else 0


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0


def quant(xs, q):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


# ---- charts (inline SVG) ---------------------------------------------------------------
def stacked_outcomes(data, labels, total):
    W, LBL, H, ROW = 720, 110, 0, 46
    bars, y = [], 30
    for run, name, color in labels:
        c = Counter(v["status"] for v in data[run].values())
        c["pending"] += total - sum(c.values())
        x, segs = LBL, []
        for key, lab, var, dark in OUTCOMES:
            n = c.get(key, 0)
            if not n:
                continue
            w = n / total * (W - LBL - 10)
            tip = f"{name}: {lab} {n} ({pct(n, total):.1f}%)"
            segs.append(f'<g><title>{esc(tip)}</title><rect x="{x:.1f}" y="{y}" width="{max(w - 2, 1):.1f}" height="26" rx="3" fill="var(--{var})"/>')
            if w > 26:
                segs.append(f'<text x="{x + w / 2 - 1:.1f}" y="{y + 17}" text-anchor="middle" class="seg{" dk" if dark else ""}">{n}</text>')
            segs.append("</g>")
            x += w
        bars.append(f'<text x="{LBL - 10}" y="{y + 17}" text-anchor="end" class="lab">{esc(name)}</text>' + "".join(segs))
        y += ROW
    legend, lx = [], LBL
    for key, lab, var, dark in OUTCOMES:
        legend.append(f'<rect x="{lx}" y="8" width="10" height="10" rx="2" fill="var(--{var})"/><text x="{lx + 14}" y="17" class="sm">{lab}</text>')
        lx += 14 + len(lab) * 6.2 + 14
    return f'<svg viewBox="0 0 {W} {y + 6}" role="img" aria-label="Outcome of each task per run">{"".join(legend)}{"".join(bars)}</svg>'


def repo_bars(data, labels):
    repos = sorted({repo_of(i) for i in data[labels[-1][0]]}, key=lambda r: -sum(repo_of(i) == r for i in data[labels[-1][0]]))
    W, LBL, RIGHT, BH, GAP = 720, 190, 120, 9, 16
    rows, y = [], 28
    for r in repos:
        ids = [i for i in data[labels[-1][0]] if repo_of(i) == r]
        n, cnts = len(ids), []
        rows.append(f'<text x="{LBL - 8}" y="{y + 16}" text-anchor="end" class="lab">{esc(r)} ({n})</text>')
        for k, (run, name, color) in enumerate(labels):
            got = sum(data[run].get(i, {}).get("status") == "resolved" for i in ids)
            cnts.append(got)
            w = pct(got, n) / 100 * (W - LBL - RIGHT)
            tip = f"{r} · {name}: {got}/{n} resolved ({pct(got, n):.0f}%)"
            rows.append(f'<g><title>{esc(tip)}</title><rect x="{LBL}" y="{y + k * (BH + 2)}" width="{max(w, 1.5):.1f}" height="{BH}" rx="2" fill="var(--{color})"/></g>')
        rows.append(f'<text x="{W - RIGHT + 10}" y="{y + 18}" class="sm">{" / ".join(map(str, cnts))}</text>')
        y += len(labels) * (BH + 2) + GAP
    axis = "".join(f'<line x1="{LBL + p / 100 * (W - LBL - RIGHT):.1f}" x2="{LBL + p / 100 * (W - LBL - RIGHT):.1f}" y1="20" y2="{y}" class="grid"/><text x="{LBL + p / 100 * (W - LBL - RIGHT):.1f}" y="{y + 14}" text-anchor="middle" class="sm">{p}%</text>' for p in (0, 25, 50, 75, 100))
    legend = "".join(f'<rect x="{LBL + i * 130}" y="{y + 28}" width="10" height="10" rx="2" fill="var(--{c})"/><text x="{LBL + i * 130 + 14}" y="{y + 37}" class="sm">{esc(n)}</text>' for i, (_, n, c) in enumerate(labels))
    return f'<svg viewBox="0 0 {W} {y + 48}" role="img" aria-label="Share of tasks resolved per repository">{axis}{"".join(rows)}{legend}<text x="{W - RIGHT + 10}" y="14" class="sm">solved (legend order)</text></svg>'


def histogram(values_by_run, labels, edges, xlabel, fmt):
    W, PW, PH = 720, 224, 120
    out, k = [], len(labels)
    for idx, (run, name, color) in enumerate(labels):
        vals = [v for v in values_by_run[run] if v is not None]
        counts = [0] * (len(edges) - 1)
        for v in vals:
            for b in range(len(edges) - 1):
                if edges[b] <= v < edges[b + 1] or (b == len(edges) - 2 and v >= edges[b + 1]):
                    counts[b] += 1
                    break
        mx = max(max(counts), 1)
        ox = idx * (PW + 14)
        bars = []
        bw = PW / len(counts)
        for b, c in enumerate(counts):
            h = c / mx * (PH - 20)
            lo, hi = fmt(edges[b]), (fmt(edges[b + 1]) if b < len(counts) - 1 else "+")
            tip = f"{name}: {lo}–{hi}: {c} tasks"
            bars.append(f'<g><title>{esc(tip)}</title><rect x="{ox + b * bw + 1:.1f}" y="{PH - h:.1f}" width="{bw - 2:.1f}" height="{h:.1f}" rx="2" fill="var(--{color})"/></g>')
        med = st.median(vals) if vals else 0
        out.append(f'<text x="{ox}" y="12" class="lab">{esc(name)}</text><text x="{ox + PW}" y="12" text-anchor="end" class="sm">median {fmt(med)}</text>'
                   + f'<g transform="translate(0,14)">{"".join(bars)}<line x1="{ox}" x2="{ox + PW}" y1="{PH}" y2="{PH}" class="grid"/>'
                   f'<text x="{ox}" y="{PH + 14}" class="sm">{fmt(edges[0])}</text><text x="{ox + PW}" y="{PH + 14}" text-anchor="end" class="sm">{fmt(edges[-1])}+</text></g>')
    return f'<svg viewBox="0 0 {W} {PH + 52}" role="img" aria-label="{esc(xlabel)}">{"".join(out)}<text x="{W / 2}" y="{PH + 46}" text-anchor="middle" class="sm">{esc(xlabel)}</text></svg>'


def hbars(items, fmt, title, W=340):
    """items: (label, value, colorvar)."""
    mx = max((v for _, v, _ in items), default=1) or 1
    rows, y = [], 0
    for lab, v, c in items:
        w = v / mx * (W - 190)
        rows.append(f'<g><title>{esc(lab)}: {esc(fmt(v))}</title><text x="92" y="{y + 15}" text-anchor="end" class="lab">{esc(lab)}</text>'
                    f'<rect x="100" y="{y + 3}" width="{max(w, 1.5):.1f}" height="16" rx="3" fill="var(--{c})"/>'
                    f'<text x="{108 + w:.1f}" y="{y + 16}" class="sm">{esc(fmt(v))}</text></g>')
        y += 26
    return f'<svg viewBox="0 0 {W} {y + 22}" role="img" aria-label="{esc(title)}"><text x="0" y="12" class="lab" font-weight="600">{esc(title)}</text><g transform="translate(0,18)">{"".join(rows)}</g></svg>'


def overlap_bars(a, b, na, nb):
    ids = sorted(set(a) | set(b))
    cells = Counter()
    for i in ids:
        ra = a.get(i, {}).get("status") == "resolved"
        rb = b.get(i, {}).get("status") == "resolved"
        cells[(ra, rb)] += 1
    items = [("Both solved", cells[(True, True)], "s1"), (f"Only {nb}", cells[(False, True)], "s3"),
             (f"Only {na}", cells[(True, False)], "s2"), ("Neither", cells[(False, False)], "gray")]
    return hbars(items, lambda v: f"{int(v)}", f"{na} vs {nb}, all {len(ids)} tasks", W=360)


# ---- page ------------------------------------------------------------------------------
CSS = """
:root{--bg:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--mute:#8a8984;--line:#e4e3de;--grid:#e9e8e3;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--good:#0ca30c;--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;--gray:#9a998f;--pend:#d8d7d0}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121211;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--mute:#8d8c85;--line:#2e2e2b;--grid:#2a2a28;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--gray:#7b7a73;--pend:#3a3a37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:40px 0 4px}
p.sub{color:var(--ink2);margin:0 0 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.kpi{border:1px solid var(--line);background:var(--card);border-radius:12px;padding:14px}
.kpi b{display:block;font-size:34px;line-height:1.1}.kpi span{color:var(--ink2);font-size:13px}.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
svg{width:100%;height:auto;display:block}.lab{fill:var(--ink);font-size:13px}.sm{fill:var(--ink2);font-size:12px}.seg{fill:#fff;font-size:11px;font-weight:600}.seg.dk{fill:#1a1a19}
.grid{stroke:var(--grid);stroke-width:1}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--ink2);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.note{color:var(--ink2);font-size:13px}
details{margin-top:8px}summary{cursor:pointer;color:var(--ink2);font-size:13px}code{background:var(--grid);padding:1px 5px;border-radius:4px;font-size:12.5px}
.tw{overflow-x:auto}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.rcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.rcard h3{margin:0 0 2px;font-size:17px}h3{font-size:15px;margin:0 0 8px}
.big{display:block;font-size:40px;line-height:1.1;margin:8px 0}dl{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0;font-size:13px}dt{color:var(--ink2)}dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}ul{padding-left:20px}li{margin:4px 0}
"""


def table(headers, rows, num_from=1):
    h = "".join(f'<th class="{"n" if i >= num_from else ""}">{esc(x)}</th>' for i, x in enumerate(headers))
    b = "".join("<tr>" + "".join(f'<td class="{"n" if i >= num_from else ""}">{esc(c)}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def range_bars(rows, title, unit, W=720):
    """rows: (label, avg, lo, hi). Bars show the average, the whisker the lowest to highest sample."""
    mx = max(r[3] for r in rows) * 1.08 or 1
    LBL, y, out = 250, 26, []
    for lab, avg, lo, hi in rows:
        sx = lambda v: LBL + v / mx * (W - LBL - 90)
        tip = f"{lab}: average {avg:,.0f} {unit}, lowest {lo:,.0f}, highest {hi:,.0f}"
        out.append(f'<g><title>{esc(tip)}</title><text x="{LBL - 10}" y="{y + 14}" text-anchor="end" class="lab">{esc(lab)}</text>'
                   f'<rect x="{LBL}" y="{y}" width="{max(sx(avg) - LBL, 1.5):.1f}" height="18" rx="3" fill="var(--gray)"/>'
                   f'<line x1="{sx(lo):.1f}" x2="{sx(hi):.1f}" y1="{y + 9}" y2="{y + 9}" stroke="var(--ink)" stroke-width="1.5"/>'
                   f'<line x1="{sx(lo):.1f}" x2="{sx(lo):.1f}" y1="{y + 4}" y2="{y + 14}" stroke="var(--ink)" stroke-width="1.5"/>'
                   f'<line x1="{sx(hi):.1f}" x2="{sx(hi):.1f}" y1="{y + 4}" y2="{y + 14}" stroke="var(--ink)" stroke-width="1.5"/>'
                   f'<text x="{sx(hi) + 8:.1f}" y="{y + 14}" class="sm">{avg:,.0f}</text></g>')
        y += 32
    return (f'<svg viewBox="0 0 {W} {y + 20}" role="img" aria-label="{esc(title)}"><text x="0" y="14" class="lab" font-weight="600">{esc(title)}</text>'
            f'{"".join(out)}<text x="{LBL}" y="{y + 14}" class="sm">bar = average of 3 to 4 samples; line = lowest to highest sample</text></svg>')


def thinking_page(data):
    """A page on the tests of Loop's thinking levels against GLM-5.3."""
    ids13 = None
    small = {}
    for rid, label in [("loop-think1", "Loop 0.5.0 · medium"), ("loop06-max13", "Loop 0.6.0 · max")]:
        d = load(rid)
        if d:
            small[rid] = (label, d)
            ids13 = ids13 or sorted(d)
    base = data.get("full1") or load("full1")
    rows13 = []
    if ids13 and base:
        sub = {i: base[i] for i in ids13 if i in base}
        rows13.append(["Loop 0.5.0 · off (from the full run)", len(sub), sum(v["status"] == "resolved" for v in sub.values()),
                       f"{sum(v['tout'] or 0 for v in sub.values()):,}"])
    for rid, (label, d) in small.items():
        rows13.append([label, len(d), sum(v["status"] == "resolved" for v in d.values()), f"{sum(v['tout'] or 0 for v in d.values()):,}"])
    t13 = table(["Setting (first 13 tasks)", "Tasks", "Resolved", "Output tokens"], rows13, 1)

    # Probe results recorded from the terminal on 3 Nov 2026 (the probe script prints them; they are not stored in state files).
    before = [["reasoning_effort = max / high / medium", "HTTP 400 · not supported by the gateway", "ok (max, medium); high timed out once"],
              ["thinking = {type: enabled}  (top level)", "HTTP 400 · not supported", "HTTP 400 · not supported"],
              ["thinking = {type: disabled}  (top level)", "HTTP 400 · not supported", "HTTP 400 · not supported"],
              ["chat_template_kwargs / enable_thinking  (top level)", "HTTP 502 · model unavailable", "HTTP 502 or timeout"],
              ["same fields inside extra_body", "not tested", "ok"],
              ["no parameter at all", "ok · the model already thinks", "ok · the model already thinks"]]
    gate = table(["What was sent to Soket's API", "First test (gateway as it was)", "After Soket changed it"], before, 1)
    rep = [("No parameter (default)", 3180, 2727, 3616), ("reasoning_effort = max", 2499, 1332, 3445),
           ("reasoning_effort = medium", 2822, 1742, 3935), ("reasoning_effort = high", 169, 129, 284),
           ("extra_body reasoning_effort = high", 168, 149, 204),
           ("extra_body enable_thinking = false", 0, 0, 0)]
    toks = [("No parameter (default)", 1657), ("reasoning_effort = max", 1330), ("reasoning_effort = medium", 1456),
            ("reasoning_effort = high", 222), ("extra_body reasoning_effort = high", 197), ("extra_body enable_thinking = false", 1235)]
    chart = range_bars(rep, "Reasoning written per answer (characters)", "characters")
    rtab = table(["Setting", "Avg output tokens", "Avg reasoning (chars)", "Lowest", "Highest"],
                 [[a, f"{t:,}", f"{c:,}", f"{lo:,}", f"{hi:,}"] for (a, c, lo, hi), (_, t) in zip(rep, toks)], 1)
    body = f"""<h1>Do thinking levels change anything?</h1>
<p class="sub">Tests of Loop's thinking levels against GLM-5.3 on Soket. <a href="comparison.html">← back to the harness comparison</a></p>
<div class="card"><b>Short answer:</b> no. GLM-5.3 already thinks by default. Setting Loop's level to <code>max</code> gives the same amount of reasoning as the default,
and <code>high</code> makes it think far less. Only an on/off switch is real, so changing the level does not explain Loop's lower score, and moving to <code>max</code> would not raise it.</div>

<h2>1. How Loop sets thinking</h2>
<div class="card"><ul>
<li>Loop's setting <code>thinking_level</code> is one of off, minimal, low, medium, high, xhigh, max. Anything other than off adds <code>reasoning_effort: "&lt;level&gt;"</code> to each request.</li>
<li>Off sends nothing, so the model does what it does by default. For GLM-5.3 that is thinking on.</li>
<li>Loop 0.6.0 cannot send GLM's own format (<code>thinking: {{type: enabled}}</code>) from its config, so the level is the only control we have.</li>
</ul></div>

<h2>2. Did Soket's API accept it?</h2>
<p class="sub">Each test sent one small question with a different thinking parameter.</p>
<div class="card tw">{gate}</div>
<p class="note">At first Soket's LiteLLM gateway refused <code>reasoning_effort</code> with HTTP 400 ("does not support parameters"), so Loop dropped it and retried without it
(the "rejected reasoning parameters" warning in 225 of 300 tasks in the 0.6.0 run). Soket then changed the gateway and <code>max</code> and <code>medium</code> were accepted.
Being accepted is not the same as having an effect, which the next test measured.</p>

<h2>3. Does the level change how much it thinks?</h2>
<p class="sub">Each setting was asked the same multi-step maths question 3 to 4 times (one call per setting timed out). Bars show the average reasoning written; the line shows the lowest and highest sample.</p>
<div class="card">{chart}</div><div class="card tw">{rtab}</div>
<ul>
<li><b><code>max</code> is not more thinking.</b> Its average (2,499) is below the default (3,180), and the ranges overlap with both default and <code>medium</code>. The levels are not graded upward.</li>
<li><b><code>high</code> collapses reasoning</b> to about 170 characters, around 95% less, and the same happens when it is sent through <code>extra_body</code>. That is the model or backend reacting to the value, not the gateway. I would avoid it; shorter reasoning on hard tasks probably means worse answers, though that was not measured on real tasks.</li>
<li><b>Only on/off works.</b> Sending <code>enable_thinking = false</code> inside <code>extra_body</code> gave zero reasoning in all 3 samples, and the answers were still written out in full (about 1,200 tokens). So a real switch exists, but it is a switch, not a dial.</li>
</ul>

<h2>4. Does it change the SWE-bench score?</h2>
<p class="sub">The same first 13 tasks under three Loop settings.</p>
<div class="card tw">{t13}</div>
<ul>
<li>Thinking off and <code>medium</code> both solved 11 of 13. Output tokens roughly doubled from off to <code>medium</code>, then barely moved at <code>max</code> (138k vs 134k), which matches the probe: the extra reasoning comes from the default thinking being on, not from the level.</li>
<li>Loop 0.6.0 with <code>max</code> solved all 13, but DeepSeek also solved all 13 of these, and they are the easiest tasks. On the full 300 tasks Loop 0.6.0 scored 74.0%, below the 77.0% Loop 0.5.0 got with thinking off, so the label made no difference to the result.</li>
</ul>

<h2>What this means</h2>
<div class="card"><ul>
<li>Loop's 77% with the default thinking is a fair result; a "max thinking" run would not be expected to beat it.</li>
<li>DeepSeek sends no thinking parameter either, so both harnesses ran GLM at its default effort. The comparison between them is fair on this point.</li>
<li>The score gap comes from the harness: timeouts, early stops and (for 0.6.0) cut-off model streams, not from the thinking level.</li>
</ul>
<p class="note">Limits: the probe used one made-up question rather than real coding tasks, with 4 samples per setting; the on/off test had 3 samples; the 13-task runs are small. The probe numbers were read from the terminal on 3 Nov 2026 and are typed into this page; the task results are computed from the run files.</p></div>"""
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "comparison.html"))
    ap.add_argument("--runs", default="full1,dsh-full2",
                    help="comma-separated run ids to include (add loop06-max-full to bring in Loop 0.6.0)")
    args = ap.parse_args()
    want = [r for r in args.runs.split(",") if r]
    data, labels, missing = {}, [], []
    for rid, name, desc, color in RUNS:
        if rid not in want:
            continue
        d = load(rid)
        if d is None:
            missing.append(rid)
            continue
        data[rid] = d
        labels.append((rid, name, color))
    if len(labels) < 1:
        raise SystemExit("no run data found under results/ or results-lite/")
    total = 300
    base = labels[-1][0]
    desc = {r[0]: r[2] for r in RUNS}

    def res(run):
        return sum(v["status"] == "resolved" for v in data[run].values())

    kpis = "".join(f'<div class="kpi"><span><i class="dot" style="background:var(--{c})"></i>{esc(n)}</span><b>{pct(res(r), total):.1f}%</b>'
                   f'<span>{res(r)} of {total} resolved</span></div>' for r, n, c in labels)

    cards = []
    for rid, name, color in labels:
        d = data[rid]
        c = Counter(v["status"] for v in d.values())
        tin = sum(v["tin"] or 0 for v in d.values())
        tout = sum(v["tout"] or 0 for v in d.values())
        prov = ", ".join(f"{k} {v}" for k, v in Counter(v["provider"] or "unknown" for v in d.values()).most_common())
        facts = [("Resolved", f"{res(rid)} of {total}"), ("Unresolved", c["unresolved"]), ("Agent error", c["agent_error"]),
                 ("Timeout", c["timeout"]), ("Empty patch", c["nopatch"]), ("Tokens in / out", f"{tin / 1e6:,.0f}M / {tout / 1e6:,.2f}M"),
                 ("Provider", prov)]
        cards.append(f'<div class="rcard" style="border-top:4px solid var(--{color})"><h3>{esc(name)}</h3><p class="note">{esc(desc[rid])}</p>'
                     f'<b class="big">{pct(res(rid), total):.1f}%</b><dl>' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts) + "</dl></div>")
    inv = '<div class="cards">' + "".join(cards) + "</div>"

    def col(fn):
        return [fn(list(data[r].values()), r) for r, _, _ in labels]

    metrics = [
        ("Resolved", col(lambda v, r: f"{res(r)} ({pct(res(r), total):.1f}%)")),
        ("Mean steps per task", col(lambda v, r: f"{mean([x['steps'] for x in v]):.0f}")),
        ("Median steps per task", col(lambda v, r: f"{quant([x['steps'] for x in v], .5):.0f}")),
        ("Input tokens per task", col(lambda v, r: f"{mean([x['tin'] for x in v]) / 1e3:,.0f}k")),
        ("Output tokens per task", col(lambda v, r: f"{mean([x['tout'] for x in v]) / 1e3:,.1f}k")),
        ("Median agent time (min)", col(lambda v, r: f"{quant([x['wall'] for x in v], .5) / 60:.1f}")),
        ("Slowest 10% start at (min)", col(lambda v, r: f"{quant([x['wall'] for x in v], .9) / 60:.1f}")),
        ("Tokens per resolved task", col(lambda v, r: f"{sum((x['tin'] or 0) + (x['tout'] or 0) for x in v) / max(res(r), 1) / 1e6:.2f}M")),
    ]
    stats = table(["Metric"] + [n for _, n, _ in labels], [[m] + vals for m, vals in metrics], 1)

    # a section on the last run (DeepSeek): what it solved, what it missed, how it was run
    ds_html = ""
    if "dsh-full2" in data:
        D = data["dsh-full2"]
        dc = Counter(v["status"] for v in D.values())
        dv = list(D.values())
        dk = "".join(f'<div class="kpi"><span>{l}</span><b>{v}</b><span>{sub}</span></div>' for l, v, sub in [
            ("Resolved", res("dsh-full2"), f"{pct(res('dsh-full2'), total):.1f}% of {total}"),
            ("Wrong patch", dc["unresolved"], f"{pct(dc['unresolved'], total):.1f}%"),
            ("Timeouts", dc["timeout"], "30 minute limit"),
            ("Agent errors", dc["agent_error"], "none"),
            ("Steps per task", f"{mean([x['steps'] for x in dv]):.0f}", f"median {quant([x['steps'] for x in dv], .5):.0f}"),
            ("Tokens per task", f"{(mean([x['tin'] for x in dv]) + mean([x['tout'] for x in dv])) / 1e3:,.0f}k", "input + output")])
        rrows = []
        for r in sorted({repo_of(i) for i in D}, key=lambda r: -sum(repo_of(i) == r for i in D)):
            ids = [i for i in D if repo_of(i) == r]
            g = sum(D[i]["status"] == "resolved" for i in ids)
            rrows.append([r, len(ids), g, f"{pct(g, len(ids)):.0f}%"])
        loops = [(r, n) for r, n, _ in labels if r != "dsh-full2"]
        miss = sorted(i for i in D if D[i]["status"] != "resolved")
        mrows = [[i, D[i]["status"].replace("nopatch", "empty patch").replace("_", " "), D[i]["steps"], D[i]["patch"]]
                 + [data[r].get(i, {}).get("status", "-").replace("nopatch", "empty patch").replace("_", " ") for r, _ in loops] for i in miss]
        only_loop = sorted(i for i in D if D[i]["status"] != "resolved" and any(data[r].get(i, {}).get("status") == "resolved" for r, _ in loops))
        ds_html = f"""
<h2>DeepSeek Harness in detail</h2>
<p class="sub">How the DeepSeek run was set up and where it stands.</p>
<div class="kpis">{dk}</div>
<div class="card"><ul>
<li><b>Version:</b> DeepSeek Harness 0.2.1-rc.5 (latest published; there is no stable release), <code>headless</code> profile, one task per run.</li>
<li><b>Same conditions as Loop:</b> GLM-5.3 on Soket, 8 parallel workers, 30 minute limit per task, full tool access inside a throwaway pod, standard issue prompt.</li>
<li><b>Fairness changes:</b> its web tool is disabled (it could otherwise fetch the upstream fix), its telemetry exporters are disabled, and its shell tool timeout was raised from 60 s to 10 min so test suites can finish.</li>
<li><b>No thinking parameter is sent.</b> GLM-5.3 thinks by default, so the model reasons at its default effort, same as Loop's effective setting.</li>
<li><b>First run discarded:</b> an earlier run (<code>dsh-full1</code>) had the web tool on, so it was thrown away and redone.</li>
</ul></div>
<div class="two"><div class="card"><h3>Resolved by repo</h3><div class="tw">{table(["Repo", "Tasks", "Solved", "Rate"], rrows, 1)}</div></div>
<div class="card"><h3>Where DeepSeek is strong</h3><ul>
<li>It finishes nearly every task: <b>{dc['resolved'] + dc['unresolved']}</b> ended with a graded patch, only <b>{dc['timeout']}</b> timed out and none crashed.</li>
<li>It works longer on each task (mean {mean([x['steps'] for x in dv]):.0f} steps) and uses more tokens, which fits its higher solve rate.</li>
<li>Every sympy task but two was solved ({sum(1 for i in D if repo_of(i) == 'sympy/sympy' and D[i]['status'] == 'resolved')} of {sum(1 for i in D if repo_of(i) == 'sympy/sympy')}).</li>
<li>Tasks only Loop solved: <b>{len(only_loop)}</b>{(' (' + ', '.join(only_loop) + ')') if only_loop else ''}.</li></ul></div></div>
<div class="card"><h3>The {len(miss)} tasks DeepSeek did not solve</h3><p class="note">With what Loop got on the same task.</p>
<div class="tw">{table(["Task", "DeepSeek", "Steps", "Patch bytes"] + [n + " got" for _, n in loops], mrows, 2)}</div></div>"""

    # only-one-solved detail for the second-to-last run vs base
    detail = ""
    others = [l for l in labels if l[0] != base]
    if others:
        focus = others[-1][0]
        f, b = data[focus], data[base]
        only_b = [i for i in sorted(f) if f[i]["status"] not in ("resolved", "pending") and b.get(i, {}).get("status") == "resolved"]
        why = Counter(f[i]["status"] for i in only_b)
        rows = [[i, f[i]["status"], f[i]["steps"], b[i]["steps"], f[i]["patch"], b[i]["patch"]] for i in only_b]
        detail = (f'<p class="note">Of {len([i for i in f if f[i]["status"] != "pending"])} tasks {esc(focus)} finished, '
                  f'{len(only_b)} were solved only by DeepSeek: ' + ", ".join(f"{k.replace('_', ' ').replace('nopatch', 'empty patch')} {v}" for k, v in why.most_common()) + ".</p>"
                  + "<details><summary>Show those tasks</summary><div class=\"tw\">"
                  + table(["Task", focus + " outcome", "steps (Loop)", "steps (DeepSeek)", "patch B (Loop)", "patch B (DeepSeek)"], rows, 2) + "</div></details>")

    small_rows = []
    for rid, label in SMALL:
        d = load(rid)
        if d:
            c = Counter(v["status"] for v in d.values())
            small_rows.append([label, len(d), c["resolved"], c["unresolved"], c["agent_error"],
                               f"{sum(v['tout'] or 0 for v in d.values()):,}"])
    small = table(["13-task test", "Tasks", "Resolved", "Unresolved", "Agent err.", "Output tokens"], small_rows, 1) if small_rows else ""

    ov = "".join(f'<div class="card">{overlap_bars(data[r], data[base], n, labels[-1][1])}</div>' for r, n, c in labels[:-1])
    steps_h = histogram({r: [v["steps"] for v in data[r].values()] for r, _, _ in labels}, labels,
                        [0, 10, 20, 30, 40, 60, 80, 100, 150, 200, 400], "Agent steps (model calls) per task", lambda x: f"{x:g}")
    wall_h = histogram({r: [(v["wall"] or 0) / 60 for v in data[r].values() if v["wall"]] for r, _, _ in labels}, labels,
                       [0, 3, 6, 9, 12, 15, 20, 25, 30], "Agent time per task (minutes)", lambda x: f"{x:g}")
    tok_in = hbars([(n, mean([v["tin"] for v in data[r].values()]) / 1e3, c) for r, n, c in labels], lambda v: f"{v:,.0f}k", "Input tokens per task")
    tok_out = hbars([(n, mean([v["tout"] for v in data[r].values()]) / 1e3, c) for r, n, c in labels], lambda v: f"{v:,.1f}k", "Output tokens per task")
    missing_note = f'<p class="note">Not found locally, skipped: {esc(", ".join(missing))}.</p>' if missing else ""

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SWE-bench Lite Harness Comparison</title><style>{CSS}</style></head><body><main>
<h1>SWE-bench Lite: Loop vs DeepSeek Harness</h1>
<p class="sub">Same model (GLM-5.3), same 300 tasks, same prompt, no web access, graded with the official swebench parsers on a Kubernetes cluster.</p>
<p class="note"><a href="thinking-tests.html">Thinking-level tests: do they change anything? →</a></p>
<div class="kpis">{kpis}</div>{missing_note}

<h2>The runs</h2><p class="sub">Every full run in this comparison.</p>{inv}
<h2>Head to head</h2><p class="sub">Effort and cost per task.</p><div class="card tw">{stats}</div>

<h2>What happened to each task</h2>
<p class="sub">Outcome of all 300 tasks.{ " Loop 0.6.0 also loses tasks to agent errors (a cut model stream)." if any(r == "loop06-max-full" for r, _, _ in labels) else "" }</p>
<div class="card">{stacked_outcomes(data, labels, total)}</div>

<h2>Where the harnesses differ</h2><p class="sub">Share of each repository's tasks resolved. Counts at right are in legend order.</p>
<div class="card">{repo_bars(data, labels)}</div>

<h2>Who solves what</h2><p class="sub">How the runs overlap with DeepSeek, task by task.</p>
<div class="two">{ov}</div>{detail}

<h2>How each agent works</h2>
<p class="sub">DeepSeek takes more, shorter steps; Loop either stops early or wanders on hard tasks until the 30 minute limit.</p>
<div class="card">{steps_h}</div><div class="card">{wall_h}</div>
<div class="two"><div class="card">{tok_in}</div><div class="card">{tok_out}</div></div>

{ds_html}

<h2>Small tests</h2><p class="sub">The first 13 tasks, used to check each setup before a full run.</p><div class="card tw">{small}</div>

<h2>What we learned</h2><div class="card"><ul>
<li><b>Thinking level did not explain the gap.</b> GLM-5.3 thinks by default on Soket. The gateway used to reject <code>reasoning_effort</code>; it now accepts it, but repeated tests show <code>max</code>, <code>medium</code> and the default give similar amounts of reasoning, while <code>high</code> collapses it to a few hundred characters. Only on/off is a real switch.</li>
{ "<li><b>Agent errors are mostly API stream cut-offs.</b> Soket's backend drops some streamed answers mid-way; Loop 0.6.0 does not retry a broken stream. DeepSeek, run through the same proxy, had none.</li>" if any(r == "loop06-max-full" for r, _, _ in labels) else "" }
<li><b>Timeouts are a harness trait.</b> Loop has no step limit and often explores for 150 to 300 steps without editing; DeepSeek solves the same tasks in 30 to 80.</li>
<li><b>Loop's losses are mostly tasks it never finished</b> (timeouts, early stops), not only wrong answers; DeepSeek finishes nearly every task.</li>
</ul>
<p class="note">Caveats: the Loop 0.5.0 run mixed Soket and OpenRouter; on a 300-task benchmark a difference of one or two points is within noise; results from a model that may have seen the upstream fixes in training can overstate every harness.</p></div>
<p class="note">Generated from per-task <code>state.json</code> files. Hover any bar for its numbers.</p>
</main></body></html>"""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    shell = page[:page.index("<main>") + 6]
    think = shell.replace("SWE-bench Lite Harness Comparison", "Thinking-level tests") + thinking_page(data) + "</main></body></html>"
    (out.parent / "thinking-tests.html").write_text(think)
    print(f"wrote {out} ({len(page) // 1024} KB) with runs: {', '.join(r for r, _, _ in labels)}")


if __name__ == "__main__":
    main()
