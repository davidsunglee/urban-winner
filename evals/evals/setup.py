import hashlib
import json
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from evals.discovery import FrameworkSpec
from evals.env import build_setup_env

_CAP_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class SetupResult:
    framework: str
    status: str  # "ok" | "skipped" | "failed"
    reason: str | None  # "nonzero_exit" | "timeout" | None
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
    ok_path.unlink(missing_ok=True)
    fail_path.unlink(missing_ok=True)

    env = build_setup_env(
        declared_keys=spec.env_keys,
        base_env=base_env,
        dotenv=dotenv,
    )
    cmd = shlex.split(spec.setup)
    stdout_log = setup_dir / f"{spec.name}.stdout.log"
    stderr_log = setup_dir / f"{spec.name}.stderr.log"

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    proc = subprocess.Popen(
        cmd,
        cwd=str(spec.dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    t1.join()
    t2.join()

    ended_at = datetime.now(timezone.utc).isoformat()
    duration_s = time.monotonic() - t0

    if not timed_out and proc.returncode == 0:
        content = json.dumps(
            {
                "hash": _manifest_hash(spec),
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
