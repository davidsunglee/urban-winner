# Code Review — Eval Harness v3 (Iteration 1)

**Git range:** `2da6909b4bdbed9bfc9740ccc84ec1dca940b43b`..`5078b68c8de5fc50000b86f222722c469e92b5af`
**Reviewer:** inline (claude-sonnet-4-6)
**Date:** 2026-04-29

---

### Strengths

- **Complete implementation:** All 16 tasks delivered. 126 tests pass (108 unit + 18 integration) in ~40s.
- **Strong spec compliance:** Every key spec rule is present — temp-and-rename for sentinels, `GIT_INDEX_FILE` temp-index diff, per-field config provenance with `sources`, `--force-unlock` cross-host logic, pipe drain after cap.
- **Atomic I/O done correctly:** Both `scoring.json` and `meta.json` use `os.rename()` after `os.fsync()`. `campaign.py`'s symlink swap (`CURRENT.tmp` → `CURRENT`) is also atomic.
- **Error precedence table correct:** `_classify_error` in `runner.py` implements the spec's precedence chain faithfully; integration tests cover every row.
- **Forbidden output keys enforced:** `FORBIDDEN_OUTPUT_KEYS = {"fixed", "not_fixed", "status"}` is checked in `validate_agent_output` and tested explicitly.
- **Fake framework complete:** All 16 `FAKE_BEHAVIOR` modes implemented and covered by parametrized integration tests.
- **Setup sentinel logic correct:** `.ok` / `.fail` are mutually exclusive; retries delete both before re-running; `run_all_setups` continues past failures.

---

### Issues

#### Critical (Must Fix)

*None found.*

#### Important (Should Fix)

1. **`failure_output_path` read without `errors="replace"` breaks case discovery on non-UTF-8 bytes**
   - File: `evals/evals/discovery.py:140`
   - What's wrong: `fop.read_text()` uses the system default encoding with no error handler. If a captured test output contains non-UTF-8 bytes (emoji, pytest ANSI codes, corrupted bytes), this raises `UnicodeDecodeError` and kills case discovery for that case without a recovery path.
   - Why it matters: The spec (risk item 8, plan line 1694) explicitly mandates `errors="replace"`. Real SWE-bench fixtures output emoji (`✓`, `⚠`) in test traces. A single bad case breaks `discover_cases()` for that entry.
   - Fix: Change line 140 to `fop.read_text(encoding="utf-8", errors="replace")`.

#### Minor (Nice to Have)

2. **`datetime.utcnow()` deprecated — causes 75 warnings in test output**
   - File: `evals/evals/campaign.py:18,22`
   - What's wrong: `datetime.utcnow()` is deprecated in Python 3.12+. Both `_now_iso()` and `_iso_zulu()` use it.
   - Why it matters: 75 deprecation warnings are emitted per test run, making it hard to spot real issues. The rest of the codebase (`setup.py`, `pipeline.py`) already uses `datetime.now(timezone.utc)`.
   - Fix: Replace both occurrences with `datetime.now(timezone.utc)`. Add `from datetime import timezone` import.

3. **`pathspec.PathSpec.from_lines("gitwildmatch", ...)` deprecated — 80+ warnings**
   - File: `evals/evals/pipeline.py:248,254`
   - What's wrong: `"gitwildmatch"` factory string is deprecated; pathspec now uses `"gitignore"`.
   - Why it matters: 80+ deprecation warnings per integration run. The fix is one-line.
   - Fix: Change both `"gitwildmatch"` to `"gitignore"` in `pipeline.py`.

4. **`assert proc.stdin is not None` in production path**
   - File: `evals/evals/runner.py:259`
   - What's wrong: Python `assert` is stripped under `-O` (optimized bytecode). If `proc.stdin` is somehow None, the program silently writes nothing.
   - Why it matters: Low probability but `assert` is not the right guard for production code.
   - Fix: Replace with `if proc.stdin is None: raise RuntimeError("Popen stdin pipe unexpectedly None")` or just remove the guard (Popen with `stdin=PIPE` always sets stdin).

---

### Recommendations

- The `_DEFAULT_DISALLOWED_PATHS` list and `_resolve_edit_constraints` function are copied identically between `runner.py` and `pipeline.py`. The spec explicitly permits this for v1, but a follow-on clean-up in a shared `constraints.py` would prevent them diverging.
- Consider silencing or upgrading the pathspec deprecation warning proactively before pathspec drops the old factory in a future release.

---

### Assessment

**Ready to merge: With fixes**

**Reasoning:** The core implementation is solid, complete, and spec-compliant with all 126 tests passing. Three minor/important issues (Unicode read, two deprecation warnings) are straightforward one-line fixes that improve robustness and test output clarity.

---

## Remediation Log

### Iteration 1

**Batch 1: Unicode + deprecation fixes**
- Fixed `discovery.py:140` — `fop.read_text(encoding="utf-8", errors="replace")`
- Fixed `campaign.py:18,22` — replace `datetime.utcnow()` with `datetime.now(timezone.utc)`
- Fixed `pipeline.py:248,254` — replace `"gitwildmatch"` with `"gitignore"`
- Fixed `runner.py:259` — removed unnecessary `assert proc.stdin is not None`
