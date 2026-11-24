"""Probe which thinking parameters a chat-completions endpoint accepts.

Run on a machine that can reach the API. The key is read from the environment
(never printed): export SOKET_API_KEY=... first, or pipe it in from the K8s secret.

    python scripts/probe_thinking.py --base https://api.soket.ai/v1 --model GLM-5.3
"""
import argparse
import json
import os
import urllib.error
import urllib.request

VARIANTS = {
    "none": {},
    "reasoning_effort=max": {"reasoning_effort": "max"},
    "reasoning_effort=high": {"reasoning_effort": "high"},
    "reasoning_effort=medium": {"reasoning_effort": "medium"},
    "thinking={type:enabled}": {"thinking": {"type": "enabled"}},
    "thinking={type:disabled}": {"thinking": {"type": "disabled"}},
    "chat_template_kwargs.enable_thinking=true": {"chat_template_kwargs": {"enable_thinking": True}},
    "chat_template_kwargs.enable_thinking=false": {"chat_template_kwargs": {"enable_thinking": False}},
    "enable_thinking=true": {"enable_thinking": True},
    # LiteLLM skips its supported-parameter check for fields under extra_body
    "extra_body.reasoning_effort=high": {"extra_body": {"reasoning_effort": "high"}},
    "extra_body.thinking={type:enabled}": {"extra_body": {"thinking": {"type": "enabled"}}},
    "extra_body.chat_template_kwargs.enable_thinking=true": {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}
    },
    "extra_body.chat_template_kwargs.enable_thinking=false": {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    },
}
PROMPT = (
    "A farmer has 17 sheep. All but 9 run away. How many are left? Then multiply that by 13. "
    "Next, list the first 8 primes larger than that product, and say which of them is closest to 500."
)


def call(base, key, model, extra):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 2000, **extra}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode(errors='replace')[:160]}"
    except Exception as e:  # network, timeout
        return f"ERROR {type(e).__name__}: {e}"
    msg = d["choices"][0]["message"]
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    out = (d.get("usage") or {}).get("completion_tokens")
    return ("ok", out or 0, len(reasoning))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--key-env", default="SOKET_API_KEY")
    ap.add_argument("--repeat", type=int, default=1, help="ask each variant this many times and average")
    ap.add_argument("--only", help="comma-separated substrings; run only variants whose name contains one")
    a = ap.parse_args()
    key = os.environ.get(a.key_env)
    if not key:
        raise SystemExit(f"set {a.key_env} in the environment first")
    wanted = [w for w in (a.only or "").split(",") if w]
    for name, extra in VARIANTS.items():
        if wanted and not any(w in name for w in wanted):
            continue
        results = [call(a.base, key, a.model, extra) for _ in range(a.repeat)]
        good = [r for r in results if isinstance(r, tuple)]
        bad = [r for r in results if not isinstance(r, tuple)]
        if good:
            n = len(good)
            toks = sum(r[1] for r in good) / n
            chars = [r[2] for r in good]
            line = f"ok x{n}  avg_tokens={toks:.0f}  reasoning_chars avg={sum(chars) / n:.0f} min={min(chars)} max={max(chars)}"
            if bad:
                line += f"  ({len(bad)} failed: {bad[0][:60]})"
        else:
            line = bad[0]
        print(f"{name:52s} {line}", flush=True)


if __name__ == "__main__":
    main()
