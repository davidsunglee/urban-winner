"""CLI-level regression tests focused on misconfiguration surfacing."""
from contextlib import contextmanager
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from evals import cli
from evals.discovery import discover_cases, discover_frameworks
from evals.setup import SetupResult, run_framework_setup
from evals.workspace import WorkspaceError


def _init_repo(repo: Path) -> None:
    (repo / "frameworks").mkdir(parents=True)
    (repo / "cases").mkdir()


def _write_good_framework(repo: Path, name: str = "good") -> None:
    fw = repo / "frameworks" / name
    fw.mkdir()
    (fw / "manifest.json").write_text(
        json.dumps({"entry": "./run.py", "env": [], "model": "fake"})
    )


def _write_setup_framework(repo: Path, name: str = "setup-fw") -> None:
    fw = repo / "frameworks" / name
    fw.mkdir()
    (fw / "manifest.json").write_text(
        json.dumps(
            {"entry": "./run.py", "setup": "./setup.sh", "env": [], "model": "fake"}
        )
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


def _write_malformed_case(repo: Path, name: str = "broken-case") -> None:
    (repo / "cases" / f"{name}.json").write_text("{ this is not valid json")


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


def test_eval_new_fails_on_case_discovery_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    _write_malformed_case(repo)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    args = cli._build_parser().parse_args(["eval-new"])
    rc = cli.cmd_eval_new(args)

    assert rc == 1
    assert "broken-case" in capsys.readouterr().err
    assert not (repo / "runs" / "CURRENT").exists()


def test_eval_prepare_fails_on_case_discovery_errors_before_prepare(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    _write_malformed_case(repo)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    prepare_called = False

    def fake_prepare(**_kwargs):
        nonlocal prepare_called
        prepare_called = True
        return cli._PrepareResult(summary=[], failed=False, case_failed=False)

    monkeypatch.setattr(cli, "_do_prepare", fake_prepare)
    args = cli._build_parser().parse_args(["eval-prepare"])
    rc = cli.cmd_eval_prepare(args)

    assert rc == 1
    assert prepare_called is False
    assert "broken-case" in capsys.readouterr().err


def test_eval_all_fails_on_case_discovery_errors_before_campaign_creation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    _write_malformed_case(repo)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: False)
    monkeypatch.setattr(cli, "_run_one_cell", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)
    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)

    assert rc == 1
    assert "broken-case" in capsys.readouterr().err
    assert not (repo / "runs" / "CURRENT").exists()


def test_cmd_eval_new_accepts_force_unlock_for_current_campaign_lock(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo, name="good")
    fixture = tmp_path / "fix"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    first_campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )
    (first_campaign_dir / ".lock").write_text(
        json.dumps(
            {
                "pid": 12345,
                "hostname": "other-host",
                "started_at": "2026-01-01T00:00:00Z",
                "argv": ["eval-all"],
            }
        )
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    args = cli._build_parser().parse_args(["eval-new", "--force-unlock"])
    rc = cli.cmd_eval_new(args)

    assert rc == 0
    second_campaign_dir = Path(capsys.readouterr().out.strip())
    assert second_campaign_dir != first_campaign_dir
    assert (repo / "runs" / "CURRENT").resolve() == second_campaign_dir.resolve()


# ---------------------------------------------------------------------------
# _prepare_needed — must detect stale fixture/lock hashes, not just missing dirs.
# ---------------------------------------------------------------------------

def _make_cached_repo(tmp_path: Path, *, case_id: str = "case-001") -> tuple[Path, Path]:
    """Set up a repo + populated cache where prepare-needed should be False."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    # Initialize a real git repo so compute_fixture_hash can ls-files.
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=str(repo), check=True, capture_output=True,
    )

    # A fixture committed to git so compute_fixture_hash sees tracked files.
    fixture = repo / "fixtures" / case_id
    fixture.mkdir(parents=True)
    (fixture / "main.py").write_text("x = 1\n")
    (fixture / "pyproject.toml").write_text(
        '[project]\nname="f"\nversion="0"\n'
    )
    _write_good_case(repo, fixture, case_id=case_id)
    _write_good_framework(repo, name="good")

    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": __import__("os").environ["PATH"],
        },
    )

    cache = repo / ".runs-cache"
    cache.mkdir()

    # Populate cache with everything _prepare_needed inspects.
    (cache / f"{case_id}.git").mkdir()
    (cache / f"{case_id}.venv").mkdir()

    from evals.workspace import compute_fixture_hash, compute_lock_hash
    (cache / f"{case_id}.fixture-hash").write_text(
        compute_fixture_hash(repo, case_id, fixture)
    )
    (cache / f"{case_id}.lock-hash").write_text(compute_lock_hash(fixture))

    setup_dir = cache / "setup"
    setup_dir.mkdir()
    fw_manifest = repo / "frameworks" / "good" / "manifest.json"
    (setup_dir / "good.ok").write_text(
        json.dumps({"hash": hashlib.sha256(fw_manifest.read_bytes()).hexdigest()})
    )

    return repo, cache


def test_prepare_needed_false_when_caches_are_fresh(tmp_path: Path) -> None:
    repo, cache = _make_cached_repo(tmp_path)
    frameworks, _ = discover_frameworks(repo)
    cases, _ = discover_cases(repo)

    assert cli._prepare_needed(repo, frameworks, cases, cache) is False


def test_prepare_needed_true_when_fixture_hash_is_stale(tmp_path: Path) -> None:
    """Editing a tracked fixture file must trigger a layer rebuild on the next
    `eval-all` even though the bare repo and venv directories still exist.
    """
    repo, cache = _make_cached_repo(tmp_path)
    frameworks, _ = discover_frameworks(repo)
    cases, _ = discover_cases(repo)

    # Mutate a tracked fixture file — fixture_hash now diverges from the
    # value persisted in cache/.fixture-hash.
    case_id = cases[0].case_id
    (repo / "fixtures" / case_id / "main.py").write_text("x = 999\n")

    assert cli._prepare_needed(repo, frameworks, cases, cache) is True, (
        "fixture mutation must trigger prepare; otherwise eval-all reuses a stale bare repo"
    )


def test_prepare_needed_true_when_lock_hash_is_stale(tmp_path: Path) -> None:
    """Editing the case's pyproject.toml/uv.lock must trigger a venv rebuild."""
    repo, cache = _make_cached_repo(tmp_path)
    frameworks, _ = discover_frameworks(repo)
    cases, _ = discover_cases(repo)

    case_id = cases[0].case_id
    (repo / "fixtures" / case_id / "pyproject.toml").write_text(
        '[project]\nname="f"\nversion="1"\n'
    )

    assert cli._prepare_needed(repo, frameworks, cases, cache) is True, (
        "lock-file mutation must trigger prepare; otherwise eval-all reuses a stale venv"
    )


def _make_repo_with_cached_setup(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    fw = repo / "frameworks" / "setup-fw"
    fw.mkdir()
    (fw / "manifest.json").write_text(
        json.dumps(
            {"entry": "./run.py", "setup": "./setup.sh", "env": [], "model": "fake"}
        )
    )
    (fw / "setup.sh").write_text("#!/bin/sh\nexit 0\n")
    (fw / "setup.sh").chmod(0o755)
    (fw / "pyproject.toml").write_text('[project]\nname="setup-fw"\nversion="0"\n')

    cache = repo / ".runs-cache"
    cache.mkdir()
    frameworks, _ = discover_frameworks(repo)
    result = run_framework_setup(
        frameworks[0], cache_dir=cache, base_env=dict(), dotenv={}, timeout_s=30
    )
    assert result.status == "ok"
    return repo, cache


def test_prepare_needed_false_when_framework_setup_fail_is_fresh(
    tmp_path: Path,
) -> None:
    repo, cache = _make_repo_with_cached_setup(tmp_path)
    setup_dir = cache / "setup"
    (setup_dir / "setup-fw.ok").unlink()
    frameworks, _ = discover_frameworks(repo)
    (setup_dir / "setup-fw.fail").write_text(
        json.dumps(
            {
                "reason": "nonzero_exit",
                "fingerprint": cli.setup_fingerprint(frameworks[0]),
            }
        )
    )

    assert cli._prepare_needed(repo, frameworks, [], cache) is False, (
        "eval-all must honor a fresh .fail sentinel instead of rerunning framework setup"
    )


def test_prepare_needed_true_when_framework_setup_script_is_stale(
    tmp_path: Path,
) -> None:
    repo, cache = _make_repo_with_cached_setup(tmp_path)
    (repo / "frameworks" / "setup-fw" / "setup.sh").write_text(
        "#!/bin/sh\necho changed\n"
    )
    frameworks, _ = discover_frameworks(repo)

    assert cli._prepare_needed(repo, frameworks, [], cache) is True, (
        "setup script changes must invalidate the cached framework setup"
    )


def test_prepare_needed_true_when_framework_dependency_file_is_stale(
    tmp_path: Path,
) -> None:
    repo, cache = _make_repo_with_cached_setup(tmp_path)
    (repo / "frameworks" / "setup-fw" / "pyproject.toml").write_text(
        '[project]\nname="setup-fw"\nversion="1"\n'
    )
    frameworks, _ = discover_frameworks(repo)

    assert cli._prepare_needed(repo, frameworks, [], cache) is True, (
        "dependency file changes must invalidate the cached framework setup"
    )


# ---------------------------------------------------------------------------
# eval-all --framework / --case typo handling
# ---------------------------------------------------------------------------

def test_eval_all_unknown_framework_exits_2(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, _cache = _make_cached_repo(tmp_path)
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    args = cli._build_parser().parse_args(["eval-all", "--framework", "typo"])
    rc = cli.cmd_eval_all(args)

    assert rc == 2, "unknown --framework must exit 2, not silently no-op"
    err = capsys.readouterr().err
    assert "typo" in err
    assert "framework" in err.lower()


def test_eval_all_unknown_case_exits_2(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo, _cache = _make_cached_repo(tmp_path)
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    args = cli._build_parser().parse_args(["eval-all", "--case", "nope"])
    rc = cli.cmd_eval_all(args)

    assert rc == 2, "unknown --case must exit 2, not silently no-op"
    err = capsys.readouterr().err
    assert "nope" in err
    assert "case" in err.lower()


def test_eval_all_prepares_only_selected_campaign_cells(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo, name="keep-fw")
    _write_good_framework(repo, name="skip-fw")
    keep_fixture = tmp_path / "keep-fixture"
    keep_fixture.mkdir()
    skip_fixture = tmp_path / "skip-fixture"
    skip_fixture.mkdir()
    _write_good_case(repo, keep_fixture, case_id="keep-case")
    _write_good_case(repo, skip_fixture, case_id="skip-case")
    cli.eval_new(
        repo,
        frameworks=["keep-fw"],
        cases=["keep-case"],
        config_overrides={},
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    prepared: dict[str, list[str]] = {}

    def fake_prepare_needed(_repo_root, frameworks, cases, _cache_dir):
        prepared["frameworks"] = [fw.name for fw in frameworks]
        prepared["cases"] = [case.case_id for case in cases]
        return False

    attempted_cells: list[tuple[str, str]] = []

    def record_cell_attempt(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "_prepare_needed", fake_prepare_needed)
    monkeypatch.setattr(cli, "_run_one_cell", record_cell_attempt)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)

    args = cli._build_parser().parse_args(
        ["eval-all", "--framework", "keep-fw", "--case", "keep-case"]
    )
    rc = cli.cmd_eval_all(args)

    assert rc == 0
    assert prepared == {"frameworks": ["keep-fw"], "cases": ["keep-case"]}
    assert attempted_cells == [("keep-fw", "keep-case")]


def test_cli_reports_lock_refusal_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )
    (campaign_dir / ".lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": "2026-01-01T00:00:00Z",
                "argv": ["eval-all"],
            }
        )
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: False)

    rc = cli.main(["eval-all"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Campaign in use" in err
    assert "Traceback" not in err


def test_eval_all_aborts_nonzero_when_case_prepare_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    def fail_bare_repo(*_args, **_kwargs):
        raise WorkspaceError("unable to materialize bare repo")

    attempted_cells: list[tuple[str, str]] = []

    def record_cell_attempt(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))

    monkeypatch.setattr(cli, "ensure_case_bare_repo", fail_bare_repo)
    monkeypatch.setattr(cli, "_run_one_cell", record_cell_attempt)

    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)

    assert rc == 1, "case cache/workspace prepare failures must make eval-all fail"
    assert attempted_cells == [], "eval-all must not run cells after case prepare failure"
    out = capsys.readouterr().out
    assert "case case-001: bare-repo FAIL" in out


def test_prepare_summary_includes_uv_sync_stderr_on_case_venv_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    cases, errors = discover_cases(repo)
    assert errors == []
    cache = repo / ".runs-cache"

    def create_bare_repo(_repo_root, case_id, cache_dir, _fixture_repo):
        bare_repo = cache_dir / f"{case_id}.git"
        bare_repo.mkdir(parents=True, exist_ok=True)
        return bare_repo

    def fail_venv(_repo_root, _case_id, _fixture_repo, _cache_dir):
        raise WorkspaceError("uv sync failed", stderr="No solution found for dependencies\n")

    monkeypatch.setattr(cli, "ensure_case_bare_repo", create_bare_repo)
    monkeypatch.setattr(cli, "ensure_case_venv", fail_venv)

    result = cli._do_prepare(
        repo_root=repo,
        frameworks=[],
        cases=cases,
        cache_dir=cache,
        base_env={},
        dotenv={},
        setup_timeout_s=30,
    )

    assert result.failed is True
    assert result.case_failed is True
    assert result.summary == [
        "case case-001: venv FAIL: uv sync failed: No solution found for dependencies"
    ]


def test_eval_all_skips_fresh_framework_setup_fail_when_case_prepare_needed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_setup_framework(repo)
    setup_script = repo / "frameworks" / "setup-fw" / "setup.sh"
    setup_marker = repo / "frameworks" / "setup-fw" / "setup-ran.txt"
    setup_script.write_text("#!/bin/sh\necho ran >> setup-ran.txt\nexit 7\n")
    setup_script.chmod(0o755)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)

    cache = repo / ".runs-cache"
    setup_dir = cache / "setup"
    setup_dir.mkdir(parents=True)
    frameworks, _ = discover_frameworks(repo)
    (setup_dir / "setup-fw.fail").write_text(
        json.dumps(
            {
                "reason": "nonzero_exit",
                "fingerprint": cli.setup_fingerprint(frameworks[0]),
            }
        )
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    def create_bare_repo(_repo_root, case_id, cache_dir, _fixture_repo):
        bare_repo = cache_dir / f"{case_id}.git"
        bare_repo.mkdir(parents=True, exist_ok=True)
        return bare_repo

    def create_venv(_repo_root, case_id, _fixture_repo, cache_dir):
        venv = cache_dir / f"{case_id}.venv"
        venv.mkdir(parents=True, exist_ok=True)
        return venv

    attempted_cells: list[tuple[str, str]] = []

    def record_cell_attempt(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "ensure_case_bare_repo", create_bare_repo)
    monkeypatch.setattr(cli, "ensure_case_venv", create_venv)
    monkeypatch.setattr(cli, "_run_one_cell", record_cell_attempt)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)

    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)

    assert rc == 0
    assert attempted_cells == [("setup-fw", "case-001")]
    assert not setup_marker.exists(), (
        "eval-all must not retry a fresh failed framework setup just because case prep ran"
    )


def test_eval_all_continues_after_framework_setup_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_setup_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    def create_bare_repo(_repo_root, case_id, cache_dir, _fixture_repo):
        bare_repo = cache_dir / f"{case_id}.git"
        bare_repo.mkdir(parents=True, exist_ok=True)
        return bare_repo

    def create_venv(_repo_root, case_id, _fixture_repo, cache_dir):
        venv = cache_dir / f"{case_id}.venv"
        venv.mkdir(parents=True, exist_ok=True)
        return venv

    def fail_framework_setup(fw, *, cache_dir, base_env, dotenv, timeout_s):
        setup_dir = cache_dir / "setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        (setup_dir / f"{fw.name}.fail").write_text('{"reason":"nonzero_exit"}')
        return SetupResult(
            framework=fw.name,
            status="failed",
            reason="nonzero_exit",
            exit_code=1,
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0.0,
        )

    attempted_cells: list[tuple[str, str]] = []

    def record_cell_attempt(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "ensure_case_bare_repo", create_bare_repo)
    monkeypatch.setattr(cli, "ensure_case_venv", create_venv)
    monkeypatch.setattr(cli, "run_framework_setup", fail_framework_setup)
    monkeypatch.setattr(cli, "_run_one_cell", record_cell_attempt)

    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)

    assert rc == 0
    assert attempted_cells == [("setup-fw", "case-001")]


def test_eval_rejects_framework_outside_current_campaign_matrix(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo, name="good")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )
    _write_good_framework(repo, name="late-fw")

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: False)
    attempted_cells: list[tuple[str, str]] = []

    def fake_run_one_cell(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))

    monkeypatch.setattr(cli, "_run_one_cell", fake_run_one_cell)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)

    args = cli._build_parser().parse_args(["eval", "late-fw", "case-001"])
    rc = cli.cmd_eval(args)

    assert rc == 2
    assert attempted_cells == []
    assert not (campaign_dir / "late-fw").exists()
    err = capsys.readouterr().err
    assert "current campaign" in err
    assert "late-fw" in err


def test_eval_rejects_case_outside_current_campaign_matrix(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo, name="good")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture, case_id="case-001")
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )
    late_fixture = tmp_path / "late-fixture"
    late_fixture.mkdir()
    _write_good_case(repo, late_fixture, case_id="late-case")

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)
    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: False)
    attempted_cells: list[tuple[str, str]] = []

    def fake_run_one_cell(**kwargs):
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))

    monkeypatch.setattr(cli, "_run_one_cell", fake_run_one_cell)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)

    args = cli._build_parser().parse_args(["eval", "good", "late-case"])
    rc = cli.cmd_eval(args)

    assert rc == 2
    assert attempted_cells == []
    assert not (campaign_dir / "good" / "late-case").exists()
    err = capsys.readouterr().err
    assert "current campaign" in err
    assert "late-case" in err


def test_eval_auto_prepares_selected_cell_when_cache_missing(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    prepared: dict[str, object] = {"done": False}

    def fake_prepare_needed(_repo_root, frameworks, cases, _cache_dir):
        prepared["frameworks"] = [fw.name for fw in frameworks]
        prepared["cases"] = [case.case_id for case in cases]
        return True

    def fake_do_prepare(**kwargs):
        prepared["done"] = True
        assert [fw.name for fw in kwargs["frameworks"]] == ["good"]
        assert [case.case_id for case in kwargs["cases"]] == ["case-001"]
        return cli._PrepareResult(
            summary=["case case-001: ok", "framework good: skipped (no setup)"],
            failed=False,
            case_failed=False,
        )

    attempted_cells: list[tuple[str, str]] = []

    def fake_run_one_cell(**kwargs):
        assert prepared["done"], "single-cell eval must prepare missing caches before running"
        assert kwargs["cell_dir"] == campaign_dir / "good" / "case-001"
        attempted_cells.append((kwargs["fw"].name, kwargs["case"].case_id))
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "_prepare_needed", fake_prepare_needed)
    monkeypatch.setattr(cli, "_do_prepare", fake_do_prepare)
    monkeypatch.setattr(cli, "_run_one_cell", fake_run_one_cell)
    monkeypatch.setattr(cli, "write_report", lambda _campaign_dir: None)

    args = cli._build_parser().parse_args(["eval", "good", "case-001"])
    rc = cli.cmd_eval(args)

    assert rc == 0
    assert prepared["frameworks"] == ["good"]
    assert prepared["cases"] == ["case-001"]
    assert attempted_cells == [("good", "case-001")]


def test_eval_report_regenerates_report_while_holding_lock(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    actions: list[str] = []
    locked = False

    @contextmanager
    def fake_lock(campaign_dir_arg, *, argv, force_unlock=False):
        nonlocal locked
        assert campaign_dir_arg == campaign_dir
        actions.append("lock")
        locked = True
        try:
            yield
        finally:
            locked = False
            actions.append("unlock")

    def fake_write_report(campaign_dir_arg: Path) -> None:
        assert campaign_dir_arg == campaign_dir
        assert locked, "eval-report must rewrite report.md while holding the campaign lock"
        actions.append("report")

    monkeypatch.setattr(cli, "lock", fake_lock)
    monkeypatch.setattr(cli, "write_report", fake_write_report)

    args = cli._build_parser().parse_args(["eval-report"])
    rc = cli.cmd_eval_report(args)

    assert rc == 0
    assert actions == ["lock", "report", "unlock"]
    assert str(campaign_dir / "report.md") in capsys.readouterr().out


def test_eval_regenerates_report_after_rerun_while_holding_lock(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    actions: list[str] = []
    locked = False

    @contextmanager
    def fake_lock(campaign_dir_arg, *, argv, force_unlock=False):
        nonlocal locked
        assert campaign_dir_arg == campaign_dir
        actions.append("lock")
        locked = True
        try:
            yield
        finally:
            locked = False
            actions.append("unlock")

    def fake_run_one_cell(**kwargs):
        assert locked, "cell rerun must happen under the campaign lock"
        actions.append("run")
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    def fake_write_report(campaign_dir_arg: Path) -> None:
        assert campaign_dir_arg == campaign_dir
        assert locked, "report regeneration must happen before releasing the lock"
        actions.append("report")

    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: False)
    monkeypatch.setattr(cli, "lock", fake_lock)
    monkeypatch.setattr(cli, "_run_one_cell", fake_run_one_cell)
    monkeypatch.setattr(cli, "write_report", fake_write_report)

    args = cli._build_parser().parse_args(["eval", "good", "case-001"])
    rc = cli.cmd_eval(args)

    assert rc == 0
    assert actions == ["lock", "run", "report", "unlock"]


def test_eval_all_auto_prepare_runs_while_holding_campaign_lock(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_good_framework(repo)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    _write_good_case(repo, fixture)
    campaign_dir = cli.eval_new(
        repo,
        frameworks=["good"],
        cases=["case-001"],
        config_overrides={},
    )

    monkeypatch.setattr(cli, "_repo_root", lambda: repo)

    actions: list[str] = []
    locked = False

    @contextmanager
    def fake_lock(campaign_dir_arg, *, argv, force_unlock=False):
        nonlocal locked
        assert campaign_dir_arg == campaign_dir
        actions.append("lock")
        locked = True
        try:
            yield
        finally:
            locked = False
            actions.append("unlock")

    def fake_do_prepare(**_kwargs):
        actions.append("prepare:locked" if locked else "prepare:unlocked")
        return cli._PrepareResult(summary=[], failed=False, case_failed=False)

    def fake_run_one_cell(**kwargs):
        actions.append("run:locked" if locked else "run:unlocked")
        kwargs["cell_dir"].mkdir(parents=True, exist_ok=True)

    def fake_write_report(_campaign_dir: Path) -> None:
        actions.append("report:locked" if locked else "report:unlocked")

    monkeypatch.setattr(cli, "_prepare_needed", lambda *_args: True)
    monkeypatch.setattr(cli, "_do_prepare", fake_do_prepare)
    monkeypatch.setattr(cli, "lock", fake_lock)
    monkeypatch.setattr(cli, "_run_one_cell", fake_run_one_cell)
    monkeypatch.setattr(cli, "write_report", fake_write_report)

    args = cli._build_parser().parse_args(["eval-all"])
    rc = cli.cmd_eval_all(args)

    assert rc == 0
    assert actions == [
        "lock",
        "prepare:locked",
        "run:locked",
        "report:locked",
        "unlock",
    ]
