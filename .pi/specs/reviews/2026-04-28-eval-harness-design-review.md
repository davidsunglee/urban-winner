# Review: Eval Harness Design

Spec reviewed: `.pi/specs/2026-04-28-eval-harness-design.md`

## Strengths

- **Clear scope control.** V1 is explicitly sequential, resumable, report-producing, and avoids premature concurrency/leaderboard/judge complexity.
- **Good framework isolation model.** Subprocess + stdin/stdout JSON keeps the harness framework-agnostic and avoids importing adapter code.
- **Strong artifact discipline.** The `runs/<campaign>/<framework>/<case>/` layout, `meta.json` done-sentinel, and preserved worktree make debugging and resumption practical.
- **Canonical scoring is harness-owned.** The spec correctly treats agent-reported fields as informational and derives diff/test outcomes independently.
- **Partial failures are well handled.** Running diff/tests/constraint checks even after crashes/timeouts will make failures much easier to inspect.
- **Testing plan is unusually concrete.** The fake framework behaviors cover the main runner/pipeline error paths and give a good implementation target.
- **Parallelism is anticipated without overbuilding.** The per-cell artifact model and preflight cache design should extend naturally later.

## Concrete improvement suggestions

### 1. Clarify CLI override semantics

There is a tension between:

- `runs/<ts>/manifest.json` is captured at `eval-new` and never mutated.
- `eval-all` / `eval` accept `--model`, `--timeout-s`, `--max-steps`.

Clarify where overrides are recorded if they are passed after `eval-new`.

Suggested rule:

> Campaign-level overrides are fixed when the campaign is created. If `eval-all` auto-creates a campaign, its flags are written into `manifest.json`. Single-cell `eval` may override per run, but those effective values must be recorded in `request.json` and `meta.json`.

Or simpler:

> Overrides are only accepted when creating a new campaign; rerunning cells inside an existing campaign must use the campaign config.

### 2. Define error precedence precisely

Several states can overlap:

- nonzero exit + valid error JSON
- nonzero exit + malformed stdout
- timeout + partial stdout
- exit 0 + invalid envelope
- stdout larger than cap
- missing stdout

Add a precedence table, e.g.

| condition | `meta.error_reason` |
| --- | --- |
| external timeout fired | `timeout` |
| process could not be started | `framework_misconfigured` |
| stdout missing/empty | `missing_response` |
| stdout over cap or invalid JSON | `malformed_response_json` |
| JSON valid but envelope invalid | `envelope_schema_violation` |
| exit code nonzero | `nonzero_exit` |

Also specify whether `scoring.json` is still written for every error state and what `schema_validity` should be when the envelope itself is invalid.

### 3. Separate framework env from test env

The spec says visible/hidden tests use “same scrubbed env.” That may forward framework API keys into fixture test processes.

Consider defining two environments:

- `agent_env`: declared framework env vars + base env + `UV_PROJECT_ENVIRONMENT`
- `test_env`: base env + `UV_PROJECT_ENVIRONMENT`, but **no framework secrets**

This keeps tests deterministic and prevents accidental secret exposure through failing test output.

### 4. Make command execution semantics explicit

The spec uses command strings in several places:

- framework manifest `entry`
- `setup`
- `failing_test_command`
- `hidden_test_command`

Define whether each is executed via shell or argv.

Suggested:

- `entry` / `setup`: either manifest field is an argv array, or string parsed with `shlex.split`; no shell.
- test commands: run as `/bin/sh -c <command>` with `cwd=<repo>` because case authors expect shell syntax.

This avoids implementation drift and quoting bugs.

### 5. Avoid mutating the preserved repo index during diff capture

Step 2 does:

```bash
git add -A
git diff --cached HEAD
```

Because the worktree is preserved for inspection, this leaves all agent changes staged, which may surprise users.

Better options:

- use a temporary index via `GIT_INDEX_FILE`, or
- document that the preserved repo will have changes staged, or
- derive untracked files separately without staging the real index.

### 6. Clarify run identity

The text alternates between per-`(framework, run)` and storage at:

```text
runs/CURRENT/<framework>/<case>/
```

That is really one cell per `(framework, case)` per campaign.

If repeated trials are out of scope, rename references to `(framework, case)` for v1. If repeated stochastic runs are planned, add a future `run_id` dimension now, e.g.

```text
runs/<campaign>/<framework>/<case>/<run_id>/
```

### 7. Tighten shared venv reproducibility

Layer 2 says the shared venv is “read-only at runtime” but not enforced. Since an agent could mutate it and contaminate later cells, add one mitigation:

- set uv/test commands to frozen/no-sync mode where possible,
- chmod the venv read-only during agent runs,
- hash/check the venv before/after a cell,
- or explicitly accept contamination risk in V1.

Right now the design notes the issue but does not give operators a way to detect it.

### 8. Add artifact naming for malformed stdout

The layout lists:

```text
response.json
```

But stdout may be non-JSON, truncated, or empty. Consider:

```text
stdout.log
response.json        # only parsed/valid JSON
stderr.log
```

This makes artifact semantics clearer.

### 9. Add a few missing test cases

The test plan is strong. I would add explicit coverage for:

- empty stdout / missing response
- stdout cap exceeded
- stderr truncation
- malformed manifest / missing executable
- partial cell dir without `meta.json` gets blown away on resume
- `eval-all` skips cells with `meta.json`, including error cells
- env scrubbing does not leak undeclared vars
- campaign override recording behavior

### 10. Resolve cross-spec deviation in one place

The spec notes that keeping `repo/` is a deliberate deviation from `task-spec.md`, which says destroy after scoring. Good. I’d add a short “Compatibility with prior specs” section listing all intentional deviations/clarifications:

- worktree kept after scoring
- output schema violations are non-fatal, but envelope violations are fatal
- v1 campaign cell identity is `(framework, case)`, not repeated runs

That will prevent later implementers from trying to reconcile conflicting docs ad hoc.

## Overall assessment

This is a solid, implementation-ready spec. The main improvements are around edge-case determinism: exact command invocation, override persistence, error precedence, env separation, and avoiding artifact ambiguity.
