import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from evals.campaign import LockBusyError, current_campaign, eval_new, lock
from evals.discovery import discover_cases, discover_frameworks
from evals.env import load_dotenv
from evals.pipeline import run_pipeline
from evals.report import write_report
from evals.runner import resolve_effective_config, run_cell
from evals.setup import run_framework_setup, setup_fingerprint
from evals.status import print_status
from evals.workspace import (
    clone_cell_worktree,
    compute_fixture_hash,
    compute_lock_hash,
    compute_venv_fingerprint,
    ensure_case_bare_repo,
    ensure_case_venv,
)


def _repo_root() -> Path:
    # The package lives at <repo_root>/evals/evals/cli.py.
    return Path(__file__).resolve().parents[2]


def _build_overrides(args) -> dict:
    overrides: dict = {}
    if getattr(args, "model", None) is not None:
        overrides["model"] = args.model
    if getattr(args, "timeout_s", None) is not None:
        overrides["timeout_s"] = args.timeout_s
    if getattr(args, "max_steps", None) is not None:
        overrides["max_steps"] = args.max_steps
    return overrides


def _campaign_overrides(campaign_dir: Path) -> dict:
    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    raw = manifest.get("config_overrides", {})
    return {k: v for k, v in raw.items() if v is not None}


def _report_case_discovery_errors(errors) -> bool:
    if not errors:
        return False
    for err in errors:
        print(
            f"error: case {err.name} skipped: {'; '.join(err.messages)}",
            file=sys.stderr,
        )
    return True


@dataclass(frozen=True)
class _PrepareResult:
    summary: list[str]
    failed: bool
    case_failed: bool


def _run_one_cell(
    *,
    repo_root: Path,
    fw,
    case,
    campaign_overrides: dict,
    cell_overrides: dict,
    cell_dir: Path,
    cache_dir: Path,
    base_env: dict[str, str],
    dotenv: dict[str, str],
) -> None:
    cell_dir.mkdir(parents=True, exist_ok=True)
    effective_config = resolve_effective_config(
        fw,
        campaign_overrides=campaign_overrides,
        cell_overrides=cell_overrides,
        harness_defaults={},
    )

    bare_repo = cache_dir / f"{case.case_id}.git"
    clone_cell_worktree(bare_repo, cell_dir / "repo")

    case_venv = cache_dir / f"{case.case_id}.venv"
    venv_hash_before = compute_venv_fingerprint(case_venv) if case_venv.exists() else ""

    runner_result = run_cell(
        framework=fw,
        case=case,
        effective_config=effective_config,
        cell_dir=cell_dir,
        cache_dir=cache_dir,
        repo_root=repo_root,
        base_env=base_env,
        dotenv=dotenv,
    )
    run_pipeline(
        cell_dir,
        runner_result,
        framework=fw,
        case=case,
        effective_config=effective_config,
        cache_dir=cache_dir,
        base_env=base_env,
        venv_hash_before=venv_hash_before,
    )


def _prepare_needed(repo_root: Path, frameworks, cases, cache_dir: Path) -> bool:
    for fw in frameworks:
        if fw.discovery_error is not None:
            continue  # misconfigured manifest; setup not runnable
        if fw.setup is None:
            continue
        setup_dir = cache_dir / "setup"
        ok_path = setup_dir / f"{fw.name}.ok"
        fail_path = setup_dir / f"{fw.name}.fail"
        if fail_path.exists():
            return True
        if not ok_path.exists():
            return True
        try:
            data = json.loads(ok_path.read_text())
            current_fingerprint = setup_fingerprint(fw)
        except Exception:
            return True
        if (data.get("fingerprint") or data.get("hash")) != current_fingerprint:
            return True
    for case in cases:
        if not (cache_dir / f"{case.case_id}.git").exists():
            return True
        if not (cache_dir / f"{case.case_id}.venv").exists():
            return True
        # Detect mutated fixtures or lock files even when the cached layers
        # still exist on disk: ensure_case_bare_repo / ensure_case_venv key
        # off these hash files, so a stale or missing one means the next
        # eval-all would otherwise reuse outdated cache contents.
        fixture_hash_file = cache_dir / f"{case.case_id}.fixture-hash"
        if not fixture_hash_file.exists():
            return True
        try:
            current_fixture_hash = compute_fixture_hash(
                repo_root, case.case_id, case.fixture_repo
            )
        except Exception:
            return True
        if fixture_hash_file.read_text().strip() != current_fixture_hash:
            return True

        lock_hash_file = cache_dir / f"{case.case_id}.lock-hash"
        if not lock_hash_file.exists():
            return True
        try:
            current_lock_hash = compute_lock_hash(case.fixture_repo)
        except Exception:
            return True
        if lock_hash_file.read_text().strip() != current_lock_hash:
            return True
    return False


def _do_prepare(
    *,
    repo_root: Path,
    frameworks,
    cases,
    cache_dir: Path,
    base_env: dict[str, str],
    dotenv: dict[str, str],
    setup_timeout_s: int,
) -> _PrepareResult:
    summary: list[str] = []
    failed = False
    case_failed = False

    for case in cases:
        try:
            ensure_case_bare_repo(repo_root, case.case_id, cache_dir, case.fixture_repo)
        except Exception as exc:
            failed = True
            case_failed = True
            summary.append(f"case {case.case_id}: bare-repo FAIL: {exc}")
            continue
        try:
            ensure_case_venv(repo_root, case.case_id, case.fixture_repo, cache_dir)
            summary.append(f"case {case.case_id}: ok")
        except Exception as exc:
            failed = True
            case_failed = True
            summary.append(f"case {case.case_id}: venv FAIL: {exc}")

    for fw in frameworks:
        if fw.discovery_error is not None:
            failed = True
            summary.append(
                f"framework {fw.name}: misconfigured ({'; '.join(fw.discovery_error.messages)})"
            )
            continue
        if fw.setup is None:
            summary.append(f"framework {fw.name}: skipped (no setup)")
            continue
        result = run_framework_setup(
            fw,
            cache_dir=cache_dir,
            base_env=base_env,
            dotenv=dotenv,
            timeout_s=setup_timeout_s,
        )
        suffix = f" ({result.reason})" if result.reason else ""
        summary.append(f"framework {fw.name}: {result.status}{suffix}")
        if result.status == "failed":
            failed = True

    return _PrepareResult(summary=summary, failed=failed, case_failed=case_failed)


# --------------------------------------------------------------------------
# Subcommand handlers
# --------------------------------------------------------------------------

def cmd_frameworks(args) -> int:
    repo_root = _repo_root()
    specs, errors = discover_frameworks(repo_root)
    for spec in specs:
        if spec.discovery_error is not None:
            print(f"{spec.name}\t(misconfigured)")
        else:
            print(spec.name)
    for err in errors:
        print(
            f"warning: framework {err.name} misconfigured: {'; '.join(err.messages)}",
            file=sys.stderr,
        )
    return 0


def cmd_cases(args) -> int:
    repo_root = _repo_root()
    specs, errors = discover_cases(repo_root)
    for spec in specs:
        try:
            rel = spec.fixture_repo.relative_to(repo_root)
        except ValueError:
            rel = spec.fixture_repo
        print(f"{spec.case_id}\t{rel}")
    for err in errors:
        print(
            f"warning: case {err.name} skipped: {'; '.join(err.messages)}",
            file=sys.stderr,
        )
    return 0


def cmd_eval_prepare(args) -> int:
    repo_root = _repo_root()
    frameworks, _ = discover_frameworks(repo_root)
    cases, case_errors = discover_cases(repo_root)
    if _report_case_discovery_errors(case_errors):
        return 1
    cache_dir = repo_root / ".runs-cache"
    base_env = os.environ.copy()
    dotenv = load_dotenv(repo_root)

    prepare_result = _do_prepare(
        repo_root=repo_root,
        frameworks=frameworks,
        cases=cases,
        cache_dir=cache_dir,
        base_env=base_env,
        dotenv=dotenv,
        setup_timeout_s=args.setup_timeout_s,
    )
    for line in prepare_result.summary:
        print(line)
    return 1 if prepare_result.failed else 0


def cmd_eval_new(args) -> int:
    repo_root = _repo_root()
    frameworks, _ = discover_frameworks(repo_root)
    cases, case_errors = discover_cases(repo_root)
    if _report_case_discovery_errors(case_errors):
        return 1
    overrides = {
        "model": args.model,
        "timeout_s": args.timeout_s,
        "max_steps": args.max_steps,
    }
    campaign_dir = eval_new(
        repo_root,
        frameworks=[f.name for f in frameworks],
        cases=[c.case_id for c in cases],
        config_overrides=overrides,
        force_unlock=args.force_unlock,
        argv=sys.argv,
    )
    print(str(campaign_dir))
    return 0


def cmd_eval_all(args) -> int:
    repo_root = _repo_root()
    frameworks, _ = discover_frameworks(repo_root)
    cases, case_errors = discover_cases(repo_root)
    if _report_case_discovery_errors(case_errors):
        return 1

    # Fail fast on unknown filter values so typos like `--framework caude`
    # don't silently no-op (and don't trigger campaign creation / prepare).
    if args.framework and args.framework not in {f.name for f in frameworks}:
        known = ", ".join(sorted(f.name for f in frameworks)) or "<none>"
        print(
            f"unknown framework: {args.framework!r}; known frameworks: {known}",
            file=sys.stderr,
        )
        return 2
    if args.case and args.case not in {c.case_id for c in cases}:
        known = ", ".join(sorted(c.case_id for c in cases)) or "<none>"
        print(
            f"unknown case: {args.case!r}; known cases: {known}",
            file=sys.stderr,
        )
        return 2

    overrides = _build_overrides(args)
    existing = current_campaign(repo_root)

    if existing is not None and overrides:
        flag = next(iter(overrides)).replace("_", "-")
        msg = (
            f"--{flag} passed but campaign already exists; "
            f"use 'just eval-new --{flag} X' to start a fresh campaign with overrides, "
            f"or omit the flag to fill missing cells with the campaign's config."
        )
        print(msg, file=sys.stderr)
        return 2

    if existing is None:
        campaign_dir = eval_new(
            repo_root,
            frameworks=[f.name for f in frameworks],
            cases=[c.case_id for c in cases],
            config_overrides={
                "model": args.model,
                "timeout_s": args.timeout_s,
                "max_steps": args.max_steps,
            },
        )
    else:
        campaign_dir = existing

    cache_dir = repo_root / ".runs-cache"
    base_env = os.environ.copy()
    dotenv = load_dotenv(repo_root)

    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    campaign_overrides = _campaign_overrides(campaign_dir)

    fw_run = [f for f in frameworks if f.name in manifest["frameworks"]]
    case_run = [c for c in cases if c.case_id in manifest["cases"]]
    if args.framework:
        fw_run = [f for f in fw_run if f.name == args.framework]
    if args.case:
        case_run = [c for c in case_run if c.case_id == args.case]

    if _prepare_needed(repo_root, fw_run, case_run, cache_dir):
        prepare_result = _do_prepare(
            repo_root=repo_root,
            frameworks=fw_run,
            cases=case_run,
            cache_dir=cache_dir,
            base_env=base_env,
            dotenv=dotenv,
            setup_timeout_s=600,
        )
        for line in prepare_result.summary:
            print(line)
        if prepare_result.case_failed:
            print("aborting eval-all due to case prepare failure", file=sys.stderr)
            return 1

    with lock(campaign_dir, argv=sys.argv, force_unlock=args.force_unlock):
        for fw in fw_run:
            for case in case_run:
                cell_dir = campaign_dir / fw.name / case.case_id
                if (cell_dir / "meta.json").exists():
                    continue
                if cell_dir.exists():
                    shutil.rmtree(cell_dir)
                _run_one_cell(
                    repo_root=repo_root,
                    fw=fw,
                    case=case,
                    campaign_overrides=campaign_overrides,
                    cell_overrides={},
                    cell_dir=cell_dir,
                    cache_dir=cache_dir,
                    base_env=base_env,
                    dotenv=dotenv,
                )

        write_report(campaign_dir)
    return 0


def cmd_eval(args) -> int:
    repo_root = _repo_root()
    frameworks, _ = discover_frameworks(repo_root)
    cases, case_errors = discover_cases(repo_root)
    if _report_case_discovery_errors(case_errors):
        return 1

    fw = next((f for f in frameworks if f.name == args.framework), None)
    case = next((c for c in cases if c.case_id == args.case), None)
    if fw is None:
        print(f"unknown framework: {args.framework}", file=sys.stderr)
        return 2
    if case is None:
        print(f"unknown case: {args.case}", file=sys.stderr)
        return 2

    cell_overrides = _build_overrides(args)

    campaign_dir = current_campaign(repo_root)
    if campaign_dir is None:
        print("no current campaign; run 'eval-new' first", file=sys.stderr)
        return 2

    manifest = json.loads((campaign_dir / "manifest.json").read_text())
    if fw.name not in manifest.get("frameworks", []):
        print(
            f"framework {fw.name!r} is not in the current campaign matrix",
            file=sys.stderr,
        )
        return 2
    if case.case_id not in manifest.get("cases", []):
        print(
            f"case {case.case_id!r} is not in the current campaign matrix",
            file=sys.stderr,
        )
        return 2

    raw_overrides = manifest.get("config_overrides", {})
    campaign_overrides = {k: v for k, v in raw_overrides.items() if v is not None}
    base_env = os.environ.copy()
    dotenv = load_dotenv(repo_root)
    cache_dir = repo_root / ".runs-cache"

    if _prepare_needed(repo_root, [fw], [case], cache_dir):
        prepare_result = _do_prepare(
            repo_root=repo_root,
            frameworks=[fw],
            cases=[case],
            cache_dir=cache_dir,
            base_env=base_env,
            dotenv=dotenv,
            setup_timeout_s=600,
        )
        for line in prepare_result.summary:
            print(line)
        if prepare_result.case_failed:
            print("aborting eval due to case prepare failure", file=sys.stderr)
            return 1

    with lock(campaign_dir, argv=sys.argv, force_unlock=args.force_unlock):
        cell_dir = campaign_dir / fw.name / case.case_id
        if cell_dir.exists():
            shutil.rmtree(cell_dir)
        _run_one_cell(
            repo_root=repo_root,
            fw=fw,
            case=case,
            campaign_overrides=campaign_overrides,
            cell_overrides=cell_overrides,
            cell_dir=cell_dir,
            cache_dir=cache_dir,
            base_env=base_env,
            dotenv=dotenv,
        )
        write_report(campaign_dir)
    return 0


def cmd_eval_status(args) -> int:
    repo_root = _repo_root()
    campaign_dir = current_campaign(repo_root)
    if campaign_dir is None:
        print("no current campaign", file=sys.stderr)
        return 2
    print_status(campaign_dir)
    return 0


def cmd_eval_report(args) -> int:
    repo_root = _repo_root()
    campaign_dir = current_campaign(repo_root)
    if campaign_dir is None:
        print("no current campaign", file=sys.stderr)
        return 2
    write_report(campaign_dir)
    print(str(campaign_dir / "report.md"))
    return 0


def cmd_eval_clean_cache(args) -> int:
    repo_root = _repo_root()
    target = repo_root / ".runs-cache"
    if target.exists():
        shutil.rmtree(target)
    return 0


def cmd_eval_clean_runs(args) -> int:
    repo_root = _repo_root()
    target = repo_root / "runs"
    if target.exists():
        shutil.rmtree(target)
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("frameworks", help="list discovered frameworks").set_defaults(
        func=cmd_frameworks
    )
    sub.add_parser("cases", help="list discovered cases").set_defaults(func=cmd_cases)

    p_prepare = sub.add_parser(
        "eval-prepare", help="materialize bare repos, venvs, and run framework setups"
    )
    p_prepare.add_argument("--setup-timeout-s", type=int, default=600)
    p_prepare.set_defaults(func=cmd_eval_prepare)

    p_new = sub.add_parser(
        "eval-new", help="create a new campaign with optional config overrides"
    )
    p_new.add_argument("--model", default=None)
    p_new.add_argument("--timeout-s", dest="timeout_s", type=int, default=None)
    p_new.add_argument("--max-steps", dest="max_steps", type=int, default=None)
    p_new.add_argument("--force-unlock", dest="force_unlock", action="store_true")
    p_new.set_defaults(func=cmd_eval_new)

    p_all = sub.add_parser(
        "eval-all", help="fill missing cells in CURRENT (auto-creates campaign if absent)"
    )
    p_all.add_argument("--model", default=None)
    p_all.add_argument("--timeout-s", dest="timeout_s", type=int, default=None)
    p_all.add_argument("--max-steps", dest="max_steps", type=int, default=None)
    p_all.add_argument("--framework", default=None)
    p_all.add_argument("--case", default=None)
    p_all.add_argument("--force-unlock", dest="force_unlock", action="store_true")
    p_all.set_defaults(func=cmd_eval_all)

    p_eval = sub.add_parser("eval", help="run a single (framework, case) cell")
    p_eval.add_argument("framework")
    p_eval.add_argument("case")
    p_eval.add_argument("--model", default=None)
    p_eval.add_argument("--timeout-s", dest="timeout_s", type=int, default=None)
    p_eval.add_argument("--max-steps", dest="max_steps", type=int, default=None)
    p_eval.add_argument("--force-unlock", dest="force_unlock", action="store_true")
    p_eval.set_defaults(func=cmd_eval)

    sub.add_parser("eval-status", help="print matrix of CURRENT campaign").set_defaults(
        func=cmd_eval_status
    )
    sub.add_parser("eval-report", help="regenerate CURRENT/report.md").set_defaults(
        func=cmd_eval_report
    )
    sub.add_parser("eval-clean-cache", help="wipe .runs-cache/").set_defaults(
        func=cmd_eval_clean_cache
    )
    sub.add_parser("eval-clean-runs", help="wipe runs/").set_defaults(
        func=cmd_eval_clean_runs
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LockBusyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
