import hashlib
import json
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from evals.discovery import FrameworkSpec
from evals.env import build_setup_env
from evals.process_tree import PROCESS_GROUP_POPEN_KWARGS, terminate_process_tree

_CAP_BYTES = 5 * 1024 * 1024
_DEPENDENCY_FILE_NAMES = (
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)


@dataclass(frozen=True)
class SetupResult:
    framework: str
    status: str  # "ok" | "skipped" | "failed"
    # "nonzero_exit" | "timeout" | "parse_error" | "spawn_error" | None
    reason: str | None
    exit_code: int | None
    stdout_truncated: bool
    stderr_truncated: bool
    duration_s: float


def is_setup_ok(framework_name: str, cache_dir: Path) -> bool:
    return (cache_dir / "setup" / f"{framework_name}.ok").exists()


def is_setup_failed(framework_name: str, cache_dir: Path) -> bool:
    return (cache_dir / "setup" / f"{framework_name}.fail").exists()


def _pump_capped(reader, dest_path: Path, cap_bytes: int) -> bool:
    """Read from reader into dest_path up to cap_bytes; drain remainder. Returns True if truncated."""
    chunk_size = 65536
    written = 0
    truncated = False
    with open(dest_path, "wb") as f:
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            if written < cap_bytes:
                to_write = min(len(chunk), cap_bytes - written)
                f.write(chunk[:to_write])
                written += to_write
                if len(chunk) > to_write:
                    truncated = True
            else:
                truncated = True
    return truncated


def _atomic_write(dest: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with open(fd, "w") as f:
            f.write(content)
        Path(tmp).rename(dest)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _manifest_hash(spec: FrameworkSpec) -> str:
    if spec.manifest_path.exists():
        return hashlib.sha256(spec.manifest_path.read_bytes()).hexdigest()
    return ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _file_token_path(spec_dir: Path, token: str) -> Path | None:
    if not token or token.startswith("-"):
        return None
    if "=" in token and token.startswith("--"):
        token = token.split("=", 1)[1]
    if not token or any(ch in token for ch in "*$?[]{}"):
        return None

    path = Path(token)
    if not path.is_absolute():
        path = spec_dir / path

    spec_root = spec_dir.resolve()
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, spec_root):
        return None
    if not path.is_file():
        return None
    return path


def _setup_fingerprint_files(spec: FrameworkSpec) -> list[Path]:
    files: set[Path] = set()

    try:
        tokens = shlex.split(spec.setup or "")
    except ValueError:
        tokens = []
    for token in tokens:
        path = _file_token_path(spec.dir, token)
        if path is not None:
            files.add(path)

    for name in _DEPENDENCY_FILE_NAMES:
        path = spec.dir / name
        if path.is_file():
            files.add(path)
    for path in spec.dir.glob("requirements*.txt"):
        if path.is_file():
            files.add(path)

    return sorted(files, key=lambda p: p.relative_to(spec.dir).as_posix())


def setup_fingerprint(spec: FrameworkSpec) -> str:
    """Return the cache key for a framework setup.

    The setup result depends on more than the manifest: the command can point at
    a setup script, and setup commands commonly install dependencies from local
    lockfiles/manifests. Include those directly-known inputs so a cached `.ok`
    sentinel goes stale when they change.
    """
    h = hashlib.sha256()

    def add_part(kind: str, name: str, content: bytes) -> None:
        h.update(kind.encode())
        h.update(b"\0")
        h.update(name.encode())
        h.update(b"\0")
        h.update(str(len(content)).encode())
        h.update(b"\0")
        h.update(content)
        h.update(b"\0")

    if spec.manifest_path.exists():
        add_part(
            "file",
            spec.manifest_path.relative_to(spec.dir).as_posix(),
            spec.manifest_path.read_bytes(),
        )
    else:
        add_part("missing", "manifest.json", b"")

    add_part("setup", "command", (spec.setup or "").encode())
    for path in _setup_fingerprint_files(spec):
        add_part("file", path.relative_to(spec.dir).as_posix(), path.read_bytes())

    return h.hexdigest()


def _ok_is_fresh(ok_path: Path, current_fingerprint: str) -> bool:
    try:
        data = json.loads(ok_path.read_text())
    except Exception:
        return False
    return (data.get("fingerprint") or data.get("hash")) == current_fingerprint


def _record_pre_exec_failure(
    spec: FrameworkSpec,
    *,
    reason: str,
    message: str,
    stdout_log: Path,
    stderr_log: Path,
    fail_path: Path,
    started_at: str,
    t0: float,
) -> "SetupResult":
    """Record a setup failure that occurred before the child process executed
    (shlex parse error, Popen OSError). Writes empty stdout.log, the diagnostic
    to stderr.log, and a `.fail` sentinel; returns SetupResult(status="failed").
    """
    stdout_log.write_bytes(b"")
    stderr_log.write_text(message)
    ended_at = datetime.now(timezone.utc).isoformat()
    duration_s = time.monotonic() - t0
    content = json.dumps(
        {
            "reason": reason,
            "exit_code": None,
            "started_at": started_at,
            "ended_at": ended_at,
        }
    )
    _atomic_write(fail_path, content)
    return SetupResult(
        framework=spec.name,
        status="failed",
        reason=reason,
        exit_code=None,
        stdout_truncated=False,
        stderr_truncated=False,
        duration_s=duration_s,
    )


def run_framework_setup(
    spec: FrameworkSpec,
    *,
    cache_dir: Path,
    base_env: dict[str, str],
    dotenv: dict[str, str],
    timeout_s: int,
) -> SetupResult:
    if spec.setup is None:
        return SetupResult(
            framework=spec.name,
            status="skipped",
            reason=None,
            exit_code=None,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0.0,
        )

    setup_dir = cache_dir / "setup"
    setup_dir.mkdir(parents=True, exist_ok=True)

    ok_path = setup_dir / f"{spec.name}.ok"
    fail_path = setup_dir / f"{spec.name}.fail"
    current_fingerprint = setup_fingerprint(spec)
    if not fail_path.exists() and ok_path.exists() and _ok_is_fresh(
        ok_path, current_fingerprint
    ):
        return SetupResult(
            framework=spec.name,
            status="skipped",
            reason="fresh",
            exit_code=0,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0.0,
        )

    ok_path.unlink(missing_ok=True)
    fail_path.unlink(missing_ok=True)

    env = build_setup_env(
        declared_keys=spec.env_keys,
        base_env=base_env,
        dotenv=dotenv,
    )
    stdout_log = setup_dir / f"{spec.name}.stdout.log"
    stderr_log = setup_dir / f"{spec.name}.stderr.log"

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    # Parse and spawn errors must surface as a `.fail` SetupResult, not
    # propagate up the stack and abort `run_all_setups`.
    try:
        cmd = shlex.split(spec.setup)
    except ValueError as exc:
        return _record_pre_exec_failure(
            spec,
            reason="parse_error",
            message=f"setup parse error (shlex): {exc}\ncommand: {spec.setup!r}\n",
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            fail_path=fail_path,
            started_at=started_at,
            t0=t0,
        )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(spec.dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **PROCESS_GROUP_POPEN_KWARGS,
        )
    except OSError as exc:
        return _record_pre_exec_failure(
            spec,
            reason="spawn_error",
            message=f"setup spawn error: {exc}\ncommand: {spec.setup!r}\n",
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            fail_path=fail_path,
            started_at=started_at,
            t0=t0,
        )

    stdout_truncated = False
    stderr_truncated = False

    def pump_stdout() -> None:
        nonlocal stdout_truncated
        stdout_truncated = _pump_capped(proc.stdout, stdout_log, _CAP_BYTES)

    def pump_stderr() -> None:
        nonlocal stderr_truncated
        stderr_truncated = _pump_capped(proc.stderr, stderr_log, _CAP_BYTES)

    t1 = threading.Thread(target=pump_stdout, daemon=True)
    t2 = threading.Thread(target=pump_stderr, daemon=True)
    t1.start()
    t2.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_tree(proc, 5)

    t1.join()
    t2.join()

    ended_at = datetime.now(timezone.utc).isoformat()
    duration_s = time.monotonic() - t0

    if not timed_out and proc.returncode == 0:
        content = json.dumps(
            {
                "fingerprint": current_fingerprint,
                "fingerprint_version": 1,
                "hash": current_fingerprint,
                "manifest_hash": _manifest_hash(spec),
                "started_at": started_at,
                "ended_at": ended_at,
            }
        )
        _atomic_write(ok_path, content)
        return SetupResult(
            framework=spec.name,
            status="ok",
            reason=None,
            exit_code=0,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_s=duration_s,
        )

    reason = "timeout" if timed_out else "nonzero_exit"
    exit_code = None if timed_out else proc.returncode
    content = json.dumps(
        {
            "reason": reason,
            "exit_code": exit_code,
            "started_at": started_at,
            "ended_at": ended_at,
        }
    )
    _atomic_write(fail_path, content)
    return SetupResult(
        framework=spec.name,
        status="failed",
        reason=reason,
        exit_code=exit_code,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_s=duration_s,
    )


def run_all_setups(
    specs: list[FrameworkSpec],
    *,
    cache_dir: Path,
    base_env: dict[str, str],
    dotenv: dict[str, str],
    timeout_s: int,
) -> list[SetupResult]:
    results: list[SetupResult] = []
    for spec in specs:
        results.append(
            run_framework_setup(
                spec,
                cache_dir=cache_dir,
                base_env=base_env,
                dotenv=dotenv,
                timeout_s=timeout_s,
            )
        )
    return results
