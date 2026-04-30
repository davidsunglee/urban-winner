# Plan: Pydantic-AI Framework Adapter

**Source:** TODO-9d4a65e1
**Spec:** .pi/specs/2026-04-29-pydantic-ai-adapter.md

## Goal

Replace the v1 stub at `frameworks/pydantic-ai/` with a working Pydantic-AI adapter that satisfies `shared/contract.md` and `shared/task-spec.md`. The adapter reads one JSON request envelope from stdin, runs a Pydantic-AI agent (model `claude-sonnet-4-6` via Anthropic) against the per-cell worktree at `input.repo_path`, edits the worktree in place to fix the failing test, and writes one contract-compliant JSON response envelope to stdout. Logs go to stderr; the adapter never mutates `.git/` or the harness-owned case venv. Structured output is produced via Pydantic-AI's native `output_type=AgentReport` (no `submit_report` tool). On success the harness end-to-end produces `runs/CURRENT/pydantic-ai/py-parse-duration-001/` with `meta.status == "ok"`, `scoring.schema_validity == true`, and `visible_test_outcome == hidden_test_outcome == "pass"`.

## Architecture summary

The harness invokes `frameworks/pydantic-ai/run.sh` with `cwd=frameworks/pydantic-ai/`. `run.sh` shells out to `uv run python adapter.py`, the single Python entry script implementing the adapter. The adapter:

1. **Parses request envelope** from stdin (`task_id`, `input.*`, `config.*`).
2. **Defines `AgentReport(BaseModel)`** with the contract's six output fields (`root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, `confidence`). No forbidden keys (`fixed`, `not_fixed`, `status`) are declared.
3. **Builds the agent** via `Agent(model='anthropic:<config.model>', output_type=AgentReport, instructions=<bugfix-task-prompt>)`. The model id is normalized by prepending `anthropic:` to `config.model`.
4. **Registers the hybrid tool surface**: filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`) are individual `@agent.tool_plain` functions whose path arguments are anchored at `input.repo_path` (rejected if they escape it); test execution and `git diff` go through one `run_shell(command: str)` tool that calls `subprocess.run(..., cwd=input.repo_path, env=os.environ.copy())`. The repo-path anchor is held in a module-level dict set in `main()` before `agent.run_sync` (the spec mandates `@agent.tool_plain`, which has no `RunContext` to thread state through).
5. **Invokes the agent** via `agent.run_sync(<single user message including failing_test_command, failure_output, edit_constraints>, usage_limits=UsageLimits(request_limit=2*max_steps))`. The 2x multiplier is the spec-suggested heuristic for the `request_limit` ↔ `max_steps` mapping. A best-effort watchdog timer derived from `config.timeout_s` (with a small safety margin) interrupts the run before the harness hard-kills the subprocess, so the adapter can still emit a contract-valid error envelope.
6. **Captures `result.all_messages()` and `result.usage()`** and converts them to the contract's `trace.steps` / `trace.tokens` / `trace.latency_ms` shape: each `ToolCallPart` becomes a `tool_call` step, each `TextPart` becomes a `model_call` step, and `ToolReturnPart`s fill in the matching `tool_call.result` by `tool_call_id`. Tokens come from `result.usage().input_tokens` / `output_tokens` (the field names used in pydantic-ai 1.x).
7. **Emits the final envelope** to stdout: `{task_id, output: AgentReport.model_dump(), trace, error: null}` and exits 0. Any failure (model error, `UsageLimitExceeded`, output-validation failure, tool exception, parse error, watchdog timeout) is caught at the top level: the adapter writes a contract-compliant envelope with `error.message` populated and exits non-zero. Stdout carries **only** the final envelope — Pydantic-AI internal output is redirected to stderr at startup.

`frameworks/pydantic-ai/` ships its own `pyproject.toml` and `uv.lock`; the manifest declares `setup: "uv sync --frozen"`. The harness-owned case venv (on `PATH`) is for test commands the agent runs via `run_shell`; the pydantic-ai adapter runs from its own `uv` environment.

## Tech stack

- **Python ≥ 3.11** (matches the harness baseline; pydantic-ai requires Python ≥ 3.10).
- **pydantic-ai** (the agent loop, structured output, tool registration). Version pin: `>=1.0,<2.0` — the latest stable 1.x at implementation time.
- **pydantic** v2 (transitively pulled by pydantic-ai; used for `AgentReport` and tool argument schemas).
- **Anthropic provider** — included in the full `pydantic-ai` package; API access via the `ANTHROPIC_API_KEY` env var.
- **uv** (per-framework dependency manager; isolated lockfile under `frameworks/pydantic-ai/`).
- External tools at runtime: `git` (for `git diff` invoked via `run_shell`), `pytest` (provided by the harness on `PATH` via the case venv).

---

## File Structure

### Created

- `frameworks/pydantic-ai/.python-version` (Create) — pins Python `3.12` (matches the rest of the repo; satisfies pydantic-ai's `>=3.10` requirement). One line, trailing newline.
- `frameworks/pydantic-ai/pyproject.toml` (Create) — declares Python ≥ 3.11 and the runtime dependency `pydantic-ai>=1.0,<2.0`. No `[build-system]` block (no installable package) and no dev/test deps (per spec non-goals).
- `frameworks/pydantic-ai/uv.lock` (Create) — generated by `uv lock` after writing `pyproject.toml`. Committed so `uv sync --frozen` is reproducible.
- `frameworks/pydantic-ai/adapter.py` (Create) — the entry script. Reads JSON from stdin, redirects logging to stderr, defines `AgentReport`, builds the Pydantic-AI agent, registers the hybrid tool surface, invokes it, converts the message history, writes one envelope to stdout, and handles all error paths.

### Modified

- `frameworks/pydantic-ai/run.sh` (Modify) — replaces the v1 stub. New body: `cd` into the script's directory, then `exec uv run --quiet python adapter.py "$@"`. Stdin/stdout pass through. Errors during `uv run` (missing deps, etc.) propagate to the harness as a non-zero exit + stderr.
- `frameworks/pydantic-ai/manifest.json` (Modify) — add `"setup": "uv sync --frozen"`, change `"env"` from `[]` to `["ANTHROPIC_API_KEY"]`. `"entry"` remains `"./run.sh"`. `"model"` remains `"claude-sonnet-4-6"`.
- `frameworks/pydantic-ai/README.md` (Modify) — replaces the TODO list with documentation: one-line library description, model choice (`claude-sonnet-4-6`, Anthropic-only for v1), `setup` command, required env (`ANTHROPIC_API_KEY`), the hybrid tool surface (filesystem `@agent.tool_plain` functions plus a `run_shell` shell tool), the `output_type=AgentReport` structured-output approach, and the Anthropic-only constraint.

---

## Tasks

### Task 1: Pin Python and write `pyproject.toml`

**Files:**
- Create: `frameworks/pydantic-ai/.python-version`
- Create: `frameworks/pydantic-ai/pyproject.toml`
- Create: `frameworks/pydantic-ai/uv.lock`

**Steps:**

- [ ] **Step 1: Pin Python interpreter** — write `frameworks/pydantic-ai/.python-version` containing exactly `3.12` (one line, trailing newline). Rationale: matches the interpreter used elsewhere in the repo (`evals/.venv/lib/python3.12/`) and is comfortably above pydantic-ai's `>=3.10` floor.

- [ ] **Step 2: Write the project metadata** — write `frameworks/pydantic-ai/pyproject.toml` with this exact content:
  ```toml
  [project]
  name = "pydantic-ai-adapter"
  version = "0.0.0"
  description = "Pydantic-AI framework adapter for the agent shootout"
  requires-python = ">=3.11"
  dependencies = [
    "pydantic-ai>=1.0,<2.0",
  ]
  ```
  Notes:
  - No `[build-system]` block — we are not building an installable package; `uv` only resolves and installs deps.
  - No dev/test dependencies (per spec non-goals: no adapter-level unit tests).
  - `pydantic-ai` (full) is used rather than `pydantic-ai-slim[anthropic]` for simpler resolution; the full package transitively includes the Anthropic provider plus its dependency on the `anthropic` Python SDK.
  - Pinning `<2.0` prevents silent breakage on a future major bump.

- [ ] **Step 3: Resolve and lock dependencies** — run `cd frameworks/pydantic-ai && uv lock`. Expected: writes `frameworks/pydantic-ai/uv.lock`, exits 0, prints something like `Resolved N packages`. If the resolver fails because `pydantic-ai`'s published majors have moved past `1.x`, **adjust the upper bound in `pyproject.toml` to the latest compatible major** rather than removing the upper bound, then re-run `uv lock`.

- [ ] **Step 4: Verify the lockfile resolves the expected libraries** — run `cd frameworks/pydantic-ai && uv sync --frozen`. Expected: exits 0, populates `.venv/`. Then run:
  ```sh
  cd frameworks/pydantic-ai && uv run --quiet python -c "
  from pydantic_ai import Agent, UsageLimits
  from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, TextPart
  from pydantic import BaseModel
  print('ok')
  "
  ```
  Expected: prints `ok` and exits 0. Imports failing here indicate a mismatched specifier or a renamed module — see Step 3.

- [ ] **Step 5: Stage the artifacts** — stage `frameworks/pydantic-ai/.python-version`, `frameworks/pydantic-ai/pyproject.toml`, and `frameworks/pydantic-ai/uv.lock`. Do NOT stage `.venv/` (gitignored at repo root).

**Acceptance criteria:**

- `frameworks/pydantic-ai/pyproject.toml` declares `requires-python = ">=3.11"` and lists `pydantic-ai` as a dependency with a bounded upper version.
  Verify: `grep -E "^(requires-python|.*pydantic-ai>=)" frameworks/pydantic-ai/pyproject.toml` returns at least two matching lines (one for `requires-python`, one for the `pydantic-ai>=` dependency).
- `frameworks/pydantic-ai/uv.lock` exists and is non-empty.
  Verify: run `test -s frameworks/pydantic-ai/uv.lock` and confirm exit code 0.
- `uv sync --frozen` completes successfully against the lockfile, and the pydantic-ai `Agent` symbol plus the message classes import cleanly.
  Verify: run `cd frameworks/pydantic-ai && uv sync --frozen && uv run --quiet python -c "from pydantic_ai import Agent, UsageLimits; from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, TextPart; print('ok')"` and confirm stdout contains `ok` and exit code 0.

**Model recommendation:** standard

---

### Task 2: Implement `adapter.py`

**Files:**
- Create: `frameworks/pydantic-ai/adapter.py`

**Steps:**

- [ ] **Step 1: Write the script header and imports** — start the file with:
  ```python
  #!/usr/bin/env python3
  from __future__ import annotations

  import json
  import logging
  import os
  import re
  import signal
  import subprocess
  import sys
  import threading
  import time
  import traceback
  from pathlib import Path
  from typing import Any

  from pydantic import BaseModel, Field
  from pydantic_ai import Agent, UsageLimits
  from pydantic_ai.messages import (
      ModelRequest,
      ModelResponse,
      TextPart,
      ToolCallPart,
      ToolReturnPart,
  )
  ```
  No additional `from pydantic_ai...` imports are needed; everything else (the model registry, the Anthropic provider) is reached transitively via the model-id string. `signal` and `threading` are used by the watchdog in Step 14.

- [ ] **Step 2: Redirect logging to stderr** — add at module level after imports:
  ```python
  logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
  ```
  This guarantees Python `logging` output (including pydantic-ai's internal logger) never bleeds into stdout. The contract requires stdout to carry only the final envelope JSON.

- [ ] **Step 3: Define caps and module-level state** — add:
  ```python
  _FILE_READ_CAP_BYTES = 256 * 1024  # 256 KiB per read
  _GREP_OUTPUT_CAP_BYTES = 256 * 1024
  _LIST_DIR_ENTRIES_CAP = 500
  _GLOB_RESULTS_CAP = 500
  _SHELL_TIMEOUT_S = 60
  _SHELL_OUTPUT_CAP_BYTES = 1024 * 1024  # 1 MiB per call
  _WATCHDOG_SAFETY_MARGIN_S = 5.0  # leave time to emit an envelope before the harness SIGKILLs us

  _STATE: dict[str, Path] = {}  # populated in main(); holds {"repo_path": <Path>}
  ```
  Notes:
  - The 1 MiB shell cap matches the harness's per-test stdout cap and prevents context blowup.
  - The 60 s shell timeout is a per-call guardrail; the outer harness `timeout_s` still applies.
  - `_STATE` carries `repo_path` from `main()` to the `@agent.tool_plain` functions (which cannot accept `RunContext` per the spec's mandate to use `tool_plain`).
  - `_WATCHDOG_SAFETY_MARGIN_S` is the headroom we leave between our internal deadline and the harness `timeout_s` so we still get to write a contract-valid envelope on timeout.

- [ ] **Step 4: Define the `AgentReport` Pydantic model** — add:
  ```python
  class _TestRun(BaseModel):
      command: str
      exit_code: int
      summary: str

  class AgentReport(BaseModel):
      """Final structured report. Field names match shared/task-spec.md's `output` schema exactly.

      Forbidden top-level keys (`fixed`, `not_fixed`, `status`) are intentionally absent.
      """
      root_cause: str
      summary: str
      changed_files: list[str]
      tests_run: list[_TestRun]
      evidence: str
      confidence: float = Field(ge=0.0, le=1.0)
  ```
  This exactly mirrors `shared/task-spec.md`'s `output` schema and `evals/evals/schemas.py:11`'s `OUTPUT_REQUIRED`. The `confidence` constraint matches the contract's `[0.0, 1.0]` range.

- [ ] **Step 5: Define `_read_request`** — add:
  ```python
  def _read_request() -> dict[str, Any]:
      raw = sys.stdin.read()
      if not raw.strip():
          raise RuntimeError("empty stdin: expected one JSON request envelope")
      try:
          obj = json.loads(raw)
      except json.JSONDecodeError as exc:
          raise RuntimeError(f"stdin is not valid JSON: {exc}") from exc
      if not isinstance(obj, dict):
          raise RuntimeError("request envelope must be a JSON object")
      for key in ("task_id", "input", "config"):
          if key not in obj:
              raise RuntimeError(f"request envelope missing required key: {key}")
      inp = obj["input"]
      for key in ("repo_path", "failing_test_command", "failure_output", "edit_constraints"):
          if key not in inp:
              raise RuntimeError(f"input missing required key: {key}")
      cfg = obj["config"]
      for key in ("model", "max_steps", "timeout_s"):
          if key not in cfg:
              raise RuntimeError(f"config missing required key: {key}")
      return obj
  ```

- [ ] **Step 6: Define `_resolve_within` for path containment** — add:
  ```python
  def _resolve_within(repo_root: Path, user_path: str) -> Path | None:
      """Resolve user_path relative to repo_root; return None if it escapes repo_root."""
      if not user_path:
          return None
      candidate = Path(user_path)
      if not candidate.is_absolute():
          candidate = repo_root / candidate
      try:
          resolved = candidate.resolve(strict=False)
          repo_resolved = repo_root.resolve(strict=False)
          resolved.relative_to(repo_resolved)
      except (OSError, RuntimeError, ValueError):
          return None
      return resolved
  ```
  Notes:
  - `strict=False` on `resolve` so write-target paths that don't yet exist still resolve.
  - On macOS, both `repo_root` and `candidate` get the same `/private/var/...` realpath, so the `relative_to` check is sound.
  - Returns `None` (the caller renders an error string back to the model) rather than raising — per pydantic-ai docs, raising inside `@agent.tool_plain` triggers a `ModelRetry`, which costs tokens and conversation turns.

- [ ] **Step 7: Define the system prompt** — add a module-level constant:
  ```python
  SYSTEM_PROMPT = """You are a software engineer fixing a failing test in a Python repository.

  Your tools:
  - Filesystem (rooted at the repo path; paths outside are rejected): list_dir, glob, grep, read_file, write_file, edit_file.
  - Shell: run_shell(command) (runs with cwd pinned to the repo root and the harness-prepared PATH; use for pytest, git diff, ls, etc.).

  Your final response will be validated as an AgentReport with these fields: root_cause, summary, changed_files, tests_run, evidence, confidence.

  Workflow you must follow:
  1. Read the failing test command and captured failure output. Form a hypothesis about the root cause.
  2. Inspect the repository (list_dir, read_file, grep) before making any edits. Read the file the stack trace points at AND any files it imports from.
  3. Apply a minimal in-place edit that addresses the root cause. Do not edit files matching the disallowed_paths globs you were given. Do not edit tests, fixtures, lockfiles, or .git/ contents.
  4. Re-run the failing test command via run_shell to confirm the fix.
  5. Return the final AgentReport with root_cause, summary, changed_files, tests_run, evidence, confidence.

  Hard constraints:
  - Do not commit, reset, or otherwise modify .git/ — the harness derives the diff itself.
  - Do not run pip install, uv sync, uv add, or any command that would mutate the Python environment. Tests already have all the dependencies they need on PATH.
  - Keep the change set small (target one or two files). Prefer fixing the underlying data/logic over hardcoding the failing input.
  """
  ```

- [ ] **Step 8: Construct the agent and register tools** — add at module level (so the decorators register against the module-level `agent`):
  ```python
  # The agent object is created at module load with a placeholder model id and
  # then re-bound to the real model in main() once we know config.model. Tool
  # registration happens against this module-level agent.
  _AGENT: Agent[None, AgentReport] | None = None

  def _make_agent(model_id: str) -> Agent[None, AgentReport]:
      return Agent(
          model=model_id,
          output_type=AgentReport,
          instructions=SYSTEM_PROMPT,
      )
  ```
  Then, after this, define every tool function. Because `@agent.tool_plain` requires an existing `Agent` instance, we cannot decorate at module load before we know the model. Instead, define the tools as plain functions and register them with the agent inside `_build_agent`. Replace the placeholder above with:
  ```python
  def _build_agent(model_id: str) -> Agent[None, AgentReport]:
      agent = Agent(
          model=model_id,
          output_type=AgentReport,
          instructions=SYSTEM_PROMPT,
      )
      agent.tool_plain(read_file)
      agent.tool_plain(write_file)
      agent.tool_plain(edit_file)
      agent.tool_plain(list_dir)
      agent.tool_plain(glob)
      agent.tool_plain(grep)
      agent.tool_plain(run_shell)
      return agent
  ```
  This uses the imperative `agent.tool_plain(fn)` form (the decorator and the imperative method are equivalent — `@agent.tool_plain` is sugar for `agent.tool_plain(fn)`). Pydantic-AI will introspect each function's signature and Pydantic-validate args at call time.

- [ ] **Step 9: Define filesystem tools** — add (each one above `_build_agent`, since `_build_agent` references them):
  ```python
  def read_file(path: str) -> str:
      """Read a UTF-8 text file from the repository.

      Args:
          path: Repo-relative path (or absolute path inside the repo). Paths outside the repo are rejected.

      Returns the file contents (truncated to 256 KiB) or an error string starting with `error:`.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      target = _resolve_within(repo, path)
      if target is None:
          return f"error: path '{path}' is outside the repository"
      try:
          data = target.read_bytes()
      except FileNotFoundError:
          return f"error: file not found: {path}"
      except IsADirectoryError:
          return f"error: path is a directory: {path}"
      except OSError as exc:
          return f"error: {exc}"
      truncated = len(data) > _FILE_READ_CAP_BYTES
      data = data[:_FILE_READ_CAP_BYTES]
      tail = "\n[truncated]" if truncated else ""
      return data.decode("utf-8", errors="replace") + tail


  def write_file(path: str, content: str) -> str:
      """Overwrite a file in the repository with the given UTF-8 content.

      Creates parent directories as needed. Paths outside the repo are rejected.
      Returns "ok: wrote N bytes" on success or an error string starting with `error:`.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      target = _resolve_within(repo, path)
      if target is None:
          return f"error: path '{path}' is outside the repository"
      try:
          target.parent.mkdir(parents=True, exist_ok=True)
          data = content.encode("utf-8")
          target.write_bytes(data)
      except OSError as exc:
          return f"error: {exc}"
      return f"ok: wrote {len(data)} bytes to {path}"


  def edit_file(path: str, old: str, new: str) -> str:
      """Replace the first occurrence of `old` with `new` in the file at `path`.

      Returns "ok: replaced 1 occurrence" on success, "error: no match" if `old` is not present,
      or an error string starting with `error:` for IO/path issues. Paths outside the repo are rejected.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      target = _resolve_within(repo, path)
      if target is None:
          return f"error: path '{path}' is outside the repository"
      try:
          original = target.read_text(encoding="utf-8")
      except FileNotFoundError:
          return f"error: file not found: {path}"
      except OSError as exc:
          return f"error: {exc}"
      if old not in original:
          return "error: no match for `old` in file"
      replaced = original.replace(old, new, 1)
      try:
          target.write_text(replaced, encoding="utf-8")
      except OSError as exc:
          return f"error: {exc}"
      return "ok: replaced 1 occurrence"


  def list_dir(path: str = ".") -> str:
      """List entries in a directory under the repo root. Returns a newline-separated listing
      with a trailing slash on directories. Paths outside the repo are rejected.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      target = _resolve_within(repo, path)
      if target is None:
          return f"error: path '{path}' is outside the repository"
      if not target.exists():
          return f"error: not found: {path}"
      if not target.is_dir():
          return f"error: not a directory: {path}"
      entries: list[str] = []
      try:
          for child in sorted(target.iterdir(), key=lambda p: p.name):
              suffix = "/" if child.is_dir() else ""
              entries.append(child.name + suffix)
              if len(entries) >= _LIST_DIR_ENTRIES_CAP:
                  entries.append(f"[truncated at {_LIST_DIR_ENTRIES_CAP} entries]")
                  break
      except OSError as exc:
          return f"error: {exc}"
      return "\n".join(entries)


  def glob(pattern: str) -> str:
      """Glob a pattern relative to the repo root (e.g. `**/*.py`). Returns matching repo-relative paths,
      one per line. Paths that resolve outside the repo are filtered out. Capped at 500 results.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      try:
          repo_resolved = repo.resolve(strict=False)
          matches = []
          for match in repo_resolved.glob(pattern):
              try:
                  rel = match.relative_to(repo_resolved)
              except ValueError:
                  continue
              matches.append(str(rel))
              if len(matches) >= _GLOB_RESULTS_CAP:
                  matches.append(f"[truncated at {_GLOB_RESULTS_CAP} matches]")
                  break
      except (OSError, ValueError) as exc:
          return f"error: {exc}"
      return "\n".join(matches) if matches else "(no matches)"


  def grep(pattern: str, path: str = ".") -> str:
      """Search for a regex pattern in files under `path` (defaults to repo root).
      Returns at most 256 KiB of `<relpath>:<line_no>:<line>` matches. Paths outside the repo are rejected.
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      target = _resolve_within(repo, path)
      if target is None:
          return f"error: path '{path}' is outside the repository"
      try:
          regex = re.compile(pattern)
      except re.error as exc:
          return f"error: invalid regex: {exc}"
      hits: list[str] = []
      out_bytes = 0
      paths_to_scan: list[Path]
      if target.is_file():
          paths_to_scan = [target]
      elif target.is_dir():
          paths_to_scan = [
              p for p in target.rglob("*")
              if p.is_file() and ".git" not in p.parts
          ]
      else:
          return f"error: not found: {path}"
      repo_resolved = repo.resolve(strict=False)
      for p in paths_to_scan:
          try:
              with p.open("r", encoding="utf-8", errors="replace") as fh:
                  for lineno, line in enumerate(fh, start=1):
                      if regex.search(line):
                          try:
                              rel = p.resolve().relative_to(repo_resolved)
                          except ValueError:
                              continue
                          entry = f"{rel}:{lineno}:{line.rstrip()}"
                          if out_bytes + len(entry) + 1 > _GREP_OUTPUT_CAP_BYTES:
                              hits.append("[truncated]")
                              return "\n".join(hits)
                          hits.append(entry)
                          out_bytes += len(entry) + 1
          except OSError:
              continue
      return "\n".join(hits) if hits else "(no matches)"
  ```

- [ ] **Step 10: Define the `run_shell` tool** — add (above `_build_agent`):
  ```python
  def run_shell(command: str) -> str:
      """Run a shell command in the repository root and return its stdout+stderr (capped).

      Use for pytest, git diff, ls, etc. The cwd is pinned to the repo root and the env
      is propagated from the parent process (which the harness has populated with the
      case venv on PATH).
      """
      repo = _STATE.get("repo_path")
      if repo is None:
          return "error: repo_path not initialized"
      try:
          completed = subprocess.run(
              ["/bin/sh", "-c", command],
              cwd=str(repo),
              env=os.environ.copy(),
              capture_output=True,
              timeout=_SHELL_TIMEOUT_S,
          )
      except subprocess.TimeoutExpired:
          return f"error: command timed out after {_SHELL_TIMEOUT_S}s"
      out = (completed.stdout or b"") + (completed.stderr or b"")
      truncated = len(out) > _SHELL_OUTPUT_CAP_BYTES
      out = out[:_SHELL_OUTPUT_CAP_BYTES]
      tail = "\n[output truncated]" if truncated else ""
      return f"exit_code: {completed.returncode}\n{out.decode('utf-8', errors='replace')}{tail}"
  ```

- [ ] **Step 11: Build the user message** — add:
  ```python
  def _user_message(input_obj: dict[str, Any]) -> str:
      ec = input_obj["edit_constraints"]
      disallowed = ec.get("disallowed_paths", [])
      allowed = ec.get("allowed_paths")
      max_files = ec.get("max_changed_files")
      lines = [
          f"Repo path (cwd for run_shell, root for filesystem tools): {input_obj['repo_path']}",
          f"Failing test command: {input_obj['failing_test_command']}",
          "",
          "Captured failure output:",
          "```",
          input_obj["failure_output"].rstrip(),
          "```",
          "",
          "Edit constraints:",
          f"- disallowed_paths (gitignore-style globs you must NOT modify): {disallowed}",
      ]
      if allowed is not None:
          lines.append(f"- allowed_paths (you may ONLY modify these): {allowed}")
      if max_files is not None:
          lines.append(f"- max_changed_files: {max_files}")
      lines.append("")
      lines.append("Diagnose, fix, and produce your final AgentReport.")
      return "\n".join(lines)
  ```

- [ ] **Step 12: Convert message history to the contract trace** — add:
  ```python
  def _stringify_content(content: Any) -> str:
      if isinstance(content, str):
          return content
      if isinstance(content, list):
          parts: list[str] = []
          for item in content:
              if isinstance(item, dict) and "text" in item:
                  parts.append(str(item["text"]))
              else:
                  parts.append(str(item))
          return "".join(parts)
      return str(content)


  def _messages_to_trace(messages: list, latency_ms: int, total_usage: Any) -> dict[str, Any]:
      steps: list[dict[str, Any]] = []
      tool_call_index: dict[str, int] = {}  # tool_call_id -> index into steps

      for msg in messages:
          parts = list(getattr(msg, "parts", []) or [])
          if isinstance(msg, ModelResponse):
              for part in parts:
                  if isinstance(part, ToolCallPart):
                      args = getattr(part, "args", None)
                      if isinstance(args, str):
                          try:
                              args_dict = json.loads(args)
                          except (json.JSONDecodeError, ValueError):
                              args_dict = {"_raw": args}
                      elif isinstance(args, dict):
                          args_dict = args
                      else:
                          args_dict = {}
                      step = {
                          "kind": "tool_call",
                          "name": getattr(part, "tool_name", "") or "",
                          "args": args_dict,
                          "result": {},
                      }
                      steps.append(step)
                      call_id = getattr(part, "tool_call_id", None)
                      if call_id:
                          tool_call_index[call_id] = len(steps) - 1
                  elif isinstance(part, TextPart):
                      steps.append({
                          "kind": "model_call",
                          "name": "text",
                          "args": {},
                          "result": {"content": _stringify_content(getattr(part, "content", ""))},
                      })
          elif isinstance(msg, ModelRequest):
              for part in parts:
                  if isinstance(part, ToolReturnPart):
                      call_id = getattr(part, "tool_call_id", None)
                      content = _stringify_content(getattr(part, "content", ""))
                      if call_id and call_id in tool_call_index:
                          idx = tool_call_index[call_id]
                          steps[idx]["result"] = {"content": content}
                      else:
                          steps.append({
                              "kind": "tool_call",
                              "name": getattr(part, "tool_name", "") or "",
                              "args": {},
                              "result": {"content": content},
                          })
                  # UserPromptPart, RetryPromptPart, SystemPromptPart are not surfaced as steps.

      input_tokens = 0
      output_tokens = 0
      if total_usage is not None:
          input_tokens = int(getattr(total_usage, "input_tokens", 0) or 0)
          output_tokens = int(getattr(total_usage, "output_tokens", 0) or 0)

      return {
          "steps": steps,
          "tokens": {"input": input_tokens, "output": output_tokens},
          "latency_ms": int(latency_ms),
      }
  ```
  Notes:
  - Pydantic-AI 1.x exposes `RunUsage(input_tokens, output_tokens, requests)` from `result.usage()` (per the Context7 docs at `/pydantic/pydantic-ai`); we read those names directly. If the pinned version exposes them under different names, the `getattr(..., 0)` fallback returns `0` and the trace stays contract-valid (0/0 ints).
  - The contract envelope schema (`evals/evals/schemas.py:177-180`) requires `tokens.input` and `tokens.output` to be ints, so we always emit them rather than omitting under the spec's "tokens are omitted" phrasing — the harness-side fallback applies only when the trace is invalid, not when it reports 0/0.
  - `ToolCallPart.args` may be either a JSON string (Anthropic's tool calls historically) or a dict; we handle both.

- [ ] **Step 13: Define the envelope writer** — add:
  ```python
  def _emit_envelope(envelope: dict[str, Any]) -> None:
      sys.stdout.write(json.dumps(envelope, ensure_ascii=False))
      sys.stdout.write("\n")
      sys.stdout.flush()
  ```
  Single trailing newline; this is the only place anything is written to stdout in the script.

- [ ] **Step 14: Define `main()` with a best-effort `config.timeout_s` watchdog** — add:
  ```python
  def _normalize_model(model: str) -> str:
      """Map a manifest-style model id (`claude-sonnet-4-6`) to pydantic-ai's
      provider-prefixed form (`anthropic:claude-sonnet-4-6`). If the caller
      already supplied a colon-prefixed id, pass through.
      """
      if ":" in model:
          return model
      return f"anthropic:{model}"


  def _start_watchdog(timeout_s: float) -> threading.Timer:
      """Best-effort internal deadline. Fires SIGINT into our own PID a few seconds
      before `config.timeout_s` so that `agent.run_sync` is interrupted (raising
      KeyboardInterrupt, which `except BaseException` catches) and we still have
      headroom to emit a contract-valid error envelope before the harness hard-kills us.
      """
      deadline = max(1.0, float(timeout_s) - _WATCHDOG_SAFETY_MARGIN_S)

      def _interrupt() -> None:
          try:
              os.kill(os.getpid(), signal.SIGINT)
          except Exception:
              pass

      timer = threading.Timer(deadline, _interrupt)
      timer.daemon = True
      timer.start()
      return timer


  def main() -> int:
      t0 = time.monotonic()
      task_id = "unknown"
      watchdog: threading.Timer | None = None
      try:
          request = _read_request()
          task_id = request["task_id"]
          cfg = request["config"]
          input_obj = request["input"]
          model_name = _normalize_model(str(cfg["model"]))
          max_steps = int(cfg["max_steps"])
          timeout_s = float(cfg["timeout_s"])
          repo_path = Path(input_obj["repo_path"])

          if not repo_path.is_dir():
              raise RuntimeError(f"input.repo_path does not exist or is not a directory: {repo_path}")

          _STATE["repo_path"] = repo_path

          agent = _build_agent(model_name)
          user_text = _user_message(input_obj)
          request_limit = max(2, max_steps * 2)

          watchdog = _start_watchdog(timeout_s)
          try:
              result = agent.run_sync(
                  user_text,
                  usage_limits=UsageLimits(request_limit=request_limit),
              )
          finally:
              watchdog.cancel()
              watchdog = None

          latency_ms = int((time.monotonic() - t0) * 1000)
          messages = result.all_messages()
          usage = result.usage()
          trace = _messages_to_trace(messages, latency_ms, usage)

          output_obj = result.output.model_dump()
          envelope = {
              "task_id": task_id,
              "output": output_obj,
              "trace": trace,
              "error": None,
          }
          _emit_envelope(envelope)
          return 0
      except BaseException as exc:
          if watchdog is not None:
              try:
                  watchdog.cancel()
              except Exception:
                  pass
          latency_ms = int((time.monotonic() - t0) * 1000)
          tb = traceback.format_exc()
          sys.stderr.write(tb)
          sys.stderr.flush()
          envelope = {
              "task_id": task_id,
              "output": None,
              "trace": {"steps": [], "tokens": {"input": 0, "output": 0}, "latency_ms": latency_ms},
              "error": {"message": f"{type(exc).__name__}: {exc}"},
          }
          try:
              _emit_envelope(envelope)
          except Exception:
              pass
          return 1


  if __name__ == "__main__":
      sys.exit(main())
  ```
  Notes:
  - The watchdog uses `threading.Timer` + `os.kill(os.getpid(), SIGINT)` rather than `signal.alarm` so it works even if `agent.run_sync` is itself running on a non-main signal-aware thread; `SIGINT` propagates to the main thread as a `KeyboardInterrupt`, which `except BaseException` catches.
  - The deadline is `config.timeout_s - 5s` (floored at 1s). The 5s margin gives us time to walk back through `_messages_to_trace` (cheap — local), serialize the envelope, and flush stdout before the harness's `terminate_process_tree` lands. If the harness later tightens its timeout enforcement, only `_WATCHDOG_SAFETY_MARGIN_S` needs adjustment.
  - This is a best-effort safeguard: the harness `timeout_s` is still the hard ceiling. We are not promising to always interrupt cleanly; we are promising that *when we can*, we emit an envelope first.
  - `BaseException` is intentional: it catches `KeyboardInterrupt` (from the watchdog), `SystemExit`, `UsageLimitExceeded` (raised when `request_limit` is exceeded), `UnexpectedModelBehavior` (raised when output validation exhausts retries), and any tool-tier exception that bubbles up. Any failure produces a contract-valid envelope; the harness records `error_reason="nonzero_exit"` plus the envelope.
  - `task_id="unknown"` is the placeholder for failures before stdin parsing completes. The contract envelope schema requires `task_id` to be a non-empty string; `"unknown"` satisfies that.
  - `request_limit = max(2, max_steps * 2)` is the spec-recommended `2x max_steps` heuristic with a sane floor; the implementer can tune the multiplier upward if Task 6 shows runs hitting the limit before completing.

- [ ] **Step 15: Compile-check the script** — run `cd frameworks/pydantic-ai && uv run --quiet python -m py_compile adapter.py`. Expected: exits 0 with no output. If syntax errors are reported, fix them before proceeding.

- [ ] **Step 16: Smoke-test that the symbols import** — run `cd frameworks/pydantic-ai && uv run --quiet python -c "import adapter; assert callable(adapter.main); assert hasattr(adapter, 'AgentReport'); assert hasattr(adapter, 'run_shell'); print('ok')"`. Expected: prints `ok`.

**Acceptance criteria:**

- `adapter.py` exists, is well-formed Python, and exposes `main()`, `AgentReport`, the filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`), and `run_shell`.
  Verify: run `cd frameworks/pydantic-ai && uv run --quiet python -c "import adapter; assert callable(adapter.main); assert hasattr(adapter, 'AgentReport'); [getattr(adapter, n) for n in ('read_file','write_file','edit_file','list_dir','glob','grep','run_shell')]; print('ok')"` and confirm stdout contains `ok` and exit code 0.
- The script has no `print(...)` or `sys.stdout.write(...)` calls outside the body of `_emit_envelope` (logging goes to stderr).
  Verify: run `cd frameworks/pydantic-ai && uv run --quiet python -c "
  import ast
  src = open('adapter.py').read()
  tree = ast.parse(src)
  emit_range = None
  for node in ast.walk(tree):
      if isinstance(node, ast.FunctionDef) and node.name == '_emit_envelope':
          emit_range = (node.lineno, node.end_lineno)
          break
  assert emit_range is not None, '_emit_envelope not found'
  bad = []
  for node in ast.walk(tree):
      if isinstance(node, ast.Call):
          f = node.func
          name = None
          if isinstance(f, ast.Name) and f.id == 'print':
              name = 'print'
          elif (isinstance(f, ast.Attribute) and f.attr == 'write'
                and isinstance(f.value, ast.Attribute) and f.value.attr == 'stdout'
                and isinstance(f.value.value, ast.Name) and f.value.value.id == 'sys'):
              name = 'sys.stdout.write'
          if name and not (emit_range[0] <= node.lineno <= emit_range[1]):
              bad.append((name, node.lineno))
  assert not bad, f'forbidden stdout writes outside _emit_envelope: {bad}'
  print('ok')
  "` and confirm stdout contains `ok` and exit code 0.
- `AgentReport` declares exactly the six contract-required output fields and none of the forbidden keys (`fixed`, `not_fixed`, `status`).
  Verify: run `cd frameworks/pydantic-ai && uv run --quiet python -c "from adapter import AgentReport; fields=set(AgentReport.model_fields.keys()); assert fields == {'root_cause','summary','changed_files','tests_run','evidence','confidence'}, fields; print('ok')"` and confirm stdout contains `ok`.
- `AgentReport.confidence` is constrained to `[0.0, 1.0]` via Pydantic.
  Verify: run `cd frameworks/pydantic-ai && uv run --quiet python -c "from adapter import AgentReport; from pydantic import ValidationError;
  ok = AgentReport(root_cause='', summary='', changed_files=[], tests_run=[], evidence='', confidence=0.5)
  try:
      AgentReport(root_cause='', summary='', changed_files=[], tests_run=[], evidence='', confidence=1.5)
      raise SystemExit('expected validation error')
  except ValidationError: pass
  print('ok')"` and confirm stdout contains `ok`.
- All required envelope keys are populated in the success path: `task_id`, `output` (dict), `trace.steps`, `trace.tokens.{input,output}`, `trace.latency_ms`, `error: null`.
  Verify: open `frameworks/pydantic-ai/adapter.py` and confirm the success-path envelope literal in `main()` includes the keys `task_id`, `output`, `trace`, and `error`, and that `_messages_to_trace` returns a dict with `steps`, `tokens`, and `latency_ms` keys.
- The error path emits a contract-valid envelope (with `error.message` set, `output: None`, and a default trace) and exits non-zero.
  Verify: open `frameworks/pydantic-ai/adapter.py` and confirm the `except BaseException` block builds an envelope with `error={"message": ...}`, `output: None`, and `trace={"steps": [], "tokens": {"input": 0, "output": 0}, "latency_ms": ...}`, and that `main()` returns 1 from this branch.
- Filesystem tool functions reject paths outside the repo via `_resolve_within`.
  Verify: run `cd frameworks/pydantic-ai && uv run --quiet python -c "
  import adapter
  from pathlib import Path
  import tempfile, os
  with tempfile.TemporaryDirectory() as d:
      adapter._STATE['repo_path'] = Path(d)
      assert 'outside the repository' in adapter.read_file('../../../etc/passwd'), 'expected escape rejection'
      assert 'outside the repository' in adapter.write_file('/etc/evil', 'x'), 'expected absolute escape rejection'
      print('ok')"` and confirm stdout contains `ok`.
- `run_shell` passes `cwd=<repo_path>` and `env=os.environ.copy()` to `subprocess.run`.
  Verify: open `frameworks/pydantic-ai/adapter.py` and confirm the `run_shell` function calls `subprocess.run(...)` with both `cwd=str(repo)` (where `repo` is `_STATE["repo_path"]`) and `env=os.environ.copy()`.
- `main()` arms a `config.timeout_s`-derived watchdog around `agent.run_sync` and cancels it on both the success and error paths.
  Verify: open `frameworks/pydantic-ai/adapter.py` and confirm (a) `_start_watchdog` exists and computes its deadline as `max(1.0, float(timeout_s) - _WATCHDOG_SAFETY_MARGIN_S)`, (b) `main()` calls `_start_watchdog(timeout_s)` before invoking `agent.run_sync`, (c) the success path cancels the timer in a `finally` block immediately around `agent.run_sync`, and (d) the `except BaseException` block also cancels the timer if it is still active.

**Model recommendation:** capable

---

### Task 3: Update `manifest.json`

**Files:**
- Modify: `frameworks/pydantic-ai/manifest.json`

**Steps:**

- [ ] **Step 1: Replace the file contents** — overwrite `frameworks/pydantic-ai/manifest.json` with:
  ```json
  {
    "entry": "./run.sh",
    "setup": "uv sync --frozen",
    "env": ["ANTHROPIC_API_KEY"],
    "model": "claude-sonnet-4-6"
  }
  ```
  Constraints (mandatory; see `evals/evals/schemas.py:23-60`):
  - Single JSON object. No comments (JSON does not support them).
  - `entry` non-empty string. We use `./run.sh` (relative to `frameworks/pydantic-ai/`, which the harness sets as `cwd`).
  - `setup` is optional but we declare it. The harness runs it via `cd frameworks/pydantic-ai/ && uv sync --frozen` during `eval-prepare`; cached `.ok`/`.fail` sentinels are keyed off `pyproject.toml` and `uv.lock` content, so editing those triggers a re-run.
  - `env` is a list of strings. We declare `["ANTHROPIC_API_KEY"]` so `build_agent_env` (in `evals/evals/env.py`) forwards that key into the adapter subprocess; without this, pydantic-ai's Anthropic provider would fail to authenticate.
  - `model` non-empty string. `claude-sonnet-4-6` is the spec default.
  - Unknown keys are rejected by the validator. Do not add fields beyond `entry`, `setup`, `env`, `model`.

- [ ] **Step 2: Validate the schema** — run `cd evals && uv run python -c "import json; from evals.schemas import validate_framework_manifest; print(validate_framework_manifest(json.loads(open('../frameworks/pydantic-ai/manifest.json').read())))"`. Expected: prints `[]` (empty error list).

**Acceptance criteria:**

- `manifest.json` declares `entry`, `setup`, `env: ["ANTHROPIC_API_KEY"]`, and `model: "claude-sonnet-4-6"` and validates against `validate_framework_manifest`.
  Verify: run `cd evals && uv run python -c "import json; from evals.schemas import validate_framework_manifest; errs = validate_framework_manifest(json.loads(open('../frameworks/pydantic-ai/manifest.json').read())); print('FAIL' if errs else 'OK', errs)"` and confirm stdout begins with `OK`.
- The file is valid JSON and contains exactly the four expected top-level keys.
  Verify: run `python3 -c "import json; m = json.load(open('frameworks/pydantic-ai/manifest.json')); assert set(m.keys()) == {'entry','setup','env','model'}; assert m['env'] == ['ANTHROPIC_API_KEY']; assert m['model'] == 'claude-sonnet-4-6'; print('ok')"` and confirm stdout contains `ok`.

**Model recommendation:** cheap

---

### Task 4: Replace `run.sh`

**Files:**
- Modify: `frameworks/pydantic-ai/run.sh`

**Steps:**

- [ ] **Step 1: Overwrite the stub** — replace `frameworks/pydantic-ai/run.sh` with:
  ```sh
  #!/bin/sh
  set -eu
  cd "$(dirname "$0")"
  exec uv run --quiet python adapter.py "$@"
  ```
  Notes:
  - `set -eu` — fail on unset vars and on any preceding command failure (the only pre-`exec` command is `cd`, which fails loudly if the directory does not exist).
  - `cd "$(dirname "$0")"` — pin cwd to the script's directory before invoking `uv run`. The harness already sets `cwd=frameworks/pydantic-ai/`, but this makes the script robust to direct invocation during local debugging.
  - `exec uv run --quiet python adapter.py` — replaces the shell with the Python process so signals (SIGTERM/SIGKILL) from the harness's `terminate_process_tree` reach the actual interpreter, not an intermediate shell. `--quiet` suppresses uv's resolution chatter on stdout (uv prints info to stderr; the harness contract requires stdout to carry only the envelope).
  - `"$@"` — pass through any args (none expected, but harmless).

- [ ] **Step 2: Make it executable** — run `chmod +x frameworks/pydantic-ai/run.sh`. Verify with `ls -l frameworks/pydantic-ai/run.sh` showing the `x` bit on owner.

- [ ] **Step 3: Smoke-test the entry script offline** — run `cd frameworks/pydantic-ai && (echo '{}' | ./run.sh > /tmp/pyd-stdout 2> /tmp/pyd-stderr; echo "exit=$?")`. Expected: stderr contains a Python traceback (because the request envelope is missing required fields), stdout contains a single JSON line with `error.message` populated and `task_id == "unknown"`, and the printed exit code is `1`. This validates that the error path produces a contract-valid envelope without needing an Anthropic API key.

**Acceptance criteria:**

- `run.sh` is executable (`+x` for owner) and points at `adapter.py` via `uv run`.
  Verify: run `test -x frameworks/pydantic-ai/run.sh` and confirm exit code 0; then `grep -F "uv run" frameworks/pydantic-ai/run.sh` returns at least one line.
- The smoke test (malformed stdin) yields exit 1 and a single-line JSON envelope on stdout with `error.message` populated.
  Verify: run `cd frameworks/pydantic-ai && (echo '{}' | ./run.sh > /tmp/pyd-stdout 2> /tmp/pyd-stderr; echo "exit=$?") | grep -F "exit=1"` and confirm exit code 0 of the pipeline. Then run `python3 -c "import json,sys; obj=json.loads(open('/tmp/pyd-stdout').read()); assert obj.get('error') is not None and isinstance(obj['error'].get('message'), str); assert obj.get('task_id') == 'unknown'; print('ok')"` and confirm stdout contains `ok`.

**Model recommendation:** cheap

---

### Task 5: Write `README.md`

**Files:**
- Modify: `frameworks/pydantic-ai/README.md`

**Steps:**

- [ ] **Step 1: Replace the TODO content** — overwrite `frameworks/pydantic-ai/README.md` with:
  ```markdown
  # pydantic-ai

  Pydantic AI (Python) — adapter for the agent shootout's software-bugfix benchmark.

  ## Model

  - Default: `claude-sonnet-4-6` (Anthropic).
  - Anthropic-only for v1. Pydantic-AI supports OpenAI, Gemini, Mistral, etc. natively, but provider switching is deferred — see `.pi/specs/2026-04-29-pydantic-ai-adapter.md`.
  - The harness may pass another Anthropic model via `--model claude-...`; the adapter prepends the `anthropic:` prefix and forwards it to `Agent(model='anthropic:<model>')` without further validation.

  ## Setup

  - Manifest setup command: `uv sync --frozen` (run once by the harness during `just eval-prepare`).
  - Dependency: `pydantic-ai>=1.0,<2.0`. Locked in `uv.lock`.
  - Python: `>= 3.11` (this directory pins `3.12` via `.python-version`).

  ## Environment variables

  - `ANTHROPIC_API_KEY` — required. Forwarded by the harness from the calling shell or repo-root `.env`.

  ## Tool wiring

  The agent uses a **hybrid tool surface**:

  - **Filesystem (Pydantic-validated `@agent.tool_plain` Python functions)**: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`. Path arguments are anchored at `input.repo_path` — any path that resolves outside the repo is rejected with an error string the model receives back.
  - **Shell (`run_shell(command: str)`)**: a single tool that calls `subprocess.run(["/bin/sh", "-c", command], cwd=input.repo_path, env=os.environ.copy(), capture_output=True, timeout=60)` and returns `exit_code: <N>` plus the combined stdout+stderr (capped at 1 MiB). This is the path for `pytest`, `git diff`, and anything else that needs shell composability.

  Why hybrid: filesystem ops benefit from Pydantic-validated arg shapes and clearer per-step traces; shell composability is preserved for tests and `git diff` so we don't have to invent a separate test-runner tool.

  ## Structured output

  The agent's final response is produced via Pydantic-AI's native `output_type=AgentReport`, where `AgentReport` is a Pydantic `BaseModel` with the contract's six required fields:

  ```python
  class AgentReport(BaseModel):
      root_cause: str
      summary: str
      changed_files: list[str]
      tests_run: list[_TestRun]   # _TestRun = {command, exit_code, summary}
      evidence: str
      confidence: float = Field(ge=0.0, le=1.0)
  ```

  The model validates the structured output before the agent run returns. There is no `submit_report` tool (cf. the deepagents adapter, which uses one). Forbidden top-level keys (`fixed`, `not_fixed`, `status`) are not declared on `AgentReport`, so they cannot appear in the response.

  If output validation fails (model returns malformed structured data after Pydantic-AI's internal retries), the adapter catches the exception, emits a contract-valid envelope with `error.message` populated and `output: None`, and exits non-zero.

  ## Capabilities (per `shared/task-spec.md`)

  - File inspection — `list_dir`, `read_file`.
  - File search — `glob`, `grep`.
  - File editing — `write_file`, `edit_file`.
  - Test execution — `run_shell` (e.g. the harness-provided `failing_test_command`).
  - Diff inspection — `run_shell git diff`.

  ## Usage limits

  `config.max_steps` is mapped to `UsageLimits(request_limit=max(2, 2 * max_steps))` and passed to `agent.run_sync`. Pydantic-AI's `request_limit` counts model requests rather than tool calls, so a small multiplier is used. If a run hits the limit, Pydantic-AI raises `UsageLimitExceeded`, which the adapter catches and renders as a contract-valid error envelope.

  ## Constraints honored by the agent

  - The agent does not commit, reset, or otherwise mutate `.git/` in `input.repo_path` — diff derivation is the harness's responsibility.
  - The agent does not run `pip install`, `uv sync`, `uv add`, or any command that would mutate the harness-owned case venv. Tests use the venv on `PATH` only.
  - The agent respects `edit_constraints.disallowed_paths` (gitignore-style globs blocking edits to tests, fixtures, lockfiles, `.git/`, etc.).
  ```

**Acceptance criteria:**

- `README.md` covers setup, model, env vars, the hybrid tool surface, the `output_type=AgentReport` pattern, and the Anthropic-only constraint.
  Verify: open `frameworks/pydantic-ai/README.md` and confirm the file contains explicit mentions of each of: `claude-sonnet-4-6`, `ANTHROPIC_API_KEY`, `uv sync --frozen`, `output_type=AgentReport`, `@agent.tool_plain`, `run_shell`, `Anthropic-only`, and `request_limit`.

**Model recommendation:** cheap

---

### Task 6: Verify cell run produces a passing envelope

**Files:**
- Read-only inspection of `runs/CURRENT/pydantic-ai/py-parse-duration-001/`

**Steps:**

- [ ] **Step 1: Confirm `ANTHROPIC_API_KEY` is set** — run `printenv ANTHROPIC_API_KEY | head -c 6` (or read from `.env` at repo root). Expected: prints the key prefix (e.g., `sk-ant`). If unset, populate `.env` per `evals/README.md` before continuing.

- [ ] **Step 2: Prepare** — from the repo root, run `just eval-prepare`. Expected: exits 0; the summary line for `framework pydantic-ai` reads `ok` (or `skipped (fresh)` on a re-run).

- [ ] **Step 3: Start a fresh campaign** — run `just eval-new`. Expected: prints a path under `runs/<timestamp>/`; `runs/CURRENT` symlinks to it.

- [ ] **Step 4: Run the bootstrap cell** — run `just eval pydantic-ai py-parse-duration-001`. Expected: completes within `timeout_s` (default 120 s), exit code 0 from `just`. The cell directory `runs/CURRENT/pydantic-ai/py-parse-duration-001/` must exist and contain `meta.json`, `scoring.json`, `request.json`, `response.json`, `stdout.log`, `stderr.log`, `diff.patch`, `visible_test.json`, and `hidden_test.json`.

- [ ] **Step 5: Inspect `meta.json`** — run `cat runs/CURRENT/pydantic-ai/py-parse-duration-001/meta.json | python3 -c "import json,sys; m=json.load(sys.stdin); assert m['status']=='ok' and m['error_reason'] is None and m['venv_mutated']==False; print('ok')"`. Expected: prints `ok`. If `status != "ok"`, read `stderr.log` to diagnose; common causes are missing `ANTHROPIC_API_KEY`, pydantic-ai import errors (re-run Task 1 Steps 3–4), or a `pydantic_ai.messages` symbol rename across versions.

- [ ] **Step 6: Inspect `scoring.json`** — run `cat runs/CURRENT/pydantic-ai/py-parse-duration-001/scoring.json | python3 -c "import json,sys; s=json.load(sys.stdin); assert s['schema_validity']==True; assert s['visible_test_outcome']=='pass'; assert s['hidden_test_outcome']=='pass'; ec=s['edit_constraint_compliance']; assert ec['disallowed_violations']==[] and ec['allowed_violations']==[] and ec['over_max_changed_files']==False; assert s['minimality']['changed_files']<=5; print('ok')"`. Expected: prints `ok`.

- [ ] **Step 7: Inspect the trace** — run `cat runs/CURRENT/pydantic-ai/py-parse-duration-001/response.json | python3 -c "import json,sys; r=json.load(sys.stdin); steps=r['trace']['steps']; assert len(steps) >= 4, f'too few steps: {len(steps)}'; tool_calls=[s for s in steps if s['kind']=='tool_call']; reads=[s for s in tool_calls if s['name']=='read_file']; tests=[s for s in tool_calls if s['name']=='run_shell']; assert reads, 'no read_file steps'; assert tests, 'no run_shell steps'; print('ok')"`. Expected: prints `ok`. This corresponds to the spec's `trace_quality` criterion — at least one file read and at least one shell invocation, with a non-trivial step count.

- [ ] **Step 8: Confirm the AgentReport shape in `response.json`** — run `cat runs/CURRENT/pydantic-ai/py-parse-duration-001/response.json | python3 -c "import json,sys; r=json.load(sys.stdin); o=r['output']; assert set(o.keys()) >= {'root_cause','summary','changed_files','tests_run','evidence','confidence'}; assert not (set(o.keys()) & {'fixed','not_fixed','status'}); assert 0.0 <= o['confidence'] <= 1.0; print('ok')"`. Expected: prints `ok`.

- [ ] **Step 9: Confirm harness tests still pass** — from the repo root, run `cd evals && uv run pytest -q`. Expected: all unit tests pass (integration tests skipped without `-m integration`). The pydantic-ai adapter is not in the harness test scope; this run confirms our changes did not perturb harness internals.

**Acceptance criteria:**

- The cell run produces all nine artifact files.
  Verify: run `for f in meta.json scoring.json request.json response.json stdout.log stderr.log diff.patch visible_test.json hidden_test.json; do test -f runs/CURRENT/pydantic-ai/py-parse-duration-001/$f || { echo "MISSING: $f"; exit 1; }; done; echo ok` and confirm stdout ends with `ok`.
- `meta.json` records `status: "ok"`, `error_reason: null`, and `venv_mutated: false`.
  Verify: run `python3 -c "import json; m=json.load(open('runs/CURRENT/pydantic-ai/py-parse-duration-001/meta.json')); assert m['status']=='ok' and m['error_reason'] is None and m['venv_mutated']==False; print('ok')"` and confirm stdout contains `ok`.
- `scoring.json` records `schema_validity: true`, `visible_test_outcome: "pass"`, `hidden_test_outcome: "pass"`, clean edit-constraint compliance, and `minimality.changed_files <= 5`.
  Verify: run the Step 6 one-liner above and confirm stdout contains `ok`.
- The `response.json` trace contains at least one `read_file` tool call and at least one `run_shell` tool call (the spec's `trace_quality` rubric).
  Verify: run the Step 7 one-liner above and confirm stdout contains `ok`.
- The `response.json` `output` block contains exactly the contract's six required keys, no forbidden keys, and `confidence` in `[0.0, 1.0]`.
  Verify: run the Step 8 one-liner above and confirm stdout contains `ok`.
- Existing harness tests in `evals/tests/` pass.
  Verify: run `cd evals && uv run pytest -q` and confirm exit code 0 and that the pytest summary line contains `passed` and zero `failed`/`error` reports.

**Model recommendation:** standard

---

## Dependencies

```
- Task 1: (no deps)
- Task 2 depends on: Task 1                  # adapter.py needs the venv populated to import pydantic-ai cleanly
- Task 3 depends on: Task 1                  # manifest's `setup: "uv sync --frozen"` presupposes uv.lock
- Task 4 depends on: Task 2                  # run.sh invokes adapter.py
- Task 5 depends on: Task 2                  # README documents the implementation choices in adapter.py
- Task 6 depends on: Tasks 1, 2, 3, 4, 5     # everything must be in place to run a real cell
```

---

## Risk Assessment

- **Pydantic-AI version drift.** `pydantic-ai` is at 1.x but actively evolving. Token-field names on `RunUsage` have shifted across minor versions (`input_tokens`/`output_tokens` vs older `request_tokens`/`response_tokens`), and message-part class names could be reorganized. Mitigation: `_messages_to_trace` reads usage attributes via `getattr(..., 0)` and uses `isinstance` checks against imported part classes; if a future version renames a part class, the import in Task 2 Step 1 fails fast and the implementer updates the import. The lockfile pins a specific resolved version so a drive-by `uv sync --frozen` cannot silently re-resolve.

- **Anthropic API non-determinism.** The acceptance criterion `visible_test_outcome == "pass"` depends on the model fixing the bug. The bootstrap bug (`UNITS = {"m": 60}` missing `s` and `h`) is small and well-scoped, and the spec calls out that single-shot runs may be model-flaky. Mitigation: the README and acceptance criteria explicitly note this; if Task 6 Step 6 fails on a single run, re-run the cell (`just eval pydantic-ai py-parse-duration-001`) before debugging the adapter. The adapter must not be the source of flakiness — the agent must always be allowed to fail naturally rather than being short-circuited or retried by the adapter.

- **`request_limit` undercount.** Pydantic-AI's `UsageLimits.request_limit` counts model requests, not tool calls. The `2 * max_steps` multiplier is a heuristic. If a run hits the limit, `UsageLimitExceeded` is raised, our `except BaseException` catches it, and we emit a contract-valid error envelope. Tuning the multiplier upward (e.g., to 3x) is a follow-on adjustment and does not require an adapter rewrite. If tuning proves insufficient, the spec's Open Questions allow falling back to `agent.iter()` and counting steps manually.

- **`ANTHROPIC_API_KEY` not in `agent_env`.** `build_agent_env` (in `evals/evals/env.py:30-48`) only forwards keys the manifest lists in `env`. If the manifest's `env` field is wrong, the adapter sees no key and fails. Task 3 explicitly sets `env: ["ANTHROPIC_API_KEY"]`; Task 6 Step 1 verifies the key is in the parent env.

- **Stdout contamination by pydantic-ai or its providers.** Pydantic-AI's default behavior writes nothing to stdout, but optional integrations (Logfire, when configured) and provider-side debug flags can. Mitigation: Task 2 Step 2 redirects `logging` to stderr at startup. The smoke test in Task 4 Step 3 confirms the error path emits exactly one JSON line on stdout. If a future version writes to stdout outside the logging module, the fix is to dup-redirect file descriptor 1 to stderr at startup and reopen a Python `sys.stdout` handle pointing at the original FD captured before the dup (a one-line change in `main()` — `os.dup2(2, 1)` after capturing the original fd).

- **Path-containment escape.** The `_resolve_within` helper relies on `Path.resolve(strict=False)` plus `relative_to`. On filesystems with symlinks (e.g., macOS `/var` → `/private/var`), both `repo_root` and `candidate` are resolved through the same symlink chain, so the comparison is sound. If a future change adds a symlink *inside* the repo that points outside, the resolved path would escape and the tool would correctly reject the access. Mitigation: this is the behavior we want — the tool surfaces an error, the model retries, and the harness's edit-constraint checker still polices the final canonical changed-file set.

- **Venv mutation.** The agent has shell access via `run_shell`. The system prompt forbids `pip install`/`uv sync`/`uv add`, but a determined agent could bypass the prompt. The harness fingerprints the case venv before/after each cell (`evals/evals/workspace.py:compute_venv_fingerprint`) and reports `venv_mutated: true` if it detects a change. Mitigation: the prompt's hard-constraints section is explicit; if `venv_mutated` is observed in production, tighten the prompt rather than blocking shell commands (which would limit legitimate `pytest` runs).

- **`.git/` mutation.** Same threat model. The harness derives the diff from a temporary index against pristine HEAD (`evals/evals/pipeline.py:81-126`), so even if the agent commits inside the worktree the harness still computes the canonical diff against the original state. The system prompt is the primary defense; the diff derivation is the safety net.

- **`@agent.tool_plain` decoration timing.** Pydantic-AI registers tools against an `Agent` instance. Because the agent is built only after we know `config.model`, tools must be registered after construction (inside `_build_agent`). The plan uses the imperative `agent.tool_plain(fn)` form rather than the `@decorator` form so this works cleanly without globals or import-time agent creation. If a future Pydantic-AI version requires decorator-only registration, the fix is to construct a placeholder agent at module load against a dummy model and re-bind `agent._model` (or equivalent) inside `_build_agent` — but this is contingency work, not expected.

- **Watchdog timer race.** The `threading.Timer`-based watchdog in Task 2 Step 14 fires `SIGINT` into the running PID. On the success path we cancel the timer in a `finally` block immediately around `agent.run_sync`, so a late-arriving fire is impossible in practice. If the timer fires *during* envelope serialization (the milliseconds between `agent.run_sync` returning and our `cancel()` running), `KeyboardInterrupt` would surface and our outer `except BaseException` would still emit a (degenerate) error envelope. Mitigation: the safety margin is 5s, far larger than serialization time; if production traces ever show this race, raise `_WATCHDOG_SAFETY_MARGIN_S` rather than deleting the watchdog.

---

## Test Command

```bash
cd evals && uv run pytest
```

(Run this from the repo root after Task 6 Step 9 to confirm harness tests still pass. Adapter changes alone should not affect harness tests; this is a regression check, not a primary verification path.)

---

## Self-Review

### Spec coverage

| Spec requirement | Implementing task |
|---|---|
| `run.sh` reads one envelope from stdin, writes one to stdout, exits | Task 4 (run.sh), Task 2 (adapter.py main loop) |
| `manifest.json` declares `entry`, idempotent `setup`, `env: ["ANTHROPIC_API_KEY"]`, `model: "claude-sonnet-4-6"` | Task 3 |
| Per-framework deps under `frameworks/pydantic-ai/` (`pyproject.toml` + lockfile) | Task 1 |
| Required capabilities (file inspection, search, edit, test, diff) rooted at `repo_path` | Task 2 (filesystem tools + `run_shell`) |
| Test invocations and `git diff` use `cwd=input.repo_path` | Task 2 Step 10 (`run_shell` calls `subprocess.run(..., cwd=str(repo))`) |
| Logging to stderr, stdout carries only the envelope | Task 2 Step 2, Task 2 Step 13 |
| `Agent(..., output_type=AgentReport, ...)` with the 6 contract fields, no forbidden keys, no `submit_report` | Task 2 Step 4 (`AgentReport`), Task 2 Step 8 (agent construction) |
| Hybrid tool surface: filesystem `@agent.tool_plain` + one `run_shell` | Task 2 Steps 8–10 |
| Filesystem-tool path arguments anchored at `input.repo_path`; rejected if they escape | Task 2 Step 6 (`_resolve_within`), Task 2 Step 9 (each tool calls `_resolve_within`) |
| Trace has steps, tokens, latency_ms (from `result.all_messages()` and `result.usage()`) | Task 2 Step 12 (`_messages_to_trace`) |
| `config.timeout_s` (best-effort) and `config.max_steps` (→ `UsageLimits.request_limit`) | Task 2 Step 14 (`_start_watchdog` derives an internal deadline from `config.timeout_s`; `request_limit = max(2, max_steps * 2)`) |
| Manifest model normalized by prepending `anthropic:` | Task 2 Step 14 (`_normalize_model`) |
| All failure paths emit a contract-compliant envelope with `error` populated; non-zero exit; never malformed JSON | Task 2 Step 14 (`except BaseException`); Task 4 Step 3 verifies via smoke test |
| README documents setup, model, env, hybrid tool surface, `output_type=AgentReport`, Anthropic-only | Task 5 |
| End-to-end: `just eval pydantic-ai py-parse-duration-001` produces all nine artifacts; `meta.status==ok`; both tests pass; clean edit constraints; `venv_mutated==false`; reasonable trace | Task 6 |

### Placeholder scan

- No "TBD" / "TODO" / "implement later" / "similar to Task N" markers in the plan.
- Every `Verify:` line is on its own line directly under the criterion bullet, with a concrete recipe (named artifact + command + success condition or named file + content check). No `Verify:` recipe is a generic placeholder like "check the file" or "looks right".
- Every code snippet in the plan is concrete (no `<...>` placeholders that the worker is expected to fill in arbitrarily); the only intentional substitution is `_normalize_model` prepending `anthropic:` to whatever `config.model` the harness sends, which is a deterministic transform.

### Type / interface consistency

- The `AgentReport` Pydantic model fields exactly match `shared/task-spec.md`'s `output` schema (`root_cause: str`, `summary: str`, `changed_files: list[str]`, `tests_run: list[{command, exit_code, summary}]`, `evidence: str`, `confidence: float in [0,1]`) and exclude the forbidden keys (`fixed`, `not_fixed`, `status`) per `evals/evals/schemas.py:4`.
- The envelope literal in `main()` matches `shared/contract.md`: top-level `task_id`, `output`, `trace: {steps, tokens: {input, output}, latency_ms}`, `error: null | {message: str}`. The harness validator (`evals/evals/schemas.py:145-211`) confirms the shape.
- `Agent(model='anthropic:<model>', output_type=BaseModel, instructions=str)` is the documented entry point per the Pydantic-AI docs (`/pydantic/pydantic-ai` Context7 source).
- `agent.tool_plain(fn)` accepts plain Python functions; Pydantic-AI introspects type hints and Pydantic-validates args at call time — this is the documented imperative equivalent of the `@agent.tool_plain` decorator.
- `agent.run_sync(message, usage_limits=UsageLimits(request_limit=int))` is the documented sync entry point per the Pydantic-AI agent docs; it returns a result whose `output` is a validated `AgentReport`, whose `all_messages()` returns the `ModelRequest`/`ModelResponse` history, and whose `usage()` returns a `RunUsage` with `input_tokens`/`output_tokens` int fields.
- The trace conversion's `isinstance(part, ToolCallPart | ToolReturnPart | TextPart)` checks reference the imports declared in Task 2 Step 1; if the version pin in `pyproject.toml` resolves a Pydantic-AI release that has renamed these classes, the import statement fails immediately at module load, and the implementer updates the names rather than silently skipping unknown parts.
