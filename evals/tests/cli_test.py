"""CLI-level regression tests focused on misconfiguration surfacing."""
import json
import subprocess
from pathlib import Path

import pytest

from evals import cli


def _init_repo(repo: Path) -> None:
    (repo / "frameworks").mkdir(parents=True)
    (repo / "cases").mkdir()


def _write_good_framework(repo: Path, name: str = "good") -> None:
    fw = repo / "frameworks" / name
    fw.mkdir()
    (fw / "manifest.json").write_text(
        json.dumps({"entry": "./run.py", "env": [], "model": "fake"})
    )


def _write_malformed_framework(repo: Path, name: str = "broken") -> None:
    fw = repo / "frameworks" / name
    fw.mkdir()
    (fw / "manifest.json").write_text("{ this is not valid json")


def _write_good_case(repo: Path, fixture_dir: Path, case_id: str = "case-001") -> None:
    (repo / "cases" / f"{case_id}.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "fixture_repo": str(fixture_dir),
                "failing_test_command": "true",
                "failure_output": "boom",
            }
        )
    )


def test_cmd_eval_new_includes_malformed_framework_in_matrix(
    tmp_path: Path, monkeypatch
) -> None:
    """Malformed framework manifests must appear in the campaign matrix so
    they render as `framework_misconfigured` cells, not silently disappear."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo, name="good")
    _write_malformed_framework(repo, name="broken")
    fixture = tmp_path / "fix"
    fixture.mkdir()
    _write_good_case(repo, fixture)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    args = cli._build_parser().parse_args(["eval-new"])
    rc = cli.cmd_eval_new(args)
    assert rc == 0

    current = repo / "runs" / "CURRENT"
    manifest = json.loads((current / "manifest.json").read_text())
    assert "good" in manifest["frameworks"]
    assert "broken" in manifest["frameworks"], (
        "malformed framework was silently dropped from the campaign matrix"
    )
