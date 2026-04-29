from pathlib import Path

import pytest

from evals.env import build_agent_env, build_setup_env, build_test_env, load_dotenv


def test_load_dotenv_missing_returns_empty(tmp_path):
    result = load_dotenv(tmp_path)
    assert result == {}


def test_load_dotenv_parses_pairs(tmp_path):
    (tmp_path / ".env").write_text("K=V\n")
    result = load_dotenv(tmp_path)
    assert result == {"K": "V"}


def test_agent_env_includes_declared_keys():
    env = build_agent_env(
        declared_keys=["FOO"],
        case_venv_path=None,
        base_env={"FOO": "bar", "PATH": "/usr/bin", "HOME": "/root"},
        dotenv={},
    )
    assert env["FOO"] == "bar"


def test_agent_env_path_prepends_venv_bin(tmp_path):
    venv = tmp_path / "v"
    env = build_agent_env(
        declared_keys=[],
        case_venv_path=venv,
        base_env={"PATH": "/usr/bin"},
        dotenv={},
    )
    assert env["PATH"].startswith(str(venv.resolve()) + "/bin:")


def test_agent_env_excludes_undeclared_keys():
    env = build_agent_env(
        declared_keys=[],
        case_venv_path=None,
        base_env={"SECRET": "x", "PATH": "/usr/bin"},
        dotenv={},
    )
    assert "SECRET" not in env


def test_agent_env_dotenv_overrides_base_for_declared():
    env = build_agent_env(
        declared_keys=["K"],
        case_venv_path=None,
        base_env={"K": "base", "PATH": "/usr/bin"},
        dotenv={"K": "dot"},
    )
    assert env["K"] == "dot"


def test_test_env_excludes_framework_keys():
    agent_env = build_agent_env(
        declared_keys=["FOO"],
        case_venv_path=Path("/tmp/v"),
        base_env={"FOO": "val", "PATH": "/usr/bin"},
        dotenv={},
    )
    assert "FOO" in agent_env

    test_env = build_test_env(
        case_venv_path=Path("/tmp/v"),
        cell_repo_path=Path("/tmp/repo"),
        base_env={"FOO": "val", "PATH": "/usr/bin"},
    )
    assert "FOO" not in test_env


def test_test_env_path_prepends_venv_bin(tmp_path):
    venv = tmp_path / "v"
    env = build_test_env(
        case_venv_path=venv,
        cell_repo_path=tmp_path / "repo",
        base_env={"PATH": "/usr/bin"},
    )
    assert env["PATH"].startswith(str(venv.resolve()) + "/bin:")


def test_test_env_includes_uv_project_environment(tmp_path):
    venv = tmp_path / "v"
    env = build_test_env(
        case_venv_path=venv,
        cell_repo_path=tmp_path / "repo",
        base_env={"PATH": "/usr/bin"},
    )
    assert env["UV_PROJECT_ENVIRONMENT"] == str(venv.resolve())


def test_test_env_disables_uv_sync(tmp_path):
    # Regression: `uv run pytest ...` must not sync/install the project into the
    # shared case venv during test reruns. UV_NO_SYNC=1 enforces this.
    env = build_test_env(
        case_venv_path=tmp_path / "v",
        cell_repo_path=tmp_path / "repo",
        base_env={"PATH": "/usr/bin"},
    )
    assert env["UV_NO_SYNC"] == "1"


def test_test_env_pythonpath_points_at_cell_repo(tmp_path):
    # The case venv is built with `--no-install-project`, so the project is not
    # importable from site-packages. Tests must be able to import the cell's
    # checked-out source via PYTHONPATH instead.
    repo = tmp_path / "repo"
    env = build_test_env(
        case_venv_path=tmp_path / "v",
        cell_repo_path=repo,
        base_env={"PATH": "/usr/bin"},
    )
    assert env["PYTHONPATH"] == str(repo.resolve())


def test_setup_env_does_not_include_uv_project_environment():
    env = build_setup_env(
        declared_keys=[],
        base_env={"PATH": "/usr/bin"},
        dotenv={},
    )
    assert "UV_PROJECT_ENVIRONMENT" not in env


def test_setup_env_path_does_not_prepend_venv():
    base_path = "/usr/bin"
    env = build_setup_env(
        declared_keys=[],
        base_env={"PATH": base_path},
        dotenv={},
    )
    assert env["PATH"] == base_path
