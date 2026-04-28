{
  "id": "fa1c0ce6",
  "title": "Define benchmark contracts for data incident and review/planning use cases",
  "tags": [
    "benchmark",
    "agent-contract",
    "evals"
  ],
  "status": "open",
  "created_at": "2026-04-28T18:57:07.916Z"
}

Capture and formalize the remaining candidate benchmark use cases for comparing agent frameworks against a shared contract.

The software bugfix benchmark was extracted to TODO-4f8d5efc.

## Use cases

### 1. Data incident benchmark
Agent receives a small warehouse/database fixture, schemas, failing data-quality assertion, business rules, and possibly an ETL/dbt/SQL model. It must inspect data, identify the root cause, propose or apply a SQL/model fix, rerun data tests, and return evidence.

Key contract elements:
- Inputs: database path/connection, table schemas, failing assertion, business rules, relevant model/script paths.
- Tools: list tables, describe table, run SQL, sample rows, read files, apply patch, run data tests.
- Output: status, root cause, changed files or recommended query, SQL evidence, tests run, confidence.
- Scoring: schema validity, data tests pass, SQL evidence correctness, no forbidden edits, trace quality, latency/tokens.

### 2. Review/planning benchmark
Agent receives a code diff or repository plus a software-engineering request and must produce a structured review or implementation plan. This is less mechanical than the first two and is useful for evaluating judgment, prioritization, context selection, and explanation quality.

Possible variants:
- Pull request reviewer: identify seeded issues in a diff and produce actionable findings with severity, file/line, rationale, and suggested fix.
- Architecture onboarding/planning assistant: inspect a repo and produce a step-by-step implementation plan for a requested change.

Key contract elements:
- Inputs: repo path/snapshot, diff or feature request, project constraints/conventions, optional test output.
- Tools: list/read/search files, inspect diff, optionally run tests.
- Output: summary, findings or plan steps, relevant files, risks, recommended tests, confidence.
- Scoring: schema validity, seeded issue recall/precision or rubric-based plan quality, evidence quality, trace quality, latency/tokens.

## Next step
After the software bugfix benchmark is defined, decide whether the data incident benchmark or review/planning benchmark should become the next concrete `shared/task-spec.md` implementation.
