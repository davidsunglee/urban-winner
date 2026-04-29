import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import evals.pipeline as pipeline_module
from evals.discovery import CaseSpec, FrameworkSpec, discover_cases
from evals.pipeline import (
    assemble_scoring,
    check_edit_constraints,
    derive_canonical_diff,
    run_pipeline,
    run_test_command,
    write_meta_json,
)
from evals.runner import EffectiveConfig, RunnerResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_init_commit(repo: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@local",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@local",
    }
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@local"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"],
        cwd=str(repo), check=True, capture_output=True, env=env,
    )


def _make_buggy_worktree(tmp_path: Path) -> Path:
    """Create cell_dir/repo with a buggy `add` (returns a-b) committed at HEAD."""
    cell_dir = tmp_path / "cell"
    repo = cell_dir / "repo"
    repo.mkdir(parents=True)
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .arith import add\n")
    (pkg / "arith.py").write_text("def add(a, b):\n    return a - b\n")
    _git_init_commit(repo)
    return cell_dir


def _apply_fix(cell_dir: Path) -> None:
    (cell_dir / "repo" / "pkg" / "arith.py").write_text("def add(a, b):\n    return a + b\n")


# Command that exercises the buggy/fixed file directly without needing pytest installed.
_VERIFY_ADD_CMD = (
    "python3 -c \"import sys; sys.path.insert(0, '.'); "
    "from pkg.arith import add; "
    "sys.exit(0 if add(2, 3) == 5 else 1)\""
)


def _make_case(
    *,
    failing_test_command: str = _VERIFY_ADD_CMD,
    hidden_test_command: str | None = None,
    edit_constraints: dict | None = None,
    case_id: str = "synthetic",
) -> CaseSpec:
    return CaseSpec(
        case_id=case_id,
        manifest_path=Path("/tmp/case.json"),
        fixture_repo=Path("/tmp/fixture"),
        failing_test_command=failing_test_command,
        hidden_test_command=hidden_test_command,
        failure_output="boom",
        edit_constraints=edit_constraints or {},
        notes=None,
    )


def _make_framework(tmp_path: Path) -> FrameworkSpec:
    fw_dir = tmp_path / "fw"
    fw_dir.mkdir(exist_ok=True)
    return FrameworkSpec(
        name="synthetic-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./run.sh",
        setup=None,
        env_keys=[],
        model="synthetic-model",
    )


def _make_effective_config() -> EffectiveConfig:
    return EffectiveConfig(
        model="synthetic-model",
        timeout_s=30,
        max_steps=10,
        sources={
            "model": "framework-manifest",
            "timeout_s": "harness-default",
            "max_steps": "harness-default",
        },
    )


def _make_runner_result(
    cell_dir: Path,
    *,
    error_reason: str | None = None,
    exit_code: int | None = 0,
    has_response: bool = True,
    stdout_obj: object | None = None,
) -> RunnerResult:
    stdout_path = cell_dir / "stdout.log"
    stderr_path = cell_dir / "stderr.log"
    response_path = cell_dir / "response.json"

    if stdout_obj is not None:
        stdout_path.write_text(json.dumps(stdout_obj))
    elif has_response:
        envelope = {
            "task_id": "T",
            "output": {
                "root_cause": "rc",
                "summary": "s",
                "changed_files": ["pkg/arith.py"],
                "tests_run": [],
                "evidence": "e",
                "confidence": 0.9,
            },
            "trace": {"steps": [], "tokens": {"input": 10, "output": 20}, "latency_ms": 100},
            "error": None,
        }
        stdout_path.write_text(json.dumps(envelope))
    else:
        stdout_path.write_text("")
    if not stderr_path.exists():
        stderr_path.write_text("")

    return RunnerResult(
        task_id="synthetic-fw:synthetic:abc",
        exit_code=exit_code,
        timed_out=False,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_truncated=False,
        stderr_truncated=False,
        response_path=response_path if has_response else None,
        error_reason=error_reason,
        latency_ms=123,
        framework_misconfigured_reason=None,
    )


# ---------------------------------------------------------------------------
# derive_canonical_diff
# ---------------------------------------------------------------------------

def test_diff_does_not_modify_real_index(tmp_path):
    cell_dir = _make_buggy_worktree(tmp_path)
    repo = cell_dir / "repo"

    # Mutate the worktree
    (repo / "pkg" / "arith.py").write_text("def add(a, b):\n    return a + b\n")

    summary = derive_canonical_diff(cell_dir)
    assert "pkg/arith.py" in summary["changed_files"]
    assert summary["added"] >= 1 and summary["removed"] >= 1
    assert (cell_dir / "diff.patch").exists()

    # Real index must be untouched: `git diff --cached HEAD` should be empty
    cached = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "HEAD", "--name-only"],
        capture_output=True, text=True, check=True,
    )
    assert cached.stdout.strip() == ""


# ---------------------------------------------------------------------------
# run_test_command
# ---------------------------------------------------------------------------

def test_visible_test_outcome_pass(tmp_path):
    cell_dir = _make_buggy_worktree(tmp_path)
    _apply_fix(cell_dir)
    result = run_test_command(
        _VERIFY_ADD_CMD,
        cwd=cell_dir / "repo",
        env={**os.environ},
        timeout_s=30,
        output_path=cell_dir / "visible_test.json",
    )
    assert result.outcome == "pass"
    assert result.exit_code == 0
    payload = json.loads((cell_dir / "visible_test.json").read_text())
    assert payload["outcome"] == "pass"


def test_visible_test_outcome_fail(tmp_path):
    cell_dir = _make_buggy_worktree(tmp_path)
    result = run_test_command(
        _VERIFY_ADD_CMD,
        cwd=cell_dir / "repo",
        env={**os.environ},
        timeout_s=30,
    )
    assert result.outcome == "fail"
    assert result.exit_code == 1


def test_visible_test_outcome_error_on_timeout(tmp_path):
    result = run_test_command(
        "sleep 30",
        cwd=tmp_path,
        env={**os.environ},
        timeout_s=1,
    )
    assert result.outcome == "error"
    assert result.exit_code is None


def test_test_command_returns_when_background_descendant_holds_stdout_pipe(tmp_path):
    command = shlex.join(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); "
                "raise SystemExit(0)"
            ),
        ]
    )

    start = time.monotonic()
    result = run_test_command(
        command,
        cwd=tmp_path,
        env={**os.environ},
        timeout_s=1,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.5, "run_test_command should not wait for a pipe-holding background child"
    assert result.outcome == "pass"
    assert result.exit_code == 0


def test_test_command_marks_error_when_detached_descendant_keeps_stdout_pipe_open(tmp_path):
    pid_file = tmp_path / "detached-test-child.pid"
    child_code = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = shlex.join(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
                "raise SystemExit(0)"
            ),
        ]
    )
    result_holder: dict = {}

    def run() -> None:
        try:
            result_holder["result"] = run_test_command(
                command,
                cwd=tmp_path,
                env={**os.environ},
                timeout_s=1,
            )
        except BaseException as exc:
            result_holder["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=4)
    returned_under_timeout = not t.is_alive()
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not returned_under_timeout:
        t.join(timeout=2)

    assert returned_under_timeout, (
        "run_test_command should not hang when a detached descendant keeps stdout open"
    )
    assert "exc" not in result_holder
    result = result_holder["result"]
    assert result.outcome == "error"
    assert result.exit_code is None


def test_test_command_timeout_terminates_process_tree(tmp_path, process_tree_probe):
    result = run_test_command(
        process_tree_probe.shell_command(),
        cwd=tmp_path,
        env={
            **os.environ,
            "GRANDCHILD_PID_FILE": str(process_tree_probe.grandchild_pid_path),
            "GRANDCHILD_TERM_FILE": str(process_tree_probe.grandchild_term_path),
        },
        timeout_s=1,
    )

    assert result.outcome == "error"
    assert process_tree_probe.wait_for_grandchild_exit()
    assert process_tree_probe.grandchild_term_path.exists()


def test_visible_test_output_caps_and_drains(tmp_path):
    # Generate ~6 MiB of stdout
    cmd = (
        "python3 -c \"import sys; sys.stdout.buffer.write(b'x' * (6 * 1024 * 1024))\""
    )
    result = run_test_command(
        cmd,
        cwd=tmp_path,
        env={**os.environ},
        timeout_s=30,
    )
    assert result.outcome == "pass"
    assert result.stdout_truncated is True


def test_test_command_preserves_declared_shell_command_with_redirection(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pytest_exe = bin_dir / "pytest"
    pytest_exe.write_text("#!/bin/sh\nprintf 'from-shell:%s\\n' \"$*\"\n")
    pytest_exe.chmod(0o755)
    output_path = tmp_path / "visible_test.json"

    result = run_test_command(
        "pytest -q > shell-output.txt",
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        timeout_s=30,
        output_path=output_path,
    )

    payload = json.loads(output_path.read_text())
    assert result.outcome == "pass"
    assert (tmp_path / "shell-output.txt").read_text() == "from-shell:-q\n"
    assert payload["original_command"] == "pytest -q > shell-output.txt"
    assert payload["effective_command"] == "pytest -q > shell-output.txt"
    assert payload["command"] == "pytest -q > shell-output.txt"


def test_test_command_artifact_records_declared_command_as_effective(tmp_path):
    output_path = tmp_path / "visible_test.json"

    result = run_test_command(
        "printf ok",
        cwd=tmp_path,
        env={**os.environ},
        timeout_s=30,
        output_path=output_path,
    )

    payload = json.loads(output_path.read_text())
    assert result.outcome == "pass"
    assert payload["original_command"] == "printf ok"
    assert payload["effective_command"] == "printf ok"
    assert payload["command"] == "printf ok"


def test_uv_run_pytest_command_is_executed_as_declared(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_exe = bin_dir / "uv"
    uv_exe.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > uv-args.txt\n")
    uv_exe.chmod(0o755)

    result = run_test_command(
        "uv run pytest -q sentinel_test.py",
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        timeout_s=30,
    )

    assert result.outcome == "pass"
    assert result.exit_code == 0
    assert (tmp_path / "uv-args.txt").read_text() == "run pytest -q sentinel_test.py\n"
    assert result.original_command == "uv run pytest -q sentinel_test.py"
    assert result.effective_command == "uv run pytest -q sentinel_test.py"


# ---------------------------------------------------------------------------
# check_edit_constraints
# ---------------------------------------------------------------------------

def test_edit_constraint_disallowed_paths_default_blocks_tests():
    constraints = {
        "disallowed_paths": ["tests/**", "**/*test*"],
        "max_changed_files": 5,
    }
    out = check_edit_constraints(["pkg/arith.py", "tests/foo.py"], constraints)
    assert "tests/foo.py" in out["disallowed_violations"]
    assert out["over_max_changed_files"] is False


def test_edit_constraint_max_files_over_threshold():
    constraints = {"disallowed_paths": [], "max_changed_files": 5}
    files = [f"pkg/file{i}.py" for i in range(6)]
    out = check_edit_constraints(files, constraints)
    assert out["over_max_changed_files"] is True


def test_pytest_case_constraints_allow_real_fix_but_still_block_tests(repo_root):
    cases, errors = discover_cases(repo_root)
    assert errors == []
    case = next(c for c in cases if c.case_id == "pytest-dev__pytest-7571")
    constraints = pipeline_module._resolve_edit_constraints(case.edit_constraints)

    out = check_edit_constraints(
        ["src/_pytest/logging.py", "testing/logging/test_fixture.py"], constraints
    )

    assert "src/_pytest/logging.py" not in out["disallowed_violations"]
    assert "testing/logging/test_fixture.py" in out["disallowed_violations"]


# ---------------------------------------------------------------------------
# assemble_scoring
# ---------------------------------------------------------------------------

def test_assemble_scoring_includes_n_a_for_hidden_when_absent(tmp_path):
    # Build a minimal scenario via run_pipeline against a worktree without hidden test
    cell_dir = _make_buggy_worktree(tmp_path)
    _apply_fix(cell_dir)
    case = _make_case(hidden_test_command=None)
    fw = _make_framework(tmp_path)
    cfg = _make_effective_config()
    rr = _make_runner_result(cell_dir)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    run_pipeline(
        cell_dir, rr,
        framework=fw, case=case, effective_config=cfg,
        cache_dir=cache_dir, base_env=dict(os.environ),
        venv_hash_before="ZZZ",
    )

    scoring = json.loads((cell_dir / "scoring.json").read_text())
    assert scoring["hidden_test_outcome"] == "n/a"
    assert not (cell_dir / "hidden_test.json").exists()


def test_assemble_scoring_token_usage_omitted_when_response_absent():
    scoring = assemble_scoring(
        schema_validity=False,
        visible_test_outcome="fail",
        hidden_test_outcome="n/a",
        edit_constraint_compliance={
            "disallowed_violations": [], "allowed_violations": [],
            "over_max_changed_files": False,
        },
        diff_summary={"changed_files": [], "added": 0, "removed": 0},
        latency_ms=42,
        parsed_envelope=None,
        response_present=False,
    )
    assert "token_usage" not in scoring


def test_pipeline_omits_token_usage_when_runner_rejected_envelope(tmp_path):
    cell_dir = _make_buggy_worktree(tmp_path)
    _apply_fix(cell_dir)
    case = _make_case(hidden_test_command=None)
    fw = _make_framework(tmp_path)
    cfg = _make_effective_config()
    rejected_envelope = {
        "task_id": "T",
        "output": {
            "root_cause": "rc",
            "summary": "s",
            "changed_files": ["pkg/arith.py"],
            "tests_run": [],
            "evidence": "e",
            "confidence": 0.9,
        },
        "trace": {
            "steps": [],
            "tokens": {"input": 10, "output": 20},
            "latency_ms": 100,
        },
        # Missing required "error" key means the runner does not write response.json.
    }
    rr = _make_runner_result(
        cell_dir,
        error_reason="envelope_schema_violation",
        has_response=False,
        stdout_obj=rejected_envelope,
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    run_pipeline(
        cell_dir, rr,
        framework=fw, case=case, effective_config=cfg,
        cache_dir=cache_dir, base_env=dict(os.environ),
        venv_hash_before="ZZZ",
    )

    assert not (cell_dir / "response.json").exists()
    scoring = json.loads((cell_dir / "scoring.json").read_text())
    assert "token_usage" not in scoring


# ---------------------------------------------------------------------------
# meta.json
# ---------------------------------------------------------------------------

def test_meta_json_is_atomic_temp_and_rename(tmp_path, monkeypatch):
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cfg = _make_effective_config()

    rename_calls: list[tuple[str, str]] = []
    real_rename = os.rename

    def spy_rename(src, dst):
        rename_calls.append((str(src), str(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", spy_rename)

    write_meta_json(
        cell_dir,
        framework="fw", case_id="c", task_id="t",
        model="m", started_at="s", ended_at="e",
        status="ok", error_reason=None, exit_code=0,
        stdout_truncated=False, stderr_truncated=False,
        harness_latency_ms=10, framework_reported_latency_ms=5,
        effective_config=cfg,
        venv_hash_before="A", venv_hash_after="A", venv_mutated=False,
        scoring={"schema_validity": True},
    )

    meta_renames = [
        (s, d) for (s, d) in rename_calls if d.endswith("/meta.json")
    ]
    assert meta_renames, "meta.json not written via rename"
    src, dst = meta_renames[0]
    assert src.endswith("/meta.json.tmp")
    assert dst.endswith("/meta.json")
    assert (cell_dir / "meta.json").exists()
    assert (cell_dir / "scoring.json").exists()


def test_meta_ended_at_is_captured_after_pipeline_steps(tmp_path, monkeypatch):
    class FakeDateTime:
        calls = 0

        @classmethod
        def now(cls, tz):
            instants = [
                datetime(2024, 1, 1, 0, 0, 0, tzinfo=tz),
                datetime(2024, 1, 1, 0, 0, 1, tzinfo=tz),
            ]
            instant = instants[min(cls.calls, len(instants) - 1)]
            cls.calls += 1
            return instant

        @classmethod
        def fromtimestamp(cls, timestamp, tz):
            return datetime.fromtimestamp(timestamp, tz=tz)

    monkeypatch.setattr(pipeline_module, "datetime", FakeDateTime)

    cell_dir = _make_buggy_worktree(tmp_path)
    _apply_fix(cell_dir)
    case = _make_case(hidden_test_command=None)
    fw = _make_framework(tmp_path)
    cfg = _make_effective_config()
    rr = _make_runner_result(cell_dir)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    run_pipeline(
        cell_dir, rr,
        framework=fw, case=case, effective_config=cfg,
        cache_dir=cache_dir, base_env=dict(os.environ),
        venv_hash_before="ZZZ",
    )

    meta = json.loads((cell_dir / "meta.json").read_text())
    assert meta["ended_at"] == "2024-01-01T00:00:01+00:00"
    assert FakeDateTime.calls >= 2


def test_meta_json_records_per_field_config_sources(tmp_path):
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cfg = EffectiveConfig(
        model="m1", timeout_s=99, max_steps=7,
        sources={
            "model": "framework-manifest",
            "timeout_s": "cell-flag",
            "max_steps": "campaign",
        },
    )
    write_meta_json(
        cell_dir,
        framework="fw", case_id="c", task_id="t",
        model="m1", started_at="s", ended_at="e",
        status="ok", error_reason=None, exit_code=0,
        stdout_truncated=False, stderr_truncated=False,
        harness_latency_ms=10, framework_reported_latency_ms=None,
        effective_config=cfg,
        venv_hash_before="A", venv_hash_after="A", venv_mutated=False,
        scoring={},
    )
    meta = json.loads((cell_dir / "meta.json").read_text())
    eff = meta["effective_config"]
    assert eff["model"] == "m1"
    assert eff["sources"]["model"] == "framework-manifest"
    assert eff["sources"]["timeout_s"] == "cell-flag"
    assert eff["sources"]["max_steps"] == "campaign"


# ---------------------------------------------------------------------------
# Orchestrator: framework_misconfigured
# ---------------------------------------------------------------------------

def test_pipeline_failure_writes_scoring_and_meta_artifacts(tmp_path):
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    case = _make_case(hidden_test_command=None)
    fw = _make_framework(tmp_path)
    cfg = _make_effective_config()
    rr = _make_runner_result(cell_dir)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    run_pipeline(
        cell_dir,
        rr,
        framework=fw,
        case=case,
        effective_config=cfg,
        cache_dir=cache_dir,
        base_env=dict(os.environ),
        venv_hash_before="HASH0",
    )

    scoring = json.loads((cell_dir / "scoring.json").read_text())
    meta = json.loads((cell_dir / "meta.json").read_text())
    assert meta["status"] == "error"
    assert meta["error_reason"] == "pipeline_failure"
    assert "pipeline_error" in scoring
    assert "repo" in scoring["pipeline_error"]["message"]


def test_pipeline_runs_against_pristine_for_framework_misconfigured(tmp_path):
    cell_dir = _make_buggy_worktree(tmp_path)  # pristine, agent never touched it
    case = _make_case(hidden_test_command=None)
    fw = _make_framework(tmp_path)
    cfg = _make_effective_config()

    # Empty stdout (framework_misconfigured runner writes empty stdout.log)
    rr = _make_runner_result(cell_dir, error_reason="framework_misconfigured", has_response=False)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    run_pipeline(
        cell_dir, rr,
        framework=fw, case=case, effective_config=cfg,
        cache_dir=cache_dir, base_env=dict(os.environ),
        venv_hash_before="HASH0",
    )

    # All artifacts present
    assert (cell_dir / "diff.patch").exists()
    assert (cell_dir / "diff.patch").read_bytes() == b""  # pristine: no diff
    assert (cell_dir / "visible_test.json").exists()
    assert (cell_dir / "scoring.json").exists()
    assert (cell_dir / "meta.json").exists()

    visible = json.loads((cell_dir / "visible_test.json").read_text())
    # Buggy fixture means visible test fails
    assert visible["outcome"] == "fail"

    scoring = json.loads((cell_dir / "scoring.json").read_text())
    assert scoring["schema_validity"] is False
    assert scoring["visible_test_outcome"] == "fail"
    assert scoring["hidden_test_outcome"] == "n/a"

    meta = json.loads((cell_dir / "meta.json").read_text())
    assert meta["status"] == "error"
    assert meta["error_reason"] == "framework_misconfigured"
