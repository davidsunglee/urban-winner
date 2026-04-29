import json
import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

import evals.setup as setup_mod
from evals.discovery import FrameworkSpec
from evals.setup import (
    SetupResult,
    is_setup_failed,
    is_setup_ok,
    run_all_setups,
    run_framework_setup,
)

BASE_ENV = dict(os.environ)
DOTENV: dict[str, str] = {}


def make_spec(
    tmp_path: Path,
    *,
    setup: str | None = None,
    name: str = "test-fw",
) -> FrameworkSpec:
    fw_dir = tmp_path / name
    fw_dir.mkdir(parents=True, exist_ok=True)
    manifest = fw_dir / "manifest.json"
    manifest.write_text(json.dumps({"entry": "entry.py", "env": [], "model": "claude-3"}))
    return FrameworkSpec(
        name=name,
        dir=fw_dir,
        manifest_path=manifest,
        entry="entry.py",
        setup=setup,
        env_keys=[],
        model="claude-3",
    )


def test_run_framework_setup_skipped_when_no_setup_field(tmp_path):
    spec = make_spec(tmp_path, setup=None)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "skipped"
    setup_dir = cache_dir / "setup"
    assert not (setup_dir / f"{spec.name}.ok").exists()
    assert not (setup_dir / f"{spec.name}.fail").exists()


def test_run_framework_setup_writes_ok_on_exit_0(tmp_path):
    spec = make_spec(tmp_path, setup='sh -c "exit 0"')
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "ok"
    assert (cache_dir / "setup" / f"{spec.name}.ok").exists()
    assert not (cache_dir / "setup" / f"{spec.name}.fail").exists()


def test_run_framework_setup_writes_fail_on_exit_nonzero(tmp_path):
    spec = make_spec(tmp_path, setup='sh -c "exit 7"')
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "failed"
    assert result.exit_code == 7
    assert (cache_dir / "setup" / f"{spec.name}.fail").exists()
    fail_data = json.loads((cache_dir / "setup" / f"{spec.name}.fail").read_text())
    assert fail_data["exit_code"] == 7
    assert fail_data["fingerprint"] == setup_mod.setup_fingerprint(spec)
    assert not (cache_dir / "setup" / f"{spec.name}.ok").exists()


def test_run_framework_setup_timeout(tmp_path):
    spec = make_spec(tmp_path, setup='sh -c "sleep 30"')
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=2
    )
    assert result.status == "failed"
    assert result.reason == "timeout"
    assert (cache_dir / "setup" / f"{spec.name}.fail").exists()
    fail_data = json.loads((cache_dir / "setup" / f"{spec.name}.fail").read_text())
    assert fail_data["reason"] == "timeout"
    assert not (cache_dir / "setup" / f"{spec.name}.ok").exists()


def test_run_framework_setup_returns_when_background_descendant_holds_stdout_pipe(tmp_path):
    spec = make_spec(
        tmp_path,
        setup=shlex.join(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); "
                    "raise SystemExit(0)"
                ),
            ]
        ),
        name="pipe-holder-setup",
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    start = time.monotonic()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=1
    )
    elapsed = time.monotonic() - start

    assert elapsed < 2.5, "setup should not wait for a pipe-holding background child"
    assert result.status == "ok"
    assert result.exit_code == 0


def test_run_framework_setup_marks_timeout_when_detached_descendant_keeps_stdout_pipe_open(tmp_path):
    pid_file = tmp_path / "detached-setup-child.pid"
    child_code = (
        "import os, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(30)"
    )
    spec = make_spec(
        tmp_path,
        setup=shlex.join(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys; "
                    f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True); "
                    "raise SystemExit(0)"
                ),
            ]
        ),
        name="detached-pipe-holder-setup",
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result_holder: dict = {}

    def run() -> None:
        try:
            result_holder["result"] = run_framework_setup(
                spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=1
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
        "setup should not hang when a detached descendant keeps stdout open"
    )
    assert "exc" not in result_holder
    result = result_holder["result"]
    assert result.status == "failed"
    assert result.reason == "timeout"
    assert result.exit_code is None


def test_run_framework_setup_timeout_terminates_process_tree(tmp_path, process_tree_probe):
    spec = make_spec(
        tmp_path,
        setup=process_tree_probe.shell_command(),
        name="process-tree-setup",
    )
    spec = FrameworkSpec(
        name=spec.name,
        dir=spec.dir,
        manifest_path=spec.manifest_path,
        entry=spec.entry,
        setup=spec.setup,
        env_keys=["GRANDCHILD_PID_FILE", "GRANDCHILD_TERM_FILE"],
        model=spec.model,
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    result = run_framework_setup(
        spec,
        cache_dir=cache_dir,
        base_env={
            **BASE_ENV,
            "GRANDCHILD_PID_FILE": str(process_tree_probe.grandchild_pid_path),
            "GRANDCHILD_TERM_FILE": str(process_tree_probe.grandchild_term_path),
        },
        dotenv=DOTENV,
        timeout_s=1,
    )

    assert result.status == "failed"
    assert result.reason == "timeout"
    assert process_tree_probe.wait_for_grandchild_exit()
    assert process_tree_probe.grandchild_term_path.exists()


def test_run_framework_setup_truncates_oversize_stdout(tmp_path):
    # Script writes 6 MiB to stdout; harness should cap at 5 MiB
    spec = make_spec(
        tmp_path,
        setup=(
            "python3 -c \""
            "import sys; "
            "sys.stdout.buffer.write(b'x' * 6 * 1024 * 1024); "
            "sys.stdout.buffer.flush()"
            "\""
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    log = cache_dir / "setup" / f"{spec.name}.stdout.log"
    assert log.stat().st_size == 5 * 1024 * 1024
    assert result.stdout_truncated is True


def test_run_framework_setup_persists_truncation_flags_in_ok_sentinel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(setup_mod, "_CAP_BYTES", 4)
    spec = make_spec(
        tmp_path,
        setup=shlex.join(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('abcdef'); sys.stdout.flush()",
            ]
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )

    assert result.status == "ok"
    assert result.stdout_truncated is True
    data = json.loads((cache_dir / "setup" / f"{spec.name}.ok").read_text())
    assert data["stdout_truncated"] is True
    assert data["stderr_truncated"] is False


def test_run_framework_setup_persists_truncation_flags_in_fail_sentinel(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(setup_mod, "_CAP_BYTES", 4)
    spec = make_spec(
        tmp_path,
        setup=shlex.join(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stderr.write('abcdef'); "
                    "sys.stderr.flush(); "
                    "raise SystemExit(7)"
                ),
            ]
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )

    assert result.status == "failed"
    assert result.stderr_truncated is True
    data = json.loads((cache_dir / "setup" / f"{spec.name}.fail").read_text())
    assert data["stdout_truncated"] is False
    assert data["stderr_truncated"] is True


def test_run_framework_setup_pipe_drain_does_not_block(tmp_path):
    # Script writes 8 MiB to stdout; without pipe drain would deadlock
    spec = make_spec(
        tmp_path,
        setup=(
            "python3 -c \""
            "import sys; "
            "sys.stdout.buffer.write(b'x' * 8 * 1024 * 1024); "
            "sys.stdout.buffer.write(b'DONE\\n'); "
            "sys.stdout.buffer.flush()"
            "\""
        ),
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    start = time.monotonic()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    elapsed = time.monotonic() - start
    assert elapsed < 20, f"took {elapsed:.1f}s — likely deadlocked"
    assert result.status in ("ok", "failed")


def test_run_framework_setup_retries_clear_prior_fail(tmp_path):
    spec = make_spec(tmp_path, setup='sh -c "exit 0"')
    cache_dir = tmp_path / "cache"
    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / f"{spec.name}.fail").write_text('{"reason":"prior"}')
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "ok"
    assert (setup_dir / f"{spec.name}.ok").exists()
    assert not (setup_dir / f"{spec.name}.fail").exists()


def test_run_framework_setup_skips_when_ok_fingerprint_is_fresh(tmp_path):
    spec = make_spec(tmp_path, setup="./setup.sh")
    script = spec.dir / "setup.sh"
    script.write_text(
        "#!/bin/sh\n"
        "count_file=count.txt\n"
        "n=0\n"
        "if [ -f \"$count_file\" ]; then n=$(cat \"$count_file\"); fi\n"
        "echo $((n + 1)) > \"$count_file\"\n"
    )
    script.chmod(0o755)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    first = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    second = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )

    assert first.status == "ok"
    assert second.status == "skipped"
    assert second.reason == "fresh"
    assert (spec.dir / "count.txt").read_text().strip() == "1"


def test_run_framework_setup_reruns_when_ok_fingerprint_is_stale(tmp_path):
    spec = make_spec(tmp_path, setup="./setup.sh")
    script = spec.dir / "setup.sh"
    script.write_text("#!/bin/sh\necho v1 > token.txt\n")
    script.chmod(0o755)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    first = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    script.write_text("#!/bin/sh\necho v2 > token.txt\n")
    second = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )

    assert first.status == "ok"
    assert second.status == "ok"
    assert (spec.dir / "token.txt").read_text().strip() == "v2"


def test_run_framework_setup_handles_shell_parse_error(tmp_path):
    """shlex.split() on a malformed setup string must surface as a SetupResult
    with status=failed (not propagate ValueError up the stack)."""
    spec = make_spec(tmp_path, setup='echo "unterminated quote', name="parse-bad")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "failed"
    assert result.exit_code is None
    fail_path = cache_dir / "setup" / f"{spec.name}.fail"
    assert fail_path.exists()
    fail_data = json.loads(fail_path.read_text())
    assert fail_data["reason"] == result.reason
    # Diagnostic stderr log is captured for the user.
    stderr_log = cache_dir / "setup" / f"{spec.name}.stderr.log"
    assert stderr_log.exists()
    assert "parse" in stderr_log.read_text().lower() or "quote" in stderr_log.read_text().lower()
    assert not (cache_dir / "setup" / f"{spec.name}.ok").exists()


def test_run_framework_setup_handles_spawn_error(tmp_path):
    """A nonexistent executable must surface as a SetupResult with status=failed,
    not propagate FileNotFoundError up the stack."""
    spec = make_spec(
        tmp_path,
        setup="/this/binary/definitely/does/not/exist --flag",
        name="spawn-bad",
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    result = run_framework_setup(
        spec, cache_dir=cache_dir, base_env=BASE_ENV, dotenv=DOTENV, timeout_s=30
    )
    assert result.status == "failed"
    assert result.exit_code is None
    fail_path = cache_dir / "setup" / f"{spec.name}.fail"
    assert fail_path.exists()
    stderr_log = cache_dir / "setup" / f"{spec.name}.stderr.log"
    assert stderr_log.exists()
    assert stderr_log.read_text()  # has diagnostic content
    assert not (cache_dir / "setup" / f"{spec.name}.ok").exists()


def test_run_all_setups_continues_past_spawn_errors(tmp_path):
    """A spawn failure on one framework must not abort run_all_setups."""
    spec_bad = make_spec(
        tmp_path,
        setup="/this/binary/does/not/exist",
        name="fw-spawn-bad",
    )
    spec_ok = make_spec(tmp_path, setup='sh -c "exit 0"', name="fw-after-bad")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    results = run_all_setups(
        [spec_bad, spec_ok],
        cache_dir=cache_dir,
        base_env=BASE_ENV,
        dotenv=DOTENV,
        timeout_s=30,
    )
    assert len(results) == 2
    assert results[0].status == "failed"
    assert results[1].status == "ok"
    assert (cache_dir / "setup" / f"{spec_bad.name}.fail").exists()
    assert (cache_dir / "setup" / f"{spec_ok.name}.ok").exists()


def test_run_all_setups_continues_past_failures(tmp_path):
    spec_fail = make_spec(tmp_path, setup='sh -c "exit 1"', name="fw-fail")
    spec_ok = make_spec(tmp_path, setup='sh -c "exit 0"', name="fw-ok")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    results = run_all_setups(
        [spec_fail, spec_ok],
        cache_dir=cache_dir,
        base_env=BASE_ENV,
        dotenv=DOTENV,
        timeout_s=30,
    )
    assert len(results) == 2
    assert results[0].status == "failed"
    assert results[1].status == "ok"


def test_is_setup_ok_returns_true_on_sentinel(tmp_path):
    cache_dir = tmp_path / "cache"
    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / "fw1.ok").write_text("{}")
    assert is_setup_ok("fw1", cache_dir) is True
    assert is_setup_ok("fw2", cache_dir) is False


def test_is_setup_failed_returns_true_on_fail_sentinel(tmp_path):
    cache_dir = tmp_path / "cache"
    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / "fw1.fail").write_text("{}")
    assert is_setup_failed("fw1", cache_dir) is True
    assert is_setup_failed("fw2", cache_dir) is False
