"""Tests for the resume rule: a cell is 'done' iff meta.json exists."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import cli
from evals.campaign import eval_new
from evals.workspace import ensure_case_bare_repo


def _setup_repo(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Build a minimal repo_root with a no-uv case and the fake-framework."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Framework: copy fake-framework as-is (preserves +x on run.py).
    shutil.copytree(fixtures_dir / "fake-framework", repo / "frameworks" / "fake")

    # Fixture: minimal git-tracked content; pipeline will diff against this.
    fix = repo / "fixtures" / "test-case-001"
    fix.mkdir(parents=True)
    (fix / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\nversion = \"0\"\nrequires-python = \">=3.11\"\n"
    )
    (fix / "README.md").write_text("hi\n")

    # Case manifest: failing_test_command='true' avoids needing uv at all.
    (repo / "cases").mkdir()
    (repo / "cases" / "test-case-001.json").write_text(json.dumps({
        "case_id": "test-case-001",
        "fixture_repo": "fixtures/test-case-001",
        "failing_test_command": "true",
        "failure_output": "",
        "edit_constraints": {},
    }))

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Pre-build cache so cmd_eval_all skips _do_prepare (no uv invocation needed).
    cache = repo / ".runs-cache"
    cache.mkdir()
    ensure_case_bare_repo(repo, "test-case-001", cache)
    (cache / "test-case-001.venv").mkdir()  # presence is what _prepare_needed checks
    (cache / "test-case-001.lock-hash").write_text("dummy")

    return repo


def _create_campaign(repo: Path) -> Path:
    return eval_new(
        repo,
        frameworks=["fake"],
        cases=["test-case-001"],
        config_overrides={"model": None, "timeout_s": 10, "max_steps": None},
    )


def _run_eval_all(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setenv("FAKE_BEHAVIOR", "success-noop")
    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)
    assert rc == 0


def test_resume_blows_away_partial_cell_dir(
    tmp_path: Path, fixtures_dir: Path, monkeypatch
) -> None:
    repo = _setup_repo(tmp_path, fixtures_dir)
    campaign_dir = _create_campaign(repo)

    cell_dir = campaign_dir / "fake" / "test-case-001"
    cell_dir.mkdir(parents=True)
    (cell_dir / "request.json").write_text("{\"stale\": true}")
    (cell_dir / "diff.patch").write_text("stale")

    _run_eval_all(repo, monkeypatch)

    assert (cell_dir / "meta.json").exists()
    # Stale request.json was wiped; the new run wrote a fresh one with a real task_id.
    new_request = json.loads((cell_dir / "request.json").read_text())
    assert "task_id" in new_request


def test_resume_skips_cells_with_meta_json(
    tmp_path: Path, fixtures_dir: Path, monkeypatch
) -> None:
    repo = _setup_repo(tmp_path, fixtures_dir)
    campaign_dir = _create_campaign(repo)

    cell_dir = campaign_dir / "fake" / "test-case-001"
    cell_dir.mkdir(parents=True)
    pre_meta = {
        "framework": "fake",
        "case_id": "test-case-001",
        "task_id": "preexisting",
        "model": "fake",
        "started_at": "2020-01-01T00:00:00+00:00",
        "ended_at": "2020-01-01T00:00:01+00:00",
        "status": "error",
        "error_reason": "nonzero_exit",
        "exit_code": 1,
    }
    (cell_dir / "meta.json").write_text(json.dumps(pre_meta))

    _run_eval_all(repo, monkeypatch)

    after = json.loads((cell_dir / "meta.json").read_text())
    assert after["task_id"] == "preexisting"
    assert after["started_at"] == "2020-01-01T00:00:00+00:00"


def test_resume_treats_meta_tmp_as_partial(
    tmp_path: Path, fixtures_dir: Path, monkeypatch
) -> None:
    repo = _setup_repo(tmp_path, fixtures_dir)
    campaign_dir = _create_campaign(repo)

    cell_dir = campaign_dir / "fake" / "test-case-001"
    cell_dir.mkdir(parents=True)
    (cell_dir / "meta.json.tmp").write_text("{\"crash\": true}")

    _run_eval_all(repo, monkeypatch)

    assert (cell_dir / "meta.json").exists()
    assert not (cell_dir / "meta.json.tmp").exists()
