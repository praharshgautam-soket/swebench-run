# What we measured

Results and lessons from running Loop and the DeepSeek Harness on SWE-bench Lite with GLM-5.3, on a
shared Kubernetes cluster, between 28 Oct and 3 Nov 2026. Everything here is from those runs;
nothing has been run with Loop 0.7.0.

## The runs

All 300 Lite tasks, graded with the official `swebench` 4.1.0 parsers, no web tools, the same
prompt, 30 minute limit per task, 8 workers.

| Run | Harness | Resolved | Unresolved | Agent error | Timeout | Empty patch |
|---|---|---:|---:|---:|---:|---:|
| `full1` | Loop 0.5.0, thinking off | **231 (77.0%)** | 56 | 0 | 11 | 2 |
| `loop06-max-full` | Loop 0.6.0, thinking "max" | **222 (74.0%)** | 28 | 31 | 18 | 1 |
| `dsh-full2` | DeepSeek rc.5 | **282 (94.0%)** | 16 | 0 | 2 | 0 |

Caveats: the Loop 0.5.0 run sent 130 tasks through OpenRouter and 170 through Soket; the other two
used Soket only. The Loop 0.6.0 run's 31 agent errors were cut model streams, not wrong answers, and
were not rerun. Differences of a point or two are inside the noise on 300 tasks.

**Cost and effort** (tokens in / out, pod-hours of agent plus grading): Loop 0.5.0 310M / 3.1M, 59 h;
Loop 0.6.0 460M / 3.9M, 61 h; DeepSeek 291M / 4.7M, 57 h. A full run took 7 to 10 hours of wall
time with 8 workers (each pod requests 1 CPU). Compute was small; the model tokens are the cost.

## Why Loop scored lower than DeepSeek

Of the 66 tasks in the Loop 0.6.0 run that only DeepSeek solved: 30 agent errors, 18 wrong patches, 17
timeouts and 1 empty patch. When Loop finishes a task its solve rate is much closer to DeepSeek's than the headline
gap suggests (88% of the 251 tasks it did not error or time out on).

- **Agent errors** were cut-off model streams. The model API closed the connection mid-answer;
  Loop 0.6.0 does not retry a broken stream. DeepSeek, through the same proxy, had none.
- **Timeouts:** Loop explored for 150 to 300 steps without editing anything; DeepSeek solved the
  same tasks in 30 to 80. Loop has no step limit.
- **Compaction is not the difference.** Neither harness compacted (0 of 300 logs mention it) and
  Loop's contexts were smaller than DeepSeek's.

## Thinking levels

Loop's `thinking_level` adds `reasoning_effort: "<level>"` to each request. What we found on
Soket's GLM-5.3:

1. The gateway (LiteLLM) at first **rejected** `reasoning_effort` with HTTP 400, so Loop dropped it
   and retried (the "rejected reasoning parameters" warning). Soket later allowed it.
2. **GLM-5.3 thinks by default**, so "off" does not mean the model stops thinking.
3. Four samples each of a multi-step question:

| Setting | Avg reasoning (chars) | Range |
|---|---:|---|
| none (default) | 3,180 | 2,727 to 3,616 |
| `reasoning_effort` = max | 2,499 | 1,332 to 3,445 |
| `reasoning_effort` = medium | 2,822 | 1,742 to 3,935 |
| `reasoning_effort` = high | 169 | 129 to 284 |
| `extra_body.chat_template_kwargs.enable_thinking` = false | 0 | 0 to 0 (3 samples) |

So `max` is **not** more thinking than the default, `high` cuts reasoning by about 95%, and the only
real control is on/off, through `chat_template_kwargs` inside `extra_body`. The levels are not
graded. On 13 tasks, thinking off and `medium` both solved 11.

The probe is [`scripts/probe_thinking.py`](../scripts/probe_thinking.py); run it against any
OpenAI-compatible endpoint to see what it accepts.

## Web access

Both harnesses were run with their web tools off (`--no-web` for Loop, the `tool-web` row disabled
for dsh). That does **not** stop the shell from reaching the internet. In at least five DeepSeek
tasks (`sympy-20639`, `django-16910`, `django-13028`, `django-11964`, `matplotlib-23562`) the agent
fetched upstream commits or diffs with `curl`. Loop solved all five too, so it did not create the
gap, but a clean offline comparison needs a network policy for the pods. We could not check Loop:
its saved logs do not record shell commands. The first DeepSeek run, with the web tool on, was thrown
away and redone.

## Lessons for the next run

- **Test the model first.** Soket's GLM-5.3 backend returned `502 model unavailable` and timeouts
  several times, and cut some streams. Run the health check in the README before a long run.
- **Do not rerun all 300 for every change.** Test on 13 tasks, then rerun only failures with
  `redo`.
- **Count on retries.** The proxy records truncated streams, and the runner treats an agent error
  with a truncated stream as an infrastructure error and retries it.
- **Quote `"on"` / `"off"` in YAML.** Unquoted they become booleans (the code now converts them
  back, but quote anyway).
- **Pin versions.** Loop is a release tag (`version`), dsh is pinned to `0.2.1-rc.5`; `swebench` is
  pinned below 5.
