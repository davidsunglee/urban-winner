# Code Review — Eval Harness v3 (Era 13 / Full Review)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`1894607`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clear module separation across discovery, workspace, runner, pipeline, campaign, status, and reporting.
- Good subprocess hygiene overall: capped stdout/stderr, pipe draining, timeouts, process-group cleanup, and response writing only for envelope-valid stdout.
- Strong automated coverage: I ran `cd evals && uv run pytest -q` and got `205 passed`.
- Nice improvements beyond the plan: setup fingerprinting/staleness checks, lock owner tokens, and tests for real pytest fixture behavior.

### Issues

#### Critical (Must Fix)

1. **Real pytest case cannot run its declared visible/hidden commands under the no-install-project test environment**
   - File: `cases/pytest-dev__pytest-7571.json:4`, `cases/pytest-dev__pytest-7571.json:6`
   - File: `evals/evals/env.py:69`
   - Issue: `build_test_env` sets `UV_NO_SYNC=1`, and the pytest fixture venv intentionally lacks a `pytest` console script when built with `uv sync --no-install-project`. However the case declares `uv run pytest ...`.
   - Why it matters: visible/hidden reruns for this real case fail with spawn error instead of executing the checked-out pytest source. A successful adapter would be incorrectly scored as failing tests.
   - Evidence: after building the pytest fixture venv with `--no-install-project`, `UV_NO_SYNC=1 ... uv run pytest --version` fails with `Failed to spawn: pytest`; `uv run python -m pytest --version` works.
   - Fix: change both pytest case commands to `uv run python -m pytest ...` (or `python -m pytest ...`) and add an integration test that executes the actual manifest commands for real cases.

#### Important (Should Fix)

1. **Single-cell `eval` prepares shared caches outside the campaign lock**
   - File: `evals/evals/cli.py:497-513`
   - Issue: `cmd_eval` calls `_prepare_needed` / `_do_prepare` before acquiring `lock(campaign_dir, ...)`, unlike `cmd_eval_all`.
   - Why it matters: concurrent `eval <fw> <case>` invocations can race rebuilding `.runs-cache/<case>.git` or `.runs-cache/<case>.venv`, risking corrupt or partially replaced cache artifacts.
   - Fix: acquire the campaign lock before prepare for `cmd_eval`, matching `cmd_eval_all`, or introduce a separate cache-level lock.

2. **Fixture hash ignores tracked file mode changes**
   - File: `evals/evals/workspace.py:46-50`
   - Issue: `compute_fixture_hash` hashes file contents and paths, but not the git-tracked mode.
   - Why it matters: changing a fixture file from non-executable to executable will not invalidate the bare-repo cache, even though `ensure_case_bare_repo` intends to preserve modes.
   - Fix: include mode from `git ls-files -s` or `stat().st_mode & 0o111` in the hash buffer.

#### Minor (Nice to Have)

1. **Review artifacts are included in the feature diff**
   - File examples: `.pi/plans/*code-review*.md`
   - Issue: Many generated code-review markdown files are committed with the harness implementation.
   - Why it matters: adds repository noise unrelated to runtime harness behavior.
   - Fix: remove them from this changeset unless they are intentionally tracked project artifacts.

## Remediation Log

### Follow-up after full review
- Fixed `cases/pytest-dev__pytest-7571.json` so the declared visible/hidden commands run as `python -m pytest ...` in the intended no-install-project environment.
- Fixed `evals/evals/cli.py` so single-cell `eval` performs prepare under the campaign lock, matching `eval-all`.
- Fixed `evals/evals/workspace.py` so fixture hashes include tracked file mode, invalidating cached bare repos on executable-bit changes.
- Added regression coverage in `evals/tests/cli_test.py`, `evals/tests/workspace_test.py`, and `evals/tests/integration/test_pytest_fixture.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/workspace_test.py tests/cli_test.py tests/integration/test_pytest_fixture.py`
  - `cd evals && uv run pytest -q`

## Remediation Log

### Follow-up after full review
- Fixed `evals/evals/workspace.py` so tracked symlinks hash by link target and are recreated as symlinks in layer-1 bare repos.
- Restored safe slash-separated case ID support in `evals/evals/schemas.py` while still rejecting absolute paths, empty segments, and traversal segments.
- Added regression coverage in `evals/tests/workspace_test.py` and `evals/tests/schemas_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q -m 'not integration' tests/workspace_test.py tests/schemas_test.py`
  - `cd evals && uv run pytest -q -m 'not integration'`

### Recommendations
- Add a “real case command smoke test” that discovers each checked-in case, builds its venv, and runs the declared visible command long enough to prove the command can spawn/import correctly.
- Consider making cache preparation consistently protected by a lock across all CLI paths.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The harness architecture and coverage are strong, and the full test suite passes. However, one checked-in real case currently cannot execute its declared test commands in the intended no-install-project environment, which breaks production scoring and should be fixed before merge.
