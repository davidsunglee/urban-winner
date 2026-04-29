import json
import os
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

from evals.campaign import (
    LockBusyError,
    acquire_lock,
    current_campaign,
    eval_new,
    lock,
    release_lock,
)


# --- eval_new tests ---

def test_eval_new_creates_dir_manifest_and_symlink(tmp_repo_root):
    campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["fake"],
        cases=["test-case-001"],
        config_overrides={},
    )
    assert campaign_dir.exists()
    assert (campaign_dir / "manifest.json").exists()
    current_link = tmp_repo_root / "runs" / "CURRENT"
    assert current_link.is_symlink()
    assert current_link.resolve() == campaign_dir.resolve()


def test_eval_new_manifest_records_overrides(tmp_repo_root):
    campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["fake"],
        cases=["test-case-001"],
        config_overrides={"model": "foo"},
    )
    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    overrides = manifest["config_overrides"]
    assert overrides["model"] == "foo"
    assert overrides["timeout_s"] is None
    assert overrides["max_steps"] is None


def test_eval_new_manifest_has_started_at(tmp_repo_root):
    campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=[],
        cases=[],
        config_overrides={},
    )
    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    assert "started_at" in manifest
    assert manifest["started_at"].endswith("Z")


def test_eval_new_manifest_has_frameworks_and_cases(tmp_repo_root):
    campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["fw1", "fw2"],
        cases=["case-a"],
        config_overrides={},
    )
    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    assert manifest["frameworks"] == ["fw1", "fw2"]
    assert manifest["cases"] == ["case-a"]


def test_eval_new_preserves_existing_campaign_on_timestamp_collision(tmp_repo_root, monkeypatch):
    monkeypatch.setattr("evals.campaign._now_iso", lambda: "2026-01-01T00-00-00")

    first_campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["first"],
        cases=["case-a"],
        config_overrides={},
    )
    sentinel = first_campaign_dir / "sentinel.txt"
    sentinel.write_text("do not delete")

    second_campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["second"],
        cases=["case-b"],
        config_overrides={},
    )

    assert sentinel.exists(), "existing campaign data was deleted during name collision"
    assert second_campaign_dir != first_campaign_dir
    assert (second_campaign_dir / "manifest.json").exists()


def test_eval_new_refuses_to_repoint_when_current_campaign_is_locked(tmp_repo_root):
    first_campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["first"],
        cases=["case-a"],
        config_overrides={},
    )
    (first_campaign_dir / ".lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": "2026-01-01T00:00:00Z",
                "argv": ["eval-all"],
            }
        )
    )

    with pytest.raises(LockBusyError, match=str(os.getpid())):
        eval_new(
            tmp_repo_root,
            frameworks=["second"],
            cases=["case-b"],
            config_overrides={},
        )

    assert current_campaign(tmp_repo_root).resolve() == first_campaign_dir.resolve()


def test_eval_new_force_unlock_repoints_after_cross_host_lock(tmp_repo_root, capsys):
    first_campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["first"],
        cases=["case-a"],
        config_overrides={},
    )
    (first_campaign_dir / ".lock").write_text(
        json.dumps(
            {
                "pid": 12345,
                "hostname": "other-host",
                "started_at": "2026-01-01T00:00:00Z",
                "argv": ["eval-all"],
            }
        )
    )

    second_campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=["second"],
        cases=["case-b"],
        config_overrides={},
        force_unlock=True,
        argv=["eval-new", "--force-unlock"],
    )

    assert second_campaign_dir != first_campaign_dir
    assert current_campaign(tmp_repo_root).resolve() == second_campaign_dir.resolve()
    assert not (first_campaign_dir / ".lock").exists()
    assert "warning" in capsys.readouterr().err.lower()


# --- current_campaign tests ---

def test_current_campaign_returns_none_when_no_symlink(tmp_repo_root):
    result = current_campaign(tmp_repo_root)
    assert result is None


def test_current_campaign_returns_path_after_eval_new(tmp_repo_root):
    campaign_dir = eval_new(
        tmp_repo_root,
        frameworks=[],
        cases=[],
        config_overrides={},
    )
    result = current_campaign(tmp_repo_root)
    assert result is not None
    assert result.resolve() == campaign_dir.resolve()


# --- lock / acquire_lock / release_lock tests ---

def test_acquire_lock_writes_pid_hostname(tmp_path):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    acquire_lock(campaign_dir, argv=["test"])
    lock_path = campaign_dir / ".lock"
    assert lock_path.exists()
    data = json.loads(lock_path.read_text())
    assert data["pid"] == os.getpid()
    assert data["hostname"] == socket.gethostname()


def test_acquire_lock_refuses_alive_same_host(tmp_path, monkeypatch):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    lock_path = campaign_dir / ".lock"
    lock_data = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": "2026-01-01T00:00:00Z",
        "argv": ["test"],
    }
    lock_path.write_text(json.dumps(lock_data))
    with pytest.raises(LockBusyError, match=str(os.getpid())):
        acquire_lock(campaign_dir, argv=["test"])


def test_acquire_lock_reclaims_dead_same_host(tmp_path, capsys):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    lock_path = campaign_dir / ".lock"
    # Use a very large PID that is extremely unlikely to exist
    dead_pid = 999999999
    lock_data = {
        "pid": dead_pid,
        "hostname": socket.gethostname(),
        "started_at": "2026-01-01T00:00:00Z",
        "argv": ["test"],
    }
    lock_path.write_text(json.dumps(lock_data))
    acquire_lock(campaign_dir, argv=["test"])
    data = json.loads(lock_path.read_text())
    assert data["pid"] == os.getpid()
    captured = capsys.readouterr()
    assert "stale" in captured.err.lower() or "warning" in captured.err.lower()


def test_acquire_lock_refuses_different_host(tmp_path):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    lock_path = campaign_dir / ".lock"
    lock_data = {
        "pid": os.getpid(),
        "hostname": "other-host",
        "started_at": "2026-01-01T00:00:00Z",
        "argv": ["test"],
    }
    lock_path.write_text(json.dumps(lock_data))
    with pytest.raises(LockBusyError, match="other-host"):
        acquire_lock(campaign_dir, argv=["test"])


def test_acquire_lock_force_unlock_overrides_different_host(tmp_path, capsys):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    lock_path = campaign_dir / ".lock"
    lock_data = {
        "pid": 12345,
        "hostname": "other-host",
        "started_at": "2026-01-01T00:00:00Z",
        "argv": ["test"],
    }
    lock_path.write_text(json.dumps(lock_data))
    acquire_lock(campaign_dir, argv=["test"], force_unlock=True)
    data = json.loads(lock_path.read_text())
    assert data["pid"] == os.getpid()
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower() or "overrid" in captured.err.lower()


def test_release_lock_removes_file(tmp_path):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    lock_path = campaign_dir / ".lock"
    lock_path.write_text("{}")
    release_lock(campaign_dir)
    assert not lock_path.exists()


def test_release_lock_tolerates_missing(tmp_path):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    release_lock(campaign_dir)  # should not raise


def test_lock_context_manager_releases_on_exception(tmp_path):
    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()
    with pytest.raises(ValueError):
        with lock(campaign_dir, argv=["test"]):
            raise ValueError("deliberate")
    assert not (campaign_dir / ".lock").exists()


# --- atomicity regression tests ---

def test_acquire_lock_concurrent_threads_only_one_winner(tmp_path):
    """Regression: concurrent acquisitions must produce exactly one winner.

    The previous implementation used a non-atomic exists-check followed by
    temp+rename, which let two callers both observe no .lock and both
    succeed in writing one. Acquisition must use an atomic exclusive
    creation primitive so the kernel arbitrates the winner.
    """
    # Run several iterations to make the race observable on a non-atomic
    # implementation. With an atomic-create implementation, every iteration
    # passes deterministically.
    for _ in range(20):
        campaign_dir = tmp_path / "campaign"
        if campaign_dir.exists():
            shutil.rmtree(campaign_dir)
        campaign_dir.mkdir()

        barrier = threading.Barrier(2)
        results: list[str] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                acquire_lock(campaign_dir, argv=["test"])
                with results_lock:
                    results.append("ok")
            except LockBusyError:
                with results_lock:
                    results.append("busy")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == ["busy", "ok"], (
            f"expected exactly one winner, got {results}"
        )


def test_acquire_lock_does_not_clobber_existing_lock_file(tmp_path, monkeypatch):
    """Regression: even with a deliberately widened race window, a second
    acquirer must not blindly overwrite an existing lock file.

    Simulates the TOCTOU window between observing the lock state and
    writing by patching ``Path.exists`` to release the GIL. With an atomic
    O_EXCL-based implementation this patch is harmless; with the non-atomic
    implementation it makes the second writer reliably clobber the first.
    """
    import time

    campaign_dir = tmp_path / "campaign"
    campaign_dir.mkdir()

    original_exists = Path.exists

    def slow_exists(self):
        result = original_exists(self)
        # Yield aggressively so a competing thread can interleave.
        time.sleep(0.05)
        return result

    monkeypatch.setattr(Path, "exists", slow_exists)

    barrier = threading.Barrier(2)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            acquire_lock(campaign_dir, argv=["test"])
            with results_lock:
                results.append("ok")
        except LockBusyError:
            with results_lock:
                results.append("busy")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["busy", "ok"], (
        f"expected exactly one winner under widened race window, got {results}"
    )
