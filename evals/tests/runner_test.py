from dataclasses import replace
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from evals.discovery import CaseSpec, DiscoveryError, FrameworkSpec
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


def test_runner_timeout_covers_blocked_stdin_write(tmp_path):
    fw_dir = tmp_path / "never-read-fw"
    fw_dir.mkdir()
    pid_file = tmp_path / "never-read.pid"
    entry = fw_dir / "run.py"
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(os.environ['NEVER_READ_STDIN_PID_FILE']).write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(0.1)\n"
    )
    entry.chmod(0o755)
    fw = FrameworkSpec(
        name="never-read-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./run.py",
        setup=None,
        env_keys=["NEVER_READ_STDIN_PID_FILE"],
        model="fake",
    )
    case_repo = tmp_path / "large-case-repo"
    case_repo.mkdir()
    case = CaseSpec(
        case_id="large-stdin-case",
        manifest_path=tmp_path / "case.json",
        fixture_repo=case_repo,
        failing_test_command="pytest -q",
        hidden_test_command=None,
        failure_output="x" * (16 * 1024 * 1024),
        edit_constraints={},
        notes=None,
    )
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result_holder: dict = {}

    def run() -> None:
        try:
            result_holder["result"] = run_cell(
                framework=fw,
                case=case,
                effective_config=_effective(timeout_s=1),
                cell_dir=cell_dir,
                cache_dir=cache_dir,
                repo_root=tmp_path,
                base_env={**BASE_ENV, "NEVER_READ_STDIN_PID_FILE": str(pid_file)},
                dotenv=DOTENV,
            )
        except BaseException as exc:
            result_holder["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    returned_under_timeout = not t.is_alive()
    if not returned_under_timeout and pid_file.exists():
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
        t.join(timeout=2)

    assert returned_under_timeout, (
        "run_cell should enforce timeout while writing a large request to child stdin"
    )
    assert "exc" not in result_holder
    result = result_holder["result"]
    assert result.timed_out is True
    assert result.error_reason == "timeout"
    assert result.exit_code is None


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


def test_runner_returns_when_background_descendant_holds_stdout_pipe(tmp_path):
    fw_dir = tmp_path / "pipe-holder-fw"
    fw_dir.mkdir()
    entry = fw_dir / "run.py"
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        "request = json.load(sys.stdin)\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'])\n"
        "json.dump({\n"
        "    'task_id': request['task_id'],\n"
        "    'output': {\n"
        "        'root_cause': 'rc',\n"
        "        'summary': 's',\n"
        "        'changed_files': [],\n"
        "        'tests_run': [],\n"
        "        'evidence': 'e',\n"
        "        'confidence': 1.0,\n"
        "    },\n"
        "    'trace': {'steps': [], 'tokens': {'input': 1, 'output': 1}, 'latency_ms': 1},\n"
        "    'error': None,\n"
        "}, sys.stdout)\n"
        "sys.stdout.flush()\n"
    )
    entry.chmod(0o755)
    fw = FrameworkSpec(
        name="pipe-holder-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./run.py",
        setup=None,
        env_keys=[],
        model="fake",
    )
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    start = time.monotonic()
    result = run_cell(
        framework=fw,
        case=case,
        effective_config=_effective(timeout_s=1),
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=tmp_path,
        base_env=BASE_ENV,
        dotenv=DOTENV,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.5, "run_cell should not wait for a pipe-holding background child"
    assert result.error_reason is None
    assert result.exit_code == 0
    assert result.response_path == cell_dir / "response.json"


def test_runner_marks_timeout_when_detached_descendant_keeps_stdout_pipe_open(tmp_path):
    fw_dir = tmp_path / "detached-pipe-holder-fw"
    fw_dir.mkdir()
    pid_file = tmp_path / "detached-child.pid"
    entry = fw_dir / "run.py"
    child_code = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    entry.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import subprocess\n"
        "import sys\n"
        "request = json.load(sys.stdin)\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True)\n"
        "json.dump({\n"
        "    'task_id': request['task_id'],\n"
        "    'output': {\n"
        "        'root_cause': 'rc',\n"
        "        'summary': 's',\n"
        "        'changed_files': [],\n"
        "        'tests_run': [],\n"
        "        'evidence': 'e',\n"
        "        'confidence': 1.0,\n"
        "    },\n"
        "    'trace': {'steps': [], 'tokens': {'input': 1, 'output': 1}, 'latency_ms': 1},\n"
        "    'error': None,\n"
        "}, sys.stdout)\n"
        "sys.stdout.flush()\n"
    )
    entry.chmod(0o755)
    fw = FrameworkSpec(
        name="detached-pipe-holder-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./run.py",
        setup=None,
        env_keys=[],
        model="fake",
    )
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result_holder: dict = {}

    def run() -> None:
        try:
            result_holder["result"] = run_cell(
                framework=fw,
                case=case,
                effective_config=_effective(timeout_s=1),
                cell_dir=cell_dir,
                cache_dir=cache_dir,
                repo_root=tmp_path,
                base_env=BASE_ENV,
                dotenv=DOTENV,
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
        "run_cell should not hang when a detached descendant keeps stdout open"
    )
    assert "exc" not in result_holder
    result = result_holder["result"]
    assert result.timed_out is True
    assert result.error_reason == "timeout"
    assert result.exit_code is None


def test_runner_timeout_terminates_framework_process_tree(tmp_path, process_tree_probe):
    fw_dir = tmp_path / "process-tree-fw"
    fw_dir.mkdir()
    fw = FrameworkSpec(
        name="process-tree-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry=process_tree_probe.shell_command(),
        setup=None,
        env_keys=["GRANDCHILD_PID_FILE", "GRANDCHILD_TERM_FILE"],
        model="fake",
    )
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    (cell_dir / "repo").mkdir(parents=True)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    result = run_cell(
        framework=fw,
        case=case,
        effective_config=_effective(timeout_s=1),
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=tmp_path,
        base_env={
            **BASE_ENV,
            "GRANDCHILD_PID_FILE": str(process_tree_probe.grandchild_pid_path),
            "GRANDCHILD_TERM_FILE": str(process_tree_probe.grandchild_term_path),
        },
        dotenv=DOTENV,
    )

    assert result.error_reason == "timeout"
    assert process_tree_probe.wait_for_grandchild_exit()
    assert process_tree_probe.grandchild_term_path.exists()


def test_runner_noisy_stderr_truncates(fake_framework_dir, tmp_path):
    result, cell_dir = _run(tmp_path, fake_framework_dir, "noisy-stderr")
    assert result.stderr_truncated is True
    assert (cell_dir / "stderr.log").stat().st_size == 5 * 1024 * 1024


def test_runner_entry_allows_path_resolved_interpreter_commands(tmp_path):
    fw_dir = tmp_path / "path-fw"
    fw_dir.mkdir()
    (fw_dir / "run.py").write_text(
        "import json\n"
        "import sys\n"
        "request = json.load(sys.stdin)\n"
        "json.dump({\n"
        "    'task_id': request['task_id'],\n"
        "    'output': {\n"
        "        'root_cause': 'rc',\n"
        "        'summary': 's',\n"
        "        'changed_files': [],\n"
        "        'tests_run': [],\n"
        "        'evidence': 'e',\n"
        "        'confidence': 1.0,\n"
        "    },\n"
        "    'trace': {'steps': [], 'tokens': {'input': 1, 'output': 1}, 'latency_ms': 1},\n"
        "    'error': None,\n"
        "}, sys.stdout)\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    fw = FrameworkSpec(
        name="path-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="python run.py",
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
        base_env={**BASE_ENV, "PATH": str(bin_dir)},
        dotenv=DOTENV,
    )

    assert result.error_reason is None
    assert result.exit_code == 0
    assert (cell_dir / "response.json").exists()


def test_runner_misconfigured_when_entry_executable_but_unspawnable(tmp_path):
    fw_dir = tmp_path / "bad-fw"
    fw_dir.mkdir()
    bad_entry = fw_dir / "bad-entry"
    bad_entry.write_text("#!/definitely/missing/interpreter\n")
    bad_entry.chmod(0o755)
    fw = FrameworkSpec(
        name="bad-fw",
        dir=fw_dir,
        manifest_path=fw_dir / "manifest.json",
        entry="./bad-entry",
        setup=None,
        env_keys=[],
        model="fake",
    )
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    try:
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
    except OSError as exc:
        pytest.fail(f"run_cell should classify unspawnable entries, not raise: {exc}")

    assert result.error_reason == "framework_misconfigured"
    assert result.framework_misconfigured_reason is not None
    assert "failed to spawn entry" in result.framework_misconfigured_reason
    assert result.exit_code is None
    assert (cell_dir / "stdout.log").stat().st_size == 0
    assert "framework_misconfigured" in (cell_dir / "stderr.log").read_text()


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


def test_runner_misconfigured_when_framework_has_discovery_error(tmp_path):
    """A FrameworkSpec carrying a discovery_error must short-circuit to
    framework_misconfigured without spawning a subprocess."""
    fw_dir = tmp_path / "broken-fw"
    fw_dir.mkdir()
    manifest = fw_dir / "manifest.json"
    err = DiscoveryError(
        kind="framework",
        name="broken-fw",
        manifest_path=manifest,
        messages=["invalid JSON: Expecting value"],
    )
    fw = FrameworkSpec(
        name="broken-fw",
        dir=fw_dir,
        manifest_path=manifest,
        entry="",
        setup=None,
        env_keys=[],
        model="",
        discovery_error=err,
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
    assert "invalid JSON" in result.framework_misconfigured_reason
    assert (cell_dir / "stdout.log").stat().st_size == 0
    assert "framework_misconfigured" in (cell_dir / "stderr.log").read_text()


def test_runner_misconfigured_when_setup_fail_exists(fake_framework_dir, tmp_path):
    fw = replace(_fake_framework_spec(fake_framework_dir), setup="./setup.sh")
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


def test_runner_ignores_stale_setup_fail_when_framework_declares_no_setup(
    fake_framework_dir, tmp_path
):
    fw = _fake_framework_spec(fake_framework_dir)
    case = _make_case(tmp_path)
    cell_dir = tmp_path / "cell"
    cell_dir.mkdir()
    cache_dir = tmp_path / "cache"
    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / f"{fw.name}.fail").write_text('{"reason":"old setup removed"}')

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

    assert result.error_reason is None
    assert result.exit_code == 0
    assert (cell_dir / "response.json").exists()


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


def test_resolve_effective_config_treats_none_overrides_as_absent(tmp_path):
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
        campaign_overrides={"model": None, "timeout_s": None},
        cell_overrides={"max_steps": None},
        harness_defaults={"timeout_s": 123, "max_steps": 456},
    )

    assert cfg.model == "manifest-model"
    assert cfg.timeout_s == 123
    assert cfg.max_steps == 456
    assert cfg.sources["model"] == "framework-manifest"
    assert cfg.sources["timeout_s"] == "harness-default"
    assert cfg.sources["max_steps"] == "harness-default"
