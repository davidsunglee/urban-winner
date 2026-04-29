# Code Review — Eval Harness v3 (Era 4 / Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`a1d8840`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clear module split across discovery, workspace, setup, runner, pipeline, campaign, status, and report.
- Good subprocess artifact discipline: capped stdout/stderr, `response.json` only for envelope-valid stdout, and `meta.json` written last.
- Strong test coverage: I ran:
  - `cd evals && uv run pytest -q -m 'not integration'` → `139 passed`
  - `cd evals && uv run pytest -q -m integration` → `19 passed`
- Fake-framework integration matrix covers all major runner error classifications.

### Issues

#### Critical (Must Fix)

1. **Bare repo HEAD can point at a nonexistent branch**
   - File: `evals/evals/workspace.py:99-140`
   - Issue: `ensure_case_bare_repo()` pushes `HEAD:refs/heads/main` but never updates the bare repo’s `HEAD`. On machines where `git init --bare` defaults to `master`, cloning the bare repo succeeds with `warning: remote HEAD refers to nonexistent ref, unable to checkout`, leaving an empty/unborn worktree.
   - Why it matters: This breaks the fresh-clone/eval-all promise on systems without `init.defaultBranch=main`; subsequent pipeline steps can fail before writing `meta.json`.
   - Fix: After the push, run something like:
     ```python
     subprocess.run(
         ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
         check=True,
         capture_output=True,
     )
     ```
     Also add a regression test with git default branch forced to `master`.

#### Important (Should Fix)

1. **`eval-all` prepares all discovered cases/frameworks before applying campaign/filter selection**
   - File: `evals/evals/cli.py:345-354`, filtering only happens later at `cli.py:361-369`
   - Issue: `just eval-all --framework X --case Y` still builds venvs/bare repos for every discovered case. For existing campaigns, newly-added or broken cases outside the frozen campaign can also block resuming the old campaign.
   - Why it matters: This violates the “fill missing cells in CURRENT” expectation and can turn a one-cell rerun into expensive or failing unrelated preparation.
   - Fix: Read the campaign manifest first, compute `fw_run`/`case_run` including filters, and pass only those to `_prepare_needed()` / `_do_prepare()`.

2. **Lock refusal surfaces as an uncaught traceback from the CLI**
   - File: `evals/evals/cli.py:371`, `evals/evals/cli.py:421`, `evals/evals/cli.py:541-544`; raised from `evals/evals/campaign.py:216-240`
   - Issue: `LockBusyError` is not caught by `main()` or command handlers.
   - Why it matters: The spec requires helpful lock refusal behavior; users currently get a Python stack trace instead of a clean non-zero exit with the lock message.
   - Fix: Catch `LockBusyError` in `main()` or around lock acquisition, print the exception to stderr, and return exit code `2` or `1`.

3. **Timeout handling kills only the direct child process, not the process tree**
   - File: `evals/evals/runner.py:303-316`, `evals/evals/pipeline.py:204-217`, `evals/evals/setup.py:335-347`
   - Issue: Framework entries, setup commands, or test commands that spawn children can leave descendants alive after timeout; inherited stdout/stderr pipes can also keep pump threads blocked.
   - Why it matters: A hung framework/test can leak processes or hang the harness despite the watchdog.
   - Fix: Start subprocesses in a new process group/session (`start_new_session=True`) and terminate with `os.killpg(proc.pid, SIGTERM)` then `SIGKILL`.

#### Minor (Nice to Have)

1. **Integration acceptance test misses the branch-default portability case**
   - File: `evals/tests/integration/test_eval_all_stub.py`
   - Issue: Current tests pass under a global `init.defaultBranch=main`, so they do not catch the bare-HEAD issue above.
   - Fix: Add a workspace test that runs with git config isolated/defaulting to `master` and asserts cloned worktrees contain fixture files.

### Recommendations
- Add regression tests for lock-busy CLI behavior and filtered `eval-all` preparation.
- Consider centralizing subprocess watchdog logic to avoid fixing process-group handling in three places.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The implementation is well structured and the test suite passes, but the bare-repo HEAD bug can break the core fresh-clone workflow on common git configurations. The lock UX and process-tree timeout issues should also be addressed before relying on this in production.

---

## Remediation Log

### Era 4 / Iteration 1

**Batch 1: workspace + CLI portability hardening**
- Fixed bare fixture repos to set bare `HEAD` to `refs/heads/main` after pushing, so clones work even when `git init --bare` defaults to another branch name.
- Fixed `eval-all` to scope prepare checks/work to the selected campaign/framework/case subset before building caches.
- Fixed CLI entry to catch `LockBusyError` and return a clean `error: ...` message with exit code 2 instead of a traceback.
- Added regression coverage in `evals/tests/workspace_test.py` and `evals/tests/cli_test.py`, including the non-`main` git default portability case.
- Verification run:
  - `cd evals && uv run pytest -q tests/workspace_test.py tests/cli_test.py -m 'not integration'`
  - `cd evals && uv run pytest -q -m 'not integration'`
  - `git diff --check`

**Batch 2: process-tree timeout handling**
- Added shared subprocess process-group helpers in `evals/evals/process_tree.py`.
- Fixed runner, setup, and test-command timeout paths to launch subprocesses in isolated sessions and terminate the whole process group on timeout.
- Added regression coverage for framework/setup/test grandchild-process cleanup on timeout.
- Verification run:
  - `cd evals && uv run pytest -q tests/runner_test.py tests/pipeline_test.py tests/setup_test.py`

Remaining from reviewer: none from era 4 iteration 1.
