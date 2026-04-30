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

import pathspec
from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_FILE_READ_CAP_BYTES = 256 * 1024  # 256 KiB per read
_GREP_OUTPUT_CAP_BYTES = 256 * 1024
_LIST_DIR_ENTRIES_CAP = 500
_GLOB_RESULTS_CAP = 500
_SHELL_TIMEOUT_S = 60
_SHELL_OUTPUT_CAP_BYTES = 1024 * 1024  # 1 MiB per call
_WATCHDOG_SAFETY_MARGIN_S = 5.0  # leave time to emit an envelope before the harness SIGKILLs us

_STATE: dict[str, Path] = {}  # populated in main(); holds {"repo_path": <Path>}

# Edit-constraint enforcement at the write_file/edit_file boundary. Populated
# in main() from input.edit_constraints; consulted by every filesystem write.
# Keys (all optional except disallowed_paths defaulting to empty list):
#   disallowed_paths: list[str] of gitignore-style globs the agent must NOT modify.
#   allowed_paths:    list[str] of gitignore-style globs the agent may ONLY modify
#                     (None / absent means "no allowlist; all repo paths allowed").
#   max_changed_files: int | None — cap on the size of the modified file set.
_EDIT_CONSTRAINTS: dict[str, Any] = {}
# Set of repo-relative posix paths the agent has written to so far. Used to
# enforce max_changed_files. Re-writing the same path does not increment.
_CHANGED_FILES: set[str] = set()


# Allowlist of environment variables the model-controlled run_shell tool sees.
# Starting from `os.environ.copy()` would forward provider/API tokens
# (ANTHROPIC_API_KEY, AWS_*, GITHUB_TOKEN, ...) into traces and artifacts via
# any shell command the model issues. Mirror the deepagents adapter and only
# pass through the keys agent-side `pytest`/`git`/`uv` actually need.
_SAFE_PASSTHROUGH_ENV_KEYS: tuple[str, ...] = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "PATH",
)


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


def _relative_posix(repo_root: Path, target: Path) -> str | None:
    try:
        return target.resolve(strict=False).relative_to(repo_root.resolve(strict=False)).as_posix()
    except ValueError:
        return None


def _check_edit_constraints(rel_posix: str) -> str | None:
    """Return an `error: ...` string if `rel_posix` violates edit_constraints,
    or None if the write is allowed. Side-effect free."""
    disallowed = _EDIT_CONSTRAINTS.get("disallowed_paths") or []
    if disallowed:
        spec = pathspec.PathSpec.from_lines("gitignore", disallowed)
        if spec.match_file(rel_posix):
            return f"error: path '{rel_posix}' matches disallowed_paths constraint"
    allowed = _EDIT_CONSTRAINTS.get("allowed_paths")
    if allowed is not None:
        spec = pathspec.PathSpec.from_lines("gitignore", allowed)
        if not spec.match_file(rel_posix):
            return f"error: path '{rel_posix}' is not allowed by allowed_paths constraint"
    max_files = _EDIT_CONSTRAINTS.get("max_changed_files")
    if max_files is not None and rel_posix not in _CHANGED_FILES:
        if len(_CHANGED_FILES) + 1 > int(max_files):
            return (
                f"error: writing '{rel_posix}' would exceed max_changed_files="
                f"{max_files} (already changed: {sorted(_CHANGED_FILES)})"
            )
    return None


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


SYSTEM_PROMPT = """You are a software engineer fixing a failing test in a Python repository.

Your tools:
- Filesystem (rooted at the repo path; paths outside are rejected): list_dir, glob, grep, read_file, write_file, edit_file.
- Shell: run_shell(command) (runs with cwd pinned to the repo root and the harness-prepared PATH; use for pytest, git diff, ls, etc.).

Your final response will be validated as an AgentReport with these fields: root_cause, summary, changed_files, tests_run, evidence, confidence.

Workflow you must follow:
1. Read the failing test command and captured failure output. Form a hypothesis about the root cause.
2. Inspect the repository (list_dir, read_file, grep) before making any edits. Read the file the stack trace points at AND any files it imports from.
3. Apply a minimal in-place edit that addresses the root cause. Do not edit files matching the disallowed_paths globs you were given. Do not edit tests, fixtures, lockfiles, or .git/ contents. write_file/edit_file enforce edit_constraints (disallowed_paths, allowed_paths, max_changed_files) and will return an `error:` string instead of writing if a call would violate them — adjust your plan if you see one.
4. Re-run the failing test command via run_shell to confirm the fix.
5. Return the final AgentReport with root_cause, summary, changed_files, tests_run, evidence, confidence.

Hard constraints:
- Do not commit, reset, or otherwise modify .git/ — the harness derives the diff itself.
- Do not run pip install, uv sync, uv add, or any command that would mutate the Python environment. Tests already have all the dependencies they need on PATH.
- Keep the change set small (target one or two files). Prefer fixing the underlying data/logic over hardcoding the failing input.
"""


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
    rel_posix = _relative_posix(repo, target)
    if rel_posix is None:
        return f"error: path '{path}' is outside the repository"
    err = _check_edit_constraints(rel_posix)
    if err is not None:
        return err
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
    except OSError as exc:
        return f"error: {exc}"
    _CHANGED_FILES.add(rel_posix)
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
    rel_posix = _relative_posix(repo, target)
    if rel_posix is None:
        return f"error: path '{path}' is outside the repository"
    err = _check_edit_constraints(rel_posix)
    if err is not None:
        return err
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
    _CHANGED_FILES.add(rel_posix)
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
    if pattern.startswith("/") or (len(pattern) > 1 and pattern[1] == ":"):
        return f"error: absolute glob patterns are not supported: '{pattern}'"
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
    except (OSError, ValueError, NotImplementedError, RuntimeError) as exc:
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


def _build_shell_env(repo: Path) -> dict[str, str]:
    """Reconstruct the harness case-test env for the model-controlled run_shell tool.

    Mirrors evals/evals/env.build_test_env semantics: only safe passthrough keys
    plus PYTHONPATH=<repo>/src:<repo>; if AGENT_HARNESS_CASE_VENV is exported by
    run.sh (preserved before run.sh unsets UV_PROJECT_ENVIRONMENT for the
    adapter's own `uv run`), restore UV_PROJECT_ENVIRONMENT, set UV_NO_SYNC=1,
    and prepend <case-venv>/bin to PATH. Provider/API secrets present in the
    adapter process env are intentionally NOT forwarded so a model-issued shell
    command cannot exfiltrate them via traces or artifacts.
    """
    src = os.environ
    env: dict[str, str] = {}
    for key in _SAFE_PASSTHROUGH_ENV_KEYS:
        value = src.get(key)
        if value is not None:
            env[key] = value

    repo_resolved = repo.resolve(strict=False)
    pythonpath_entries = [str(repo_resolved / "src"), str(repo_resolved)]
    existing_pythonpath = src.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    case_venv = src.get("AGENT_HARNESS_CASE_VENV") or src.get("UV_PROJECT_ENVIRONMENT")
    if case_venv:
        case_venv_path = Path(case_venv).resolve()
        env["UV_PROJECT_ENVIRONMENT"] = str(case_venv_path)
        env["UV_NO_SYNC"] = "1"
        bin_dir = str(case_venv_path / "bin")
        existing_path = env.get("PATH")
        env["PATH"] = f"{bin_dir}{os.pathsep}{existing_path}" if existing_path else bin_dir
    return env


def run_shell(command: str) -> str:
    """Run a shell command in the repository root and return its stdout+stderr (capped).

    Use for pytest, git diff, ls, etc. The cwd is pinned to the repo root. The env
    is reconstructed (not inherited) so test commands see the same case-venv
    semantics as the harness (UV_PROJECT_ENVIRONMENT, UV_NO_SYNC, PATH, PYTHONPATH)
    and provider secrets are not forwarded into the model-controlled tool.
    """
    repo = _STATE.get("repo_path")
    if repo is None:
        return "error: repo_path not initialized"
    env = _build_shell_env(repo)
    try:
        completed = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=str(repo),
            env=env,
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


def _emit_envelope(envelope: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


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

        ec = input_obj.get("edit_constraints") or {}
        _EDIT_CONSTRAINTS.clear()
        _EDIT_CONSTRAINTS["disallowed_paths"] = list(ec.get("disallowed_paths") or [])
        if "allowed_paths" in ec and ec["allowed_paths"] is not None:
            _EDIT_CONSTRAINTS["allowed_paths"] = list(ec["allowed_paths"])
        if ec.get("max_changed_files") is not None:
            _EDIT_CONSTRAINTS["max_changed_files"] = int(ec["max_changed_files"])
        _CHANGED_FILES.clear()

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
