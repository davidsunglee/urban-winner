# Task Spec: Software Bugfix Benchmark (v1)

Every framework in `frameworks/` implements this benchmark. The agent receives a small repository with a known failing test, diagnoses the root cause, applies a minimal in-place fix to the provided mutable worktree, and returns a structured report. The eval harness in `evals/` independently re-runs the test, derives the canonical diff from the worktree, and reports per-category scores. This spec defines the contract only — the harness, fixture repos, and per-framework agents are out of scope here.

## V1 Scope

- Ships with one fixture case.
- The case format below is designed so additional fixture cases can be added later without changing the agent-facing concept of the task.
- This spec defines the contract only — implementing the harness, fixtures, and per-framework agents is out of scope here.

## Case Format

A case is the unit a future harness will load from disk. Each case has the following fields:

- `case_id` — string identifier for the case (e.g. `py-divisor-001`).
- `fixture_repo` — path to the canonical fixture repository owned by the harness; this is the read-only source of truth and is never edited in place.
- `failing_test_command` — exact shell command that reproduces the failure (e.g. `pytest -q tests/test_divisor.py::test_safe_divide`).
- `failure_output` — captured stdout/stderr from one clean run of `failing_test_command` against the fixture, recorded at case authoring time; this is the CI-style triage signal handed to the agent. Cases may instead provide `failure_output_path`, a repo-relative path to a UTF-8 sidecar file containing the captured output, when the trace is large or JSON-escapes poorly. A case must provide exactly one of `failure_output` or `failure_output_path`; the harness reads the file when the path form is used and substitutes the resolved string into `input.failure_output` either way.
- `edit_constraints` — object controlling which files the agent may modify:
  - `disallowed_paths` (optional) — list of repo-relative globs the agent must not modify. Omit to use the default in *Default Edit Constraints* below.
  - `allowed_paths` (optional) — list of repo-relative globs the agent is restricted to; omit for cases that do not need containment.
  - `max_changed_files` (optional) — integer upper bound on the number of files the agent may change.
- `hidden_test_command` (optional) — a second test command the harness runs after the agent exits; used to detect fixes that pass the visible test but break unrelated behavior.
- `notes` (optional) — case-author commentary stored alongside the case but never surfaced to the agent.

### Default Edit Constraints

When a case omits per-case constraint fields — including the case where `edit_constraints` is provided as an empty object `{}` — the harness applies these defaults to the missing fields:

- `disallowed_paths` defaults to globs covering test files, fixtures, lockfiles, changelogs, and `.git/` metadata: `tests/**`, `**/*test*`, `**/*fixture*`, `**/*lock*`, `**/CHANGELOG*`, `.git/**`. This exists to prevent the obvious gaming patterns of editing tests or pinned dependencies.
- `allowed_paths` defaults to **unrestricted** — anything not in `disallowed_paths` is editable. The default is loose so the constraint itself does not reveal the fix location.
- `max_changed_files` defaults to `5`.
- `hidden_test_command` defaults to absent.

## Agent Input (per run)

The harness passes the following JSON object inside the contract envelope's `input` field:

```json
{
  "case_id": "string",
  "repo_path": "/absolute/path/to/per-run/worktree",
  "failing_test_command": "string",
  "failure_output": "string",
  "edit_constraints": {
    "disallowed_paths": ["string", "..."],
    "allowed_paths": ["string", "..."],
    "max_changed_files": 5
  }
}
```

`allowed_paths` and `max_changed_files` are optional — they may be absent when the case uses defaults.

`repo_path` is **mutable** for the lifetime of the run and is the agent's sandbox; the agent must edit the worktree in place. The harness creates one worktree per `(framework, run)` pair so concurrent runs cannot contaminate each other. The agent must NOT create, reset, or clean up worktrees — those are harness responsibilities.

## Expected Agent Behavior

The agent's run loop:

1. Read `failing_test_command` and `failure_output` to form a working hypothesis.
2. Inspect the worktree at `repo_path` (file listing, search, file reads, optionally running the failing test) to identify the root cause.
3. Apply a minimal in-place edit to the worktree that addresses the root cause without violating `edit_constraints`.
4. Optionally re-run `failing_test_command` to confirm the fix locally.
5. Return the structured report defined in `## Agent Output Schema`.

The agent does NOT need to:

- Generate, return, or apply a separate patch file. The worktree itself IS the submission.
- Declare an authoritative `fixed` / `not_fixed` verdict. The harness determines the visible outcome by re-running the test.
- Manage isolation, reset, or cleanup of the worktree.

## Required Agent Capabilities

Every framework implementation must give its agent at least these capabilities. Names, function signatures, and tool schemas are framework-native — only the capability surface is shared:

- **File inspection** — list directory contents, read files (full or partial).
- **File search** — substring or pattern search across the repo (rg-equivalent or framework-native).
- **File editing or patch application** — modify files in place; either string-edit operations or applying unified diffs is acceptable.
- **Test execution** — run arbitrary shell commands inside `repo_path`, primarily `failing_test_command`.
- **Diff inspection** — view the current uncommitted changes against the worktree's pristine state (e.g. `git diff`).

Only the capability surface is shared across frameworks — names, function signatures, and tool schemas are allowed to be framework-native. The framework's own README should briefly note how each capability is provided.

## Agent Output Schema

The agent must return the following JSON object inside the contract envelope's `output` field:

```json
{
  "root_cause": "string",
  "summary": "string",
  "changed_files": ["repo/relative/path", "..."],
  "tests_run": [
    { "command": "string", "exit_code": 0, "summary": "string" }
  ],
  "evidence": "string",
  "confidence": 0.0
}
```

Field semantics:

- `root_cause` — short natural-language description of why the test was failing.
- `summary` — short narrative of what was changed.
- `changed_files` — agent's account of repo-relative files it modified. **Informational** — the harness derives the authoritative list from the worktree.
- `tests_run` — agent-side records of test invocations (`command`, `exit_code`, `summary`). **Informational** — the harness reruns tests independently.
- `evidence` — free-form supporting observations (relevant snippets, stack-trace excerpts, examples).
- `confidence` — agent's self-assessed likelihood the fix is correct, in `[0.0, 1.0]`.

The schema **MUST NOT** include a top-level `fixed` / `not_fixed` / `status` field that the harness would treat as authoritative. In v1, `confidence` and `evidence` together are the agent's self-assessment surface. The forbidden fields `fixed`, `not_fixed`, and `status` must not appear as top-level keys in the output object.

## Harness Responsibilities

The harness owns everything that requires consistency across frameworks:

- **Worktree lifecycle** — derive a fresh isolated worktree from `fixture_repo` for every `(framework, run)` pair; reset/destroy it after the run.
- **Diff derivation** — after the agent exits, run `git diff` (or equivalent) against the pristine worktree state to compute the canonical diff and the canonical changed-file list. This is **authoritative** over `output.changed_files`.
- **Visible test outcome** — re-run `failing_test_command` against the post-run worktree and record exit code + output. Both `failing_test_command` and `hidden_test_command` are executed with the working directory set to the per-run worktree root (i.e., the materialized `fixture_repo`); the commands are written assuming this `cwd` and may use repo-relative paths. The harness derives the canonical visible outcome; this is **authoritative** over `output.tests_run`.
- **Hidden test outcome** — when the case provides `hidden_test_command`, run it post-fix (same `cwd` rule) and record the result as a separate scoring category.
- **Edit-constraint compliance** — check the canonical changed-file list against `disallowed_paths`, `allowed_paths`, and `max_changed_files`.
- **Trace capture** — read `trace.steps`, `trace.tokens`, and `trace.latency_ms` from the response envelope for scoring.
- **Cleanup** — destroy the worktree after scoring.

Any conflict between an agent-reported field and a harness-derived observation is resolved in favor of the harness.

## Scoring Categories

Scoring is reported as independent per-category results with no aggregate across categories. Aggregation and ranking are deferred until v1 produces real benchmark data showing which categories are stable and informative — there is no single aggregate score in v1.

- `schema_validity` — boolean. Did the response envelope and `output` parse against this contract?
- `visible_test_outcome` — `pass` | `fail` | `error`. Result of harness rerun of `failing_test_command`.
- `hidden_test_outcome` — `pass` | `fail` | `error` | `n/a`. Result of harness run of `hidden_test_command`, or `n/a` when the case has none.
- `edit_constraint_compliance` — object: `{ disallowed_violations: [...], allowed_violations: [...], over_max_changed_files: bool }`. Empty arrays and `false` mean clean.
- `minimality` — object: `{ changed_files: int, changed_lines_added: int, changed_lines_removed: int }`. Reported descriptively in v1, not pass/fail. Smaller is generally better.
- `trace_quality` — qualitative or rubric-graded assessment of whether the trace shows a sensible debugging workflow (did the agent inspect before editing? rerun the test? read related files?). This is **not** a check that specific tool names appear — not strict conformance to one fixed sequence of tool calls.
- `latency_ms` — wall-clock time from request send to response receive.
- `token_usage` — `{ input: int, output: int }` when the framework reports it; otherwise omitted.

## Out of Scope for v1

- Implementing the harness, fixture repositories, or per-framework agents.
- Multiple fixture cases (one is enough; the format extends to additional cases).
- A weighted leaderboard or single overall score.
- Standardizing exact tool names, function signatures, or framework-specific schemas.
- Issue-style natural-language bug reports as the primary v1 input signal (CI-style triage only).
- Treating agent-returned patches as authoritative over the worktree diff.
- Asking agents to create, manage, or clean up their own worktrees.
