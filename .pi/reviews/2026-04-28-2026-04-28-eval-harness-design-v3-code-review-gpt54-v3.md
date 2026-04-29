# Code Review — Eval Harness v3 (Era 3 / Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`b4ac7cf`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clear module separation across discovery, workspace, runner, pipeline, campaign, status, and report.
- Good coverage of the main happy/error paths: I ran `cd evals && uv run pytest -q -m 'not integration'` and `cd evals && uv run pytest -q -m integration`; both passed (`132 passed`, `19 passed`).
- Strong subprocess artifact handling for normal exits/timeouts: capped stdout/stderr, response.json envelope rule, temp-index diff, and atomic `meta.json`/`scoring.json`.
- The real CLI smoke paths work: `python -m evals --help`, `frameworks`, `cases`, and a targeted stub eval-all cell succeeded.

### Issues

#### Critical
None found.

#### Important

1. **Executable-but-unspawnable framework entries crash the harness instead of producing `framework_misconfigured`**
   - File: `evals/evals/runner.py:251-260`
   - Issue: `subprocess.Popen(...)` is not wrapped. If the entry file exists and is executable but cannot be exec’d, e.g. bad shebang, missing interpreter, exec format error, `run_cell` raises instead of returning a `RunnerResult`.
   - Why it matters: The v3 precedence table includes exec failure as `framework_misconfigured`, and the full cell artifact set should still be written. A bad adapter can currently abort `eval-all`.
   - Fix: Catch `OSError` around `Popen`, write empty `stdout.log` and diagnostic `stderr.log`, and return `RunnerResult(error_reason="framework_misconfigured", ...)`.

2. **`eval-new` does not implement the v3 lock/`--force-unlock` requirement**
   - File: `evals/evals/cli.py:271-287`, `evals/evals/cli.py:494-500`, `evals/evals/campaign.py:75-108`
   - Issue: `eval-new` has no `--force-unlock` flag and does not consider an existing locked `runs/CURRENT` before repointing it.
   - Why it matters: The spec requires `--force-unlock` on `eval-new` and lock protection for campaign pointer updates. Currently a new campaign can be created while another process is running the current one.
   - Fix: Add `--force-unlock` to `eval-new`; if `runs/CURRENT` exists, acquire its lock before repointing or introduce a runs-level lock for CURRENT updates.

3. **Standalone `eval-prepare` can report success with missing cache directories**
   - File: `evals/evals/workspace.py:81-82`, `evals/evals/workspace.py:152-153`
   - Issue: `ensure_case_bare_repo` and `ensure_case_venv` return early when hash files match, without verifying `<case>.git/` or `<case>.venv/` still exists.
   - Why it matters: Partial cache deletion/corruption leaves `eval-prepare` non-self-healing; a later single-cell `eval` can fail cloning or run without the expected venv.
   - Fix: Include `bare_dir.exists()` / `venv_dir.exists()` in the reuse condition, and rebuild when the directory is absent.

#### Minor

1. **`meta.ended_at` is captured before the post-subprocess pipeline completes**
   - File: `evals/evals/pipeline.py:423-427`, `evals/evals/pipeline.py:519-526`
   - Issue: `ended_at_dt` is computed at the start of `run_pipeline`, before diffing, visible/hidden tests, scoring, and meta writes.
   - Why it matters: Metadata timestamps under-report cell duration and can show completion before artifacts were actually produced.
   - Fix: Capture `ended_at_dt` immediately before `write_meta_json`; optionally record runner latency separately as already done.

### Recommendations
- Add regression tests for executable-but-unspawnable framework entries.
- Add CLI/lock tests for `eval-new --force-unlock` and report/CURRENT update locking.
- Add cache-corruption tests where hash files remain but `.git`/`.venv` dirs are missing.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The core harness is well structured and thoroughly tested for normal/stub/fake-framework flows. The remaining issues are mostly edge-case production hardening, but the runner spawn failure and missing `eval-new` lock semantics should be fixed before relying on this in shared or long-running eval environments.

---

## Remediation Log

### Era 3 / Iteration 1

**Batch 1: runner/workspace/pipeline hardening**
- Fixed runner spawn-time `OSError` handling so executable-but-unspawnable framework entries produce `framework_misconfigured` artifacts instead of crashing the harness.
- Fixed workspace cache reuse so matching hash sidecars only skip rebuilds when the corresponding bare-repo / venv directories still exist.
- Fixed pipeline metadata timing so `meta.ended_at` is captured after diff/test/scoring work completes.
- Added regression coverage in `evals/tests/runner_test.py`, `evals/tests/workspace_test.py`, and `evals/tests/pipeline_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/runner_test.py tests/workspace_test.py tests/pipeline_test.py`
  - `cd evals && uv run pytest -q`

**Batch 2: eval-new lock semantics**
- Added `eval-new --force-unlock` support in the CLI.
- Fixed `eval_new()` to acquire the existing current campaign lock before repointing `runs/CURRENT`, honoring `force_unlock` when requested.
- Added regression coverage in `evals/tests/campaign_test.py` and `evals/tests/cli_test.py` for lock enforcement and `--force-unlock` flow.
- Verification run:
  - `cd evals && uv run pytest -q tests/campaign_test.py tests/cli_test.py`
  - `git diff --check`

Remaining from reviewer: none from era 3 iteration 1.
