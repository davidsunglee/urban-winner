# Code Review — Eval Harness v3 (Era 6 / Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`7593497`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clear module separation across discovery, workspace, runner, pipeline, campaign, reporting, and CLI.
- Good regression coverage: I ran both suites successfully:
  - `cd evals && uv run pytest -q -m 'not integration'` → `148 passed`
  - `cd evals && uv run pytest -q -m integration` → `19 passed`
- Strong coverage of fake-framework behaviors, process-tree termination, capped output, atomic `meta.json`/`scoring.json`, and resume behavior.

### Issues

#### Critical (Must Fix)

1. **Runner watchdog can be bypassed by blocking on stdin write**
   - File: `evals/evals/runner.py:281-306`
   - Issue: `run_cell()` writes the full request to `proc.stdin` before starting stdout/stderr pump threads or entering the timeout-controlled `proc.wait()`.
   - Why it matters: if an adapter hangs before reading stdin and the request is larger than the pipe buffer, the harness blocks forever in `proc.stdin.write()` and never enforces `timeout_s`. I reproduced this with a 10 MiB `failure_output` and a child that sleeps without reading stdin; it hung past `timeout_s=1`.
   - Fix: start pump threads immediately after `Popen`, write stdin in a separate thread or nonblocking loop, and make the timeout cover the entire child interaction. On timeout, close stdin and terminate the process group.

#### Important (Should Fix)

1. **Cases without a committed `uv.lock` rebuild every time and mutate fixtures**
   - File: `evals/evals/workspace.py:157-190`
   - Issue: `lock_hash` is computed before `uv sync`. If no `uv.lock` exists, `uv sync` creates one, but the stored hash is still the pre-sync `pyproject.toml` hash.
   - Why it matters: the next prepare sees `uv.lock`, computes a different hash, and rebuilds the venv forever. It also mutates fixture directories, which are supposed to be pristine inputs.
   - Fix: either require `uv.lock` for cases and fail clearly when absent, or intentionally generate/handle it before hashing. At minimum, recompute the lock hash after sync if `uv.lock` was created.

2. **Stale setup `.fail` poisons frameworks that no longer declare setup**
   - File: `evals/evals/runner.py:204`; `evals/evals/cli.py:108-109`
   - Issue: `_prepare_needed()` skips frameworks with `setup is None`, but `run_cell()` still treats any existing `.runs-cache/setup/<fw>.fail` as active misconfiguration.
   - Why it matters: if a framework removes its setup command after a previous setup failure, cells still fail as `framework_misconfigured` until users manually clean `.runs-cache`.
   - Fix: only honor `.fail` when `framework.setup is not None`, or clear stale setup sentinels during prepare for no-setup frameworks.

3. **Default edit constraints falsely reject the real pytest fix**
   - File: `evals/evals/pipeline.py:25-31`, `evals/evals/pipeline.py:272-276`, `cases/pytest-dev__pytest-7571.json:7`
   - Issue: the default `**/*test*` disallowed glob matches `src/_pytest/logging.py`. The pytest case notes say the correct fix is in `src/_pytest/logging.py`, but the case uses empty `edit_constraints`, so the correct fix will be scored as an edit-constraint violation.
   - Why it matters: this makes one of the shipped starter cases misleading/unfair.
   - Fix: override `edit_constraints.disallowed_paths` for the pytest case with more precise test-file patterns, or refine the default patterns if the shared spec allows it.

4. **`eval` can create orphan cells outside the frozen campaign matrix**
   - File: `evals/evals/cli.py:400-438`
   - Issue: `cmd_eval()` validates framework/case against current discovery, but not against `runs/CURRENT/manifest.json`.
   - Why it matters: if frameworks/cases change after campaign creation, `eval <fw> <case>` can create artifacts that `status` and `report` ignore because they iterate only the manifest matrix.
   - Fix: after loading the campaign manifest, reject framework/case values not present in `manifest["frameworks"]` / `manifest["cases"]`.

#### Minor (Nice to Have)

1. **Case discovery errors are dropped by eval commands**
   - File: `evals/evals/cli.py:251-252`, `evals/evals/cli.py:273-274`, `evals/evals/cli.py:294-295`
   - Issue: `discover_cases()` errors are ignored in `eval-prepare`, `eval-new`, and `eval-all`.
   - Impact: malformed case manifests can be silently omitted from campaigns.
   - Fix: print the errors and return nonzero, or at least warn prominently.

### Recommendations
- Add a regression test for an adapter that never reads stdin with a large request body.
- Add an idempotency test for `ensure_case_venv()` when `uv.lock` is initially absent.
- Add a case-level edit-constraint test for the pytest fixture’s intended changed file.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The architecture and coverage are strong, and the implemented harness is close. However, the stdin deadlock breaks the core timeout guarantee, and the venv/idempotency plus pytest constraint issues should be fixed before production use.

---

## Remediation Log

### Era 6 / Iteration 1

**Batch 1: blocked-stdin watchdog coverage**
- Fixed `evals/evals/runner.py` so stdout/stderr draining starts before stdin transmission, stdin writes run in a separate thread, and the timeout budget covers both process lifetime and request transmission.
- Added regression coverage in `evals/tests/runner_test.py` for a child that never reads stdin while receiving a large request body.
- Verification run:
  - `cd evals && uv run pytest -q tests/runner_test.py::test_runner_timeout_covers_blocked_stdin_write tests/runner_test.py`
  - `cd evals && uv run pytest -q`

**Batch 2: workspace/CLI/case hardening**
- Fixed `evals/evals/workspace.py` so cases without a committed `uv.lock` sync from a temporary copy, avoiding fixture mutation and repeated venv rebuilds.
- Fixed stale setup `.fail` handling so runners only honor setup-failure sentinels for frameworks that still declare `setup`.
- Added a case-level edit-constraint override for `cases/pytest-dev__pytest-7571.json` so the intended fix in `src/_pytest/logging.py` is allowed while test-file edits remain blocked.
- Fixed `cmd_eval()` to reject framework/case values outside the current campaign manifest matrix.
- Fixed eval commands to fail loudly on case discovery errors instead of silently omitting malformed cases.
- Added regression coverage in `evals/tests/workspace_test.py`, `evals/tests/runner_test.py`, `evals/tests/pipeline_test.py`, and `evals/tests/cli_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/workspace_test.py tests/runner_test.py tests/pipeline_test.py tests/cli_test.py`
  - `cd evals && uv run pytest -q`
  - `git diff --check`

Remaining from reviewer: none from era 6 iteration 1.
