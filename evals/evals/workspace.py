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

    if hash_file.exists() and hash_file.read_text().strip() == fixture_hash:
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

        shutil.move(str(bare), str(bare_dir))

    hash_file.write_text(fixture_hash)
    return bare_dir


def ensure_case_venv(
    repo_root: Path,
    case_id: str,
    fixture_dir: Path,
    cache_dir: Path,
) -> Path:
    lock_hash = compute_lock_hash(fixture_dir)
    hash_file = cache_dir / f"{case_id}.lock-hash"
    venv_dir = cache_dir / f"{case_id}.venv"

    if hash_file.exists() and hash_file.read_text().strip() == lock_hash:
        return venv_dir

    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    if hash_file.exists():
        hash_file.unlink()

    cache_dir.mkdir(parents=True, exist_ok=True)

    cmd = ["uv", "sync", "--no-install-project"]
    if (fixture_dir / "uv.lock").exists():
        cmd.append("--frozen")

    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir.resolve())}
    result = subprocess.run(
        cmd,
        cwd=str(fixture_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WorkspaceError("uv sync failed", stderr=result.stderr)

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
