import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class WorkspaceError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _blake2_hex(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _fixture_rel_path(repo_root: Path, case_id: str, fixture_dir: Path | None = None) -> str:
    if fixture_dir is None:
        return f"fixtures/{case_id}"
    path = fixture_dir
    if path.is_absolute():
        path = path.resolve().relative_to(repo_root.resolve())
    return path.as_posix().rstrip("/")


def compute_fixture_hash(
    repo_root: Path,
    case_id: str,
    fixture_dir: Path | None = None,
) -> str:
    fixture_rel = _fixture_rel_path(repo_root, case_id, fixture_dir)
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", f"{fixture_rel}/"],
        capture_output=True,
        check=True,
    )
    tracked = [p for p in result.stdout.split(b"\0") if p]
    if not tracked:
        raise WorkspaceError(f"No tracked files found for case {case_id!r} in {fixture_rel!r}")

    entries = []
    for rel_bytes in tracked:
        rel = rel_bytes.decode()
        file_hash = hashlib.sha256((repo_root / rel).read_bytes()).hexdigest()
        entries.append((rel, file_hash))

    entries.sort(key=lambda e: e[0])
    buf = b"".join(f"{rel}\0{fhash}\n".encode() for rel, fhash in entries)
    return _blake2_hex(buf)


def compute_lock_hash(fixture_dir: Path) -> str:
    lock_file = fixture_dir / "uv.lock"
    if lock_file.exists():
        return _blake2_hex(lock_file.read_bytes())
    return _blake2_hex((fixture_dir / "pyproject.toml").read_bytes())


def compute_venv_fingerprint(venv_dir: Path) -> str:
    dist_infos = sorted(
        p.name
        for p in venv_dir.glob("lib/python*/site-packages/*.dist-info")
        if p.is_dir()
    )
    return _blake2_hex("\n".join(dist_infos).encode())


def ensure_case_bare_repo(
    repo_root: Path,
    case_id: str,
    cache_dir: Path,
    fixture_dir: Path | None = None,
) -> Path:
    fixture_rel = _fixture_rel_path(repo_root, case_id, fixture_dir)
    fixture_hash = compute_fixture_hash(repo_root, case_id, fixture_dir)
    hash_file = cache_dir / f"{case_id}.fixture-hash"
    bare_dir = cache_dir / f"{case_id}.git"

    if (
        hash_file.exists()
        and hash_file.read_text().strip() == fixture_hash
        and bare_dir.exists()
    ):
        return bare_dir

    if bare_dir.exists():
        shutil.rmtree(bare_dir)
    if hash_file.exists():
        hash_file.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp_str:
        tmp = Path(tmp_str)
        bare = tmp / "bare"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)

        work = tmp / "work"
        work.mkdir()

        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", f"{fixture_rel}/"],
            capture_output=True,
            check=True,
        )
        tracked = [p.decode() for p in result.stdout.split(b"\0") if p]
        prefix = f"{fixture_rel}/"
        for rel in tracked:
            rel_in_fixture = rel[len(prefix):]
            dst = work / rel_in_fixture
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(repo_root / rel), str(dst))

        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "harness",
            "GIT_AUTHOR_EMAIL": "harness@local",
            "GIT_COMMITTER_NAME": "harness",
            "GIT_COMMITTER_EMAIL": "harness@local",
        }
        subprocess.run(["git", "init"], cwd=str(work), check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(work), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"fixture: {case_id} @ {fixture_hash}"],
            cwd=str(work),
            check=True,
            capture_output=True,
            env=git_env,
        )
        subprocess.run(
            ["git", "push", str(bare.resolve()), "HEAD:refs/heads/main"],
            cwd=str(work),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
            capture_output=True,
        )

        shutil.move(str(bare), str(bare_dir))

    hash_file.write_text(fixture_hash)
    return bare_dir


def _copy_fixture_for_unlocked_sync(fixture_dir: Path, dest: Path) -> None:
    """Copy an unlocked fixture to a throwaway project root for ``uv sync``.

    When a fixture has no uv.lock, ``uv sync`` may create one in its project
    root. Fixture directories are harness inputs, so run the sync against a
    temporary copy instead of letting uv write into the source fixture.
    """
    shutil.copytree(
        fixture_dir,
        dest,
        ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__"),
    )


def _run_uv_sync(sync_dir: Path, venv_dir: Path, *, frozen: bool) -> None:
    cmd = ["uv", "sync", "--no-install-project"]
    if frozen:
        cmd.append("--frozen")

    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir.resolve())}
    result = subprocess.run(
        cmd,
        cwd=str(sync_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorkspaceError("uv sync failed", stderr=result.stderr)


def ensure_case_venv(
    repo_root: Path,
    case_id: str,
    fixture_dir: Path,
    cache_dir: Path,
) -> Path:
    lock_hash = compute_lock_hash(fixture_dir)
    hash_file = cache_dir / f"{case_id}.lock-hash"
    venv_dir = cache_dir / f"{case_id}.venv"

    if (
        hash_file.exists()
        and hash_file.read_text().strip() == lock_hash
        and venv_dir.exists()
    ):
        return venv_dir

    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    if hash_file.exists():
        hash_file.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)

    if (fixture_dir / "uv.lock").exists():
        _run_uv_sync(fixture_dir, venv_dir, frozen=True)
    else:
        with tempfile.TemporaryDirectory(dir=cache_dir) as tmp_str:
            sync_dir = Path(tmp_str) / "project"
            _copy_fixture_for_unlocked_sync(fixture_dir, sync_dir)
            _run_uv_sync(sync_dir, venv_dir, frozen=False)

    hash_file.write_text(lock_hash)
    return venv_dir


def clone_cell_worktree(bare_repo: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    subprocess.run(
        ["git", "clone", "--local", str(bare_repo), str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


def wipe_cell_dir(cell_dir: Path) -> None:
    if cell_dir.exists():
        shutil.rmtree(cell_dir)
