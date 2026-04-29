import os
import signal
import subprocess
import threading
import time

PROCESS_GROUP_POPEN_KWARGS = {"start_new_session": True}


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_if_exited(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=0)
    except subprocess.TimeoutExpired:
        pass


def _wait_for_process_group_exit(proc: subprocess.Popen, pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        _reap_if_exited(proc)
        if not _process_group_exists(pgid):
            _reap_if_exited(proc)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def close_popen_pipes(proc: subprocess.Popen) -> None:
    """Best-effort close of pipes owned by a Popen object.

    Used after a process group has been terminated. Detached descendants can keep
    inherited pipe file descriptors open forever; closing our handles lets the
    harness stop waiting on pump threads and record a bounded failure.
    """
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            fd = stream.fileno()
        except (OSError, ValueError):
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def join_threads_bounded(threads: list[threading.Thread], timeout_s: float) -> bool:
    """Join threads within a shared deadline. Return True if all stopped."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    for thread in threads:
        remaining = deadline - time.monotonic()
        thread.join(timeout=max(0.0, remaining))
    return not any(thread.is_alive() for thread in threads)


def terminate_process_tree(proc: subprocess.Popen, grace_s: float) -> None:
    """Terminate a subprocess and all descendants in its process group.

    Callers must spawn the process with PROCESS_GROUP_POPEN_KWARGS so the child
    owns an isolated process group. The child PID is then also the group ID.
    """
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_if_exited(proc)
        return

    if _wait_for_process_group_exit(proc, pgid, grace_s):
        return

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    _wait_for_process_group_exit(proc, pgid, 1.0)
