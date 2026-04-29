import json
import os
import time
from pathlib import Path

import pytest

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
