import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class LockBusyError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _iso_zulu() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_state(repo_root) -> dict:
    repo_root = Path(repo_root)
    result = {}

    def run(*args):
        try:
            r = subprocess.run(args, capture_output=True, text=True, cwd=repo_root)
            if r.returncode == 0:
                return r.stdout.strip()
        except Exception:
            pass
        return None

    sha = run("git", "rev-parse", "HEAD")
    if sha:
        result["git_sha"] = sha

    dirty_out = run("git", "status", "--porcelain")
    if dirty_out is not None:
        result["git_dirty"] = bool(dirty_out)

    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        result["git_branch"] = branch

    remote_url = run("git", "remote", "get-url", "origin")
    if remote_url:
        result["git_remote_url"] = remote_url

    return result


def _atomic_write_json(path: Path, obj: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, suffix=".tmp") as f:
        json.dump(obj, f, indent=2)
        tmp_path = f.name
    os.rename(tmp_path, path)


def eval_new(
    repo_root: Path,
    *,
    frameworks: list[str],
    cases: list[str],
    config_overrides: dict,
) -> Path:
    repo_root = Path(repo_root)
    runs_dir = repo_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    ts = _now_iso()
    campaign_dir = runs_dir / ts

    if campaign_dir.exists():
        shutil.rmtree(campaign_dir)
    campaign_dir.mkdir(parents=True)

    manifest = {
        "started_at": _iso_zulu(),
        "git": _git_state(repo_root),
        "frameworks": frameworks,
        "cases": cases,
        "config_overrides": {
            "model": config_overrides.get("model", None),
            "timeout_s": config_overrides.get("timeout_s", None),
            "max_steps": config_overrides.get("max_steps", None),
        },
    }
    _atomic_write_json(campaign_dir / "manifest.json", manifest)

    current_path = runs_dir / "CURRENT"
    current_tmp = runs_dir / "CURRENT.tmp"
    if current_tmp.exists() or current_tmp.is_symlink():
        os.unlink(current_tmp)
    os.symlink(ts, current_tmp)
    os.rename(current_tmp, current_path)

    return campaign_dir


def current_campaign(repo_root) -> Path | None:
    repo_root = Path(repo_root)
    current_link = repo_root / "runs" / "CURRENT"
    if not current_link.exists() and not current_link.is_symlink():
        return None
    ts = os.readlink(current_link)
    return repo_root / "runs" / ts


def _write_lock(lock_path: Path, argv: list[str]) -> None:
    lock_data = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": _iso_zulu(),
        "argv": argv,
    }
    with tempfile.NamedTemporaryFile(mode="w", dir=lock_path.parent, delete=False, suffix=".tmp") as f:
        json.dump(lock_data, f)
        tmp_path = f.name
    os.rename(tmp_path, lock_path)


def acquire_lock(campaign_dir: Path, *, argv: list[str], force_unlock: bool = False) -> None:
    campaign_dir = Path(campaign_dir)
    lock_path = campaign_dir / ".lock"

    if not lock_path.exists():
        _write_lock(lock_path, argv)
        return

    lock_data = json.loads(lock_path.read_text())
    pid = lock_data["pid"]
    hostname = lock_data["hostname"]
    started_at = lock_data["started_at"]

    if hostname == socket.gethostname():
        try:
            os.kill(pid, 0)
            raise LockBusyError(
                f"Campaign in use by PID {pid} (since {started_at}). Delete {lock_path} if stale."
            )
        except ProcessLookupError:
            print(f"Warning: stale lock from dead PID {pid}, overwriting", file=sys.stderr)
            _write_lock(lock_path, argv)
        except PermissionError:
            raise LockBusyError(
                f"Campaign in use by PID {pid} (since {started_at}). Delete {lock_path} if stale."
            )
    else:
        if force_unlock:
            print(
                f"Warning: overriding lock from PID {pid} on host {hostname} (force_unlock=True)",
                file=sys.stderr,
            )
            _write_lock(lock_path, argv)
        else:
            raise LockBusyError(
                f"Campaign locked by PID {pid} on host {hostname} (since {started_at}). "
                f"On a shared filesystem, that lock may still be live. "
                f"If you are sure it is stale, delete {lock_path} manually or pass --force-unlock."
            )


def release_lock(campaign_dir) -> None:
    lock_path = Path(campaign_dir) / ".lock"
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


@contextmanager
def lock(campaign_dir, *, argv, force_unlock=False):
    acquire_lock(campaign_dir, argv=argv, force_unlock=force_unlock)
    try:
        yield
    finally:
        release_lock(campaign_dir)
