"""Integration tests: every FAKE_BEHAVIOR produces the expected meta + scoring."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import cli
from evals.campaign import eval_new
from evals.workspace import ensure_case_bare_repo, ensure_case_venv


def _setup_real_case_repo(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Build a tmp repo_root with the synthetic case + fake-framework, ready for cmd_eval."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Framework
    shutil.copytree(fixtures_dir / "fake-framework", repo / "frameworks" / "fake")

    # Fixture: copy the synthetic case (excluding .venv / cache).
    src = fixtures_dir / "cases" / "test-case-001"
    dst = repo / "fixtures" / "test-case-001"
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
    )

    # Case manifest with inline failure_output, pointing fixture_repo at fixtures/<id>/.
    failure_output = (fixtures_dir / "cases" / "test-case-001.failure_output.txt").read_text()
    (repo / "cases").mkdir()
    (repo / "cases" / "test-case-001.json").write_text(json.dumps({
        "case_id": "test-case-001",
        "fixture_repo": "fixtures/test-case-001",
        # -s disables pytest's stdout capture so the noisy-test-output behavior's
        # conftest writes actually reach the harness's stdout pump.
        "failing_test_command": "uv run pytest -q -s tests/test_arith.py",
        "hidden_test_command": "uv run pytest -q -s tests/test_arith_extended.py",
        "failure_output": failure_output,
        "edit_constraints": {},
    }))

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


def _prepare_cache(repo: Path) -> Path:
    cache = repo / ".runs-cache"
    cache.mkdir(parents=True, exist_ok=True)
    ensure_case_bare_repo(repo, "test-case-001", cache)
    ensure_case_venv(repo, "test-case-001", repo / "fixtures" / "test-case-001", cache)
    return cache


# Each row: (behavior, expected_error_reason, custom_timeout_s_or_None)
_BEHAVIORS = [
    ("success-noop", None, None),
    ("success-fix", None, None),
    ("hang", "timeout", 3),
    ("crash", "nonzero_exit", None),
    ("crash-with-error-envelope", "nonzero_exit", None),
    ("crash-with-bad-json", "nonzero_exit", None),
    ("garbage", "malformed_response_json", None),
    ("empty", "missing_response", None),
    ("oversize", "malformed_response_json", None),
    ("missing-field", "envelope_schema_violation", None),
    ("forbidden-field", None, None),
    ("disallowed-edit", None, None),
    ("over-max-files", None, None),
    ("noisy-stderr", None, None),
    ("mutate-venv", None, None),
    ("noisy-test-output", None, 30),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    "behavior, expected_error_reason, custom_timeout_s",
    _BEHAVIORS,
    ids=[row[0] for row in _BEHAVIORS],
)
def test_fake_behavior(
    behavior: str,
    expected_error_reason: str | None,
    custom_timeout_s: int | None,
    tmp_path: Path,
    fixtures_dir: Path,
    monkeypatch,
) -> None:
    repo = _setup_real_case_repo(tmp_path, fixtures_dir)
    _prepare_cache(repo)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setenv("FAKE_BEHAVIOR", behavior)

    timeout_s = custom_timeout_s if custom_timeout_s is not None else 20

    eval_new(
        repo,
        frameworks=["fake"],
        cases=["test-case-001"],
        config_overrides={"model": None, "timeout_s": timeout_s, "max_steps": None},
    )

    args = cli._build_parser().parse_args(
        ["eval", "fake", "test-case-001", "--timeout-s", str(timeout_s)]
    )
    rc = cli.cmd_eval(args)
    assert rc == 0, f"cmd_eval returned {rc}"

    cell = repo / "runs" / "CURRENT" / "fake" / "test-case-001"
    meta = json.loads((cell / "meta.json").read_text())
    scoring = json.loads((cell / "scoring.json").read_text())

    assert meta["error_reason"] == expected_error_reason, (
        f"behavior={behavior}: meta.error_reason={meta['error_reason']!r}, "
        f"expected={expected_error_reason!r}"
    )

    response_path = cell / "response.json"

    if behavior == "success-noop":
        assert scoring["schema_validity"] is True
        # Regression: visible/hidden test reruns use `uv run pytest ...`. They
        # must not sync the project into the shared case venv.
        assert meta["venv_mutated"] is False
    elif behavior == "success-fix":
        assert scoring["visible_test_outcome"] == "pass"
        # Same regression: a successful fix path reruns visible+hidden tests
        # with `uv run` and must not mutate the shared case venv.
        assert meta["venv_mutated"] is False
    elif behavior == "hang":
        assert meta["exit_code"] is None
    elif behavior == "crash":
        assert not response_path.exists()
    elif behavior == "crash-with-error-envelope":
        assert response_path.exists()
    elif behavior == "crash-with-bad-json":
        assert not response_path.exists()
    elif behavior == "oversize":
        assert meta["stdout_truncated"] is True
    elif behavior == "forbidden-field":
        assert scoring["schema_validity"] is False
    elif behavior == "disallowed-edit":
        assert scoring["edit_constraint_compliance"]["disallowed_violations"]
    elif behavior == "over-max-files":
        assert scoring["edit_constraint_compliance"]["over_max_changed_files"] is True
    elif behavior == "noisy-stderr":
        assert meta["stderr_truncated"] is True
    elif behavior == "mutate-venv":
        assert meta["venv_mutated"] is True
    elif behavior == "noisy-test-output":
        visible = json.loads((cell / "visible_test.json").read_text())
        assert visible["stdout_truncated"] is True
