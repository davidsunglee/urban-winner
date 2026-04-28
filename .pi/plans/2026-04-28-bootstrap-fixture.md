# parse_duration Bootstrap Fixture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `py-parse-duration-001` benchmark case described in `.pi/specs/2026-04-28-bootstrap-fixture-design.md` — a hand-rolled Python fixture where the visible test fails with `KeyError`, the bug lives in `units.py` rather than the file in the stack trace, and a hidden test catches plausible partial fixes.

**Architecture:** Plain Python package under `fixtures/parse_duration/` containing the buggy `UNITS = {"m": 60}` dict imported by an otherwise-correct parser; pytest tests in a sibling `tests/` directory; a separate `cases/py-parse-duration-001.json` manifest at the repo root pointing into the fixture, with the captured failure output stored in `cases/py-parse-duration-001.failure_output.txt`. The fixture repo contains only fixture files — no harness metadata — so a future harness can copy it cleanly into a worktree.

**Tech Stack:** Python ≥3.11, pytest ≥8, uv for local environment management. No external services, no network access, no credentials.

---

### Task 1: Fixture skeleton and dev environment

**Files:**
- Create: `fixtures/parse_duration/pyproject.toml`
- Create: `fixtures/parse_duration/parse_duration/__init__.py` (empty)

- [ ] **Step 1: Write `fixtures/parse_duration/pyproject.toml`**

```toml
[project]
name = "parse-duration-fixture"
version = "0.0.0"
description = "Bootstrap fixture for the urban-winner agent shootout"
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["parse_duration"]
```

- [ ] **Step 2: Create empty `fixtures/parse_duration/parse_duration/__init__.py`**

(Empty file — write zero bytes. Required so the next task can import the package.)

- [ ] **Step 3: Resolve dev deps**

Run: `cd fixtures/parse_duration && uv sync`
Expected: exits 0, prints something like `Resolved N packages` and `Installed N packages`. Creates `fixtures/parse_duration/.venv/` and `fixtures/parse_duration/uv.lock`.

- [ ] **Step 4: Verify pytest is available**

Run: `cd fixtures/parse_duration && uv run pytest --version`
Expected: prints `pytest 8.x.x`, exits 0.

- [ ] **Step 5: Commit**

```bash
git add fixtures/parse_duration/pyproject.toml fixtures/parse_duration/uv.lock fixtures/parse_duration/parse_duration/__init__.py
git commit -m "Bootstrap fixture: parse_duration pyproject and empty package"
```

(`.venv/` is excluded by the existing root `.gitignore`.)

### Task 2: Visible failing test

**Files:**
- Create: `fixtures/parse_duration/tests/test_parse_duration.py`

- [ ] **Step 1: Write the visible test**

`fixtures/parse_duration/tests/test_parse_duration.py`:
```python
from parse_duration import parse_duration


def test_parse_seconds():
    assert parse_duration("5s") == 5
```

- [ ] **Step 2: Run it; expect ImportError**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration.py`
Expected: exits non-zero. Output contains `ImportError` (cannot import `parse_duration` from the `parse_duration` package, which is currently empty).

- [ ] **Step 3: Commit**

```bash
git add fixtures/parse_duration/tests/test_parse_duration.py
git commit -m "Bootstrap fixture: add visible failing test"
```

### Task 3: Buggy parse_duration implementation

**Files:**
- Create: `fixtures/parse_duration/parse_duration/units.py`
- Create: `fixtures/parse_duration/parse_duration/parser.py`
- Modify: `fixtures/parse_duration/parse_duration/__init__.py`

- [ ] **Step 1: Write the (intentionally-incomplete) units map**

`fixtures/parse_duration/parse_duration/units.py`:
```python
UNITS = {"m": 60}
```

- [ ] **Step 2: Write the parser**

`fixtures/parse_duration/parse_duration/parser.py`:
```python
from .units import UNITS


def parse_duration(s: str) -> int:
    """Parse a duration string into seconds.

    Supported units: ``s`` (seconds), ``m`` (minutes), ``h`` (hours).
    """
    return int(s[:-1]) * UNITS[s[-1]]
```

- [ ] **Step 3: Re-export from the package**

`fixtures/parse_duration/parse_duration/__init__.py`:
```python
from .parser import parse_duration

__all__ = ["parse_duration"]
```

- [ ] **Step 4: Run the visible test; expect KeyError**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration.py`
Expected: exits non-zero, `1 failed`. Stack trace contains `KeyError: 's'` and the deepest project frame points at `parse_duration/parser.py` line `return int(s[:-1]) * UNITS[s[-1]]`.

- [ ] **Step 5: Sanity-check that minutes still work**

Run: `cd fixtures/parse_duration && uv run python -c "from parse_duration import parse_duration; print(parse_duration('10m'))"`
Expected: prints `600`, exits 0.

- [ ] **Step 6: Commit**

```bash
git add fixtures/parse_duration/parse_duration/parser.py fixtures/parse_duration/parse_duration/units.py fixtures/parse_duration/parse_duration/__init__.py
git commit -m "Bootstrap fixture: add buggy parse_duration (UNITS missing s, h)"
```

### Task 4: Hidden test

**Files:**
- Create: `fixtures/parse_duration/tests/test_parse_duration_extended.py`

- [ ] **Step 1: Write the hidden test file**

`fixtures/parse_duration/tests/test_parse_duration_extended.py`:
```python
from parse_duration import parse_duration


def test_parse_hours():
    assert parse_duration("1h") == 3600


def test_parse_minutes_still_works():
    assert parse_duration("10m") == 600
```

- [ ] **Step 2: Run it; expect one failure, one pass**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration_extended.py`
Expected: exits non-zero, output contains `1 failed, 1 passed`. The failure is `test_parse_hours` with `KeyError: 'h'`; `test_parse_minutes_still_works` passes.

- [ ] **Step 3: Commit**

```bash
git add fixtures/parse_duration/tests/test_parse_duration_extended.py
git commit -m "Bootstrap fixture: add hidden test (catches under-fixes for h, regressions on m)"
```

### Task 5: README

**Files:**
- Create: `fixtures/parse_duration/README.md`

- [ ] **Step 1: Write the README**

`fixtures/parse_duration/README.md`:
````markdown
# parse_duration

A small Python module that parses duration strings into seconds. Supported units:

| Suffix | Meaning |
| ------ | ------- |
| `s`    | seconds |
| `m`    | minutes |
| `h`    | hours   |

```python
from parse_duration import parse_duration

parse_duration("5s")   # 5
parse_duration("10m")  # 600
parse_duration("1h")   # 3600
```

## Tests

```sh
uv sync
uv run pytest -q tests/
```
````

- [ ] **Step 2: Commit**

```bash
git add fixtures/parse_duration/README.md
git commit -m "Bootstrap fixture: README documenting s/m/h units"
```

### Task 6: Capture failure_output

**Files:**
- Create: `cases/py-parse-duration-001.failure_output.txt`

- [ ] **Step 1: Capture the visible test's combined stdout/stderr**

Run from repo root:
```bash
mkdir -p cases
( cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration.py ) > cases/py-parse-duration-001.failure_output.txt 2>&1 ; true
```

(The trailing `; true` swallows pytest's non-zero exit so the redirection still records the file.)

- [ ] **Step 2: Verify the capture**

Run: `grep -c "KeyError: 's'" cases/py-parse-duration-001.failure_output.txt`
Expected: prints `1` (or more), exits 0.

Run: `grep -c "1 failed" cases/py-parse-duration-001.failure_output.txt`
Expected: prints `1`, exits 0.

- [ ] **Step 3: Commit**

```bash
git add cases/py-parse-duration-001.failure_output.txt
git commit -m "Bootstrap fixture: capture failure_output for the visible test"
```

### Task 7: Case manifest

**Files:**
- Create: `cases/py-parse-duration-001.json`

- [ ] **Step 1: Write the manifest**

`cases/py-parse-duration-001.json`:
```json
{
  "case_id": "py-parse-duration-001",
  "fixture_repo": "fixtures/parse_duration",
  "failing_test_command": "pytest -q tests/test_parse_duration.py",
  "failure_output_path": "cases/py-parse-duration-001.failure_output.txt",
  "hidden_test_command": "pytest -q tests/test_parse_duration_extended.py",
  "edit_constraints": {},
  "notes": "Bootstrap fixture; bug is data in parse_duration/units.py while the stack trace points at parse_duration/parser.py. Hidden test catches three under-fixes: adding only 's' to UNITS, defensive UNITS.get(..., 1) in parser.py, and hardcoding the failing input in parser.py."
}
```

The spec's `failure_output` field is referenced indirectly through `failure_output_path` so the multi-line trace lives in a sidecar file rather than being JSON-escaped inline. The harness, when it lands, reads the file and substitutes the contents into the agent's `input.failure_output`. This is a v1 manifest convention, not a spec change.

- [ ] **Step 2: Validate the JSON parses**

Run: `python -c "import json,sys; json.load(open('cases/py-parse-duration-001.json'))" && echo ok`
Expected: prints `ok`, exits 0.

- [ ] **Step 3: Validate the referenced paths exist**

Run from repo root:
```bash
test -d fixtures/parse_duration && test -f cases/py-parse-duration-001.failure_output.txt && echo ok
```
Expected: prints `ok`, exits 0.

- [ ] **Step 4: Commit**

```bash
git add cases/py-parse-duration-001.json
git commit -m "Bootstrap fixture: py-parse-duration-001 case manifest"
```

### Task 8: Verify canonical fix path (no commit)

This task does NOT modify any committed file. It applies the canonical fix locally, verifies both test files pass, then reverts so the fixture remains in its failing-as-designed state.

**Files:** none modified after this task completes.

- [ ] **Step 1: Apply the canonical fix locally**

Edit `fixtures/parse_duration/parse_duration/units.py` to:
```python
UNITS = {"s": 1, "m": 60, "h": 3600}
```

- [ ] **Step 2: Run visible test; expect pass**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration.py`
Expected: exits 0, output contains `1 passed`.

- [ ] **Step 3: Run hidden test; expect both pass**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/test_parse_duration_extended.py`
Expected: exits 0, output contains `2 passed`.

- [ ] **Step 4: Run the full test directory; expect three passes**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/`
Expected: exits 0, output contains `3 passed`.

- [ ] **Step 5: Revert the fix via the parent repo's git**

Run from repo root: `git checkout -- fixtures/parse_duration/parse_duration/units.py`

- [ ] **Step 6: Confirm fixture is back to broken state**

Run: `cd fixtures/parse_duration && uv run pytest -q tests/`
Expected: exits non-zero, output contains `2 failed, 1 passed`. The failing tests are `test_parse_seconds` and `test_parse_hours` (both `KeyError`); `test_parse_minutes_still_works` passes.

- [ ] **Step 7: Confirm git tree is clean**

Run from repo root: `git status --porcelain`
Expected: prints nothing, exits 0.

- [ ] **Step 8: No commit**

This task verifies behavior only — there is nothing to commit. Move on.

---

## Self-Review

**Spec coverage:** Every requirement and acceptance criterion from `.pi/specs/2026-04-28-bootstrap-fixture-design.md` is covered:

- Layout in `fixtures/parse_duration/` with the exact file set — Tasks 1, 3, 4, 5
- Bug located in `units.py` while parser is correct — Task 3
- Visible failure is `KeyError: 's'` traceback pointing at `parser.py` — Task 3 step 4
- Hidden test covers `"1h" == 3600` and `"10m" == 600` — Task 4
- Hidden test catches three under-fixes (under-completing UNITS, `UNITS.get(...,1)` in parser, hardcoded special-case) — verified by the test pair in Task 4 plus the canonical-fix verification in Task 8 (only the full units map turns both tests green)
- Case manifest with empty `edit_constraints` so harness defaults apply — Task 7
- README and docstring both document s/m/h units — Tasks 5 and 3
- Canonical fix `UNITS = {"s": 1, "m": 60, "h": 3600}` makes both test commands pass — Task 8 steps 2–4
- After revert, fixture remains in its failing-as-designed state — Task 8 steps 5–7
- No external network/credentials; only Python and pytest — entire plan
- Fixture repo path matches manifest's `fixture_repo` — Task 7

**Placeholder scan:** No `TBD`, `TODO`, `implement later`, or "add appropriate X" steps. The case manifest's `notes` field is intentionally informational case-author commentary, matching the spec's optional `notes` field.

**Type/name consistency:** `parse_duration` (function), `UNITS` (dict), `case_id = py-parse-duration-001`, file paths, and test command strings are identical across every task and the manifest. The manifest's `failing_test_command` and `hidden_test_command` strings match the commands the plan actually runs in Tasks 3 and 4.

**One deliberate spec extension:** the manifest stores the failure output as `failure_output_path` pointing at a sidecar file rather than a `failure_output` string inline. This is documented in Task 7's notes and noted in the architecture line at the top. It's a v1 manifest convention, not a change to what the spec requires the agent to receive.
