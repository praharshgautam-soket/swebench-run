"""The prompt every harness receives at /task/problem.md."""

# One argv entry is capped near 128 KiB on Linux; harnesses pass the prompt as an
# argument, so stay well under that.
MAX_PROBLEM_CHARS = 90_000

TEMPLATE = """\
You are fixing a bug in a software repository. The repository is checked out at \
/testbed at the commit where the issue was reported, and its environment is \
already set up.

Resolve the issue below by editing the source files in /testbed directly. \
Do not commit. Do not modify or add test files; the issue will be checked \
against hidden tests. Keep the change minimal and focused on the issue.

<issue>
{problem_statement}
</issue>
"""


def build_prompt(problem_statement: str) -> str:
    text = problem_statement.strip()
    if len(text) > MAX_PROBLEM_CHARS:
        text = text[:MAX_PROBLEM_CHARS] + "\n\n[issue text truncated]"
    return TEMPLATE.format(problem_statement=text)
