"""Headline acceptance test: eval-all against real stub framework adapters."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import cli


def _setup_real_repo(tmp_path: Path, repo_root: Path, fixtures_dir: Path) -> Path:
    """Mirror the real repo layout in tmp: real frameworks/ + the synthetic case."""
    repo = tmp_path / "repo"
    repo.mkdir()

    shutil.copytree(
        repo_root / "frameworks",
        repo / "frameworks",
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
    )

    # Copy the synthetic case fixture into fixtures/<case>/ for ensure_case_bare_repo.
    src = fixtures_dir / "cases" / "test-case-001"
    dst = repo / "fixtures" / "test-case-001"
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
    )

    failure_output = (fixtures_dir / "cases" / "test-case-001.failure_output.txt").read_text()
    (repo / "cases").mkdir()
    (repo / "cases" / "test-case-001.json").write_text(json.dumps({
        "case_id": "test-case-001",
        "fixture_repo": "fixtures/test-case-001",
        "failing_test_command": "uv run pytest -q tests/test_arith.py",
        "hidden_test_command": "uv run pytest -q tests/test_arith_extended.py",
        "failure_output": failure_output,
        "edit_constraints": {},
    }))

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.mark.integration
def test_eval_all_with_stub_frameworks(
    tmp_path: Path, repo_root: Path, fixtures_dir: Path, monkeypatch
) -> None:
    repo = _setup_real_repo(tmp_path, repo_root, fixtures_dir)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)
    assert rc == 0, f"cmd_eval_all returned {rc}"

    current = repo / "runs" / "CURRENT"
    manifest_path = current / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    expected_frameworks = {
        "agentcore",
        "claude-agent-sdk",
        "deepagents",
        "google-adk",
        "mastra",
        "openai-agents",
        "pydantic-ai",
        "strands",
    }
    assert set(manifest["frameworks"]) == expected_frameworks
    assert manifest["cases"] == ["test-case-001"]

    cells_seen = 0
    for fw_name in manifest["frameworks"]:
        for case_id in manifest["cases"]:
            cell = current / fw_name / case_id
            meta = json.loads((cell / "meta.json").read_text())
            assert meta["status"] == "error", (
                f"{fw_name}/{case_id}: status={meta['status']!r}"
            )
            assert meta["error_reason"] == "nonzero_exit", (
                f"{fw_name}/{case_id}: error_reason={meta['error_reason']!r}"
            )
            cells_seen += 1
    assert cells_seen == len(expected_frameworks) * len(manifest["cases"])

    report_path = current / "report.md"
    assert report_path.exists()
    assert report_path.stat().st_size > 0
