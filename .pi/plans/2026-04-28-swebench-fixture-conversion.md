# SWE-bench Fixture Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert three SWE-bench Verified instances (`psf__requests-1921`, `pylint-dev__pylint-7080`, `pytest-dev__pytest-7571`) into self-contained fixtures conforming to `shared/task-spec.md`, with case manifests in `cases/` and reproducible install via `uv.lock`.

**Architecture:** For each instance, snapshot the upstream repo at `base_commit` into `fixtures/<instance_id>/`, apply the SWE-bench `test_patch` so the failing test exists in the fixture, generate a `uv.lock` for reproducibility, capture failure output, and write the case manifest. Cached SWE-bench raw data lives at `.pi/scratch/swebench/<instance_id>.json` (gitignored, already populated). The bootstrap is hand-driven (3 cases doesn't justify a CLI tool yet).

**Tech Stack:** Python 3.11+, uv, pytest, git. No new dependencies in the eval harness.

**Spec reference:** `.pi/specs/2026-04-28-swebench-fixture-conversion.md`

---

## File Structure

**Created (committed):**

- `fixtures/psf__requests-1921/` — full upstream snapshot at `base_commit 3c88e520da24ae6f736929a750876e7654accc3d`, plus `test_patch` applied, plus `pyproject.toml` (overlay) and `uv.lock` at the root
- `fixtures/pylint-dev__pylint-7080/` — full upstream snapshot at `base_commit 3c5eca2ded3dd2b59ebaf23eb289453b5d2930f0`, plus `test_patch` applied, plus `uv.lock` at the root
- `fixtures/pytest-dev__pytest-7571/` — full upstream snapshot at `base_commit 422685d0bdc110547535036c1ff398b5e1c44145`, plus `test_patch` applied, plus `uv.lock` at the root
- `cases/psf__requests-1921.json` — case manifest
- `cases/psf__requests-1921.failure_output.txt` — captured pytest failure
- `cases/pylint-dev__pylint-7080.json` — case manifest
- `cases/pylint-dev__pylint-7080.failure_output.txt` — captured pytest failure
- `cases/pytest-dev__pytest-7571.json` — case manifest
- `cases/pytest-dev__pytest-7571.failure_output.txt` — captured pytest failure

**Read but not committed:**

- `.pi/scratch/swebench/psf__requests-1921.json` — raw SWE-bench instance row (already populated)
- `.pi/scratch/swebench/pylint-dev__pylint-7080.json` — raw SWE-bench instance row (already populated)
- `.pi/scratch/swebench/pytest-dev__pytest-7571.json` — raw SWE-bench instance row (already populated)

**Modified:** None. The contract in `shared/task-spec.md` is unchanged.

---

## Task 0: Pre-flight verification

**Files:**
- Read: `.pi/scratch/swebench/psf__requests-1921.json`
- Read: `.pi/scratch/swebench/pylint-dev__pylint-7080.json`
- Read: `.pi/scratch/swebench/pytest-dev__pytest-7571.json`

- [ ] **Step 1: Confirm scratch data is present**

Run: `ls .pi/scratch/swebench/`
Expected output (exact filenames):
```
psf__requests-1921.json
pylint-dev__pylint-7080.json
pytest-dev__pytest-7571.json
```
If any file is missing, the cached pages at `/tmp/swebench/page_*.json` may have been cleared. Re-fetch from HuggingFace's datasets-server: `https://datasets-server.huggingface.co/rows?dataset=princeton-nlp%2FSWE-bench_Verified&config=default&split=test&offset=<O>&length=100` — the three instances live in pages with offset 200 (requests) and 300 (pylint, pytest). Save each row's `row` field to `.pi/scratch/swebench/<instance_id>.json`.

- [ ] **Step 2: Verify each instance's expected fields**

Run:
```bash
python3 -c "
import json
for inst in ['psf__requests-1921', 'pylint-dev__pylint-7080', 'pytest-dev__pytest-7571']:
    with open(f'.pi/scratch/swebench/{inst}.json') as f:
        r = json.load(f)
    f2p = json.loads(r['FAIL_TO_PASS']) if isinstance(r['FAIL_TO_PASS'], str) else r['FAIL_TO_PASS']
    p2p = json.loads(r['PASS_TO_PASS']) if isinstance(r['PASS_TO_PASS'], str) else r['PASS_TO_PASS']
    print(f'{inst}: base={r[\"base_commit\"][:8]} F2P={len(f2p)} P2P={len(p2p)}')
"
```

Expected output (exact):
```
psf__requests-1921: base=3c88e520 F2P=6 P2P=107
pylint-dev__pylint-7080: base=3c5eca2d F2P=1 P2P=120
pytest-dev__pytest-7571: base=422685d0 F2P=1 P2P=14
```

If counts differ, the spec's analysis is stale — STOP and reconcile with the user before continuing.

- [ ] **Step 3: Create scratch workdir for upstream clones**

Run: `mkdir -p .pi/scratch/upstream && ls .pi/scratch/`
Expected: `swebench` and `upstream` listed.

The `upstream/` subdir holds raw clones during bootstrapping; we copy *from* there *to* `fixtures/<instance_id>/` after applying `test_patch`. Both `.pi/scratch/swebench/` and `.pi/scratch/upstream/` are gitignored.

- [ ] **Step 4: Confirm uv is available**

Run: `uv --version`
Expected: a version string like `uv 0.4.x` or higher. If not installed, run `curl -LsSf https://astral.sh/uv/install.sh | sh` first.

- [ ] **Step 5: Commit nothing — Task 0 is read-only**

No commit. Proceed to Task 1.

---

## Task 1: Bootstrap `psf__requests-1921`

**Bug:** `Session.merge_setting` mishandles `None` — setting a session header to `None` should drop it from the merged request, but instead persists.
**F2P count:** 6 (multi-F2P; the primary visible/hidden split rule applies — visible = 1 hand-picked F2P, hidden = the other 5).
**Visible test choice:** `test_headers_on_session_with_None_are_not_sent` — its name is the most direct restatement of the bug as a user would describe it.
**Hidden tests:** the other 5 F2P tests (digest auth, postbin, basicauth_with_netrc, cookie_persists, uppercase_scheme_redirect — all pre-existing tests that cover Session-merge behavior on different paths).
**Expected install pain:** requests at this commit ships `setup.py` only — no PEP-621 pyproject. We add a thin `pyproject.toml` overlay at the fixture root so `uv sync` can lock the dependency graph.

**Files:**
- Create: `fixtures/psf__requests-1921/` (entire directory)
- Create: `fixtures/psf__requests-1921/pyproject.toml` (overlay; new file we author)
- Create: `fixtures/psf__requests-1921/uv.lock`
- Create: `cases/psf__requests-1921.json`
- Create: `cases/psf__requests-1921.failure_output.txt`

- [ ] **Step 1: Clone upstream at `base_commit`**

Run:
```bash
git clone https://github.com/psf/requests.git .pi/scratch/upstream/psf__requests-1921
cd .pi/scratch/upstream/psf__requests-1921
git checkout 3c88e520da24ae6f736929a750876e7654accc3d
cd -
```
Expected: clone succeeds; checkout reports `HEAD is now at 3c88e520...` (detached HEAD is fine).

- [ ] **Step 2: Apply the `test_patch`**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/psf__requests-1921.json') as f:
    r = json.load(f)
with open('.pi/scratch/upstream/psf__requests-1921/.test_patch.diff', 'w') as g:
    g.write(r['test_patch'])
"
cd .pi/scratch/upstream/psf__requests-1921
git apply .test_patch.diff
rm .test_patch.diff
cd -
```
Expected: `git apply` succeeds silently. The new test `test_headers_on_session_with_None_are_not_sent` is now in `test_requests.py`.

Verify: `grep -n "test_headers_on_session_with_None_are_not_sent" .pi/scratch/upstream/psf__requests-1921/test_requests.py`
Expected: at least one matching line in the test file.

- [ ] **Step 3: Discard upstream `.git/` and copy to fixture root**

Run:
```bash
rm -rf .pi/scratch/upstream/psf__requests-1921/.git
mkdir -p fixtures
cp -R .pi/scratch/upstream/psf__requests-1921 fixtures/psf__requests-1921
ls fixtures/psf__requests-1921/
```
Expected: directory listing shows `requests/`, `test_requests.py`, `setup.py`, `requirements.txt`, etc. — upstream's layout, no `.git/`.

- [ ] **Step 4: Author the fixture-root `pyproject.toml` overlay**

requests at this commit doesn't have a PEP-621 pyproject, so uv can't lock against it directly. We add a minimal overlay that exposes the local `requests` package and pins pytest as a dev dep.

Create `fixtures/psf__requests-1921/pyproject.toml`:
```toml
[project]
name = "requests-fixture"
version = "0.0.0"
description = "SWE-bench Verified instance psf__requests-1921 — fixture for urban-winner shootout"
requires-python = ">=3.11"
dependencies = [
  "chardet>=3.0.2,<4",
  "urllib3>=1.21.1,<1.26",
]

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["requests"]
```

Note: the `chardet`/`urllib3` pins are derived from `setup.py` at this commit; if `uv sync` complains in step 5, read the upstream `setup.py` install_requires and adjust.

- [ ] **Step 5: Generate `uv.lock`**

Run:
```bash
cd fixtures/psf__requests-1921
uv sync
cd -
```
Expected: `uv.lock` is created at `fixtures/psf__requests-1921/uv.lock`. `.venv/` is created (gitignored).

If uv complains about the dependency pins, edit `pyproject.toml` to relax constraints and retry. Document any deviation from upstream `setup.py` in case `notes`.

- [ ] **Step 6: Run the failing test, capture output**

Run:
```bash
cd fixtures/psf__requests-1921
uv run pytest -q "test_requests.py::RequestsTestCase::test_headers_on_session_with_None_are_not_sent" 2>&1 | tee ../../cases/psf__requests-1921.failure_output.txt
cd -
```

Expected: pytest exits non-zero. The captured file shows an assertion failure indicating the `None` header was sent. Inspect the output:

Run: `head -40 cases/psf__requests-1921.failure_output.txt`
Expected: first lines name the failing test; later lines show an assertion against the request headers containing the `None` value.

If the test passes (exit 0), STOP — `test_patch` may not have been applied correctly, or the gold patch was inadvertently applied. Verify by checking `requests/sessions.py` does *not* contain the gold patch's `if v is None:` clause.

- [ ] **Step 7: Verify gold patch resolves all F2P**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/psf__requests-1921.json') as f:
    r = json.load(f)
with open('/tmp/psf__requests-1921.gold.diff', 'w') as g:
    g.write(r['patch'])
"
cd fixtures/psf__requests-1921
# Apply gold patch in a throwaway way (we'll revert via /tmp backup below).
# The fixture has no .git/, so this just modifies tracked files in-place;
# step 7 ends by restoring them.
cp requests/sessions.py /tmp/sessions.py.orig
patch -p1 < /tmp/psf__requests-1921.gold.diff
uv run pytest -q \
  "test_requests.py::RequestsTestCase::test_DIGESTAUTH_WRONG_HTTP_401_GET" \
  "test_requests.py::RequestsTestCase::test_POSTBIN_GET_POST_FILES" \
  "test_requests.py::RequestsTestCase::test_basicauth_with_netrc" \
  "test_requests.py::RequestsTestCase::test_cookie_persists_via_api" \
  "test_requests.py::RequestsTestCase::test_headers_on_session_with_None_are_not_sent" \
  "test_requests.py::RequestsTestCase::test_uppercase_scheme_redirect"
# Revert
cp /tmp/sessions.py.orig requests/sessions.py
cd -
```
Expected: all 6 tests pass under the gold patch. After reverting, the next step's verification still works.

- [ ] **Step 8: Construct an under-fix variant and verify hidden discriminates**

Read the gold patch at `/tmp/psf__requests-1921.gold.diff` (one screenful). It guards `merge_setting` so that a `None` value in the session setting drops the merged key.

Construct a *narrow* under-fix: same logic but only applied when the session setting is a `dict`-with-Nones, not when the request setting carries Nones. Edit `fixtures/psf__requests-1921/requests/sessions.py` to add a defensive `.pop(k)` only on the request side rather than merging both sides correctly. (Specific edit lives in your judgment — read the upstream code at lines around 59-70 of `sessions.py` and aim for "fixes the visible test by handling its specific input shape, but skips the more general case the other F2P tests cover.")

Run:
```bash
cd fixtures/psf__requests-1921
# (under-fix has been applied to sessions.py)
uv run pytest -q "test_requests.py::RequestsTestCase::test_headers_on_session_with_None_are_not_sent"
```
Expected: PASS (the visible test).

Then:
```bash
uv run pytest -q \
  "test_requests.py::RequestsTestCase::test_DIGESTAUTH_WRONG_HTTP_401_GET" \
  "test_requests.py::RequestsTestCase::test_POSTBIN_GET_POST_FILES" \
  "test_requests.py::RequestsTestCase::test_basicauth_with_netrc" \
  "test_requests.py::RequestsTestCase::test_cookie_persists_via_api" \
  "test_requests.py::RequestsTestCase::test_uppercase_scheme_redirect"
```
Expected: at least one FAIL among these five.

If the hidden command does NOT discriminate (all pass), the under-fix is too broad — narrow it. If you cannot construct an under-fix that splits visible from hidden, the visible/hidden choice is wrong: pick a different visible test from F2P and retry.

After verification, revert the under-fix:
```bash
cp /tmp/sessions.py.orig requests/sessions.py
cd -
```

Record the under-fix description in case `notes` for step 9.

- [ ] **Step 9: Write `cases/psf__requests-1921.json`**

Create with this content (substitute the under-fix description recorded in step 8):
```json
{
  "case_id": "psf__requests-1921",
  "fixture_repo": "fixtures/psf__requests-1921",
  "failing_test_command": "uv run pytest -q test_requests.py::RequestsTestCase::test_headers_on_session_with_None_are_not_sent",
  "failure_output_path": "cases/psf__requests-1921.failure_output.txt",
  "hidden_test_command": "uv run pytest -q test_requests.py::RequestsTestCase::test_DIGESTAUTH_WRONG_HTTP_401_GET test_requests.py::RequestsTestCase::test_POSTBIN_GET_POST_FILES test_requests.py::RequestsTestCase::test_basicauth_with_netrc test_requests.py::RequestsTestCase::test_cookie_persists_via_api test_requests.py::RequestsTestCase::test_uppercase_scheme_redirect",
  "edit_constraints": {},
  "notes": "Source: SWE-bench Verified, instance psf__requests-1921, base_commit 3c88e520da24ae6f736929a750876e7654accc3d, upstream https://github.com/psf/requests. Visible test = test_headers_on_session_with_None_are_not_sent (name directly restates the bug). Hidden tests = the other 5 FAIL_TO_PASS, covering session-merge paths through digest auth, file POST, basic auth via netrc, cookie persistence, and uppercase scheme redirects. Under-fix patterns the hidden tests catch: <fill in from step 8 — e.g., 'a fix that special-cases None on the request side only, missing the session-side merge case used by digest auth and netrc auth'>. uv overlay required: yes — fixture-root pyproject.toml authored because upstream at this commit ships only setup.py."
}
```

- [ ] **Step 10: Verify case manifest end-to-end**

Run:
```bash
cd fixtures/psf__requests-1921
$(jq -r '.failing_test_command' ../../cases/psf__requests-1921.json) 2>&1 | tail -5
cd -
```
Expected: pytest exits non-zero; final lines show the assertion failure.

Then run:
```bash
diff <(cd fixtures/psf__requests-1921 && uv run pytest -q test_requests.py::RequestsTestCase::test_headers_on_session_with_None_are_not_sent 2>&1 | head -20) <(head -20 cases/psf__requests-1921.failure_output.txt)
```
Expected: differences are limited to volatile fields (timestamps, durations, absolute paths). The exception type, failing test node ID, and primary assertion message must match.

- [ ] **Step 11: Commit**

```bash
git add fixtures/psf__requests-1921/ cases/psf__requests-1921.json cases/psf__requests-1921.failure_output.txt
git commit -m "Fixture: psf__requests-1921 — Session.merge_setting None handling

SWE-bench Verified instance, base_commit 3c88e520. Visible test is
test_headers_on_session_with_None_are_not_sent; hidden tests are the
other 5 FAIL_TO_PASS. Fixture root carries an authored pyproject.toml
overlay because upstream at this commit ships only setup.py."
```

Verify: `git log --oneline -3` shows the new commit at HEAD.

---

## Task 2: Bootstrap `pylint-dev__pylint-7080`

**Bug:** `pylint --recursive=y` ignores `ignore-paths`. Fix is one line in `pylint/lint/expand_modules.py:_is_ignored_file` (path normalization missing).
**F2P count:** 1 — `tests/test_self.py::TestRunTC::test_ignore_path_recursive_current_dir`. Triggers PASS_TO_PASS-fallback for hidden.
**Visible test:** the sole F2P (no choice).
**Hidden tests (curated PASS_TO_PASS subset):** other tests in `test_self.py` that exercise recursion, ignore_paths, or expand_modules. The author hand-picks at most ~10 by reading test names; the goal is "tests likely to fail under a partial fix that special-cases the visible test's input."
**Expected install pain:** pylint at this commit uses a Poetry-style PEP-621 pyproject. `uv sync` should work directly; `[testutils]`-style extras may be needed for the test deps.

**Files:**
- Create: `fixtures/pylint-dev__pylint-7080/` (entire directory)
- Create: `fixtures/pylint-dev__pylint-7080/uv.lock`
- Create: `cases/pylint-dev__pylint-7080.json`
- Create: `cases/pylint-dev__pylint-7080.failure_output.txt`

- [ ] **Step 1: Clone upstream at `base_commit`**

Run:
```bash
git clone https://github.com/pylint-dev/pylint.git .pi/scratch/upstream/pylint-dev__pylint-7080
cd .pi/scratch/upstream/pylint-dev__pylint-7080
git checkout 3c5eca2ded3dd2b59ebaf23eb289453b5d2930f0
cd -
```
Expected: clone + checkout succeed.

- [ ] **Step 2: Apply the `test_patch`**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/pylint-dev__pylint-7080.json') as f:
    r = json.load(f)
with open('.pi/scratch/upstream/pylint-dev__pylint-7080/.test_patch.diff', 'w') as g:
    g.write(r['test_patch'])
"
cd .pi/scratch/upstream/pylint-dev__pylint-7080
git apply .test_patch.diff
rm .test_patch.diff
cd -
```
Expected: `git apply` succeeds. New test `test_ignore_path_recursive_current_dir` lives in `tests/test_self.py`.

Verify: `grep -n "test_ignore_path_recursive_current_dir" .pi/scratch/upstream/pylint-dev__pylint-7080/tests/test_self.py`
Expected: at least one matching line.

- [ ] **Step 3: Discard `.git/` and copy to fixture root**

Run:
```bash
rm -rf .pi/scratch/upstream/pylint-dev__pylint-7080/.git
cp -R .pi/scratch/upstream/pylint-dev__pylint-7080 fixtures/pylint-dev__pylint-7080
ls fixtures/pylint-dev__pylint-7080/
```
Expected: pylint repo layout — `pylint/`, `tests/`, `pyproject.toml`, etc., no `.git/`.

- [ ] **Step 4: Inspect upstream `pyproject.toml`**

Run: `head -40 fixtures/pylint-dev__pylint-7080/pyproject.toml`
Expected: `[project]` table with PEP-621 metadata, plus `[project.optional-dependencies]` likely including `testutils`.

- [ ] **Step 5: Generate `uv.lock`**

Run:
```bash
cd fixtures/pylint-dev__pylint-7080
uv sync --all-extras
cd -
```
Expected: `uv.lock` created. `.venv/` created.

If `uv sync --all-extras` fails:
- If the failure is about a Poetry-only field (e.g., `[tool.poetry.dependencies]` without PEP-621 deps), add a thin `[tool.uv]` section authoring an explicit dep set, or convert the Poetry deps to PEP-621 in a fixture-local way. Document the change in case `notes`.
- If the failure is a transitive resolution issue, narrow with `uv sync --extra testutils` and proceed.

- [ ] **Step 6: Run the failing test, capture output**

Run:
```bash
cd fixtures/pylint-dev__pylint-7080
uv run pytest -q "tests/test_self.py::TestRunTC::test_ignore_path_recursive_current_dir" 2>&1 | tee ../../cases/pylint-dev__pylint-7080.failure_output.txt
cd -
```
Expected: pytest exits non-zero. The captured file shows the test failed because `--recursive=y` linted files inside the ignored path.

Run: `head -40 cases/pylint-dev__pylint-7080.failure_output.txt`
Expected: pytest output with the failing test node ID and an assertion that should not have happened (e.g., a violation reported in a path that was supposed to be ignored).

- [ ] **Step 7: Verify gold patch fixes the visible test**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/pylint-dev__pylint-7080.json') as f:
    r = json.load(f)
with open('/tmp/pylint-dev__pylint-7080.gold.diff', 'w') as g:
    g.write(r['patch'])
"
cd fixtures/pylint-dev__pylint-7080
cp pylint/lint/expand_modules.py /tmp/expand_modules.py.orig
patch -p1 < /tmp/pylint-dev__pylint-7080.gold.diff
uv run pytest -q "tests/test_self.py::TestRunTC::test_ignore_path_recursive_current_dir"
# Revert
cp /tmp/expand_modules.py.orig pylint/lint/expand_modules.py
cd -
```
Expected: with the gold patch applied, the visible test passes.

- [ ] **Step 8: Pick the hidden subset and verify under-fix discrimination**

Read `tests/test_self.py` and identify tests that exercise the same code path (recursion, ignore-paths, expand_modules). Aim for a small, focused subset (5-10 tests). Construct a `pytest -k` selector or an explicit list.

Suggested starting selector: `pytest -k "recursive or ignore_path or ignore_paths or expand"`

Run from the fixture root:
```bash
cd fixtures/pylint-dev__pylint-7080
uv run pytest --collect-only -q -k "recursive or ignore_path or ignore_paths or expand" tests/test_self.py | head -30
cd -
```
Expected: a list of tests that's neither empty nor enormous (~5-15 tests).

Construct an under-fix variant of the gold patch — for example, a fix that normalizes paths via `str.startswith` rather than the gold patch's correct approach, or one that handles only the "current directory" case. Apply it and verify:

```bash
cd fixtures/pylint-dev__pylint-7080
# (under-fix has been applied)
uv run pytest -q "tests/test_self.py::TestRunTC::test_ignore_path_recursive_current_dir"  # should PASS
uv run pytest -q -k "recursive or ignore_path or ignore_paths or expand" tests/test_self.py  # should have at least one FAIL
cp /tmp/expand_modules.py.orig pylint/lint/expand_modules.py  # revert
cd -
```

If no hidden test fails under any plausible under-fix, narrow the hidden selector to tests that more directly exercise the bug's input shape. Record the under-fix description in `notes`.

Note: if `--recursive` and `ignore_path` aren't keywords that match enough P2P tests, switch to listing test node IDs explicitly.

- [ ] **Step 9: Write `cases/pylint-dev__pylint-7080.json`**

Create with this content (substitute the chosen hidden selector and under-fix description):
```json
{
  "case_id": "pylint-dev__pylint-7080",
  "fixture_repo": "fixtures/pylint-dev__pylint-7080",
  "failing_test_command": "uv run pytest -q tests/test_self.py::TestRunTC::test_ignore_path_recursive_current_dir",
  "failure_output_path": "cases/pylint-dev__pylint-7080.failure_output.txt",
  "hidden_test_command": "uv run pytest -q -k 'recursive or ignore_path or ignore_paths or expand' tests/test_self.py",
  "edit_constraints": {},
  "notes": "Source: SWE-bench Verified, instance pylint-dev__pylint-7080, base_commit 3c5eca2ded3dd2b59ebaf23eb289453b5d2930f0, upstream https://github.com/pylint-dev/pylint. Single FAIL_TO_PASS test → degradation case: hidden subset drawn from PASS_TO_PASS by selector matching 'recursive', 'ignore_path', 'ignore_paths', or 'expand' in the same test_self.py module. Bug is one-line fix in pylint/lint/expand_modules.py:_is_ignored_file (path normalization). Under-fix patterns the hidden tests catch: <fill in from step 8>. uv overlay required: <yes/no, with reason>."
}
```

- [ ] **Step 10: Verify case manifest end-to-end**

Run:
```bash
cd fixtures/pylint-dev__pylint-7080
$(jq -r '.failing_test_command' ../../cases/pylint-dev__pylint-7080.json) 2>&1 | tail -5
cd -
```
Expected: pytest exits non-zero; output matches the captured failure structurally.

- [ ] **Step 11: Commit**

```bash
git add fixtures/pylint-dev__pylint-7080/ cases/pylint-dev__pylint-7080.json cases/pylint-dev__pylint-7080.failure_output.txt
git commit -m "Fixture: pylint-dev__pylint-7080 — --recursive ignores ignore-paths

SWE-bench Verified instance, base_commit 3c5eca2d. Single F2P, so
hidden subset is curated from PASS_TO_PASS by keyword selector
covering recursion, ignore-path, and expand-modules tests. Failure
surfaces in tests/test_self.py; fix is one line in
pylint/lint/expand_modules.py."
```

---

## Task 3: Bootstrap `pytest-dev__pytest-7571`

**Bug:** `caplog` fixture doesn't restore the handler log level after a test mutates it. Fix is three coordinated edits in `src/_pytest/logging.py` (init / set / finalize).
**F2P count:** 1 — `testing/logging/test_fixture.py::test_change_level_undos_handler_level`. Triggers PASS_TO_PASS-fallback for hidden.
**Visible test:** the sole F2P (no choice).
**Hidden tests (curated PASS_TO_PASS subset):** other tests in `testing/logging/test_fixture.py` that exercise level changes or fixture finalization. With only 14 P2P tests total, all of them in `testing/logging/`, we can take a generous selector.
**Expected install pain:** pytest at this commit uses setuptools + `tox.ini`. May need a fixture-root `pyproject.toml` overlay; pytest's own dev dep set (`mock`, `hypothesis`, etc.) needs to be installable.

**Files:**
- Create: `fixtures/pytest-dev__pytest-7571/` (entire directory)
- Create: `fixtures/pytest-dev__pytest-7571/pyproject.toml` (overlay if upstream pyproject is insufficient)
- Create: `fixtures/pytest-dev__pytest-7571/uv.lock`
- Create: `cases/pytest-dev__pytest-7571.json`
- Create: `cases/pytest-dev__pytest-7571.failure_output.txt`

- [ ] **Step 1: Clone upstream at `base_commit`**

Run:
```bash
git clone https://github.com/pytest-dev/pytest.git .pi/scratch/upstream/pytest-dev__pytest-7571
cd .pi/scratch/upstream/pytest-dev__pytest-7571
git checkout 422685d0bdc110547535036c1ff398b5e1c44145
cd -
```

- [ ] **Step 2: Apply the `test_patch`**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/pytest-dev__pytest-7571.json') as f:
    r = json.load(f)
with open('.pi/scratch/upstream/pytest-dev__pytest-7571/.test_patch.diff', 'w') as g:
    g.write(r['test_patch'])
"
cd .pi/scratch/upstream/pytest-dev__pytest-7571
git apply .test_patch.diff
rm .test_patch.diff
cd -
```

Verify: `grep -n "test_change_level_undos_handler_level" .pi/scratch/upstream/pytest-dev__pytest-7571/testing/logging/test_fixture.py`
Expected: matching line.

- [ ] **Step 3: Discard `.git/` and copy to fixture root**

Run:
```bash
rm -rf .pi/scratch/upstream/pytest-dev__pytest-7571/.git
cp -R .pi/scratch/upstream/pytest-dev__pytest-7571 fixtures/pytest-dev__pytest-7571
ls fixtures/pytest-dev__pytest-7571/
```

- [ ] **Step 4: Inspect upstream packaging**

Run:
```bash
head -30 fixtures/pytest-dev__pytest-7571/setup.cfg 2>/dev/null
ls fixtures/pytest-dev__pytest-7571/pyproject.toml 2>/dev/null
```
Expected: `setup.cfg` with `metadata` and `options` sections; `pyproject.toml` may exist with only build-system metadata.

- [ ] **Step 5: Generate `uv.lock`**

First attempt — let uv install pytest from its own setup.cfg:
```bash
cd fixtures/pytest-dev__pytest-7571
uv sync --extra testing
cd -
```

If that fails (setup.cfg-only projects are spotty in uv), author a fixture-root `pyproject.toml` overlay:
```toml
[project]
name = "pytest-fixture"
version = "0.0.0"
description = "SWE-bench Verified instance pytest-dev__pytest-7571 — fixture for urban-winner shootout"
requires-python = ">=3.11"
dependencies = [
  "attrs>=19.2.0",
  "iniconfig",
  "packaging",
  "pluggy>=0.12,<1.0",
  "py>=1.8.2",
  "toml",
]

[dependency-groups]
dev = [
  "hypothesis>=3.56",
  "mock",
  "requests",
]

[build-system]
requires = ["setuptools>=42", "wheel"]
build-backend = "setuptools.build_meta"
```

Then re-run `uv sync` (without `--extra testing` since we declare deps directly).

- [ ] **Step 6: Run the failing test, capture output**

Run:
```bash
cd fixtures/pytest-dev__pytest-7571
uv run pytest -q "testing/logging/test_fixture.py::test_change_level_undos_handler_level" 2>&1 | tee ../../cases/pytest-dev__pytest-7571.failure_output.txt
cd -
```
Expected: pytest exits non-zero. Captured output shows the handler level was not restored.

- [ ] **Step 7: Verify gold patch fixes the visible test**

Run:
```bash
python3 -c "
import json
with open('.pi/scratch/swebench/pytest-dev__pytest-7571.json') as f:
    r = json.load(f)
with open('/tmp/pytest-dev__pytest-7571.gold.diff', 'w') as g:
    g.write(r['patch'])
"
cd fixtures/pytest-dev__pytest-7571
cp src/_pytest/logging.py /tmp/logging.py.orig
patch -p1 < /tmp/pytest-dev__pytest-7571.gold.diff
uv run pytest -q "testing/logging/test_fixture.py::test_change_level_undos_handler_level"
cp /tmp/logging.py.orig src/_pytest/logging.py  # revert
cd -
```
Expected: gold patch makes the visible test pass.

- [ ] **Step 8: Pick the hidden subset and verify under-fix discrimination**

The bug is "handler log level not restored." Plausible under-fixes:
- Restore the *logger* level but not the *handler* level (most common partial fix — both have a `setLevel` API and an unwary engineer fixes the wrong one).
- Restore the handler level only at finalize, not on each set, so nested `set_level` calls leak.
- Hardcode the visible test's specific level value somewhere.

Hidden subset candidate: all tests in `testing/logging/test_fixture.py` excluding the visible test. With ~14 P2P tests total, this is a manageable set.

Run:
```bash
cd fixtures/pytest-dev__pytest-7571
uv run pytest --collect-only -q "testing/logging/test_fixture.py" | head -30
cd -
```
Expected: list of tests in the fixture file.

Construct under-fix #1 (restore logger but not handler): edit `src/_pytest/logging.py` to apply the gold patch's logic to the logger only, leaving handler level state untouched. Verify:
```bash
cd fixtures/pytest-dev__pytest-7571
# (under-fix #1 applied)
uv run pytest -q "testing/logging/test_fixture.py::test_change_level_undos_handler_level"  # should PASS
uv run pytest -q "testing/logging/test_fixture.py" --deselect "testing/logging/test_fixture.py::test_change_level_undos_handler_level"  # should have ≥1 FAIL
cp /tmp/logging.py.orig src/_pytest/logging.py
cd -
```
Expected: under-fix passes visible, fails at least one hidden test.

If the deselect-all-but-visible approach catches the under-fix, use it for the hidden command. If it doesn't, fall back to a narrower selector targeting tests that exercise both logger and handler levels (read the test bodies).

Record the under-fix description in `notes`.

- [ ] **Step 9: Write `cases/pytest-dev__pytest-7571.json`**

Create with this content (substitute hidden selector and under-fix description):
```json
{
  "case_id": "pytest-dev__pytest-7571",
  "fixture_repo": "fixtures/pytest-dev__pytest-7571",
  "failing_test_command": "uv run pytest -q testing/logging/test_fixture.py::test_change_level_undos_handler_level",
  "failure_output_path": "cases/pytest-dev__pytest-7571.failure_output.txt",
  "hidden_test_command": "uv run pytest -q testing/logging/test_fixture.py --deselect testing/logging/test_fixture.py::test_change_level_undos_handler_level",
  "edit_constraints": {},
  "notes": "Source: SWE-bench Verified, instance pytest-dev__pytest-7571, base_commit 422685d0bdc110547535036c1ff398b5e1c44145, upstream https://github.com/pytest-dev/pytest. Single FAIL_TO_PASS test → degradation case: hidden subset = all other tests in testing/logging/test_fixture.py (~13 tests, all in the same module exercising the caplog fixture). Bug requires three coordinated edits in src/_pytest/logging.py (init / set / finalize). Under-fix patterns the hidden tests catch: <fill in from step 8 — e.g., 'restoring logger.setLevel without restoring handler.setLevel; finalize-only restoration that leaks across nested set_level calls'>. uv overlay required: <yes/no, with reason>."
}
```

- [ ] **Step 10: Verify case manifest end-to-end**

Run:
```bash
cd fixtures/pytest-dev__pytest-7571
$(jq -r '.failing_test_command' ../../cases/pytest-dev__pytest-7571.json) 2>&1 | tail -5
cd -
```
Expected: pytest exits non-zero; output matches captured failure structurally.

- [ ] **Step 11: Commit**

```bash
git add fixtures/pytest-dev__pytest-7571/ cases/pytest-dev__pytest-7571.json cases/pytest-dev__pytest-7571.failure_output.txt
git commit -m "Fixture: pytest-dev__pytest-7571 — caplog handler level not restored

SWE-bench Verified instance, base_commit 422685d0. Single F2P, so
hidden subset is the rest of testing/logging/test_fixture.py (~13
tests). Bug requires three coordinated edits in src/_pytest/logging.py
(init/set/finalize); partial fixes that handle only logger or only
handler are highly plausible and the hidden subset catches them."
```

---

## Verification After All Three Tasks

- [ ] **Step 1: All three case files parse**

Run:
```bash
for f in cases/psf__requests-1921.json cases/pylint-dev__pylint-7080.json cases/pytest-dev__pytest-7571.json; do
  python3 -c "import json; json.load(open('$f'))" && echo "OK $f" || echo "BAD $f"
done
```
Expected: three `OK` lines.

- [ ] **Step 2: All three failure_output sidecars are non-empty**

Run: `wc -l cases/*.failure_output.txt`
Expected: each line shows >5 lines of content.

- [ ] **Step 3: Each failing_test_command still fails as captured**

Run:
```bash
for case in psf__requests-1921 pylint-dev__pylint-7080 pytest-dev__pytest-7571; do
  cmd=$(jq -r '.failing_test_command' cases/$case.json)
  echo "=== $case ==="
  (cd fixtures/$case && eval "$cmd" >/dev/null 2>&1; echo "exit=$?")
done
```
Expected: each `exit=` line is non-zero.

- [ ] **Step 4: Each fixture's repo state is clean of leftover patches**

Run:
```bash
for case in psf__requests-1921 pylint-dev__pylint-7080 pytest-dev__pytest-7571; do
  echo "=== $case ==="
  ls fixtures/$case/.test_patch.diff 2>&1 | head -1
done
```
Expected: each line says "No such file or directory" — leftover diff files have been removed.

- [ ] **Step 5: Final summary**

Run:
```bash
git log --oneline -5
```
Expected: top three commits are the three Fixture commits in order: pytest, pylint, requests.

---

## Self-Review

This plan covers every section of the spec at `.pi/specs/2026-04-28-swebench-fixture-conversion.md`:

- Conversion pipeline (clone → apply test_patch → discard .git → lock → capture → manifest → verify): Steps 1-10 of each Task 1/2/3.
- Fixture layout (snapshot at fixture root, no .git): Steps 1-4.
- Case manifest (existing schema, provenance in notes): Step 9.
- Visible/hidden test selection (multi-F2P split for requests; single-F2P + curated PASS_TO_PASS for pylint and pytest): Step 8 of each task.
- Per-case bootstrap order (requests → pylint → pytest, separate commits): Tasks 1, 2, 3 ordered as written; commits in step 11.
- Acceptance criteria (failing test fails as captured; gold patch passes; under-fix fails hidden): Verified in steps 7, 8, and 10 of each task, plus the post-task verification block.

No placeholders flagged in self-review.
