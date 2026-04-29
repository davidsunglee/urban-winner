# Code Review — Eval Harness v3 (Era 12 / Full Review)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`bdc13f6`
**Reviewer:** delegated (`openai-codex/gpt-5.5`, cli `pi`)
**Date:** 2026-04-29

---

### Reviewer Output

### Strengths
- Clean module separation matching the planned harness layers (`discovery`, `workspace`, `setup`, `runner`, `pipeline`, `campaign`, reporting/status).
- Good end-to-end coverage: `cd evals && uv run pytest -q` passes with **203 passed**.
- Runner/pipeline handle many production concerns well: capped stdout/stderr, process-group cleanup, response writing only for envelope-valid stdout, resumable `meta.json` sentinel, temp-index diffing, and venv mutation recording.
- CLI/justfile integration is complete; `python -m evals --help` and `just frameworks` smoke checks work.

### Issues

#### Critical
None found.

#### Important
1. **Prepare runs outside the campaign lock and can mutate shared caches during another run**
   - File: `evals/evals/cli.py:413-430`, `evals/evals/workspace.py:88-90`, `evals/evals/workspace.py:199-200`
   - Issue: `cmd_eval_all` runs `_prepare_needed` / `_do_prepare` before acquiring the campaign lock. `_do_prepare` can delete/rebuild `.runs-cache/<case>.git` and `.runs-cache/<case>.venv`. A concurrent `eval-all`/`eval` can therefore run cells while another process is deleting the venv or bare repo it depends on.
   - Why it matters: This can cause flaky clone/test failures or corrupted cell runs under concurrent use.
   - Fix: Acquire the campaign lock before auto-prepare, or add a separate cache-level lock around prepare/cache mutation.

2. **`resolve_effective_config` treats `None` overrides as real values**
   - File: `evals/evals/runner.py:399-405`
   - Issue: If `campaign_overrides` or `cell_overrides` contains `{"timeout_s": None}` / `{"max_steps": None}`, the resolver returns `None` and records source as `"campaign"` / `"cell-flag"`.
   - Why it matters: Campaign manifests intentionally store unset override fields as `null`; passing that raw manifest dict breaks effective config and can later crash subprocess timeout handling.
   - Fix: In `pick`, only accept override values when `field in overrides and overrides[field] is not None`, or normalize overrides inside `resolve_effective_config`.

#### Minor
1. **Case ID validation is stricter than the plan**
   - File: `evals/evals/schemas.py:12`, `evals/evals/schemas.py:81-82`
   - Issue: Plan allowed `/` in case IDs (`^[a-zA-Z0-9_.\-/]+$`), but implementation restricts to a single safe path segment.
   - Why it matters: Any planned future case IDs using slash-separated namespaces will be rejected by discovery.
   - Fix: Either update the spec/docs to say case IDs must be one path segment, or restore slash support and handle nested artifact paths deliberately.

## Remediation Log

### Follow-up after full review
- Fixed `evals/evals/cli.py` so `cmd_eval_all()` acquires the campaign lock before auto-prepare, serializing cache mutation and cell execution under the same lock.
- Fixed `evals/evals/runner.py` so `resolve_effective_config()` treats `None` overrides as absent instead of as explicit values.
- Added regression coverage in `evals/tests/cli_test.py` and `evals/tests/runner_test.py`.
- Verification run:
  - `cd evals && uv run pytest -q tests/cli_test.py tests/runner_test.py`

### Recommendations
- Add regression tests for raw manifest `config_overrides` containing `None`.
- Add a concurrency test around two `eval-all` invocations or document single-process operation explicitly if locking is intentionally campaign-only.
- Consider documenting the stricter case ID rule if keeping it.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The implementation is broadly solid and well-tested, but the cache mutation outside the lock is a production-readiness risk, and `None` override handling is a correctness footgun around the manifest shape.
