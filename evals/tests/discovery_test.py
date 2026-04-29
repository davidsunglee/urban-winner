import json
from pathlib import Path

import pytest

from evals.discovery import (
    CaseSpec,
    DiscoveryError,
    FrameworkSpec,
    discover_cases,
    discover_frameworks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isolated_repo_root(tmp_path: Path) -> Path:
    """Create a tmp directory shaped like the repo root (frameworks/ + cases/)."""
    repo = tmp_path / "repo"
    (repo / "frameworks").mkdir(parents=True)
    (repo / "cases").mkdir(parents=True)
    return repo


def _write_framework(repo: Path, name: str, manifest: dict) -> Path:
    fw_dir = repo / "frameworks" / name
    fw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = fw_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return fw_dir


def _write_case(repo: Path, case_id: str, manifest: dict) -> Path:
    case_path = repo / "cases" / f"{case_id}.json"
    case_path.write_text(json.dumps(manifest))
    return case_path


# ---------------------------------------------------------------------------
# Framework discovery tests
# ---------------------------------------------------------------------------


def test_discover_frameworks_returns_fake(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    _write_framework(repo, "x", {"entry": "./run.py", "env": ["API_KEY"], "model": "gpt-4"})

    specs, errors = discover_frameworks(repo)

    assert errors == []
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, FrameworkSpec)
    assert spec.name == "x"
    assert spec.entry == "./run.py"
    assert spec.env_keys == ["API_KEY"]
    assert spec.model == "gpt-4"
    assert spec.setup is None


def test_discover_frameworks_skips_readme_only_dir(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    y_dir = repo / "frameworks" / "y"
    y_dir.mkdir()
    (y_dir / "README.md").write_text("# y")

    specs, errors = discover_frameworks(repo)

    assert specs == []
    assert errors == []


def test_discover_frameworks_reports_malformed(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    z_dir = repo / "frameworks" / "z"
    z_dir.mkdir()
    (z_dir / "manifest.json").write_text("not valid json{{{{")

    specs, errors = discover_frameworks(repo)

    # Malformed manifests must surface as a placeholder spec carrying a
    # discovery_error so the campaign matrix can render a
    # `framework_misconfigured` cell instead of silently dropping it.
    assert len(specs) == 1
    assert specs[0].name == "z"
    assert specs[0].discovery_error is not None
    assert specs[0].discovery_error.kind == "framework"
    assert any("invalid JSON" in m for m in specs[0].discovery_error.messages)
    assert len(errors) == 1
    assert errors[0].kind == "framework"
    assert errors[0].name == "z"


def test_discover_frameworks_reports_missing_entry(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    _write_framework(repo, "bad", {"env": [], "model": "x"})  # missing entry

    specs, errors = discover_frameworks(repo)

    # Schema-violating manifests also surface as placeholder specs; the
    # campaign must include them so they render as misconfigured cells.
    assert len(specs) == 1
    assert specs[0].name == "bad"
    assert specs[0].discovery_error is not None
    assert any("missing required key: entry" in m for m in specs[0].discovery_error.messages)
    assert len(errors) == 1
    assert errors[0].kind == "framework"


def test_discover_frameworks_sorted_alphabetically(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    for name in ("beta", "alpha", "gamma"):
        _write_framework(repo, name, {"entry": "./run.py", "env": [], "model": "m"})

    specs, errors = discover_frameworks(repo)

    assert errors == []
    assert [s.name for s in specs] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Case discovery tests
# ---------------------------------------------------------------------------


def test_discover_cases_resolves_inline_failure_output(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    _write_case(
        repo,
        "case-001",
        {
            "case_id": "case-001",
            "fixture_repo": str(fixture_dir),
            "failing_test_command": "pytest",
            "failure_output": "foo",
        },
    )

    specs, errors = discover_cases(repo)

    assert errors == []
    assert len(specs) == 1
    assert specs[0].failure_output == "foo"


def test_discover_cases_resolves_sidecar_failure_output_path(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    sidecar = tmp_path / "out.txt"
    sidecar.write_text("sidecar content")
    _write_case(
        repo,
        "case-002",
        {
            "case_id": "case-002",
            "fixture_repo": str(fixture_dir),
            "failing_test_command": "pytest",
            "failure_output_path": str(sidecar),
        },
    )

    specs, errors = discover_cases(repo)

    assert errors == []
    assert len(specs) == 1
    assert specs[0].failure_output == "sidecar content"


def test_discover_cases_rejects_both_failure_output_forms(tmp_path: Path) -> None:
    repo = _isolated_repo_root(tmp_path)
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    sidecar = tmp_path / "out.txt"
    sidecar.write_text("x")
    _write_case(
        repo,
        "case-003",
        {
            "case_id": "case-003",
            "fixture_repo": str(fixture_dir),
            "failing_test_command": "pytest",
            "failure_output": "inline",
            "failure_output_path": str(sidecar),
        },
    )

    specs, errors = discover_cases(repo)

    assert specs == []
    assert len(errors) > 0
    assert errors[0].kind == "case"


def test_discover_cases_missing_failure_output_path_is_structured_error(
    tmp_path: Path,
) -> None:
    """A failure_output_path that points at a missing file must produce a
    structured DiscoveryError, not crash discovery with FileNotFoundError."""
    repo = _isolated_repo_root(tmp_path)
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    _write_case(
        repo,
        "case-missing-fop",
        {
            "case_id": "case-missing-fop",
            "fixture_repo": str(fixture_dir),
            "failing_test_command": "pytest",
            "failure_output_path": str(tmp_path / "does" / "not" / "exist.txt"),
        },
    )

    specs, errors = discover_cases(repo)

    assert specs == []
    assert len(errors) == 1
    assert errors[0].kind == "case"
    assert errors[0].name == "case-missing-fop"
    assert any("failure_output_path" in m for m in errors[0].messages)


def test_discover_cases_handles_missing_executable_entry(tmp_path: Path) -> None:
    """A non-executable framework entry produces a FrameworkSpec, not a DiscoveryError."""
    repo = _isolated_repo_root(tmp_path)
    fw_dir = repo / "frameworks" / "noexec"
    fw_dir.mkdir()
    # Write manifest pointing to ./run.sh which does NOT exist / is not executable
    (fw_dir / "manifest.json").write_text(
        json.dumps({"entry": "./run.sh", "env": [], "model": "m"})
    )

    specs, errors = discover_frameworks(repo)

    assert errors == []
    assert len(specs) == 1
    assert specs[0].entry == "./run.sh"
