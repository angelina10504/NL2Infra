#!/usr/bin/env python3
"""AI PR reviewer for CI. It reads the pull-request diff and posts ONE short comment
summarising risks. It only comments — it never approves, merges, or touches the cluster,
matching the project rule that the LLM has no tools and produces text only.

Fails soft: any problem (no key, API error, empty diff) prints a note and exits 0 so the
review step never blocks a pull request. Credentials come from the environment:

  GROQ_API_KEY   optional; without it the step posts a 'skipped' note
  GH_TOKEN       GitHub token used by the `gh` CLI to post the comment
  BASE_SHA, HEAD_SHA, PR_NUMBER   provided by the workflow
"""
import os
import subprocess
import sys

try:  # emoji-safe printing on any console (Windows cp1252 would otherwise crash)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAX_DIFF_CHARS = 16000  # keep the prompt small and cheap

PROMPT = """You are a senior platform/security engineer reviewing a pull request for NL2Infra,
a tool that turns natural language into Kubernetes YAML behind deterministic policy checks.

Review the diff below and write a SHORT risk summary for the PR author. Rules:
- 3-6 bullet points, concrete and specific to the diff.
- Call out security, correctness, and test-coverage risks. If you see none, say so briefly.
- You are advisory only: you cannot merge, approve, run code, or access the cluster.
- Do not invent changes that are not in the diff.

Diff:
```
{diff}
```

Write only the review, starting with "### 🤖 AI risk review (advisory)".
"""


def get_diff() -> str:
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    if not base or not head:
        return ""
    try:
        out = subprocess.run(
            ["git", "diff", "--unified=1", f"{base}...{head}"],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout[:MAX_DIFF_CHARS]
    except Exception:
        return ""


def post_comment(body: str) -> None:
    pr = os.environ.get("PR_NUMBER")
    if not pr or not os.environ.get("GH_TOKEN"):
        print("No PR number or GH_TOKEN; printing review instead of posting:\n")
        print(body)
        return
    try:
        subprocess.run(["gh", "pr", "comment", pr, "--body", body], check=True, timeout=60)
        print("Posted AI review comment to PR", pr)
    except Exception as error:
        print("Could not post comment:", error)
        print(body)


def main() -> int:
    diff = get_diff()
    if not diff.strip():
        post_comment("### 🤖 AI risk review (advisory)\n\nNo diff to review.")
        return 0

    if not os.environ.get("GROQ_API_KEY"):
        post_comment("### 🤖 AI risk review (advisory)\n\n"
                     "Skipped: no `GROQ_API_KEY` secret is configured, so no model was called. "
                     "The deterministic checks (tests, Rego, Checkov) still gate this PR.")
        return 0

    # Reuse the project's single LLM entry point: text in, text out, no tools.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "nl2infra"))
    try:
        from llm_service import LLMService
        review = LLMService()._call(PROMPT.format(diff=diff), stage="pr_review")
    except Exception as error:
        post_comment(f"### 🤖 AI risk review (advisory)\n\nThe reviewer could not run: `{error}`.")
        return 0

    post_comment(review.strip() + "\n\n_— automated advisory comment; it cannot merge or approve._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
