import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.workspace import (
    WorkspaceError,
    clone_cell_worktree,
    compute_fixture_hash,
    compute_lock_hash,
    compute_venv_fingerprint,
    ensure_case_bare_repo,
    ensure_case_venv,
    wipe_cell_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@local"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True,
    )


def _git_add_commit(path: Path, message: str = "initial") -> None:
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@local",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@local",
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(path), check=True, capture_output=True, env=env,
    )


def _make_fixture_repo(tmp_path: Path, case_id: str = "my-case") -> tuple[Path, str]:
    """Create a git repo with a tiny fixture committed. Returns (repo_root, case_id)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)

    fixture = repo / "fixtures" / case_id
    fixture.mkdir(parents=True)
    (fixture / "main.py").write_text("x = 1\n")
    (fixture / "helper.py").write_text("def f(): pass\n")

    _git_add_commit(repo, "add fixture")
    return repo, case_id


# ---------------------------------------------------------------------------
# compute_fixture_hash
# ---------------------------------------------------------------------------

def test_compute_fixture_hash_changes_when_file_changes(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)

    hash1 = compute_fixture_hash(repo, case_id)
    (repo / "fixtures" / case_id / "main.py").write_text("x = 2\n")
    hash2 = compute_fixture_hash(repo, case_id)

    assert hash1 != hash2


def test_compute_fixture_hash_excludes_untracked(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)

    hash1 = compute_fixture_hash(repo, case_id)

    # Drop an untracked file — must NOT change the hash
    venv = repo / "fixtures" / case_id / ".venv"
    venv.mkdir()
    (venv / "foo.py").write_text("ignored\n")

    hash2 = compute_fixture_hash(repo, case_id)

    assert hash1 == hash2


def test_compute_fixture_hash_uses_manifest_fixture_dir(tmp_path):
    repo, _case_id = _make_fixture_repo(tmp_path, case_id="fixture-dir")

    assert compute_fixture_hash(repo, "case-id", repo / "fixtures" / "fixture-dir")


# ---------------------------------------------------------------------------
# compute_lock_hash
# ---------------------------------------------------------------------------

def test_compute_lock_hash_uses_uv_lock_when_present(tmp_path):
    (tmp_path / "uv.lock").write_text("lock-content")
    (tmp_path / "pyproject.toml").write_text("pyproject-content")

    import hashlib
    expected_hash = hashlib.blake2b(b"lock-content", digest_size=16).hexdigest()
    assert compute_lock_hash(tmp_path) == expected_hash


def test_compute_lock_hash_falls_back_to_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("pyproject-content")

    import hashlib
    expected_hash = hashlib.blake2b(b"pyproject-content", digest_size=16).hexdigest()
    assert compute_lock_hash(tmp_path) == expected_hash


# ---------------------------------------------------------------------------
# compute_venv_fingerprint
# ---------------------------------------------------------------------------

def test_compute_venv_fingerprint_stable(tmp_path):
    # Empty venv dir → fixed value; same call twice → same value
    venv = tmp_path / "venv"
    venv.mkdir()

    fp1 = compute_venv_fingerprint(venv)
    fp2 = compute_venv_fingerprint(venv)

    assert fp1 == fp2
    # Should be blake2 of empty string
    import hashlib
    assert fp1 == hashlib.blake2b(b"", digest_size=16).hexdigest()


def test_compute_venv_fingerprint_changes_when_distinfo_added(tmp_path):
    venv = tmp_path / "venv"
    venv.mkdir()

    fp_before = compute_venv_fingerprint(venv)

    # Add a dist-info directory
    dist = venv / "lib" / "python3.12" / "site-packages" / "foo-1.0.dist-info"
    dist.mkdir(parents=True)

    fp_after = compute_venv_fingerprint(venv)

    assert fp_before != fp_after


# ---------------------------------------------------------------------------
# ensure_case_bare_repo
# ---------------------------------------------------------------------------

def test_ensure_case_bare_repo_reuses_when_hash_matches(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    bare1 = ensure_case_bare_repo(repo, case_id, cache)
    head_mtime = (bare1 / "HEAD").stat().st_mtime

    bare2 = ensure_case_bare_repo(repo, case_id, cache)
    assert bare1 == bare2
    assert (bare2 / "HEAD").stat().st_mtime == head_mtime, "second call should not rebuild"


def test_ensure_case_bare_repo_rebuilds_when_hash_changes(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    ensure_case_bare_repo(repo, case_id, cache)
    head_mtime1 = (cache / f"{case_id}.git" / "HEAD").stat().st_mtime

    # Modify a tracked file on disk (no git commit needed — hash reads disk content)
    (repo / "fixtures" / case_id / "main.py").write_text("x = 99\n")

    ensure_case_bare_repo(repo, case_id, cache)
    head_mtime2 = (cache / f"{case_id}.git" / "HEAD").stat().st_mtime

    assert head_mtime1 != head_mtime2, "second call should rebuild when hash changes"


def test_ensure_case_bare_repo_uses_manifest_fixture_dir(tmp_path):
    repo, _case_id = _make_fixture_repo(tmp_path, case_id="fixture-dir")
    cache = tmp_path / "cache"
    cache.mkdir()

    bare = ensure_case_bare_repo(repo, "case-id", cache, repo / "fixtures" / "fixture-dir")
    dest = tmp_path / "cell"
    clone_cell_worktree(bare, dest)

    assert (dest / "main.py").read_text() == "x = 1\n"


# ---------------------------------------------------------------------------
# clone_cell_worktree
# ---------------------------------------------------------------------------

def test_clone_cell_worktree_creates_independent_repo(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    bare = ensure_case_bare_repo(repo, case_id, cache)
    dest = tmp_path / "cell"

    clone_cell_worktree(bare, dest)

    result = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "fixture:" in result.stdout


def test_clone_cell_worktree_overwrites_existing(tmp_path):
    repo, case_id = _make_fixture_repo(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()

    bare = ensure_case_bare_repo(repo, case_id, cache)
    dest = tmp_path / "cell"
    dest.mkdir()
    (dest / "garbage.txt").write_text("old content")

    clone_cell_worktree(bare, dest)

    assert not (dest / "garbage.txt").exists()
    result = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "fixture:" in result.stdout


# ---------------------------------------------------------------------------
# wipe_cell_dir
# ---------------------------------------------------------------------------

def test_wipe_cell_dir_removes_existing(tmp_path):
    cell = tmp_path / "cell"
    cell.mkdir()
    (cell / "file.txt").write_text("data")

    wipe_cell_dir(cell)
    assert not cell.exists()


def test_wipe_cell_dir_noop_when_missing(tmp_path):
    cell = tmp_path / "nonexistent"
    wipe_cell_dir(cell)  # should not raise


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_ensure_case_venv_no_install_project(tmp_path):
    fixture_dir = Path(__file__).resolve().parent / "fixtures" / "cases" / "test-case-001"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    # repo_root not used by ensure_case_venv; pass anything valid
    repo_root = Path(__file__).resolve().parents[2]

    venv = ensure_case_venv(
        repo_root=repo_root,
        case_id="test-case-001",
        fixture_dir=fixture_dir,
        cache_dir=cache_dir,
    )

    site_pkgs = list(venv.glob("lib/python*/site-packages"))
    assert site_pkgs, "site-packages directory should exist"
    sp = site_pkgs[0]

    # Project should NOT be installed
    assert not (sp / "test_case_001").exists(), "project package must not be installed"

    # pytest (a dev dependency) SHOULD be installed
    pytest_dists = list(sp.glob("pytest-*.dist-info"))
    assert pytest_dists, "pytest dist-info should be present"
