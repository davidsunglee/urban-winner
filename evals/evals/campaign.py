import json
import os
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


def _create_unique_campaign_dir(runs_dir: Path, ts: str) -> Path:
    for attempt in range(1000):
        name = ts if attempt == 0 else f"{ts}-{attempt}"
        campaign_dir = runs_dir / name
        try:
            campaign_dir.mkdir()
            return campaign_dir
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create a unique campaign directory for {ts!r}")


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

    campaign_dir = _create_unique_campaign_dir(runs_dir, _now_iso())

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
    os.symlink(campaign_dir.name, current_tmp)
    os.rename(current_tmp, current_path)

    return campaign_dir


def current_campaign(repo_root) -> Path | None:
    repo_root = Path(repo_root)
    current_link = repo_root / "runs" / "CURRENT"
    if not current_link.exists() and not current_link.is_symlink():
        return None
    ts = os.readlink(current_link)
    return repo_root / "runs" / ts


def _build_lock_data(argv: list[str]) -> dict:
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": _iso_zulu(),
        "argv": argv,
    }


def _try_create_lock_excl(lock_path: Path, argv: list[str]) -> bool:
    """Atomically create ``lock_path`` populated with this process's lock data.

    Returns ``True`` when this call created the file (we now hold the lock)
    and ``False`` when the file already existed (someone else holds it).

    Implementation: write the lock payload into a private temp file first,
    then publish it with ``os.link``. ``link`` is atomic on POSIX and fails
    with ``FileExistsError`` if the target already exists, so the kernel
    arbitrates between concurrent callers — exactly one wins. Writing the
    payload before publishing also guarantees that any racing reader sees a
    fully-formed JSON document, never an empty or partial one.
    """
    fd, tmp_path = tempfile.mkstemp(prefix=".lock.", suffix=".tmp", dir=lock_path.parent)
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_build_lock_data(argv), f)
        try:
            os.link(tmp, lock_path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _take_over_lock(lock_path: Path, argv: list[str]) -> None:
    """Replace an existing (stale or force-overridden) lock with our own.

    The takeover is best-effort: if another process wins the race after we
    unlink the old file we raise ``LockBusyError`` rather than silently
    clobber a fresh, valid lock.
    """
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        # The old lock is already gone; that's fine — proceed to claim it.
        pass
    if not _try_create_lock_excl(lock_path, argv):
        raise LockBusyError(
            f"Lock at {lock_path} was reclaimed by another process during takeover."
        )


def acquire_lock(campaign_dir: Path, *, argv: list[str], force_unlock: bool = False) -> None:
    campaign_dir = Path(campaign_dir)
    lock_path = campaign_dir / ".lock"

    # Fast path: try to atomically create the lock. The kernel guarantees
    # that at most one concurrent caller succeeds.
    if _try_create_lock_excl(lock_path, argv):
        return

    # Lock file already exists. Inspect it to decide whether to take over.
    try:
        lock_data = json.loads(lock_path.read_text())
    except FileNotFoundError:
        # The owner released between our exclusive-create attempt and our
        # read. Try once more to claim it; if that still fails, surface the
        # current owner's diagnostic by re-reading.
        if _try_create_lock_excl(lock_path, argv):
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
            _take_over_lock(lock_path, argv)
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
            _take_over_lock(lock_path, argv)
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
