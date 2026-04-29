import json
import os
import threading
import time
from pathlib import Path

import pytest

from evals.discovery import CaseSpec, FrameworkSpec
from evals.runner import (
    EffectiveConfig,
    resolve_effective_config,
    run_cell,
)

BASE_ENV = dict(os.environ)
DOTENV: dict[str, str] = {}


def _fake_framework_spec(fake_framework_dir: Path) -> FrameworkSpec:
    return FrameworkSpec(
        name="fake",
        dir=fake_framework_dir,
        manifest_path=fake_framework_dir / "manifest.json",
        entry="./run.py",
        setup=None,
        env_keys=["FAKE_BEHAVIOR"],
        model="fake",
    )


def _make_case(tmp_path: Path) -> CaseSpec:
    repo = tmp_path / "case-repo"
    repo.mkdir(exist_ok=True)
    return CaseSpec(
        case_id="test-case",
        manifest_path=tmp_path / "case.json",
        fixture_repo=repo,
        failing_test_command="pytest -q",
        hidden_test_command=None,
        failure_output="boom",
        edit_constraints={},
        notes=None,
    )


def _effective(timeout_s: int = 30) -> EffectiveConfig:
    return EffectiveConfig(
        model="fake",
        timeout_s=timeout_s,
        max_steps=10,
        sources={
            "model": "framework-manifest",
            "timeout_s": "harness-default",
            "max_steps": "harness-default",
        },
    )


def _run(tmp_path: Path, fake_framework_dir: Path, behavior: str, *, timeout_s: int = 30):
    fw = _fake_framework_spec(fake_framework_dir)
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    env = {**BASE_ENV, "FAKE_BEHAVIOR": behavior}
    return run_cell(
        framework=fw,
        case=case,
        effective_config=_effective(timeout_s=timeout_s),
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=tmp_path,
        base_env=env,
        dotenv=DOTENV,
    ), cell_dir


def test_runner_writes_request_json_before_spawn(fake_framework_dir, tmp_path):
    fw = _fake_framework_spec(fake_framework_dir)
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    env = {**BASE_ENV, "FAKE_BEHAVIOR": "hang"}

    result_holder: dict = {}

    def go():
        try:
            r, _ = _run(tmp_path, fake_framework_dir, "hang", timeout_s=2)
            result_holder["r"] = r
        except Exception as exc:
            result_holder["exc"] = exc

    # Run in background. The request.json must be written before spawn (synchronously).
    # Easiest: just call run_cell on a thread and poll for request.json existence quickly.
    t = threading.Thread(
        target=lambda: result_holder.setdefault(
            "r",
            run_cell(
                framework=fw,
                case=case,
                effective_config=_effective(timeout_s=2),
                cell_dir=cell_dir,
                cache_dir=cache_dir,
                repo_root=tmp_path,
                base_env=env,
                dotenv=DOTENV,
            ),
        ),
        daemon=True,
    )
    t.start()
    deadline = time.monotonic() + 2.0
    request_path = cell_dir / "request.json"
    while time.monotonic() < deadline and not request_path.exists():
        time.sleep(0.02)
    assert request_path.exists(), "request.json should be written before/while child runs"
    parsed = json.loads(request_path.read_text())
    assert parsed["input"]["case_id"] == "test-case"
    assert parsed["config"]["model"] == "fake"
    t.join(timeout=10)
    assert not t.is_alive()


def test_runner_classifies_success_noop_as_ok(fake_framework_dir, tmp_path):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "success-noop")
    assert result.error_reason is None
    assert result.exit_code == 0
    assert result.response_path == cell_dir / "response.json"
    assert (cell_dir / "response.json").exists()


def test_runner_classifies_crash_as_nonzero_exit(fake_framework_dir, tmp_path):
    result, _ = _run(tmp_path, fake_framework_dir, "crash")
    assert result.error_reason == "nonzero_exit"
    assert result.exit_code == 1


def test_runner_classifies_crash_with_error_envelope_writes_response_and_keeps_nonzero_exit(
    fake_framework_dir, tmp_path
):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "crash-with-error-envelope")
    assert result.error_reason == "nonzero_exit"
    assert result.exit_code == 1
    assert result.response_path == cell_dir / "response.json"
    assert (cell_dir / "response.json").exists()


def test_runner_classifies_crash_with_bad_json_does_not_write_response(
    fake_framework_dir, tmp_path
):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "crash-with-bad-json")
    assert result.error_reason == "nonzero_exit"
    assert result.exit_code == 1
    assert result.response_path is None
    assert not (cell_dir / "response.json").exists()


def test_runner_classifies_garbage_as_malformed_response_json(fake_framework_dir, tmp_path):
    result, _ = _run(tmp_path, fake_framework_dir, "garbage")
    assert result.error_reason == "malformed_response_json"
    assert result.exit_code == 0


def test_runner_classifies_empty_as_missing_response(fake_framework_dir, tmp_path):
    result, _ = _run(tmp_path, fake_framework_dir, "empty")
    assert result.error_reason == "missing_response"
    assert result.exit_code == 0


def test_runner_classifies_oversize_truncates_and_marks_malformed(
    fake_framework_dir, tmp_path
):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "oversize")
    assert result.stdout_truncated is True
    assert result.error_reason == "malformed_response_json"
    assert (cell_dir / "stdout.log").stat().st_size == 8 * 1024 * 1024


def test_runner_classifies_missing_field_as_envelope_schema_violation(
    fake_framework_dir, tmp_path
):
    result, _ = _run(tmp_path, fake_framework_dir, "missing-field")
    assert result.error_reason == "envelope_schema_violation"
    assert result.exit_code == 0


def test_runner_timeout_kills_and_reports_timeout(fake_framework_dir, tmp_path):
    result, _ = _run(tmp_path, fake_framework_dir, "hang", timeout_s=2)
    assert result.timed_out is True
    assert result.error_reason == "timeout"
    assert result.exit_code is None


def test_runner_noisy_stderr_truncates(fake_framework_dir, tmp_path):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "noisy-stderr")
    assert result.stderr_truncated is True
    assert (cell_dir / "stderr.log").stat().st_size == 5 * 1024 * 1024


def test_runner_misconfigured_when_entry_missing(tmp_path):
    fw_dir = tmp_path / "missing-fw"
    fw_dir.mkdir()
    fw = FrameworkSpec(
        name="missing-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./does-not-exist.py",
        setup=None,
        env_keys=[],
        model="fake",
    )
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_cell(
        framework=fw,
        case=case,
        effective_config=_effective(),
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=tmp_path,
        base_env=BASE_ENV,
        dotenv=DOTENV,
    )
    assert result.error_reason == "framework_misconfigured"
    assert result.framework_misconfigured_reason is not None
    assert (cell_dir / "stdout.log").exists()
    assert (cell_dir / "stdout.log").stat().st_size == 0
    assert (cell_dir / "stderr.log").exists()
    assert "framework_misconfigured" in (cell_dir / "stderr.log").read_text()


def test_runner_misconfigured_when_setup_fail_exists(fake_framework_dir, tmp_path):
    fw = _fake_framework_spec(fake_framework_dir)
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cache_dir = tmp_path / "cache"
    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / f"{fw.name}.fail").write_text('{"reason":"prior"}')

    result = run_cell(
        framework=fw,
        case=case,
        effective_config=_effective(),
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=tmp_path,
        base_env={**BASE_ENV, "FAKE_BEHAVIOR": "success-noop"},
        dotenv=DOTENV,
    )
    assert result.error_reason == "framework_misconfigured"
    # No subprocess spawned: stdout.log must be empty.
    assert (cell_dir / "stdout.log").stat().st_size == 0


def test_resolve_effective_config_per_field_sources(tmp_path):
    fw = FrameworkSpec(
        name="fw",
        dir=tmp_path,
        manifest_path=tmp_path / "manifest.json",
        entry="./x",
        setup=None,
        env_keys=[],
        model="manifest-model",
    )
    cfg = resolve_effective_config(
        fw,
        campaign_overrides={},
        cell_overrides={"timeout_s": 99},
        harness_defaults={},
    )
    assert cfg.model == "manifest-model"
    assert cfg.timeout_s == 99
    assert cfg.max_steps == 50
    assert cfg.sources["model"] == "framework-manifest"
    assert cfg.sources["timeout_s"] == "cell-flag"
    assert cfg.sources["max_steps"] == "harness-default"
