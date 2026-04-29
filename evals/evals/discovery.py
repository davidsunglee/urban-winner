import json
from dataclasses import dataclass
from pathlib import Path

from evals.schemas import validate_case_manifest, validate_framework_manifest


@dataclass(frozen=True)
class FrameworkSpec:
    name: str
    dir: Path
    manifest_path: Path
    entry: str
    setup: str | None
    env_keys: list[str]
    model: str


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    manifest_path: Path
    fixture_repo: Path  # absolute
    failing_test_command: str
    hidden_test_command: str | None
    failure_output: str  # always resolved (file → string)
    edit_constraints: dict
    notes: str | None


@dataclass(frozen=True)
class DiscoveryError:
    kind: str  # "framework" | "case"
    name: str
    manifest_path: Path
    messages: list[str]


def discover_frameworks(
    repo_root: Path,
) -> tuple[list[FrameworkSpec], list[DiscoveryError]]:
    frameworks_dir = repo_root / "frameworks"
    specs: list[FrameworkSpec] = []
    errors: list[DiscoveryError] = []

    if not frameworks_dir.is_dir():
        return [], []

    for fw_dir in sorted(frameworks_dir.iterdir()):
        if not fw_dir.is_dir():
            continue
        manifest_path = fw_dir / "manifest.json"
        if not manifest_path.exists():
            continue  # silently skip (README-only dirs, etc.)

        try:
            raw = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(
                DiscoveryError(
                    kind="framework",
                    name=fw_dir.name,
                    manifest_path=manifest_path,
                    messages=[f"invalid JSON: {exc}"],
                )
            )
            continue

        messages = validate_framework_manifest(raw)
        if messages:
            errors.append(
                DiscoveryError(
                    kind="framework",
                    name=fw_dir.name,
                    manifest_path=manifest_path,
                    messages=messages,
                )
            )
            continue

        specs.append(
            FrameworkSpec(
                name=fw_dir.name,
                dir=fw_dir,
                manifest_path=manifest_path,
                entry=raw["entry"],
                setup=raw.get("setup"),
                env_keys=raw.get("env", []),
                model=raw["model"],
            )
        )

    specs.sort(key=lambda s: s.name)
    return specs, errors


def discover_cases(
    repo_root: Path,
) -> tuple[list[CaseSpec], list[DiscoveryError]]:
    cases_dir = repo_root / "cases"
    specs: list[CaseSpec] = []
    errors: list[DiscoveryError] = []

    if not cases_dir.is_dir():
        return [], []

    for case_path in sorted(cases_dir.glob("*.json")):
        try:
            raw = json.loads(case_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(
                DiscoveryError(
                    kind="case",
                    name=case_path.stem,
                    manifest_path=case_path,
                    messages=[f"invalid JSON: {exc}"],
                )
            )
            continue

        messages = validate_case_manifest(raw)
        if messages:
            errors.append(
                DiscoveryError(
                    kind="case",
                    name=raw.get("case_id", case_path.stem),
                    manifest_path=case_path,
                    messages=messages,
                )
            )
            continue

        # Resolve failure_output
        if "failure_output" in raw:
            failure_output = raw["failure_output"]
        else:
            fop = Path(raw["failure_output_path"])
            if not fop.is_absolute():
                fop = repo_root / fop
            failure_output = fop.read_text(encoding="utf-8", errors="replace")

        # Resolve fixture_repo to absolute
        fixture_repo = Path(raw["fixture_repo"])
        if not fixture_repo.is_absolute():
            fixture_repo = repo_root / fixture_repo

        specs.append(
            CaseSpec(
                case_id=raw["case_id"],
                manifest_path=case_path,
                fixture_repo=fixture_repo,
                failing_test_command=raw["failing_test_command"],
                hidden_test_command=raw.get("hidden_test_command"),
                failure_output=failure_output,
                edit_constraints=raw.get("edit_constraints", {}),
                notes=raw.get("notes"),
            )
        )

    specs.sort(key=lambda s: s.case_id)
    return specs, errors
