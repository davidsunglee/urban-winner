# Code Review — Eval Harness v3 (Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`8d0e7c59aee08afbf05348e3b50c6f351e9e7a2e`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clear module separation across discovery, workspace, runner, pipeline, campaign, status, and report.
- Good coverage breadth: fake framework behaviors, resume behavior, capped streams, temp-index diff, and CLI flows are covered.
- Stream-capping/draining and atomic `meta.json`/`scoring.json` writes are implemented thoughtfully.
- Verified test suite: `cd evals && uv run pytest -q` → `126 passed`.

### Issues

#### Critical (Must Fix)

1. **Test reruns mutate the shared venv via `uv run`, violating the no-install-project requirement**
   - File: `evals/evals/env.py:45-56`, `evals/evals/pipeline.py:431-447`, e.g. `cases/psf__requests-1921.json:4-6`
   - Issue: `build_test_env` sets `UV_PROJECT_ENVIRONMENT` and the pipeline runs case commands verbatim. Several real cases use `uv run pytest ...`; `uv run` syncs/installs the project into the shared case venv by default.
   - Why it matters: This violates the layer-2 `--no-install-project` invariant and contaminates the venv across framework cells. It can also leave editable installs pointing at a previous cell worktree, invalidating test/scoring results.
   - Fix: Prevent test commands from syncing/installing the project, e.g. use `UV_NO_SYNC=1` / inject `uv run --no-sync`, and explicitly make the cell repo importable (`PYTHONPATH=<repo>:<repo>/src` or similar). Add regression tests asserting normal success cases leave `meta.venv_mutated == false` and no project `.dist-info`/editable `.pth` appears.

#### Important (Should Fix)

1. **Malformed framework manifests are silently omitted from campaigns**
   - File: `evals/evals/discovery.py:56-79`, `evals/evals/cli.py:219-231`
   - Issue: invalid framework manifests become `DiscoveryError`s, but `eval-new`/`eval-all` ignore those errors and build the matrix only from valid `FrameworkSpec`s.
   - Why it matters: The plan requires bad manifests to produce `framework_misconfigured` cell artifacts. Current behavior can make a broken framework disappear from reports entirely.
   - Fix: Preserve errored framework names in the campaign matrix and generate fail-fast `framework_misconfigured` cells, or create a placeholder spec carrying the discovery error.

2. **`eval-all` does not detect stale fixture/lock hashes**
   - File: `evals/evals/cli.py:95-114`, `evals/evals/cli.py:274-285`
   - Issue: `_prepare_needed` only checks whether `<case>.git` and `<case>.venv` exist, not whether `.fixture-hash` / `.lock-hash` still match current inputs.
   - Why it matters: After fixture or `uv.lock` changes, `eval-all` can run stale code/dependencies, violating the workspace rebuild-trigger requirements.
   - Fix: Always call `ensure_case_bare_repo` / `ensure_case_venv` before running cells, or compare current `compute_fixture_hash` / `compute_lock_hash` to cached hashes.

3. **Lock acquisition is not atomic**
   - File: `evals/evals/campaign.py:127-133`, `evals/evals/campaign.py:114-124`
   - Issue: two processes can both observe no `.lock`, both write via temp+rename, and both proceed.
   - Why it matters: Concurrent `eval-all` runs can race, delete partial cells, and corrupt campaign artifacts.
   - Fix: Use atomic exclusive creation (`os.open(..., O_CREAT | O_EXCL)`) or an atomic lock directory. Release should also verify ownership before unlinking.

4. **Setup command exec errors abort instead of writing `.fail` and continuing**
   - File: `evals/evals/setup.py:108-121`, `evals/evals/setup.py:206-216`
   - Issue: `shlex.split` or `subprocess.Popen` failures are not caught. A missing setup executable crashes `eval-prepare`.
   - Why it matters: The setup pipeline is required to continue past per-framework failures and record `.fail` sentinels.
   - Fix: Catch parse/spawn exceptions, write diagnostic stderr + `.fail`, return `SetupResult(status="failed", ...)`, and have `run_all_setups` continue defensively.

#### Minor (Nice to Have)

1. **Missing `failure_output_path` crashes discovery**
   - File: `evals/evals/discovery.py:133-140`
   - Issue: `read_text()` errors are not converted to `DiscoveryError`.
   - Fix: Catch `OSError` and report a structured case discovery error.

2. **`eval-all --framework/--case` silently no-ops on typos**
   - File: `evals/evals/cli.py:290-295`
   - Issue: an unknown filter produces an empty matrix and exits 0.
   - Fix: return exit code 2 with a helpful “unknown framework/case” message.

### Recommendations
- Add integration coverage for “normal framework run does not mutate venv”.
- Add a malformed-framework end-to-end test asserting `framework_misconfigured` artifacts are produced.
- Consider making campaign directory names collision-proof beyond second precision.

### Assessment

**Ready to merge: No**

**Reasoning:** The architecture and tests are strong, but the shared venv contamination can invalidate core eval results, and malformed frameworks can be silently excluded from campaigns. These need fixes before production use.

---

## Remediation Log

### Iteration 1

**Batch 1: test rerun venv contamination**
- Fixed `evals/evals/env.py` / `evals/evals/pipeline.py` so visible/hidden test reruns set `UV_NO_SYNC=1` and `PYTHONPATH=<cell repo>`.
- Added regression coverage in `evals/tests/env_test.py` and `evals/tests/integration/test_fake_framework.py` to prove `uv run pytest ...` does not mutate the shared case venv on normal success paths.
- Verification run:
  - `cd evals && uv run pytest -q tests/env_test.py tests/integration/test_fake_framework.py`
  - `cd evals && uv run pytest -q tests/`

Remaining from reviewer: stale prepare hashes, non-atomic campaign lock, and `eval-all --framework/--case` typo handling.

**Batch 2: misconfiguration surfacing + setup pre-exec failures**
- Fixed malformed framework discovery so invalid manifests remain in the campaign matrix as placeholder specs and surface as `framework_misconfigured` cells instead of disappearing.
- Fixed `discover_cases()` to convert unreadable `failure_output_path` files into structured `DiscoveryError` records.
- Fixed `run_framework_setup()` pre-exec parse/spawn failures to emit diagnostic logs plus `.fail` sentinels and continue through `run_all_setups()`.
- Added regression coverage in `evals/tests/discovery_test.py`, `evals/tests/setup_test.py`, `evals/tests/runner_test.py`, and new `evals/tests/cli_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/discovery_test.py tests/setup_test.py tests/runner_test.py tests/cli_test.py`
  - `cd evals && uv run pytest -q tests/`
  - `cd evals && uv run pytest -q -m integration`

Remaining from reviewer: stale prepare hashes, non-atomic campaign lock, and `eval-all --framework/--case` typo handling.
