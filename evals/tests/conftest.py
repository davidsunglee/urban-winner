import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass(frozen=True)
class ProcessTreeProbe:
    script_path: Path
    grandchild_pid_path: Path
    grandchild_term_path: Path

    def command(self) -> list[str]:
        return [sys.executable, str(self.script_path)]

    def shell_command(self) -> str:
        return shlex.join(self.command())

    def grandchild_pid(self) -> int:
        return int(self.grandchild_pid_path.read_text())

    def grandchild_is_alive(self) -> bool:
        if not self.grandchild_pid_path.exists():
            return False
        try:
            os.kill(self.grandchild_pid(), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_for_grandchild_exit(self, timeout_s: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.grandchild_is_alive():
                return True
            time.sleep(0.05)
        return not self.grandchild_is_alive()

    def cleanup(self) -> None:
        if not self.grandchild_pid_path.exists() or self.grandchild_term_path.exists():
            return
        pid = self.grandchild_pid()
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


@pytest.fixture
def process_tree_probe(tmp_path: Path):
    pid_path = tmp_path / "grandchild.pid"
    term_path = tmp_path / "grandchild.term"
    script_path = tmp_path / "spawn_process_tree.py"
    script_path.write_text(
        """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

pid_path = Path(os.environ["GRANDCHILD_PID_FILE"])
term_path = Path(os.environ["GRANDCHILD_TERM_FILE"])

grandchild_code = r'''
import os
import signal
import time
from pathlib import Path

term_path = Path(os.environ["GRANDCHILD_TERM_FILE"])

def handle_sigterm(signum, frame):
    term_path.write_text(str(signum))
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
while True:
    time.sleep(0.1)
'''

grandchild = subprocess.Popen(
    [sys.executable, "-c", grandchild_code],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    env=os.environ.copy(),
)
pid_path.write_text(str(grandchild.pid))
while True:
    time.sleep(0.1)
"""
    )
    probe = ProcessTreeProbe(script_path, pid_path, term_path)
    try:
        yield probe
    finally:
        probe.cleanup()


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fixtures_dir(repo_root: Path) -> Path:
    return repo_root / "evals" / "tests" / "fixtures"


@pytest.fixture
def tmp_repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture
def synthetic_case_manifest(fixtures_dir: Path) -> Path:
    return fixtures_dir / "cases" / "test-case-001.json"


@pytest.fixture
def synthetic_case_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "cases" / "test-case-001"


@pytest.fixture
def fake_framework_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "fake-framework"
