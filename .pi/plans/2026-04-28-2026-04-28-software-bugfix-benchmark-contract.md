# Plan: v1 Software Bugfix Benchmark Contract

**Source:** TODO-4f8d5efc
**Spec:** .pi/specs/2026-04-28-software-bugfix-benchmark-contract.md

## Goal

Replace the placeholder `shared/task-spec.md` with a concrete v1 software bugfix benchmark contract. The new spec must define a reusable benchmark case format, the per-run agent input, expected agent behavior, required agent capabilities, the agent output schema, harness responsibilities, and a non-aggregated set of scoring categories — all framework-agnostic, with one fixture sufficient for v1 and explicitly no weighted leaderboard.

## Architecture summary

This is a documentation-only change. The only edited file is `shared/task-spec.md`. No code, no harness, no fixture repos, and no per-framework agents are implemented as part of this plan.

The new task spec plugs into the existing transport contract in `shared/contract.md` (which already defines the JSON request/response envelope with `task_id`, `input`, `config`, `output`, `trace`, `error`). The task spec defines what goes inside `input` and `output` for the bugfix benchmark, and documents the surrounding lifecycle the harness owns (worktree creation, post-run diff derivation, post-run test rerun, scoring, cleanup).

A second verification task (no edits expected) confirms the existing surrounding docs (`README.md`, `shared/contract.md`, `evals/README.md`, `frameworks/README.md`, all `frameworks/<name>/README.md`) remain semantically consistent with the new task spec, satisfying the spec's "existing documentation remains consistent" acceptance criterion.

## Tech stack

- Markdown (CommonMark). The spec was originally tagged "Prose, not code" but JSON shape blocks are required because `shared/contract.md` already uses them for the envelope and the new task spec must show the precise shape of `input` and `output` so framework implementers can wire their adapters.
- No build system, test runner, lockfile, or CI config is touched. The repo has no test infrastructure today (`evals/pyproject.toml` declares zero dependencies; `justfile` targets call into a not-yet-implemented harness).

## File Structure

- `shared/task-spec.md` (Modify) — Currently a 22-line placeholder containing TODO bullets. Will be fully replaced with the v1 software bugfix benchmark contract using the section structure listed in Task 1, Step 2.
- `README.md` (Verify only, no edits expected) — Already says "every framework implements the same task (see `shared/task-spec.md`) against the same I/O contract (`shared/contract.md`)". Confirm this remains accurate after the rewrite.
- `shared/contract.md` (Verify only, no edits expected) — Already references `shape defined in task-spec.md` at both `input` (line 14) and `output` (line 28). Confirm those references still align with the new schema and that no envelope key (`task_id`, `input`, `output`, `trace`, `error`, `config.model`, `config.max_steps`, `config.timeout_s`) is contradicted by the new task spec.
- `evals/README.md` (Verify only, no edits expected) — Already says "scores them against `../shared/task-spec.md`". Confirm still accurate.
- `frameworks/README.md` (Verify only, no edits expected) — Already says "Each is independent... eval harness reaches in only through the contract". Confirm still accurate.
- `frameworks/<name>/README.md` × 8 (`deepagents`, `pydantic-ai`, `google-adk`, `strands`, `agentcore`, `claude-agent-sdk`, `openai-agents`, `mastra`) (Verify only, no edits expected) — Each contains "Implementation against `../../shared/task-spec.md`". Confirm still accurate.

## Tasks

### Task 1 — Rewrite `shared/task-spec.md` as the v1 software bugfix benchmark contract

Files:
- Modify: `shared/task-spec.md`

Steps:

- [ ] **Step 1: Re-read source artifacts.** Read `.pi/specs/2026-04-28-software-bugfix-benchmark-contract.md`, the current `shared/task-spec.md`, and `shared/contract.md` end-to-end. Confirm the contract envelope's exact key names (`task_id`, `input`, `config { model, max_steps, timeout_s }`, `output`, `trace { steps, tokens { input, output }, latency_ms }`, `error`) and confirm the existing task-spec body is the placeholder TODO so a full Write replacement is safe.

- [ ] **Step 2: Use the Write tool to overwrite `shared/task-spec.md`** with the new contract. The replacement document must contain the following top-level sections in this order, each fulfilling the obligations listed. Use level-2 `##` headings for top-level sections and level-3 `###` for any subsections.

  1. **Title + intro paragraph.** Title is `# Task Spec: Software Bugfix Benchmark (v1)`. One paragraph stating that every framework in `frameworks/` implements this benchmark; the agent receives a small repository with a known failing test, diagnoses the root cause, applies a minimal in-place fix to the provided mutable worktree, and returns a structured report; the eval harness in `evals/` independently re-runs the test, derives the canonical diff from the worktree, and reports per-category scores. Mention that this spec defines the contract only and that the harness, fixture repos, and per-framework agents are out of scope here.

  2. **`## V1 Scope`.** Bulleted list stating: ships with one fixture case; the case format below is designed so additional fixture cases can be added later without changing the agent-facing concept of the task; this spec defines the contract only — implementing the harness, fixtures, and per-framework agents is out of scope here.

  3. **`## Case Format`.** Describes the unit a future harness will load from disk. Must list these case-level fields with one-line descriptions each:
     - `case_id` (string, e.g. `py-divisor-001`).
     - `fixture_repo` (path to canonical fixture repository owned by the harness, read-only source of truth — never edited in place).
     - `failing_test_command` (exact shell command that reproduces the failure, e.g. `pytest -q tests/test_divisor.py::test_safe_divide`).
     - `failure_output` (captured stdout/stderr from one clean run of `failing_test_command` against the fixture, recorded at case authoring time — this is the CI-style triage signal handed to the agent).
     - `edit_constraints` (object containing `disallowed_paths` (required, list of repo-relative globs the agent must not modify), `allowed_paths` (optional, list of repo-relative globs the agent is restricted to — omit for cases that don't need containment), `max_changed_files` (optional integer upper bound)).
     - `hidden_test_command` (optional, second test command the harness runs after the agent exits — used to detect fixes that pass the visible test but break unrelated behavior).
     - `notes` (optional case-author commentary, stored alongside but never surfaced to the agent).

     Include a level-3 subsection `### Default Edit Constraints` documenting the harness defaults applied when a case omits per-case fields:
     - `disallowed_paths` defaults to globs covering test files, fixtures, lockfiles, changelogs, and `.git/` metadata (concretely list at least: `tests/**`, `**/*test*`, `**/*fixture*`, `**/*lock*`, `**/CHANGELOG*`, `.git/**`). State explicitly that this exists to prevent the obvious gaming patterns of editing tests or pinned dependencies.
     - `allowed_paths` defaults to **unrestricted** — anything not in `disallowed_paths` is editable. State explicitly that the default is loose so the constraint itself does not reveal the fix location.
     - `max_changed_files` defaults to `5`. Use this exact integer.
     - `hidden_test_command` defaults to absent.

  4. **`## Agent Input (per run)`.** Show the JSON object the harness passes inside the contract envelope's `input` field. The block must include exactly these keys:
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
     Annotate `allowed_paths` and `max_changed_files` as optional. Follow the JSON block with prose stating: `repo_path` is **mutable** for the lifetime of the run and is the agent's sandbox; the harness creates one worktree per `(framework, run)` pair so concurrent runs cannot contaminate each other; the agent must NOT create, reset, or clean up worktrees — those are harness responsibilities. Use both the words "mutable" and "in place" (or "in-place") at least once in this section.

  5. **`## Expected Agent Behavior`.** Numbered list of the agent's run loop:
     1. Read `failing_test_command` and `failure_output` to form a working hypothesis.
     2. Inspect the worktree at `repo_path` (file listing, search, file reads, optionally running the failing test) to identify the root cause.
     3. Apply a minimal in-place edit to the worktree that addresses the root cause without violating `edit_constraints`.
     4. Optionally re-run `failing_test_command` to confirm the fix locally.
     5. Return the structured report defined in `## Agent Output Schema`.

     Followed by a "The agent does NOT need to:" sub-list with at least these bullets:
     - Generate, return, or apply a separate patch file. The worktree itself IS the submission.
     - Declare an authoritative `fixed` / `not_fixed` verdict. The harness determines the visible outcome by re-running the test.
     - Manage isolation, reset, or cleanup of the worktree.

  6. **`## Required Agent Capabilities`.** Bulleted list — every framework implementation must give its agent at least these capabilities, with the explicit caveat that names, function signatures, and tool schemas are framework-native:
     - **File inspection** — list directory contents, read files (full or partial).
     - **File search** — substring or pattern search across the repo (rg-equivalent or framework-native).
     - **File editing or patch application** — modify files in place; either string-edit operations or applying unified diffs is acceptable.
     - **Test execution** — run arbitrary shell commands inside `repo_path`, primarily `failing_test_command`.
     - **Diff inspection** — view the current uncommitted changes against the worktree's pristine state (e.g. `git diff`).

     Conclude with a paragraph stating: only the capability surface is shared across frameworks — names, function signatures, and tool schemas are allowed to be framework-native. The framework's own README should briefly note how each capability is provided. Use the phrase "framework-native" verbatim at least once in this section.

  7. **`## Agent Output Schema`.** Show the JSON object the agent must return inside the contract envelope's `output` field. The block must contain exactly these keys:
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
     Follow with a field-by-field semantics list:
     - `root_cause` — short natural-language description of why the test was failing.
     - `summary` — short narrative of what was changed.
     - `changed_files` — agent's account of repo-relative files it modified. **Informational** — the harness derives the authoritative list from the worktree.
     - `tests_run` — agent-side records of test invocations (`command`, `exit_code`, `summary`). **Informational** — the harness reruns tests independently.
     - `evidence` — free-form supporting observations (relevant snippets, stack-trace excerpts, examples).
     - `confidence` — agent's self-assessed likelihood the fix is correct, in `[0.0, 1.0]`.

     Conclude with a paragraph stating: the schema **MUST NOT** include a top-level `fixed` / `not_fixed` / `status` field that the harness would treat as authoritative; `confidence` and `evidence` together are the agent's self-assessment surface in v1. Use the literal string `MUST NOT` (or `must not`) and explicitly mention the words `fixed`, `not_fixed`, or `status` in this prohibition.

  8. **`## Harness Responsibilities`.** Bulleted list, framing the harness as the owner of everything that requires consistency across frameworks:
     - **Worktree lifecycle** — derive a fresh isolated worktree from `fixture_repo` for every `(framework, run)` pair; reset/destroy it after the run.
     - **Diff derivation** — after the agent exits, run `git diff` (or equivalent) against the pristine worktree state to compute the canonical changed-file list and patch. This is **authoritative** over `output.changed_files`.
     - **Visible test outcome** — re-run `failing_test_command` against the post-run worktree and record exit code + output. This is **authoritative** over `output.tests_run`.
     - **Hidden test outcome** — when the case provides `hidden_test_command`, run it post-fix and record the result as a separate scoring category.
     - **Edit-constraint compliance** — check the canonical changed-file list against `disallowed_paths`, `allowed_paths`, and `max_changed_files`.
     - **Trace capture** — read `trace.steps`, `trace.tokens`, and `trace.latency_ms` from the response envelope for scoring.
     - **Cleanup** — destroy the worktree after scoring.

     Close the section with a sentence stating: "Any conflict between an agent-reported field and a harness-derived observation is resolved in favor of the harness." Use either `harness derives`, `derives the canonical`, or `canonical diff` somewhere in this section.

  9. **`## Scoring Categories`.** Bulleted list — each item is one independent category, with no aggregate or weighted total:
     - `schema_validity` — boolean. Did the response envelope and `output` parse against this contract?
     - `visible_test_outcome` — `pass` | `fail` | `error`. Result of harness rerun of `failing_test_command`.
     - `hidden_test_outcome` — `pass` | `fail` | `error` | `n/a`. Result of harness run of `hidden_test_command`, or `n/a` when the case has none.
     - `edit_constraint_compliance` — object: `{ disallowed_violations: [...], allowed_violations: [...], over_max_changed_files: bool }`. Empty arrays and `false` mean clean.
     - `minimality` — object: `{ changed_files: int, changed_lines_added: int, changed_lines_removed: int }`. Reported descriptively in v1, not pass/fail. Smaller is generally better.
     - `trace_quality` — qualitative or rubric-graded assessment of whether the trace shows a sensible debugging workflow (did the agent inspect before editing? rerun the test? read related files?). **Not** a check that specific tool names appear.
     - `latency_ms` — wall-clock time from request send to response receive.
     - `token_usage` — `{ input: int, output: int }` when the framework reports it; otherwise omitted.

     Close the section with a sentence stating that aggregation and ranking are deferred until v1 produces real benchmark data showing which categories are stable and informative, and use one of the literal phrases `no aggregate`, `no weighted aggregate`, `no overall`, or `no single` to make the no-leaderboard stance unambiguous.

  10. **`## Out of Scope for v1`.** Bulleted list mirroring the spec's Non-Goals:
      - Implementing the harness, fixture repositories, or per-framework agents.
      - Multiple fixture cases (one is enough; the format extends).
      - A weighted leaderboard or single overall score.
      - Standardizing exact tool names, function signatures, or framework-specific schemas.
      - Issue-style natural-language bug reports as the primary v1 input signal (CI-style triage only).
      - Treating agent-returned patches as authoritative over the worktree diff.
      - Asking agents to create, manage, or clean up their own worktrees.

- [ ] **Step 3: Self-check against acceptance criteria A1–A8 from the spec.** Re-read the new `shared/task-spec.md` and walk down each acceptance-criterion bullet from `.pi/specs/2026-04-28-software-bugfix-benchmark-contract.md` lines 38–48. For every AC, point to the section in the new file that satisfies it. If any AC has no matching section, edit the file before declaring Task 1 complete.

Acceptance criteria:

- The replaced `shared/task-spec.md` contains all required structural sections: `V1 Scope`, `Case Format`, `Agent Input`, `Expected Agent Behavior`, `Required Agent Capabilities`, `Agent Output Schema`, `Harness Responsibilities`, `Scoring Categories`, and an out-of-scope section.
  Verify: open `shared/task-spec.md` and confirm it contains, in this order, level-2 headings whose text contains each of: `V1 Scope`, `Case Format`, `Agent Input`, `Expected Agent Behavior`, `Required Agent Capabilities`, `Agent Output Schema`, `Harness Responsibilities`, `Scoring Categories`, and `Out of Scope` (case-insensitive). The check fails if any of these nine substrings is absent from a `## ` heading.
- The case format documents one case shape that naturally extends to more cases.
  Verify: open `shared/task-spec.md` and confirm the `## Case Format` section explicitly names the fields `case_id`, `fixture_repo`, `failing_test_command`, `failure_output`, `edit_constraints`, `hidden_test_command`. Then `grep -in "additional cases\|additional fixture\|multiple cases\|multiple fixture\|extends to" shared/task-spec.md` returns at least one match.
- A framework implementer can see that the agent receives a mutable per-run repo path, a failing test command, captured failure output, and edit constraints, and edits the worktree in place.
  Verify: `grep -n "repo_path" shared/task-spec.md` returns at least one match inside a JSON block under the `## Agent Input` section, AND `grep -in "mutable" shared/task-spec.md` returns at least one match, AND `grep -in "in place\|in-place" shared/task-spec.md` returns at least one match.
- The contract clearly states that the harness derives the canonical diff and reruns the failing test command after the agent exits.
  Verify: `grep -in "harness derives\|derives the canonical\|canonical diff" shared/task-spec.md` returns at least one match inside the `## Harness Responsibilities` section, AND `grep -in "re-run\|rerun" shared/task-spec.md` returns at least one match referring to `failing_test_command`.
- The output schema captures `root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, `confidence`, and explicitly forbids an authoritative agent-declared status.
  Verify: open `shared/task-spec.md`, locate the `## Agent Output Schema` JSON block, and confirm it contains the keys `root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, `confidence`. Then `grep -in "MUST NOT\|must not" shared/task-spec.md` returns at least one match in or immediately after that section, and the same paragraph mentions `fixed`, `not_fixed`, or `status` as the forbidden field.
- Scoring is reported as independent per-category results with no v1 aggregate.
  Verify: open `shared/task-spec.md`, the `## Scoring Categories` section lists items named `schema_validity`, `visible_test_outcome`, `hidden_test_outcome`, `edit_constraint_compliance`, `minimality`, `trace_quality`, `latency_ms`, and `token_usage`. Then `grep -in "no aggregate\|no weighted aggregate\|no overall\|no single" shared/task-spec.md` returns at least one match.
- The spec explicitly allows framework-native tool names and shapes provided the capability surface is equivalent.
  Verify: `grep -in "framework-native" shared/task-spec.md` returns at least one match within the `## Required Agent Capabilities` section, and that section enumerates inspection, search, editing/patching, test execution, and diff inspection capabilities.
- The default edit constraints prevent obvious gaming (modifying tests/fixtures/lockfiles) without revealing the fix location, and `max_changed_files` has a concrete integer default.
  Verify: open `shared/task-spec.md`, the `### Default Edit Constraints` subsection (or equivalent under `## Case Format`) names at least the glob patterns `tests/`, `**/*fixture*`, and `**/*lock*` under the `disallowed_paths` defaults; states `allowed_paths` defaults to unrestricted; sets `max_changed_files` default to `5` (the literal integer); and contains the words "reveal" or "fix location" to justify why the default is loose.
- The trace-quality category is described as a workflow assessment, not a fixed-tool-name check.
  Verify: open `shared/task-spec.md`, the `trace_quality` bullet under `## Scoring Categories` contains language equivalent to "sensible debugging workflow" AND explicitly states it is **not** a check that specific tool names appear (search for "not a check\|not strict\|not conformance\|not specific tool names" — at least one such phrase must be present in the bullet).

Model recommendation: standard

### Task 2 — Verify existing repo documentation remains consistent with the new task spec

Files:
- (no edits expected; verification only)
- Verify-target files: `README.md`, `shared/contract.md`, `evals/README.md`, `frameworks/README.md`, `frameworks/deepagents/README.md`, `frameworks/pydantic-ai/README.md`, `frameworks/google-adk/README.md`, `frameworks/strands/README.md`, `frameworks/agentcore/README.md`, `frameworks/claude-agent-sdk/README.md`, `frameworks/openai-agents/README.md`, `frameworks/mastra/README.md`.

Steps:

- [ ] **Step 1: Confirm no stale placeholder wording survives in user-facing repo docs.** Run `grep -nR "task-spec" --include='*.md' README.md shared evals frameworks` from the repo root and check every match. Each match must be either inside `shared/task-spec.md` itself or a one-line pointer like `shared/task-spec.md` / `../shared/task-spec.md` / `../../shared/task-spec.md`. None of the original placeholder phrases should appear in those user-facing docs after the rewrite: `grep -nR "TODO — fill in\|single non-trivial use case" --include='*.md' README.md shared evals frameworks` must return zero matches.

- [ ] **Step 2: Confirm contract-envelope alignment.** Open `shared/contract.md` and confirm that both the request `input` and the response `output` still say `shape defined in task-spec.md` (or pointer-equivalent text). Confirm the envelope keys it defines (`task_id`, `input`, `config.model`, `config.max_steps`, `config.timeout_s`, `output`, `trace.steps`, `trace.tokens`, `trace.latency_ms`, `error`) are not renamed or contradicted in the new `shared/task-spec.md` — the new task spec must not redefine `task_id` and must keep the agent's input nested inside `input` and the agent's report nested inside `output`.

- [ ] **Step 3: Confirm framework-independence wording.** Open `frameworks/README.md` and confirm it still says each framework directory is independent and that the eval harness reaches in only through the contract.

- [ ] **Step 4: Confirm harness-as-only-comparator wording.** Open `evals/README.md` and `README.md` and confirm both still describe the harness as the cross-framework runner/comparator (e.g. "scores them against `../shared/task-spec.md`", "comparative report").

- [ ] **Step 5: Confirm framework READMEs still point at the spec.** Each `frameworks/<name>/README.md` should still contain "Implementation against `../../shared/task-spec.md`" — no edits are expected.

- [ ] **Step 6: If any of Steps 1–5 surfaces an inconsistency, make the smallest possible edit to restore consistency.** Then re-run the failing grep to confirm the fix. If everything passes on first read, no edits are needed and Task 2 is complete.

Acceptance criteria:

- Every `task-spec` reference in the user-facing repo docs points at the new spec file or is the spec itself, and no stale placeholder phrases survive there.
  Verify: run `grep -nR "task-spec" --include='*.md' README.md shared evals frameworks` and confirm every match is either inside `shared/task-spec.md` or a one-line pointer to it. Then run `grep -nR "TODO — fill in\|single non-trivial use case" --include='*.md' README.md shared evals frameworks` and confirm zero matches.
- `shared/contract.md` still references `task-spec.md` for both `input` and `output` shapes, and the envelope keys it declares are not contradicted by the new task spec.
  Verify: `grep -n "task-spec" shared/contract.md` returns at least two matches (one in the request block around line 14, one in the response block around line 28). Then open `shared/task-spec.md` and confirm it does NOT redefine `task_id` (no top-level "task_id" field in either the Agent Input or Agent Output JSON blocks — those blocks live under `input` and `output` respectively) and does not introduce a top-level envelope key contradicting `trace`, `error`, or `config`.
- Framework directories remain documented as independent and the eval harness remains documented as the only cross-framework comparator.
  Verify: `grep -in "independent\|reaches in only through the contract" frameworks/README.md` returns at least one match. Then `grep -in "scores them against\|comparative report\|across frameworks" README.md evals/README.md` returns at least one match across the two files combined.
- All eight framework READMEs still point at `../../shared/task-spec.md`.
  Verify: `grep -nR "../../shared/task-spec.md" frameworks/ --include='README.md'` returns one match in each of `deepagents`, `pydantic-ai`, `google-adk`, `strands`, `agentcore`, `claude-agent-sdk`, `openai-agents`, `mastra` — eight matches in total.

Model recommendation: cheap

## Dependencies

- Task 2 depends on: Task 1

(Task 2 is a verification of consistency with the file produced in Task 1; running it first would be meaningless because Task 2 reads the new `shared/task-spec.md` for cross-checks.)

## Risk Assessment

- **Drift between task-spec field names and the contract envelope.** `shared/contract.md` already defines `task_id`, `input`, `output`, `trace`, `error`. The new task spec must place its agent input inside `input` and its agent report inside `output`, must not redefine `task_id` at the task level, and must not introduce a sibling envelope key. Mitigation: Task 1 Step 1 explicitly re-reads `shared/contract.md` before drafting, and Task 2 Step 2 grep-checks the cross-reference and the absence of envelope-key contradictions.
- **Leaking the fix location through over-tight default `allowed_paths`.** The spec calls out that defaults must not reveal where the bug lives. If `allowed_paths` defaulted to a narrow scope, every default-cases run would tell the agent exactly where to look. Mitigation: defaults set `allowed_paths` to unrestricted; tighter containment is per-case opt-in only. Recorded explicitly in the `### Default Edit Constraints` subsection and verified by Task 1's "fix location" acceptance criterion.
- **Implicit aggregate-score creep.** The spec forbids any v1 aggregate. If wording like "overall score" or "weighted total" sneaks into the doc, downstream readers may assume a leaderboard exists. Mitigation: Task 1's `Scoring Categories` acceptance criterion grep-asserts at least one of `no aggregate` / `no weighted aggregate` / `no overall` / `no single` is present in the section.
- **Agents managing their own worktrees by mistake.** If the spec doesn't loudly say worktree lifecycle is harness-owned, framework authors may bake in worktree creation/cleanup logic. Mitigation: explicit "must NOT manage isolation, reset, or cleanup of the worktree" bullet in `## Expected Agent Behavior` and explicit `## Harness Responsibilities` section listing worktree lifecycle as the harness's job.
- **Authoritative agent verdict creep.** If the output schema gains a `status` / `fixed` field by accident, scoring becomes ambiguous. Mitigation: Task 1's Output Schema acceptance criterion grep-asserts a `MUST NOT` prohibition mentioning `fixed` / `not_fixed` / `status` is present.
- **Stale documentation outside `shared/task-spec.md`.** If a framework README or top-level README contains language that contradicts the new spec (e.g. "single non-trivial use case" wording carried over from the placeholder), the spec's A8 acceptance criterion fails. Mitigation: Task 2 Steps 1 and 2 grep-check for stale phrasing and require zero matches.
- **Trace-quality misread as tool-name conformance.** Without an explicit disclaimer, trace-quality scoring will look like a fixed tool-naming rubric. Mitigation: Task 1 Step 2.9 requires the `trace_quality` bullet to explicitly say it is **not** a check that specific tool names appear, and Task 1's last acceptance criterion verifies that wording.

## Self-Review Notes (planner)

- Spec coverage map (each numbered Acceptance Criterion from `.pi/specs/2026-04-28-software-bugfix-benchmark-contract.md` → task that implements it):
  - AC line 40 (replace with concrete description covering case inputs, behavior, capabilities, output, harness responsibilities, scoring) → Task 1, Step 2 sections 2–9.
  - AC line 41 (case format extends from one to many) → Task 1, Step 2 sections 2 + 3.
  - AC line 42 (framework implementer can identify inputs and in-place edit model) → Task 1, Step 2 sections 4 + 5.
  - AC line 43 (harness-derived diff and rerun) → Task 1, Step 2 section 8.
  - AC line 44 (output schema with no authoritative status) → Task 1, Step 2 section 7.
  - AC line 45 (independent scoring categories, no aggregate) → Task 1, Step 2 section 9.
  - AC line 46 (framework-native tool names allowed) → Task 1, Step 2 section 6.
  - AC line 47 (existing docs remain consistent) → Task 2.
- No tasks contain placeholders ("TBD", "TODO", "implement later", "similar to Task N"). Every `Verify:` line names the artifact, the check, and the success condition.
- Type/name consistency check: the keys used in the Agent Input JSON (`case_id`, `repo_path`, `failing_test_command`, `failure_output`, `edit_constraints`) match across Task 1's Case Format, Agent Input, Expected Agent Behavior, Harness Responsibilities, and Scoring sections. The keys used in the Agent Output JSON (`root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, `confidence`) match across Task 1's Agent Output Schema and Harness Responsibilities. The envelope keys (`task_id`, `input`, `output`, `trace`, `error`, `config`) are referenced consistently with `shared/contract.md`.
