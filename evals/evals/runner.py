import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from evals.discovery import CaseSpec, FrameworkSpec
from evals.env import build_agent_env
from evals.process_tree import PROCESS_GROUP_POPEN_KWARGS, terminate_process_tree
from evals.schemas import validate_envelope
from evals.setup import is_setup_failed

STDOUT_CAP_BYTES = 8 * 1024 * 1024  # 8 MiB
STDERR_CAP_BYTES = 5 * 1024 * 1024  # 5 MiB
KILL_GRACE_S = 5

_DEFAULT_DISALLOWED_PATHS = [
    "tests/**",
    "**/*test*",
    "**/*fixture*",
    "**/*lock*",
    "**/CHANGELOG*",
    ".git/**",
]
_DEFAULT_MAX_CHANGED_FILES = 5

_HARNESS_DEFAULT_TIMEOUT_S = 120
_HARNESS_DEFAULT_MAX_STEPS = 50


@dataclass(frozen=True)
class RunnerResult:
    task_id: str
    exit_code: int | None  # None on timeout
    timed_out: bool
    stdout_path: Path
    stderr_path: Path
    stdout_truncated: bool
    stderr_truncated: bool
    response_path: Path | None
    error_reason: str | None
    latency_ms: int
    framework_misconfigured_reason: str | None


@dataclass(frozen=True)
class EffectiveConfig:
    model: str
    timeout_s: int
    max_steps: int
    sources: dict[str, str]  # field -> "framework-manifest" | "campaign" | "cell-flag" | "harness-default"


def _pump_capped(reader, dest_path: Path, cap_bytes: int) -> bool:
    chunk_size = 65536
    written = 0
    truncated = False
    with open(dest_path, "wb") as f:
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            if written < cap_bytes:
                to_write = min(len(chunk), cap_bytes - written)
                f.write(chunk[:to_write])
                written += to_write
                if len(chunk) > to_write:
                    truncated = True
            else:
                truncated = True
    return truncated


def _atomic_write_bytes(dest: Path, content: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with open(fd, "wb") as f:
            f.write(content)
        Path(tmp).rename(dest)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _classify_error(
    *,
    exit_code: int | None,
    timed_out: bool,
    stdout_size: int,
    stdout_truncated: bool,
    parse_error: bool,
    envelope_errors: list[str],
) -> str | None:
    if timed_out:
        return "timeout"
    if exit_code is not None and exit_code != 0:
        return "nonzero_exit"
    if exit_code == 0 and stdout_size == 0:
        return "missing_response"
    if exit_code == 0 and (stdout_truncated or parse_error):
        return "malformed_response_json"
    if exit_code == 0 and not parse_error and envelope_errors:
        return "envelope_schema_violation"
    return None


def _parse_and_validate_stdout(
    stdout_path: Path, stdout_truncated: bool
) -> tuple[bool, list[str], object | None]:
    """Return (parse_error, envelope_errors, parsed_obj_or_None)."""
    if stdout_truncated:
        return True, [], None
    if not stdout_path.exists() or stdout_path.stat().st_size == 0:
        return False, [], None
    try:
        raw = stdout_path.read_bytes()
        obj = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True, [], None
    errors = validate_envelope(obj)
    return False, errors, obj


def _write_response_if_valid(
    stdout_path: Path,
    response_path: Path,
    *,
    stdout_truncated: bool,
) -> Path | None:
    parse_error, envelope_errors, obj = _parse_and_validate_stdout(
        stdout_path, stdout_truncated
    )
    if parse_error or envelope_errors or obj is None:
        return None
    canonical = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")
    _atomic_write_bytes(response_path, canonical)
    return response_path


def _resolve_edit_constraints(case_constraints: dict) -> dict:
    out: dict = {
        "disallowed_paths": list(_DEFAULT_DISALLOWED_PATHS),
        "max_changed_files": _DEFAULT_MAX_CHANGED_FILES,
    }
    if "disallowed_paths" in case_constraints:
        out["disallowed_paths"] = list(case_constraints["disallowed_paths"])
    if "max_changed_files" in case_constraints:
        out["max_changed_files"] = case_constraints["max_changed_files"]
    if "allowed_paths" in case_constraints:
        out["allowed_paths"] = list(case_constraints["allowed_paths"])
    return out


def _entry_is_runnable(
    framework: FrameworkSpec, *, path: str | None = None
) -> tuple[bool, str | None]:
    try:
        argv = shlex.split(framework.entry)
    except ValueError as exc:
        return False, f"entry parse error: {exc}"
    if not argv:
        return False, "entry is empty"
    exe = argv[0]
    exe_path = Path(exe)
    has_path_separator = "/" in exe or (os.altsep is not None and os.altsep in exe)
    if exe_path.is_absolute() or has_path_separator:
        if not exe_path.is_absolute():
            exe_path = (framework.dir / exe).resolve()
        if not exe_path.is_file():
            return False, f"entry not found: {exe_path}"
        if not os.access(exe_path, os.X_OK):
            return False, f"entry not executable: {exe_path}"
        return True, None

    resolved = shutil.which(exe, path=path)
    if resolved is None:
        return False, f"entry not found on PATH: {exe}"
    return True, None


def run_cell(
    *,
    framework: FrameworkSpec,
    case: CaseSpec,
    effective_config: EffectiveConfig,
    cell_dir: Path,
    cache_dir: Path,
    repo_root: Path,
    base_env: dict[str, str],
    dotenv: dict[str, str],
) -> RunnerResult:
    cell_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = cell_dir / "stdout.log"
    stderr_path = cell_dir / "stderr.log"
    response_path = cell_dir / "response.json"
    task_id = f"{framework.name}:{case.case_id}:{uuid4().hex[:8]}"

    case_venv_path = cache_dir / f"{case.case_id}.venv"
    agent_env = build_agent_env(
        declared_keys=framework.env_keys,
        case_venv_path=case_venv_path,
        base_env=base_env,
        dotenv=dotenv,
    )

    # Pre-check framework misconfiguration.
    misconfig_reason: str | None = None
    if framework.discovery_error is not None:
        misconfig_reason = (
            "manifest invalid: " + "; ".join(framework.discovery_error.messages)
        )
    else:
        runnable, reason = _entry_is_runnable(framework, path=agent_env.get("PATH"))
        if not runnable:
            misconfig_reason = reason
        elif framework.setup is not None and is_setup_failed(framework.name, cache_dir):
            misconfig_reason = "setup .fail sentinel exists"

    if misconfig_reason is not None:
        stdout_path.write_bytes(b"")
        stderr_path.write_text(f"framework_misconfigured: {misconfig_reason}\n")
        return RunnerResult(
            task_id=task_id,
            exit_code=None,
            timed_out=False,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=False,
            stderr_truncated=False,
            response_path=None,
            error_reason="framework_misconfigured",
            latency_ms=0,
            framework_misconfigured_reason=misconfig_reason,
        )

    # Build request.
    request = {
        "task_id": task_id,
        "input": {
            "case_id": case.case_id,
            "repo_path": str((cell_dir / "repo").resolve()),
            "failing_test_command": case.failing_test_command,
            "failure_output": case.failure_output,
            "edit_constraints": _resolve_edit_constraints(case.edit_constraints),
        },
        "config": {
            "model": effective_config.model,
            "max_steps": effective_config.max_steps,
            "timeout_s": effective_config.timeout_s,
        },
    }
    request_json = json.dumps(request, sort_keys=True, indent=2)
    (cell_dir / "request.json").write_text(request_json)

    argv = shlex.split(framework.entry)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(framework.dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=agent_env,
            **PROCESS_GROUP_POPEN_KWARGS,
        )
    except OSError as exc:
        misconfig_reason = f"failed to spawn entry: {exc}"
        stdout_path.write_bytes(b"")
        stderr_path.write_text(f"framework_misconfigured: {misconfig_reason}\n")
        return RunnerResult(
            task_id=task_id,
            exit_code=None,
            timed_out=False,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_truncated=False,
            stderr_truncated=False,
            response_path=None,
            error_reason="framework_misconfigured",
            latency_ms=0,
            framework_misconfigured_reason=misconfig_reason,
        )

    stdout_truncated = False
    stderr_truncated = False
    request_bytes = request_json.encode("utf-8")
    deadline = t0 + effective_config.timeout_s

    def remaining_timeout() -> float:
        return max(0.0, deadline - time.monotonic())

    def pump_stdout() -> None:
        nonlocal stdout_truncated
        stdout_truncated = _pump_capped(proc.stdout, stdout_path, STDOUT_CAP_BYTES)

    def pump_stderr() -> None:
        nonlocal stderr_truncated
        stderr_truncated = _pump_capped(proc.stderr, stderr_path, STDERR_CAP_BYTES)

    def write_stdin() -> None:
        try:
            proc.stdin.write(request_bytes)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    t1 = threading.Thread(target=pump_stdout, daemon=True)
    t2 = threading.Thread(target=pump_stderr, daemon=True)
    t_stdin = threading.Thread(target=write_stdin, daemon=True)
    t1.start()
    t2.start()
    t_stdin.start()

    timed_out = False
    try:
        proc.wait(timeout=remaining_timeout())
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc, KILL_GRACE_S)
    else:
        # The adapter process can exit while background descendants keep inherited
        # stdout/stderr/stdin pipes open. Clean up the isolated process group before
        # joining pump threads so artifact collection stays bounded by the harness.
        terminate_process_tree(proc, KILL_GRACE_S)

    if not timed_out:
        t_stdin.join(timeout=remaining_timeout())
        if t_stdin.is_alive():
            timed_out = True
            terminate_process_tree(proc, KILL_GRACE_S)

    t_stdin.join()
    t1.join()
    t2.join()

    latency_ms = int((time.monotonic() - t0) * 1000)
    exit_code = None if timed_out else proc.returncode

    parse_error, envelope_errors, parsed_obj = _parse_and_validate_stdout(
        stdout_path, stdout_truncated
    )
    written_response: Path | None = None
    if parsed_obj is not None and not parse_error and not envelope_errors:
        canonical = json.dumps(parsed_obj, sort_keys=True, indent=2).encode("utf-8")
        _atomic_write_bytes(response_path, canonical)
        written_response = response_path

    stdout_size = stdout_path.stat().st_size
    error_reason = _classify_error(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_size=stdout_size,
        stdout_truncated=stdout_truncated,
        parse_error=parse_error,
        envelope_errors=envelope_errors,
    )

    return RunnerResult(
        task_id=task_id,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        response_path=written_response,
        error_reason=error_reason,
        latency_ms=latency_ms,
        framework_misconfigured_reason=None,
    )


def resolve_effective_config(
    framework: FrameworkSpec,
    *,
    campaign_overrides: dict,
    cell_overrides: dict,
    harness_defaults: dict,
) -> EffectiveConfig:
    defaults = {
        "timeout_s": _HARNESS_DEFAULT_TIMEOUT_S,
        "max_steps": _HARNESS_DEFAULT_MAX_STEPS,
        **harness_defaults,
    }

    sources: dict[str, str] = {}

    def pick(field: str, manifest_value):
        if cell_overrides.get(field) is not None:
            sources[field] = "cell-flag"
            return cell_overrides[field]
        if campaign_overrides.get(field) is not None:
            sources[field] = "campaign"
            return campaign_overrides[field]
        if manifest_value is not None:
            sources[field] = "framework-manifest"
            return manifest_value
        sources[field] = "harness-default"
        return defaults[field]

    model = pick("model", framework.model)
    timeout_s = pick("timeout_s", None)
    max_steps = pick("max_steps", None)

    return EffectiveConfig(
        model=model,
        timeout_s=timeout_s,
        max_steps=max_steps,
        sources=sources,
    )
