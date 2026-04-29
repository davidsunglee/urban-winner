from pathlib import Path

from dotenv import dotenv_values

# BASE_KEYS forwarded verbatim; PATH is handled separately below.
BASE_KEYS = ("HOME", "LANG", "TERM")


def load_dotenv(repo_root: Path) -> dict[str, str]:
    env_file = repo_root / ".env"
    if not env_file.exists():
        return {}
    raw = dotenv_values(env_file)
    return {k: v for k, v in raw.items() if v is not None}


def _build_path(venv: Path | None, inherited_path: str) -> str:
    # v1 prepends <venv>/bin and inherits PATH verbatim; "minus user-local additions" is aspirational.
    if venv is None:
        return inherited_path
    return f"{venv.resolve()}/bin:{inherited_path}"


def build_agent_env(
    *,
    declared_keys: list[str],
    case_venv_path: Path | None,
    base_env: dict[str, str],
    dotenv: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in BASE_KEYS:
        if k in base_env:
            out[k] = base_env[k]
    out["PATH"] = _build_path(case_venv_path, base_env.get("PATH", ""))
    if case_venv_path is not None:
        out["UV_PROJECT_ENVIRONMENT"] = str(case_venv_path.resolve())
    merged_secrets = {**base_env, **dotenv}
    for k in declared_keys:
        if k in merged_secrets:
            out[k] = merged_secrets[k]
    return out


def build_test_env(
    *,
    case_venv_path: Path,
    cell_repo_path: Path,
    base_env: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in BASE_KEYS:
        if k in base_env:
            out[k] = base_env[k]
    out["PATH"] = _build_path(case_venv_path, base_env.get("PATH", ""))
    out["UV_PROJECT_ENVIRONMENT"] = str(case_venv_path.resolve())
    # The case venv is built with `uv sync --no-install-project`, so the
    # project itself is not in site-packages. Without UV_NO_SYNC, `uv run`
    # would sync (install) the project into the shared case venv during
    # visible/hidden test reruns, mutating it across cells and invalidating
    # the venv-mutation invariant. Pin the venv read-only and surface the
    # cell's checked-out source via PYTHONPATH instead.
    out["UV_NO_SYNC"] = "1"
    out["PYTHONPATH"] = str(cell_repo_path.resolve())
    # No declared framework keys — test reruns are deterministic and never see secrets.
    return out


def build_setup_env(
    *,
    declared_keys: list[str],
    base_env: dict[str, str],
    dotenv: dict[str, str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in BASE_KEYS:
        if k in base_env:
            out[k] = base_env[k]
    out["PATH"] = base_env.get("PATH", "")
    merged = {**base_env, **dotenv}
    for k in declared_keys:
        if k in merged:
            out[k] = merged[k]
    return out
