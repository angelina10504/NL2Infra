# Decisions log

Short record of non-obvious choices made during the industry-grade upgrade. Two to three
lines each, newest first.

## Setup / baseline
- **Test runner interpreter.** Dependencies (langgraph, streamlit, checkov, …) are installed in
  the Windows `Python 3.13` (`C:\Program Files\Python313`) interpreter, not Anaconda. Tests are run
  with that interpreter. Installed `pytest` into it to get a baseline.
- **Cross-platform `fake_tool` test helper.** `tests/test_validators.py` wrote a `#!/bin/sh` script
  as a fake scanner; Windows `subprocess` ignores the shebang so three tests failed only on Windows.
  Made `fake_tool` emit a `.bat` wrapper on `os.name == "nt"`. No production code changed.
- **Baseline:** 185 passed, 29 skipped (the 29 are `real_tools` tests needing checkov/conftest/kubectl
  binaries on PATH).

## Phase 1 — Guardrails
- **Roles kept backward-compatible.** The teacher asked for `viewer`/`developer`/`admin`. The existing
  code keys OPA policy strictness off `junior_dev`/`senior_dev`/`platform_admin` (`ROLE_PACKS`). Rather
  than rename and break the Gauntlet, rules and UI, the new names are canonical and the legacy names are
  aliases (`junior_dev`,`senior_dev`→`developer`; `platform_admin`→`admin`). `ROLE_PACKS` extended to
  accept both.
- **`check_guardrails(state) -> str` stays stateless.** The benchmark and eval call it to build a
  dry-run "would be refused" list, so it must not have rate-limit side effects. Rate limiting lives in a
  separate `RateLimiter` wired into the graph's guardrails node only.
- **Environment detected from the prompt text** (deterministic, pre-LLM) so a blocked request still
  spends zero tokens — matching the project's "blocked before inference" guarantee.
