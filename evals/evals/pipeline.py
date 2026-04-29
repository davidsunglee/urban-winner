import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pathspec

from evals.discovery import CaseSpec, FrameworkSpec
from evals.env import build_test_env
from evals.runner import EffectiveConfig, RunnerResult
from evals.schemas import validate_agent_output, validate_envelope
from evals.workspace import compute_venv_fingerprint

TEST_OUTPUT_CAP_BYTES = 5 * 1024 * 1024  # 5 MiB
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


@dataclass(frozen=True)
class TestRunResult:
    command: str
    exit_code: int | None
    outcome: str  # "pass" | "fail" | "error"
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _atomic_write_json(target: Path, obj) -> None:
    payload = json.dumps(obj, sort_keys=True, indent=2).encode("utf-8")
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(str(tmp), str(target))
    try:
        dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Step 2: derive canonical diff via temp index
# ---------------------------------------------------------------------------

def derive_canonical_diff(cell_dir: Path) -> dict:
    repo = cell_dir / "repo"
    with tempfile.NamedTemporaryFile(prefix="cell-index.", delete=False) as tf:
        temp_index = tf.name
    try:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = temp_index
        subprocess.run(
            ["git", "-C", str(repo), "read-tree", "HEAD"],
            env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            env=env, check=True, capture_output=True,
        )
        patch = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "HEAD"],
            env=env, check=True, capture_output=True,
        ).stdout
        names = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "HEAD", "--name-only"],
            env=env, check=True, capture_output=True,
        ).stdout.decode().splitlines()
        numstat = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "HEAD", "--numstat"],
            env=env, check=True, capture_output=True,
        ).stdout.decode().splitlines()
    finally:
        try:
            os.unlink(temp_index)
        except FileNotFoundError:
            pass

    (cell_dir / "diff.patch").write_bytes(patch)

    added = 0
    removed = 0
    for line in numstat:
        parts = line.split("\t")
        if len(parts) >= 2:
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                removed += int(parts[1])

    return {"changed_files": names, "added": added, "removed": removed}


# ---------------------------------------------------------------------------
# Step 3 + 4: visible/hidden test rerun (capped, draining)
# ---------------------------------------------------------------------------

def _pump_capped_to_buffer(reader, buf: bytearray, cap: int, flag: list[bool]) -> None:
    while True:
        chunk = reader.read(65536)
        if not chunk:
            break
        remaining = cap - len(buf)
        if remaining > 0:
            take = min(len(chunk), remaining)
            buf.extend(chunk[:take])
            if len(chunk) > take:
                flag[0] = True
        else:
            flag[0] = True


def run_test_command(
    command: str,
    *,
    cwd: Path,
    env: dict,
    timeout_s: int,
    output_path: Path | None = None,
) -> TestRunResult:
    t0 = time.monotonic()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_t = [False]
    stderr_t = [False]

    t1 = threading.Thread(
        target=_pump_capped_to_buffer,
        args=(proc.stdout, stdout_buf, TEST_OUTPUT_CAP_BYTES, stdout_t),
        daemon=True,
    )
    t2 = threading.Thread(
        target=_pump_capped_to_buffer,
        args=(proc.stderr, stderr_buf, TEST_OUTPUT_CAP_BYTES, stderr_t),
        daemon=True,
    )
    t1.start()
    t2.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    t1.join()
    t2.join()

    duration_ms = int((time.monotonic() - t0) * 1000)
    exit_code = None if timed_out else proc.returncode

    if timed_out:
        outcome = "error"
    elif exit_code is not None and exit_code < 0:
        outcome = "error"
    elif exit_code == 0:
        outcome = "pass"
    else:
        outcome = "fail"

    result = TestRunResult(
        command=command,
        exit_code=exit_code,
        outcome=outcome,
        stdout_truncated=stdout_t[0],
        stderr_truncated=stderr_t[0],
        duration_ms=duration_ms,
    )

    if output_path is not None:
        _atomic_write_json(
            output_path,
            {
                "command": command,
                "exit_code": exit_code,
                "outcome": outcome,
                "stdout": stdout_buf.decode("utf-8", errors="replace"),
                "stderr": stderr_buf.decode("utf-8", errors="replace"),
                "stdout_truncated": stdout_t[0],
                "stderr_truncated": stderr_t[0],
                "duration_ms": duration_ms,
            },
        )

    return result


# ---------------------------------------------------------------------------
# Step 5: edit constraint check
# ---------------------------------------------------------------------------

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


def check_edit_constraints(changed_files: list[str], constraints: dict) -> dict:
    disallowed_spec = pathspec.PathSpec.from_lines(
        "gitignore", constraints.get("disallowed_paths", [])
    )
    disallowed_violations = [f for f in changed_files if disallowed_spec.match_file(f)]

    if "allowed_paths" in constraints:
        allowed_spec = pathspec.PathSpec.from_lines(
            "gitignore", constraints["allowed_paths"]
        )
        allowed_violations = [f for f in changed_files if not allowed_spec.match_file(f)]
    else:
        allowed_violations = []

    over_max = len(changed_files) > constraints["max_changed_files"]
    return {
        "disallowed_violations": disallowed_violations,
        "allowed_violations": allowed_violations,
        "over_max_changed_files": over_max,
    }


# ---------------------------------------------------------------------------
# Step 6: assemble scoring
# ---------------------------------------------------------------------------

def assemble_scoring(
    *,
    schema_validity: bool,
    visible_test_outcome: str,
    hidden_test_outcome: str,
    edit_constraint_compliance: dict,
    diff_summary: dict,
    latency_ms: int,
    parsed_envelope: object | None,
) -> dict:
    scoring = {
        "schema_validity": schema_validity,
        "visible_test_outcome": visible_test_outcome,
        "hidden_test_outcome": hidden_test_outcome,
        "edit_constraint_compliance": edit_constraint_compliance,
        "minimality": {
            "changed_files": len(diff_summary["changed_files"]),
            "changed_lines_added": diff_summary["added"],
            "changed_lines_removed": diff_summary["removed"],
        },
        "latency_ms": latency_ms,
        "trace_quality": "n/a",
    }
    if isinstance(parsed_envelope, dict):
        trace = parsed_envelope.get("trace")
        if isinstance(trace, dict):
            tokens = trace.get("tokens")
            if (
                isinstance(tokens, dict)
                and isinstance(tokens.get("input"), int)
                and isinstance(tokens.get("output"), int)
            ):
                scoring["token_usage"] = {
                    "input": tokens["input"],
                    "output": tokens["output"],
                }
    return scoring


# ---------------------------------------------------------------------------
# Step 7: write meta + scoring
# ---------------------------------------------------------------------------

def write_meta_json(
    cell_dir: Path,
    *,
    framework: str,
    case_id: str,
    task_id: str,
    model: str,
    started_at: str,
    ended_at: str,
    status: str,
    error_reason: str | None,
    exit_code: int | None,
    stdout_truncated: bool,
    stderr_truncated: bool,
    harness_latency_ms: int,
    framework_reported_latency_ms: int | None,
    effective_config: EffectiveConfig,
    venv_hash_before: str,
    venv_hash_after: str,
    venv_mutated: bool,
    scoring: dict,
) -> None:
    _atomic_write_json(cell_dir / "scoring.json", scoring)

    meta = {
        "framework": framework,
        "case_id": case_id,
        "task_id": task_id,
        "model": model,
        "started_at": started_at,
        "ended_at": ended_at,
        "status": status,
        "error_reason": error_reason,
        "exit_code": exit_code,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "harness_latency_ms": harness_latency_ms,
        "framework_reported_latency_ms": framework_reported_latency_ms,
        "effective_config": {
            "model": effective_config.model,
            "timeout_s": effective_config.timeout_s,
            "max_steps": effective_config.max_steps,
            "sources": dict(effective_config.sources),
        },
        "venv_hash_before": venv_hash_before,
        "venv_hash_after": venv_hash_after,
        "venv_mutated": venv_mutated,
    }
    _atomic_write_json(cell_dir / "meta.json", meta)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _parse_stdout_log(stdout_path: Path, stdout_truncated: bool):
    """Return parsed envelope dict or None."""
    if stdout_truncated:
        return None
    if not stdout_path.exists() or stdout_path.stat().st_size == 0:
        return None
    try:
        return json.loads(stdout_path.read_bytes())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def run_pipeline(
    cell_dir: Path,
    runner_result: RunnerResult,
    *,
    framework: FrameworkSpec,
    case: CaseSpec,
    effective_config: EffectiveConfig,
    cache_dir: Path,
    base_env: dict[str, str],
    venv_hash_before: str,
) -> None:
    ended_at_dt = datetime.now(timezone.utc)
    started_at_dt = datetime.fromtimestamp(
        ended_at_dt.timestamp() - (runner_result.latency_ms / 1000.0),
        tz=timezone.utc,
    )

    # Step 1 — schema validation.
    parsed = _parse_stdout_log(runner_result.stdout_path, runner_result.stdout_truncated)
    envelope_errors: list[str] = []
    output_errors: list[str] = []
    response_present = False
    framework_reported_latency_ms: int | None = None

    if isinstance(parsed, dict):
        envelope_errors = validate_envelope(parsed)
        output = parsed.get("output")
        if isinstance(output, dict):
            output_errors = validate_agent_output(output)
        response_present = (
            not envelope_errors
            and isinstance(output, dict)
            and not output_errors
        )
        trace = parsed.get("trace") if not envelope_errors else None
        if isinstance(trace, dict) and isinstance(trace.get("latency_ms"), int):
            framework_reported_latency_ms = trace["latency_ms"]

    schema_validity = (not envelope_errors) and (not output_errors) and response_present

    # Step 2 — diff.
    diff_summary = derive_canonical_diff(cell_dir)

    # Step 3 — visible test rerun.
    case_venv_path = cache_dir / f"{case.case_id}.venv"
    test_env = build_test_env(case_venv_path=case_venv_path, base_env=base_env)
    repo_dir = cell_dir / "repo"

    visible = run_test_command(
        case.failing_test_command,
        cwd=repo_dir,
        env=test_env,
        timeout_s=effective_config.timeout_s,
        output_path=cell_dir / "visible_test.json",
    )

    # Step 4 — hidden test rerun.
    if case.hidden_test_command:
        hidden = run_test_command(
            case.hidden_test_command,
            cwd=repo_dir,
            env=test_env,
            timeout_s=effective_config.timeout_s,
            output_path=cell_dir / "hidden_test.json",
        )
        hidden_outcome = hidden.outcome
    else:
        hidden_outcome = "n/a"

    # Step 5 — edit constraint check.
    constraints = _resolve_edit_constraints(case.edit_constraints)
    edit_constraint_compliance = check_edit_constraints(
        diff_summary["changed_files"], constraints
    )

    # Step 6 — venv mutation check.
    if case_venv_path.exists():
        venv_hash_after = compute_venv_fingerprint(case_venv_path)
    else:
        venv_hash_after = venv_hash_before
    venv_mutated = venv_hash_after != venv_hash_before
    if venv_mutated:
        print(
            f"warning: case venv mutated during cell run: {case_venv_path}",
            file=sys.stderr,
        )

    # Step 7 — scoring + meta.
    scoring = assemble_scoring(
        schema_validity=schema_validity,
        visible_test_outcome=visible.outcome,
        hidden_test_outcome=hidden_outcome,
        edit_constraint_compliance=edit_constraint_compliance,
        diff_summary=diff_summary,
        latency_ms=runner_result.latency_ms,
        parsed_envelope=parsed,
    )

    if runner_result.error_reason is None:
        status = "ok"
    else:
        status = "error"

    write_meta_json(
        cell_dir,
        framework=framework.name,
        case_id=case.case_id,
        task_id=runner_result.task_id,
        model=effective_config.model,
        started_at=started_at_dt.isoformat(),
        ended_at=ended_at_dt.isoformat(),
        status=status,
        error_reason=runner_result.error_reason,
        exit_code=runner_result.exit_code,
        stdout_truncated=runner_result.stdout_truncated,
        stderr_truncated=runner_result.stderr_truncated,
        harness_latency_ms=runner_result.latency_ms,
        framework_reported_latency_ms=framework_reported_latency_ms,
        effective_config=effective_config,
        venv_hash_before=venv_hash_before,
        venv_hash_after=venv_hash_after,
        venv_mutated=venv_mutated,
        scoring=scoring,
    )
