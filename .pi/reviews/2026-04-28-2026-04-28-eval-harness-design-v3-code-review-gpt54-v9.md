# Code Review — Eval Harness v3 (Era 9 / Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`b06c2a2`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clean module split matching the plan (`discovery`, `workspace`, `runner`, `pipeline`, `campaign`, `report`, etc.).
- Strong coverage: unit + integration suite passed locally: `cd evals && uv run pytest -q` → `190 passed`.
- Good production hardening: process-group cleanup, capped output drains, atomic `meta.json`/`scoring.json`, response.json gating, setup sentinels, and cross-host lock handling.

### Issues

#### Critical (Must Fix)
None found.

#### Important (Should Fix)

1. **`eval-report` writes campaign artifacts without acquiring the campaign lock**
   - File: `evals/evals/cli.py:516-523`
   - Issue: `cmd_eval_report` calls `write_report(campaign_dir)` directly, unlike `eval-all`/`eval`, which hold `lock(...)`.
   - Why it matters: Running `eval-report` during an active `eval-all`/`eval` can read a partial matrix and then overwrite the final report after the active run completes. It also bypasses cross-host lock refusal.
   - Fix: Wrap report generation in `with lock(campaign_dir, argv=sys.argv, force_unlock=...)`. Consider adding `--force-unlock` to `eval-report`, or intentionally refuse locked campaigns.

2. **Schema validators accept JSON booleans as integers/numbers**
   - File: `evals/evals/schemas.py:122-125`, `171-184`, `254-268`
   - Issue: Python `bool` is a subclass of `int`, so values like `tokens.input: false`, `tests_run[].exit_code: true`, `confidence: true`, or `max_changed_files: false` pass validation.
   - Why it matters: `schema_validity` can be incorrectly marked valid for contract-invalid agent outputs.
   - Fix: Use strict helpers, e.g. `type(value) is int` for integer fields and `type(value) in (int, float)` for numeric fields, excluding bool. Add regression tests.

#### Minor (Nice to Have)

1. **Setup truncation flags are not persisted in sentinels**
   - File: `evals/evals/setup.py:353-383`
   - Issue: `stdout_truncated` / `stderr_truncated` are returned in `SetupResult` but not written into `.ok` / `.fail`.
   - Why it matters: After `eval-prepare` exits, users inspecting setup sentinels cannot tell whether logs were capped.
   - Fix: Add `stdout_truncated` and `stderr_truncated` to both sentinel JSON shapes.

2. **Pytest command normalization needs user-facing documentation**
   - File: `evals/evals/pipeline.py:143-167`
   - Issue: Harness reruns rewrite `pytest` / `uv run pytest` to `python -m pytest`.
   - Why it matters: This is probably necessary for no-install-project venvs, but case authors may expect exact command execution.
   - Fix: Document this in `evals/README.md` and case-author guidance.

### Recommendations
- Add a CLI regression test proving `eval-report` refuses or waits when `.lock` is held.
- Add schema tests for boolean rejection across every int/number field.
- Include setup truncation flags in report notes for failed setups.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** Core architecture and behavior are solid, and the full suite passes. The remaining issues are bounded, but the report lock gap and schema bool handling should be fixed before production use.

---

## Remediation Log

### Era 9 / Iteration 1

**Batch 1: report locking + strict schema typing**
- Fixed `eval-report` to regenerate `report.md` while holding the campaign lock.
- Tightened schema validators to reject JSON booleans where integer/number fields are required.
- Persisted setup `stdout_truncated` / `stderr_truncated` flags into `.ok` / `.fail` sentinels.
- Documented pytest command normalization in `evals/README.md` and added coverage in `evals/tests/readme_test.py`.
- Added regression coverage in `evals/tests/cli_test.py`, `evals/tests/schemas_test.py`, and `evals/tests/setup_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/cli_test.py tests/schemas_test.py tests/setup_test.py tests/readme_test.py`
  - `git diff --check`

Remaining from reviewer: none from era 9 iteration 1.
