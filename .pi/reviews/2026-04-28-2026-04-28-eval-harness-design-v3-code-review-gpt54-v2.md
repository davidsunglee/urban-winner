# Code Review — Eval Harness v3 (Era 2 / Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`7c3aa5d`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

Note: the planned hybrid re-review on `anthropic/claude-sonnet-4-6` failed due provider quota exhaustion, so this era was reset from delegated full-diff verification instead of an inline substitute.

---

### Reviewer Output

### Strengths
- Clear module boundaries across discovery, workspace, runner, pipeline, campaign, report, and CLI.
- Good coverage breadth: runner error precedence, capped stream draining, temp-index diff, atomic `meta.json`/`scoring.json`, resume behavior, and fake-framework integration are covered.
- Verified tests pass:
  - `cd evals && uv run pytest -q -m 'not integration'` → `123 passed`
  - `cd evals && uv run pytest -q -m integration` → `18 passed`

### Issues

#### Critical (Must Fix)

1. **Harness-side test reruns cannot execute the real pytest fixture under the no-install-project venv**
   - File: `evals/evals/env.py:55-64`, `evals/evals/pipeline.py:435-447`, `cases/pytest-dev__pytest-7571.json:4-6`
   - Issue: `test_env` sets `UV_NO_SYNC=1` and only `PYTHONPATH=<cell repo>`, then the pipeline runs `uv run pytest ...` verbatim. For `pytest-dev__pytest-7571`, the venv is built with `--no-install-project`, so there is no `pytest` console script in the venv. I reproduced this after `uv sync --no-install-project --frozen`: `UV_NO_SYNC=1 PYTHONPATH=$PWD uv run pytest --version` exits with `Failed to spawn: pytest`.
   - Why it matters: This breaks visible/hidden scoring for a starter real case. A correct agent fix would still be marked failing because the harness cannot run the test command correctly.
   - Fix: Ensure harness test reruns work for no-install src-layout/self-hosting cases. At minimum include `<cell repo>/src` in `PYTHONPATH`, and either normalize `uv run pytest`/`pytest` invocations to a no-sync `python -m pytest` path or adjust the affected case command/fixture so it is executable without installing the project. Add an integration regression using this real fixture or a synthetic src-layout self-hosting fixture.

#### Important (Should Fix)

1. **`eval-all` ignores prepare failures for cases**
   - File: `evals/evals/cli.py:329-340`
   - Issue: `_do_prepare(...)` returns `(summary, failed)`, but `cmd_eval_all` discards `failed` and continues.
   - Why it matters: If a case bare repo or venv fails to build, `eval-all` can proceed into missing/stale cache state, later crashing or running tests outside the intended venv isolation. The spec only says to continue past framework setup failures; case workspace failures should abort cleanly.
   - Fix: Track case-prep failures separately from framework setup failures. Abort `eval-all` non-zero on case cache failures; continue only for setup `.fail` frameworks that can produce `framework_misconfigured` cells.

2. **Single-cell `eval` does not regenerate the campaign report**
   - File: `evals/evals/cli.py:402-417`
   - Issue: `cmd_eval` reruns the cell and returns without calling `write_report(campaign_dir)`.
   - Why it matters: The spec says reports are generated after every `eval-all` and after any single-cell `eval`. Current behavior leaves `runs/CURRENT/report.md` stale after reruns.
   - Fix: Call `write_report(campaign_dir)` after `_run_one_cell(...)`, ideally while still holding the campaign lock.

3. **Framework setup is not idempotent/stale-aware**
   - File: `evals/evals/cli.py:173-189`, `evals/evals/setup.py:138-141`, `evals/evals/setup.py:71-73`, `evals/evals/cli.py:97-112`
   - Issue: Explicit `eval-prepare` always reruns every framework setup and deletes existing `.ok`/`.fail`. Separately, the `.ok` hash is only the manifest hash, so `eval-all` staleness detection ignores setup scripts and framework lockfiles/dependency files.
   - Why it matters: Real setup hooks may be expensive or side-effectful, and dependency/setup changes may not trigger re-prepare correctly.
   - Fix: Compute a setup fingerprint that includes the manifest plus setup script/dependency lockfiles. Skip setup when a current `.ok` exists; retry only on missing/stale `.ok` or existing `.fail`.

4. **Campaign timestamp collision can delete an existing campaign**
   - File: `evals/evals/campaign.py:75-80`
   - Issue: Campaign directories use second-resolution timestamps, and on collision the code `rmtree`s the existing directory.
   - Why it matters: Two `eval-new` calls within the same second can destroy a previous campaign, violating the “old campaigns are immutable/no auto-cleanup” storage model.
   - Fix: Never delete on collision. Use microseconds, a UUID suffix, or loop until a unique campaign dir is created.

### Recommendations
- Add at least one integration test against a real starter fixture’s visible command, not only the synthetic root-package case.
- Add a regression for `cmd_eval` report regeneration and case-prepare failure handling.

### Assessment

**Ready to merge: No**

**Reasoning:** The implementation is well-structured and heavily tested, but the harness currently cannot correctly run/scoring-rerun at least one real starter case under the required no-install-project venv model. That is a core correctness issue for production evals.

---

## Remediation Log

### Era 2 / Iteration 1

**Batch 1: no-install pytest reruns**
- Fixed test rerun environment to expose both `<cell repo>/src` and `<cell repo>` on `PYTHONPATH`.
- Fixed pipeline test-command execution to normalize `pytest ...` and `uv run pytest ...` reruns to `python -m pytest ...` inside the shared no-sync case venv.
- Added regression coverage in `evals/tests/pipeline_test.py` plus a real-fixture integration test in `evals/tests/integration/test_pytest_fixture.py`.
- Added tracked fixture stub `fixtures/pytest-dev__pytest-7571/src/_pytest/_version.py` so the real src-layout pytest fixture remains importable without project installation.
- Verification run:
  - `cd evals && uv run pytest -q -m 'not integration'`
  - `cd evals && uv run pytest -q -m integration tests/integration/test_pytest_fixture.py`

Remaining from reviewer: setup idempotence/staleness, campaign timestamp collisions.

**Batch 2: eval-all prepare aborts + eval report regeneration**
- Fixed `eval-all` to abort non-zero on case workspace/cache preparation failures while still continuing past framework setup failures that should surface as `framework_misconfigured` cells.
- Fixed single-cell `eval` to regenerate `report.md` before releasing the campaign lock.
- Added regression coverage in `evals/tests/cli_test.py` for case-prepare aborts, framework-setup continuation, and report regeneration under lock.
- Verification run:
  - `cd evals && uv run pytest -q tests/cli_test.py`
  - `cd evals && uv run pytest -q -m 'not integration'`

**Batch 3: setup idempotence + staleness**
- Added framework setup fingerprinting that covers the manifest, setup command, directly referenced setup script files, and common dependency/lock files.
- Fixed `run_framework_setup()` to skip fresh `.ok` setups and retry only when setup state is missing, stale, or failed.
- Fixed `_prepare_needed()` to treat stale setup fingerprints and `.fail` sentinels as needing prepare.
- Added regression coverage in `evals/tests/setup_test.py` and `evals/tests/cli_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/setup_test.py tests/cli_test.py`
  - `git diff --check`

Remaining from reviewer: campaign timestamp collisions.
