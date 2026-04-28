# Software Bugfix Benchmark Contract

Source: TODO-4f8d5efc

## Goal

Define the v1 software bugfix benchmark as the first concrete shared agent task contract for the agent framework shootout. The benchmark should give each framework implementation the same realistic bugfix task: inspect a small repository, diagnose a failing test, apply a minimal fix in an isolated workspace, rerun relevant tests, and return structured evidence while the harness independently evaluates the result.

## Context

The repository is currently scaffolded around a shared task and transport contract. `README.md` describes an agent framework shootout where every framework in `frameworks/` implements the same task through `shared/task-spec.md` and communicates through `shared/contract.md`. `shared/task-spec.md` is still placeholder prose and is the natural home for the concrete benchmark definition. `shared/contract.md` already defines the framework-agnostic subprocess JSON envelope: requests contain a `task_id`, task-specific `input`, and run `config`; responses contain task-specific `output`, trace metadata, and an error field. `evals/README.md` describes a future harness that discovers framework entries, invokes them through the shared contract, and scores them against the shared task spec. The framework directories are intentionally independent and should not share implementation details beyond the shared contract.

## Requirements

- The benchmark must define a reusable bugfix case format from the start, while allowing v1 to ship with only one initial fixture case.
- Each benchmark case must be based on a canonical fixture repository owned by the harness. For every framework/run, the harness must provide an isolated mutable worktree derived from that canonical fixture so agents cannot contaminate one another.
- The agent input for a case must include the mutable repository path, the failing test command, and the captured failure output. V1 should model CI-style failure triage rather than an issue-style natural-language bug report.
- The case format must support edit constraints: repo-relative disallowed paths, an optional maximum changed-file count, and optional allowed paths for cases that need tighter containment. The harness must document sane defaults when per-case constraints are omitted.
- The agent must be expected to inspect the repository, identify the root cause, apply a minimal in-place patch to the provided worktree, run tests as needed, and return a structured report.
- The worktree diff must be the authoritative submitted fix. The harness must inspect the worktree after the agent exits to derive the canonical diff and changed files; the agent is not required to return a full patch/diff payload.
- The agent report must include evidence-oriented fields such as root cause, summary, changed files, tests run, supporting evidence, and confidence. These fields represent the agent's account of the run and must not override harness-derived observations.
- The benchmark verdict must be harness-derived. The agent should not provide an authoritative `fixed`/`not_fixed` status in v1; its confidence and narrative evidence are sufficient self-assessment signals.
- The harness must rerun the case's required test command after the agent exits to determine the visible test outcome. The benchmark design should also support hidden post-run tests as a separate scoring category when a case provides them.
- Framework implementations must provide equivalent bugfix capabilities to their agents: file listing/reading, file search, editing or patch application, test execution, and diff inspection. Exact tool names and schemas may be framework-native rather than globally standardized.
- Scoring must be reported as separate categories rather than a single weighted aggregate score. Categories must include schema validity, visible test outcome, hidden test outcome when applicable, edit-constraint compliance, changed-file/minimality assessment, trace quality, latency, and token usage when available.
- Trace quality must be evaluated as evidence of a sensible debugging workflow, not as strict conformance to one fixed sequence of tool names.

## Constraints

- The shared benchmark contract must remain framework-agnostic; the eval harness should interact with framework implementations only through the shared request/response contract.
- Worktree creation, reset, diff collection, post-run test execution, scoring, and cleanup are harness responsibilities, not agent responsibilities.
- The harness-derived diff, changed files, test outcomes, and constraint checks are authoritative over any agent-reported fields.
- V1 must not require agents to return a full patch/diff because the harness can derive it directly from the git worktree.
- V1 must not impose one exact named tool surface across frameworks; it should require equivalent capabilities while allowing framework-native implementations.
- V1 must not introduce a weighted leaderboard or single aggregate score. Any rollup score should wait until real benchmark data shows which categories are stable and meaningful.
- Edit constraints should prevent obvious benchmark gaming, such as modifying tests or fixtures, without over-constraining every case or revealing the intended fix location by default.

## Acceptance Criteria

- `shared/task-spec.md` is replaced with a concrete software bugfix benchmark description covering case inputs, expected agent behavior, required capabilities, output report shape, harness responsibilities, and scoring categories.
- The documented case format can represent one initial fixture and can naturally expand to multiple fixture cases later without changing the agent-facing task concept.
- A framework implementer can read the shared task spec and know that their agent receives a mutable per-run repository path, a failing test command, failure output, and edit constraints, then edits the worktree in place.
- The contract clearly states that the harness derives the canonical diff and changed files from the worktree and reruns the required test command after the agent exits.
- The agent output schema captures root cause, summary/evidence, changed files, tests run, and confidence without making an agent-declared status authoritative.
- The scoring model reports independent category results for correctness, constraints, minimality, trace quality, and performance, with no weighted aggregate score in v1.
- The benchmark documentation explains that framework implementations may use native tool names and shapes as long as they provide equivalent inspection, editing, test-running, and diff-inspection capabilities.
- Existing repository documentation remains consistent with the shared contract: framework directories stay independent and the eval harness remains the only component responsible for cross-framework comparison.

## Non-Goals

- Implementing the eval harness, fixture repositories, or framework agents as part of this spec.
- Requiring multiple benchmark cases before v1 is useful.
- Defining a weighted leaderboard score or declaring one overall winner from a single numeric score.
- Standardizing exact tool names, function signatures, or framework-specific tool schemas.
- Making returned patches authoritative over the worktree diff.
- Asking agents to create, manage, or clean up their own worktrees.
- Using issue-style natural-language bug descriptions as the primary v1 input signal.
