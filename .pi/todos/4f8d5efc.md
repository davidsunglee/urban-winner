{
  "id": "4f8d5efc",
  "title": "Define software bugfix benchmark contract",
  "tags": [
    "benchmark",
    "agent-contract",
    "evals",
    "bugfix"
  ],
  "status": "open",
  "created_at": "2026-04-28T18:58:28.392Z"
}

Formalize the software bugfix benchmark as the first concrete shared agent contract.

## Use case

Agent receives a small repository, a failing test command/CI failure description, and edit constraints. It must inspect the repo, identify the root cause, apply a minimal patch, rerun tests, and return a structured report.

## Key contract elements

- Inputs: repo path/snapshot, issue or CI failure summary, test command, allowed/disallowed paths, max changed files.
- Tools: list/read/search files, run tests, apply patch, inspect git diff.
- Output: status, root cause, changed files, patch/diff, tests run, evidence, confidence.
- Scoring: schema validity, patch applies, tests pass including hidden tests, forbidden edit checks, minimality, trace quality, latency/tokens.

## Next step
Use this as the recommended first implementation for `shared/task-spec.md`.
