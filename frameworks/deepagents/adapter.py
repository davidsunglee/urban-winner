#!/usr/bin/env python3
# Backend choice: LocalShellBackend (the constructor accepts a root_dir argument that pins subprocess cwd and roots filesystem tools).
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import traceback
from typing import Any

from pydantic import BaseModel, Field
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


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


class _TestRun(BaseModel):
    command: str
    exit_code: int
    summary: str


class _ReportArgs(BaseModel):
    root_cause: str
    summary: str
    changed_files: list[str]
    tests_run: list[_TestRun]
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)


_CAPTURED_REPORT: dict[str, Any] = {}


@tool("submit_report", args_schema=_ReportArgs)
def submit_report(
    root_cause: str,
    summary: str,
    changed_files: list[str],
    tests_run: list[dict[str, Any]],
    evidence: str,
    confidence: float,
) -> str:
    """Submit the final structured bugfix report. Call exactly once when the fix is complete.

    Args mirror shared/task-spec.md's `output` schema. The harness derives the
    authoritative diff and rerun results — these fields are informational.
    """
    normalized_tests = [
        {"command": t["command"], "exit_code": int(t["exit_code"]), "summary": t["summary"]}
        for t in tests_run
    ]
    _CAPTURED_REPORT.clear()
    _CAPTURED_REPORT.update({
        "root_cause": root_cause,
        "summary": summary,
        "changed_files": list(changed_files),
        "tests_run": normalized_tests,
        "evidence": evidence,
        "confidence": float(confidence),
    })
    return "report received"


SYSTEM_PROMPT = """You are a software engineer fixing a failing test in a Python repository.

Your tools:
- Filesystem: ls, glob, grep, read_file, write_file, edit_file (use these to navigate and edit the repo).
- Shell: execute (run pytest, git diff, etc. — commands run with cwd pinned to the repo root).
- submit_report: call exactly once when the fix is complete.

Workflow you must follow:
1. Read the failing test command and captured failure output. Form a hypothesis about the root cause.
2. Inspect the repository (ls, read_file, grep) before making any edits. Read the file the stack trace points at AND any files it imports from.
3. Apply a minimal in-place edit that addresses the root cause. Do not edit files matching the disallowed_paths globs you were given. Do not edit tests, fixtures, lockfiles, or .git/ contents.
4. Re-run the failing test command via the execute tool to confirm the fix.
5. Call submit_report exactly once with root_cause, summary, changed_files, tests_run, evidence, confidence.

Hard constraints:
- Do not commit, reset, or otherwise modify .git/ — the harness derives the diff itself.
- Do not run pip install, uv sync, uv add, or any command that would mutate the Python environment. Tests already have all the dependencies they need on PATH.
- Keep the change set small (target one or two files). Prefer fixing the underlying data/logic over hardcoding the failing input.
"""


def _build_agent(*, model_name: str, repo_path: str):
    model = init_chat_model(f"anthropic:{model_name}")
    backend = LocalShellBackend(root_dir=repo_path)
    return create_deep_agent(
        model=model,
        backend=backend,
        tools=[submit_report],
        system_prompt=SYSTEM_PROMPT,
    )


def _user_message(input_obj: dict[str, Any]) -> str:
    ec = input_obj["edit_constraints"]
    disallowed = ec.get("disallowed_paths", [])
    allowed = ec.get("allowed_paths")
    max_files = ec.get("max_changed_files")
    lines = [
        f"Repo path (cwd for shell tool): {input_obj['repo_path']}",
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
    lines.append("Diagnose, fix, and submit your report via the submit_report tool.")
    return "\n".join(lines)


def _messages_to_trace(messages: list, latency_ms: int) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    input_tokens = 0
    output_tokens = 0
    saw_usage = False
    for msg in messages:
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None) or {}
            if usage:
                saw_usage = True
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
            tool_calls = getattr(msg, "tool_calls", None) or []
            if tool_calls:
                for tc in tool_calls:
                    steps.append({
                        "kind": "tool_call",
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                        "result": {},
                    })
            else:
                steps.append({
                    "kind": "model_call",
                    "name": "ai_message",
                    "args": {},
                    "result": {"content": _stringify_content(msg.content)},
                })
        elif isinstance(msg, ToolMessage):
            if steps and steps[-1]["kind"] == "tool_call" and not steps[-1]["result"]:
                steps[-1]["result"] = {"content": _stringify_content(msg.content)}
            else:
                steps.append({
                    "kind": "tool_call",
                    "name": getattr(msg, "name", "") or "",
                    "args": {},
                    "result": {"content": _stringify_content(msg.content)},
                })
    trace: dict[str, Any] = {"steps": steps, "latency_ms": int(latency_ms)}
    if saw_usage:
        trace["tokens"] = {"input": int(input_tokens), "output": int(output_tokens)}
    else:
        trace["tokens"] = {"input": 0, "output": 0}
    return trace


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


def _emit_envelope(envelope: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False))  # _emit_envelope
    sys.stdout.write("\n")  # _emit_envelope
    sys.stdout.flush()


class _AdapterTimeout(Exception):
    """Raised by the SIGALRM handler when the adapter soft-deadline fires."""


_DEADLINE: dict[str, float] = {}  # absolute monotonic deadline; consumed by tools needing a soft cap


def _on_timeout(signum, frame):
    raise _AdapterTimeout("adapter soft-deadline reached (config.timeout_s)")


def main() -> int:
    t0 = time.monotonic()
    task_id = "unknown"
    try:
        request = _read_request()
        task_id = request["task_id"]
        cfg = request["config"]
        input_obj = request["input"]
        model_name = cfg["model"]
        max_steps = int(cfg["max_steps"])
        timeout_s = float(cfg["timeout_s"])
        repo_path = input_obj["repo_path"]

        if not os.path.isdir(repo_path):
            raise RuntimeError(f"input.repo_path does not exist or is not a directory: {repo_path}")

        # Soft deadline: emit our own contract-valid envelope before the harness hard-kills us.
        # Reserve 5s of headroom for envelope serialization + stdout flush. Floor at 5s so a
        # tiny config.timeout_s still produces a sensible (if immediate) deadline rather than
        # a negative timer.
        soft_deadline_s = max(5.0, timeout_s - 5.0)
        _DEADLINE["deadline"] = t0 + soft_deadline_s

        agent = _build_agent(model_name=model_name, repo_path=repo_path)
        user_text = _user_message(input_obj)
        recursion_limit = max(10, max_steps * 2)

        # SIGALRM-based soft deadline. Works in the main thread (Python raises the exception
        # between bytecode instructions when control returns from blocking syscalls).
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, soft_deadline_s)
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": user_text}]},
                config={"recursion_limit": recursion_limit},
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)  # disarm even on error

        messages = result.get("messages", []) if isinstance(result, dict) else []
        latency_ms = int((time.monotonic() - t0) * 1000)
        trace = _messages_to_trace(messages, latency_ms)

        if not _CAPTURED_REPORT:
            envelope = {
                "task_id": task_id,
                "output": None,
                "trace": trace,
                "error": {"message": "agent did not call submit_report"},
            }
            _emit_envelope(envelope)
            return 1

        envelope = {
            "task_id": task_id,
            "output": dict(_CAPTURED_REPORT),
            "trace": trace,
            "error": None,
        }
        _emit_envelope(envelope)
        return 0
    except BaseException as exc:
        # Disarm the timer in case the exception was not _AdapterTimeout itself.
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
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
