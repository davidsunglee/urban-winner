# Eval Harness Design

> **v2** — incorporates feedback from `.pi/specs/reviews/2026-04-28-eval-harness-design-review.md`. Changes since v1 are flagged inline as **(v2)** the first time a v2-only rule appears in a section.

## Goal

Design the framework-agnostic eval harness that lives in `evals/` and runs every framework in `frameworks/` against every case in `cases/` through the contract defined in `shared/contract.md`, scoring per the categories defined in `shared/task-spec.md`. V1 is sequential, resumable, and produces a per-campaign markdown report. The matrix is small (≤8 frameworks × a handful of cases) so concurrency is not in scope for v1, but the design must extend cleanly to it.

## Context

Three things are already pinned by prior specs and existing artifacts:

- **`shared/contract.md`** defines the framework-agnostic transport: subprocess + JSON over stdin/stdout, one request envelope in (`task_id`, `input`, `config`), one response envelope out (`task_id`, `output`, `trace`, `error`). On failure: non-zero exit, error JSON to stdout, logs to stderr. Each framework provides an entry point declared in `frameworks/<name>/manifest.json` and owns its dependency management.
- **`shared/task-spec.md`** defines the v1 software bugfix benchmark: each case ships a fixture repo, a failing test command, captured failure output, optional hidden test command, and optional edit constraints. The harness derives a fresh worktree per cell, the agent edits in place, and the harness re-runs the test to derive the canonical visible outcome. Spec also enumerates scoring categories with no aggregate score in v1.
- **Cases and fixtures already exist** at `cases/<case_id>.json` and `fixtures/<case_id>/`. The bootstrap case (`py-parse-duration-001`) and three SWE-bench Verified cases (`psf__requests-1921`, `pylint-dev__pylint-7080`, `pytest-dev__pytest-7571`) are the v1 starter set. The repo's authoritative case manifest schema lives in `task-spec.md`.

The harness itself is the missing piece: `evals/` currently contains only a `pyproject.toml` stub and a TODO README. This spec defines what fills it in.

## Out of Scope

- Implementing per-framework adapters (each framework dir owns its own `run.sh` / `setup.sh`; v1 ships stub scripts that exit non-zero so `eval-all` runs end-to-end on day one).
- Concurrent execution. Sequential v1; the design extends cleanly to parallelism (see "Parallelism Notes" near the end).
- Multiple stochastic trials per cell. V1 has exactly one run per `(framework, case)` per campaign. A future `run_id` dimension is anticipated; see "Compatibility with `task-spec.md`."
- LLM-as-judge trace quality scoring. Traces are captured verbatim in v1; rubric grading is a deferred `just judge-traces` command.
- A weighted leaderboard / single aggregate score. Per-category reporting only, per `task-spec.md`.
- Authoring new cases or fixtures — those follow `.pi/specs/2026-04-28-bootstrap-fixture-design.md` and `.pi/specs/2026-04-28-swebench-fixture-conversion.md`.
- Real framework dependency installs. Stub setup hooks make `eval-prepare` succeed without doing real work in v1.

## Compatibility with `task-spec.md` *(new in v2)*

`task-spec.md` is the contract; this design is the implementation. Where they differ, the difference is intentional and listed here so future implementers do not try to reconcile both sources ad hoc.

1. **Worktree retained after scoring.** `task-spec.md` says the harness destroys the worktree after the run; this design keeps it at `runs/<campaign>/<framework>/<case>/repo/` for inspection. Rationale: debuggability. `eval-new` and per-cell reruns wipe and rebuild as needed.
2. **Cell identity is `(framework, case)` per campaign in v1.** `task-spec.md` says "one worktree per `(framework, run)` pair." V1 has exactly one run per cell, so `(framework, run)` reduces to `(framework, case)` for storage. The contract envelope's `task_id` still embeds a per-invocation uuid for traceability when the same cell is re-run. A future `run_id` dimension would slot in as `runs/<campaign>/<framework>/<case>/<run_id>/`.
3. **Envelope schema violations are fatal; agent-output schema violations are not.** `task-spec.md` lists `schema_validity` as a scoring category without saying which violations halt the pipeline. This design distinguishes:
   - Envelope (contract) invalid → cell errors out, response not used.
   - Agent `output` shape invalid (e.g., forbidden top-level `fixed`) → cell continues; `schema_validity: false` is recorded as a scoring signal.
4. **Test commands are executed via `/bin/sh -c`; entry/setup commands via `shlex.split`.** Case authors expect shell syntax in `failing_test_command` (and existing case manifests rely on it); manifest entry points should not.

## Architecture

The harness lives in `evals/` and never imports framework code. It interacts with the rest of the repo through three boundaries:

```
┌─────────────────────────────────────────────────────────────────────┐
│  evals/  (this design)                                              │
│                                                                     │
│  CLI ──▶ campaign mgr ──▶ run executor ──▶ subprocess (framework)   │
│                  │              │                                   │
│                  ▼              ▼                                   │
│            workspace mgr   diff/test/scoring                        │
│                  │              │                                   │
│                  ▼              ▼                                   │
│           .runs-cache/      runs/CURRENT/<fw>/<case>/               │
└─────────────────────────────────────────────────────────────────────┘
        │                    │                   │
        ▼                    ▼                   ▼
   fixtures/<case>/     cases/<case>.json    frameworks/<name>/manifest.json + entry
   (read-only source)   (manifest)           (subprocess target)
```

### Immutable inputs

- `cases/<case_id>.json` — case manifest (schema in `task-spec.md`).
- `fixtures/<case_id>/` — pristine fixture files; harness never edits.
- `frameworks/<name>/manifest.json` + entry script — framework adapter.

### Harness-owned artifact spaces

- `.runs-cache/` — gitignored; lazily-built derived state.
- `runs/<timestamp>/` — campaign dirs; per-cell results.
- `runs/CURRENT` — relative symlink to the active campaign.

### Top-level data flow for one cell `(framework=F, case=C)`

1. Workspace manager ensures `.runs-cache/<C>.git/` exists (layer 1) and `.runs-cache/<C>.venv/` exists (layer 2), then `git clone --local` into `runs/CURRENT/<F>/<C>/repo/` (layer 3).
2. Run executor builds the contract request, spawns `frameworks/<F>/<entry>` with the request on stdin, captures stdout/stderr with an external timeout.
3. Post-subprocess pipeline derives the canonical diff (without staging the worktree's index), reruns the visible test command, optionally the hidden test, and validates edit constraints.
4. The cell's `meta.json` is written last as the done-sentinel.

## Workspace Lifecycle

Three layered workspaces, each with a single owner and a single rule for when it's built or rebuilt.

### Layer 1 — Per-case bare git repo: `.runs-cache/<case_id>.git/`

- Built lazily on first reference (or eagerly by `eval-prepare`).
- Construction: `git init --bare` in a tempdir; in a sibling `.work/` dir, `cp -r fixtures/<case_id>/`, `git add -A`, `git commit -m "fixture: <case_id> @ <fixture-content-hash>"`, `git push` into the bare repo. Tempdir discarded.
- The commit message embeds a content hash of the fixture tree. The harness writes the same hash to `.runs-cache/<case_id>.fixture-hash` so it can detect "fixture changed since this `.git/` was built" and rebuild automatically.
- Rebuild trigger: fixture content hash changes. Otherwise reused indefinitely.

### Layer 2 — Per-case shared venv: `.runs-cache/<case_id>.venv/`

- Built lazily on first reference (or eagerly by `eval-prepare`).
- Construction: a `uv sync` invocation rooted at `fixtures/<case_id>/` with `UV_PROJECT_ENVIRONMENT` pointing at the absolute path of `.runs-cache/<case_id>.venv/`. Exact uv flag combination to be confirmed during implementation.
- Rebuild trigger: hash of `fixtures/<case_id>/uv.lock` (or `pyproject.toml` if no lock file exists) changes. The harness writes the last-built hash to `.runs-cache/<case_id>.lock-hash`.
- All test reruns by the harness, and the agent's own `uv run pytest` invocations, use this venv via `UV_PROJECT_ENVIRONMENT`.
- **Read-only at runtime** is documented but not filesystem-enforced in v1. If a framework's agent installs into it, that's a framework-side bug.
- **Mutation detection (v2).** Before each cell run, the harness computes a cheap content fingerprint of the venv: a sorted listing of `<.venv>/lib/python*/site-packages/*.dist-info/` directory names hashed with BLAKE2. The pre-cell hash is recorded as `venv_hash_before` in the cell's `meta.json`. After the cell completes (regardless of agent success), the same hash is recomputed and recorded as `venv_hash_after`. Mismatch sets `venv_mutated: true` in `meta.json` and emits a warning to stderr. This is detection only; the harness does not roll back.

### Layer 3 — Per-cell worktree: `runs/CURRENT/<framework>/<case>/repo/`

- Built fresh per cell run: `git clone --local .runs-cache/<case_id>.git runs/CURRENT/<F>/<C>/repo`.
- This is the agent's mutable sandbox. It has its own working tree and `.git/`; diff derivation reads it without modifying its real index.
- Lifetime: from cell run start to cell run end. **Not destroyed on completion** — left in place so the user can inspect what the agent did. `eval-new` and rerunning the cell both wipe-and-rebuild.
- This is a deliberate deviation from `task-spec.md` (see "Compatibility").

### Cleanup commands

- `just eval-clean-cache` — wipes `.runs-cache/` (forces full rebuild on next run).
- `just eval-clean-runs` — wipes `runs/`.
- No automatic cleanup; both are user-invoked.

## Framework Manifest, Invocation, Env Handling

### Manifest schema (`frameworks/<name>/manifest.json`)

```json
{
  "entry": "./run.sh",
  "setup": "./setup.sh",
  "env": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
  "model": "claude-sonnet-4-6"
}
```

- `entry` (required) — repo-relative-to-framework-dir command. Executed with cwd = `frameworks/<name>/`.
- `setup` (optional) — same shape; run once by `eval-prepare`. Sentinel at `.runs-cache/setup/<framework>.ok` includes a hash of `manifest.json` + the `setup` script + a per-framework lockfile glob (e.g., `uv.lock`, `package-lock.json`) so changes auto-trigger re-setup.
- `env` (required) — list of env var names that survive scrubbing. Empty array is fine.
- `model` (required) — default model id baked into `config.model` of the request envelope.

The harness validates the manifest at startup using a JSON schema. A missing or malformed manifest causes the cell to error out as `framework_misconfigured`.

### Command execution semantics *(new in v2)*

The harness invokes commands from two sources, each with explicit semantics:

- **Manifest commands** (`entry`, `setup`): the JSON string value is parsed with `shlex.split` and executed as an argv list — **no shell**. This avoids quoting bugs and rules out implicit dependence on shell features in framework adapters.
- **Case-manifest test commands** (`failing_test_command`, `hidden_test_command`): executed via `subprocess.run(["/bin/sh", "-c", command], ...)`. Case authors expect shell syntax (pipes, env-var interpolation, `&&`); existing case manifests reflect that convention.

Both forms run with `cwd` set per the rule below and the env constructed per "Two execution environments."

### Two execution environments *(new in v2)*

The harness builds two distinct environment dicts and never uses one in place of the other:

**`agent_env`** — for the framework subprocess.
- The declared `env` list from the framework manifest, sourced from process env + `.env` (see "Secrets sourcing").
- `UV_PROJECT_ENVIRONMENT` set to the absolute path of `.runs-cache/<case>.venv/`.
- A minimal base set: `PATH`, `HOME`, `LANG`, `TERM`.
- Nothing else.

**`test_env`** — for the harness's visible/hidden test reruns.
- `UV_PROJECT_ENVIRONMENT` set to the same `.runs-cache/<case>.venv/`.
- The same minimal base set: `PATH`, `HOME`, `LANG`, `TERM`.
- **No framework API keys.** Test reruns are deterministic test executions; they have no need for model credentials. Excluding them prevents accidental secret exposure through failing test output captured to disk.

### Subprocess invocation (agent run)

For one cell `(F, C)`:

1. Harness builds the request:
   ```json
   {
     "task_id": "<F>:<C>:<8-char uuid>",
     "input": {
       "case_id": "<C>",
       "repo_path": "<absolute path to layer 3 worktree>",
       "failing_test_command": "...",
       "failure_output": "<resolved from failure_output_path if used>",
       "edit_constraints": { /* per task-spec.md, with defaults applied */ }
     },
     "config": {
       "model": "<effective config; see Configuration Overrides>",
       "max_steps": "<effective config>",
       "timeout_s": "<effective config>"
     }
   }
   ```
   The full request is written to `<cell>/request.json` immediately, before spawn.
2. Spawns the entry command (parsed via `shlex.split`) with:
   - cwd = `frameworks/<F>/`
   - stdin: the request JSON, then EOF.
   - env: `agent_env`.
   - stdout: streamed to `<cell>/stdout.log` as it arrives, capped at 8 MiB. If the cap is hit, the stream is truncated, the file is preserved up to the cap, and `meta.stdout_truncated = true`.
   - stderr: streamed to `<cell>/stderr.log`, capped at 5 MiB. Over → truncated; `meta.stderr_truncated = true`.
3. External timeout via `subprocess.Popen` + a watchdog: SIGTERM at `effective_config.timeout_s`, 5-second grace, SIGKILL.
4. After exit: classify the run per the precedence table below. If a parseable response envelope is recovered, it is also written to `<cell>/response.json` (parsed-and-revalidated form). `<cell>/stdout.log` is the raw byte source; `response.json` is present only when parsing succeeded.

### Error state precedence *(new in v2)*

When multiple conditions are true, the higher row in this table wins as `meta.error_reason`. `meta.status` is `"ok"` only when `error_reason` is `null`.

| precedence | condition | `meta.error_reason` |
| --- | --- | --- |
| 1 | external watchdog timer fired | `timeout` |
| 2 | manifest invalid, entry missing/non-executable, exec failed | `framework_misconfigured` |
| 3 | process exited with nonzero status | `nonzero_exit` |
| 4 | exit 0, stdout empty | `missing_response` |
| 5 | exit 0, stdout exceeded cap *or* stdout did not parse as a single JSON object | `malformed_response_json` |
| 6 | exit 0, JSON parsed, but contract envelope schema invalid | `envelope_schema_violation` |
| 7 | exit 0, envelope valid | `null` (status `"ok"`) |

Notes:
- `nonzero_exit` dominates parse failures because the contract permits frameworks to "exit non-zero, write the error JSON to stdout." When that happens, the harness still attempts to parse stdout; a parsed payload is written to `response.json` but `error_reason` remains `nonzero_exit`. If parsing fails on a nonzero exit, no `response.json` is written.
- `timeout` similarly dominates downstream conditions; partial stdout is preserved in `stdout.log`.
- `framework_misconfigured` is detected before exec; in this case there is no `stdout.log` / `stderr.log` content beyond the harness's own diagnostic written to stderr.

### Scoring on error

`scoring.json` is **always written**, including on error. The categories are populated as follows:

- `schema_validity`: `false` whenever the envelope was invalid, parse failed, response was missing, or the agent `output` failed shape validation. `true` only when both envelope and `output` validate.
- `visible_test_outcome`, `hidden_test_outcome`, `edit_constraint_compliance`, `minimality`: derived from the post-exit worktree, regardless of how the agent exited. On `framework_misconfigured`, the worktree is pristine, so these reflect that.
- `latency_ms`: harness wall-clock from request send to process exit (or to watchdog termination on timeout).
- `token_usage`: present only when a parseable response provided `trace.tokens`.
- `trace_quality`: `"n/a"` in v1.

### Why scrub env

Two reasons:
1. Prevents one framework's API keys leaking into another framework's process or into the harness's test reruns.
2. Makes runs reproducible — every framework sees the same minimal env regardless of what the user has exported. The list of declared env vars is auditable in the manifest.

### Secrets sourcing

The harness loads `.env` at the repo root if present (gitignored), merges it with the process environment, and forwards only the declared `env` vars into `agent_env`. `test_env` does not receive these keys. Implementation uses `python-dotenv` or an equivalent simple parser.

### Stub `run.sh` / `setup.sh` for every framework dir

V1 includes minimal stub scripts in every `frameworks/<name>/` that exit non-zero with a "not implemented" message. This makes `just eval-all` runnable end-to-end on day one with every cell reporting `nonzero_exit`. Real framework adapters fill in these scripts as separate follow-on work.

## Configuration Overrides *(new in v2)*

Three configuration values flow into each cell run: `model`, `timeout_s`, `max_steps`. They have a clear precedence and a clear recording rule.

### Sources, in precedence order (highest first)

1. **Per-cell flags** on `just eval <fw> <case>` (`--model`, `--timeout-s`, `--max-steps`). Apply only to that single cell.
2. **Campaign overrides** captured in `runs/<ts>/manifest.json`'s `config_overrides`. Set at `eval-new` time.
3. **Framework manifest defaults** (`frameworks/<name>/manifest.json#model`). For `timeout_s` and `max_steps`, the harness defaults are `120` and `50` respectively (matching `task-spec.md`).

### Recording rule

- **Campaign-level**: `eval-new` accepts the same flags. Their values are written into `runs/<ts>/manifest.json#config_overrides` and frozen there. The campaign manifest is never mutated after creation.
- **Cell-level**: every `<cell>/request.json` records the *effective* `config` it sent to the framework. Every `<cell>/meta.json` includes an `effective_config: { model, timeout_s, max_steps, source: "campaign" | "cell-flag" }` block describing what actually ran and where each value came from.

### Bulk-run rule

- `just eval-all` in an existing campaign **rejects** override flags with a helpful error: `--model passed but campaign already exists; use 'just eval-new --model X' to start a fresh campaign with overrides, or omit the flag to fill missing cells with the campaign's config.` This keeps bulk runs internally consistent.
- `just eval-all` that auto-creates a new campaign (because there is no `runs/CURRENT`) writes any flags it received into the new campaign's `config_overrides` and proceeds.
- `just eval <fw> <case>` always accepts flags; the cell's effective config is recorded in its artifacts, and the campaign manifest is unchanged.

### Reporting

The campaign report's header line shows campaign-level `config_overrides`. Per-cell rows where `effective_config` differs from the campaign config are flagged with an asterisk pointing to the per-cell `meta.json`.

## Post-Subprocess Pipeline

After the framework subprocess exits, the harness runs a deterministic pipeline. Every step is independent of the framework — pure functions over the worktree, the response, and the case manifest.

### Step 1 — Capture and validate response

- The runner has already streamed stdout into `<cell>/stdout.log` and stderr into `<cell>/stderr.log`. This step parses `stdout.log` *(no separate capture)*.
- Parse stdout. On parse fail → `error_reason = malformed_response_json` (subject to the precedence table). Skip steps that depend on `output`; still run worktree-only steps.
- Validate the contract envelope using a JSON schema. Miss → `envelope_schema_violation`, same partial-skip rule.
- Validate the agent `output` against `task-spec.md`'s schema (including the prohibition on top-level `fixed`/`not_fixed`/`status` keys). **Non-fatal**: `scoring.json` records `schema_validity: false`; pipeline continues.
- On parse success, write `<cell>/response.json` containing the parsed envelope (re-serialized canonically). `stdout.log` is preserved as the raw byte source.

### Step 2 — Derive canonical diff *(updated in v2)*

The diff must be derivable without mutating the worktree's real index, since the worktree is preserved for inspection.

- Use a temporary index file: `GIT_INDEX_FILE=$(mktemp -t cell-index.XXXX)`.
- `GIT_INDEX_FILE=<temp> git -C <cell>/repo read-tree HEAD` (initialize the temp index from HEAD).
- `GIT_INDEX_FILE=<temp> git -C <cell>/repo add -A` (stage all changes including untracked files into the temp index only).
- `GIT_INDEX_FILE=<temp> git -C <cell>/repo diff --cached HEAD` → write to `<cell>/diff.patch`.
- `GIT_INDEX_FILE=<temp> git -C <cell>/repo diff --cached HEAD --name-only` → canonical changed-file list.
- Delete the temp index file. The worktree's real `.git/index` is untouched.
- Compute `+/-` line counts.

This works whether or not the framework crashed — the worktree is the source of truth.

### Step 3 — Visible test rerun

- Spawn `failing_test_command` from the case manifest via `/bin/sh -c <command>`, cwd = `<cell>/repo`, env = `test_env` (no framework keys).
- External timeout: same as agent timeout (`effective_config.timeout_s`) for v1. Captured exit code + stdout + stderr → `<cell>/visible_test.json`.
- Outcome: `pass` (exit 0) | `fail` (nonzero, finite output) | `error` (timeout, signal).

### Step 4 — Hidden test rerun (if case has one)

- Identical to step 3 with `hidden_test_command`. Result → `<cell>/hidden_test.json` and `hidden_test_outcome`.
- If case has none: `hidden_test_outcome: "n/a"`, no file written.

### Step 5 — Edit constraint check

- Resolve effective constraints: merge case `edit_constraints` with `task-spec.md` defaults (defaults fill missing fields).
- Match canonical changed-file list against `disallowed_paths` and `allowed_paths` using the `pathspec` library (gitignore-style globs).
- Check `len(changed_files) <= max_changed_files`.
- Result → `edit_constraint_compliance` object: `{ disallowed_violations: [...], allowed_violations: [...], over_max_changed_files: bool }`.

### Step 6 — Assemble scoring

Build `<cell>/scoring.json` with the categories from `task-spec.md`:

- `schema_validity` — bool (from step 1; see "Scoring on error").
- `visible_test_outcome` — `pass` | `fail` | `error` (from step 3).
- `hidden_test_outcome` — `pass` | `fail` | `error` | `n/a` (from step 4).
- `edit_constraint_compliance` — object (from step 5).
- `minimality` — `{ changed_files, changed_lines_added, changed_lines_removed }` (from step 2).
- `latency_ms` — harness wall-clock from request send to response receive.
- `token_usage` — `{ input, output }` from response `trace.tokens` if present, else omitted.
- `trace_quality` — `"n/a"` in v1.

### Step 7 — Write meta and sentinel

`<cell>/meta.json` is **written last** as the done-sentinel. Contents *(updated in v2)*:

```json
{
  "framework": "...",
  "case_id": "...",
  "task_id": "<F>:<C>:<uuid>",
  "model": "...",
  "started_at": "<iso8601>",
  "ended_at": "<iso8601>",
  "status": "ok" | "error",
  "error_reason": null | "timeout" | "framework_misconfigured" | "nonzero_exit"
                       | "missing_response" | "malformed_response_json"
                       | "envelope_schema_violation",
  "exit_code": "<int|null>",
  "stdout_truncated": "<bool>",
  "stderr_truncated": "<bool>",
  "harness_latency_ms": "<int>",
  "framework_reported_latency_ms": "<int|null>",
  "effective_config": {
    "model": "...",
    "timeout_s": "<int>",
    "max_steps": "<int>",
    "source": "campaign" | "cell-flag"
  },
  "venv_hash_before": "<hex>",
  "venv_hash_after": "<hex>",
  "venv_mutated": "<bool>"
}
```

Resume logic: a cell is "done" iff `meta.json` exists. Anything else (lone `request.json`, partial `diff.patch`, `stdout.log` without `meta.json`) means a crash; resume blows away the dir and reruns.

### Partial-failure visibility

When the agent crashes or times out, steps 2–6 still run. The cell ends up with the diff of whatever the agent edited before crashing, the test outcomes on the partially-edited worktree, the constraint check on whatever files it touched, and a `scoring.json`. `meta.status: "error"` plus `error_reason` makes the report attribute the failure correctly; the artifacts are there for inspection.

## Campaign + Storage Layout

### Top-level dirs (all repo-root, gitignored)

```
.runs-cache/                          # harness-derived
├── <case_id>.git/                    # bare git repo per case (layer 1)
├── <case_id>.venv/                   # shared venv per case (layer 2)
├── <case_id>.fixture-hash             # last-built fixture content hash
├── <case_id>.lock-hash                # last-built uv.lock hash
└── setup/<framework>.ok               # per-framework setup sentinel + manifest hash

runs/                                 # campaign artifacts
├── CURRENT -> 2026-04-29T14-32-08/   # relative symlink
├── 2026-04-29T14-32-08/              # one campaign
│   ├── .lock                         # campaign lockfile
│   ├── manifest.json                 # campaign manifest (immutable after eval-new)
│   ├── report.md                     # generated by eval-all + eval-report
│   └── <framework>/<case>/           # one cell
│       ├── request.json              # request envelope as sent (records effective_config)
│       ├── stdout.log                # raw subprocess stdout (always written)
│       ├── response.json             # parsed envelope (only when stdout.log parsed cleanly)
│       ├── stderr.log
│       ├── diff.patch
│       ├── visible_test.json
│       ├── hidden_test.json          # only when case has hidden_test_command
│       ├── scoring.json              # always written, even on errors
│       ├── meta.json                 # written last (sentinel)
│       └── repo/                     # the layer-3 worktree, kept for inspection
└── 2026-04-29T11-08-45/              # earlier campaign, immutable
```

### Campaign manifest: `runs/<ts>/manifest.json`

Captured at `eval-new` and never mutated:

```json
{
  "started_at": "<iso8601>",
  "git_sha": "<HEAD sha at start>",
  "git_dirty": "<bool>",
  "git_remote_url": "<git remote get-url origin, omitted if none>",
  "git_branch": "<git rev-parse --abbrev-ref HEAD, omitted if detached>",
  "frameworks": ["..."],
  "cases": ["..."],
  "config_overrides": { "model": null, "timeout_s": null, "max_steps": null }
}
```

`frameworks` and `cases` are the *discovered set at start*. If a framework dir is added mid-campaign it is not part of *this* campaign — it shows up in the next `eval-new`. `eval-status` and the report only consider the manifest's matrix.

`config_overrides` captures the campaign-level overrides as set at `eval-new` (or auto-create via `eval-all`); see "Configuration Overrides."

### Campaign lockfile: `runs/CURRENT/.lock`

JSON: `{ "pid": <int>, "hostname": "...", "started_at": "<iso8601>", "argv": [...] }`.

On any harness command that writes to the campaign:
- If the file exists *and* the recorded PID is alive on the same host: refuse with `Campaign in use by PID N (since X). Delete <path> if stale.`
- If the file exists but the PID is dead or the hostname differs: treat as stale and reclaim, after warning.
- Held campaign-wide in v1. When parallelism is added, this degrades to a per-cell claim layer, with the campaign-wide lock still held for `eval-new` and report writes.

### `runs/CURRENT` symlink details

- Relative symlink (so the repo can be moved or cloned).
- Created or updated atomically: write `runs/CURRENT.tmp -> <new>`, then `rename`.
- Unix-only in v1; Windows is undocumented and not supported.
- `eval-status` does `readlink runs/CURRENT` to find the active campaign.

### `.gitignore` delta

The repo's existing `.gitignore` already covers `.env` and `.env.*`. Add:

```
.runs-cache/
runs/
```

Existing `.gitignore` also lists `results/` from earlier scaffolding; the eval harness does not use that path. Cleaning it up is harmless and can be done as part of this work.

### No auto-cleanup

Old campaigns are never auto-deleted. `just eval-clean-runs` wipes all of `runs/`; selective pruning (`rm -rf runs/<ts>`) is left to the user.

## CLI Surface and Module Layout

### Verbs (via `justfile`, all delegating to `evals/__main__.py`)

| verb | description |
| --- | --- |
| `just frameworks` | list framework dirs (already exists) |
| `just cases` | list case ids and which fixtures back them |
| `just eval-prepare` | run all framework `setup`s, materialize `.runs-cache/<case>.git/` and `<case>.venv/`. Idempotent. |
| `just eval-new` | create `runs/<ts>/`, write `manifest.json` (recording any config-override flags), repoint `runs/CURRENT` |
| `just eval-all` | fill missing cells in `runs/CURRENT`. Auto-runs `prepare` and `new` if needed. Rejects override flags inside an existing campaign. |
| `just eval <fw> <case>` | run/rerun one cell; accepts override flags (recorded per-cell) |
| `just eval-status` | print matrix of filled / missing / error per cell in `CURRENT` |
| `just eval-report` | regenerate `runs/CURRENT/report.md` |
| `just eval-clean-cache` | wipe `.runs-cache/` |
| `just eval-clean-runs` | wipe `runs/` |

### Flags

- `--model <id>` — override `config.model`.
- `--timeout-s <n>` — override `config.timeout_s`.
- `--max-steps <n>` — override `config.max_steps`.
- `--framework <name>` and `--case <id>` — restrict the matrix on `eval-all` (intersection — pass either or both).

Override-flag scope per the "Configuration Overrides" rule:
- `eval-new` — accepts; written into campaign `manifest.json#config_overrides`.
- `eval-all` — auto-create case: accepts and forwards into the new campaign manifest. Existing-campaign case: rejects with the helpful error described above.
- `eval` (single cell) — always accepts; recorded only in the cell's `request.json` and `meta.effective_config`.

### Auto-behaviors

- `eval-all` on a cohort with no `runs/CURRENT` auto-runs `eval-new` first.
- `eval-all` auto-runs `eval-prepare` if any setup sentinel is missing or stale.
- `eval` with no args errors with usage.

### Module layout in `evals/`

```
evals/
├── pyproject.toml         # already exists; deps: pathspec, python-dotenv
├── README.md              # already exists; rewrite to match
└── evals/
    ├── __main__.py        # CLI entry (argparse subcommands)
    ├── cli.py             # subcommand dispatch
    ├── discovery.py       # find frameworks/<name>/manifest.json, cases/*.json
    ├── workspace.py       # layers 1, 2, 3 — bare git, venv, per-cell worktree; cache hashes; venv fingerprint
    ├── runner.py          # one cell: build request, spawn (shlex.split), capture, timeout, classify
    ├── pipeline.py        # post-subprocess: temp-index diff, test reruns, edit constraint, scoring
    ├── campaign.py        # eval-new, CURRENT pointer, lockfile, campaign manifest, override recording
    ├── status.py          # eval-status renderer
    ├── report.py          # eval-report renderer (markdown)
    ├── env.py             # .env loading; agent_env and test_env constructors
    └── schemas.py         # JSON schemas: framework manifest, case manifest, contract envelope, agent output
```

No circular deps: `cli` → `campaign` / `status` / `report` / `runner` / `pipeline` / `workspace` → `discovery` / `env` / `schemas`.

CLI uses stdlib `argparse` rather than `click` or `typer` to keep deps minimal. Only added third-party deps are `pathspec` and `python-dotenv`.

## Reporting

### Generation timing

- Auto-generated at the end of every `eval-all` and after any single-cell `eval` run.
- Also exposed as `just eval-report` for ad hoc regeneration after manual edits.

### Shape

One markdown file per campaign at `runs/<ts>/report.md`, accessible as `runs/CURRENT/report.md`. Single file is the right ergonomic — readable in editor, on GitHub, in terminal.

### Content (v1, expected to be tuned after first real campaign)

```markdown
# Campaign <timestamp>

Campaign config: model=<...>, timeout_s=<...>, max_steps=<...>
Cases: N — <case ids>

## Per-cell results

| framework | case | visible | hidden | edit_compl. | files | +/- lines | latency | tokens (i/o) | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ... | ... | ... | ... | ... | ... | ... |

(Cells whose effective_config differs from the campaign config are marked with `*` next to the framework name.)

## Per-framework summary

| framework | cells run | visible pass | hidden pass | mean latency | total tokens (i/o) | errors |
| --- | --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ... | ... | ... | ... |

## Notes

- <typed failure summaries with links to <cell>/stderr.log>
- <venv-mutation warnings, if any>
- trace_quality: n/a in v1 (capture-only)
```

Per `task-spec.md`, no aggregate ranking score. The per-framework summary is descriptive only.

Diff snippets are not embedded in the report; the report links to `<cell>/diff.patch` instead. Same for failure stderr. The report is a navigation index plus the comparative table; source-of-truth artifacts live in the cell dirs.

## Testing the Harness

### Test framework

`pytest`, in `evals/tests/`.

### Fake framework — `evals/tests/fixtures/fake-framework/`

A canonical "framework" the test suite controls end-to-end. A Python `run.sh` reads the request from stdin and emits a response according to a `FAKE_BEHAVIOR` env var the harness test sets:

| `FAKE_BEHAVIOR` | what it does |
| --- | --- |
| `success-noop` | valid envelope, no edits, schema-valid output |
| `success-fix` | apply a hard-coded fix to the per-cell repo, valid envelope |
| `hang` | sleep forever (test `timeout`) |
| `crash` | exit 1 with stderr (test `nonzero_exit`) |
| `crash-with-error-json` | exit 1 with a parseable error envelope (test `nonzero_exit` + response recorded) |
| `garbage` | write non-JSON to stdout (test `malformed_response_json`) |
| `empty` | exit 0 with empty stdout (test `missing_response`) |
| `oversize` | write more than the cap to stdout (test cap + `malformed_response_json` + `stdout_truncated`) |
| `missing-field` | valid JSON, missing `trace` (test `envelope_schema_violation`) |
| `forbidden-field` | output contains top-level `fixed` (test `schema_validity=false`, non-fatal) |
| `disallowed-edit` | edit `tests/foo` (test `edit_constraint_compliance`) |
| `over-max-files` | edit more files than `max_changed_files` allows |
| `noisy-stderr` | emit > 5 MiB to stderr (test stderr cap + `stderr_truncated`) |

Plus a manifest declaring `entry`, no `setup`, and `model: "fake"`. Tests dispatch the harness against this fake to drive every code path in `runner` and `pipeline`.

### Synthetic case fixture — `evals/tests/fixtures/cases/test-case-001/`

Tiny: one source file with a known bug, one failing test, one optional hidden test. Used by the fake framework's `success-fix` mode. Lets the suite assert end-to-end on:

- Layer 1 bare-repo construction, content-hash detection, rebuild-on-change.
- Layer 2 venv build (marked integration; skipped if `uv` unavailable).
- Layer 3 worktree clone, mutation, diff derivation that does not stage the real index.
- Visible/hidden test reruns producing the expected outcomes.

### Module-level unit tests

- `discovery_test.py` — finds frameworks, finds cases, **errors on malformed framework manifest** (yields `framework_misconfigured` upstream), errors on missing entry executable.
- `schemas_test.py` — validates known-good and known-bad envelopes against schemas; agent-output schema rejects forbidden top-level keys.
- `env_test.py` — `.env` loading; `agent_env` includes declared keys; `test_env` does **not** include framework keys (regression test for the v2 split); env scrubbing does not leak undeclared vars.
- `workspace_test.py` — content-hash rebuild trigger, idempotent prepare, concurrent-safe layer 3 clone, venv fingerprint stability across reads, fingerprint changes when a `.dist-info` is added.
- `pipeline_test.py` — fed canned `(response, worktree_state, case)` tuples, asserts `scoring.json` shape and contents; **temp-index diff does not modify `<cell>/repo/.git/index`**.
- `report_test.py` — golden-file test of report rendering against a synthetic campaign on disk; flags cells whose `effective_config` differs from campaign config.
- `campaign_test.py` — `eval-new` creates dir + manifest + symlink atomically; lockfile semantics (refuse on live PID, reclaim on dead PID, refuse on different host); **`eval-all` rejects override flags in existing campaign**; **`eval` records cell-level overrides in `request.json` and `meta.effective_config`**.
- `runner_test.py` — error-state precedence table is reachable for each row using the fake framework; `nonzero_exit` with parseable stdout still writes `response.json`; `nonzero_exit` with garbage stdout does not.
- `resume_test.py` — partial cell dir without `meta.json` is blown away on resume; `eval-all` skips cells that have `meta.json`, including error cells (so retries are explicit, not implicit).

### Integration tests — `evals/tests/integration/`

End-to-end via subprocess against the fake framework + synthetic case. One test per `FAKE_BEHAVIOR` value, asserting:

- `meta.json` reaches the expected `status` and `error_reason`.
- Pipeline steps that should still run on failure actually do.
- The report regenerates without crashing for any of these states.

Slower; runnable with `pytest -m integration`.

### Coverage target

No coverage number, but the integration suite must hit every `error_reason` value, every row of the error precedence table, and every `scoring.json` field.

### Layer 2 caveat

Tests that exercise real `uv sync` against fixture pyproject files require `uv` and possibly network. They're marked integration; the unit suite stubs `workspace.ensure_case_venv` to a no-op and tests it separately.

## Parallelism Notes (Future)

The design extends cleanly to parallelism without retro-changes:

- Cells are already independent units (worktree per cell, dir per cell).
- `git clone --local` from `.runs-cache/<case>.git/` is concurrent-safe.
- The shared per-case venv is fine for concurrent reads (running pytest); concurrent writes (`uv add` etc) are an out-of-scope agent-side bug, detectable via the venv fingerprint.
- `runs/CURRENT` is read during runs and only written by `eval-new`.
- Per-cell directories are independent FS writes.

Three things to add when parallelism ships:

1. Pre-flight: a `prepare` step (already in v1) that materializes `.runs-cache/<case>.git/` and `<case>.venv/` for every case in the matrix sequentially, before parallel work starts.
2. Cell-claim atomicity: enumerate the work list up front and hand cells out from a queue. No per-cell file locks needed if dispatch is done from a single coordinator.
3. Done-sentinel discipline: already baked in. Treat a cell as "done" only when `meta.json` is present.

Optional knobs at that point: `--max-concurrency`, per-provider semaphores for API rate limits.

## Acceptance Criteria

- `evals/` contains the modules listed in "Module layout" with the responsibilities described.
- `just eval-all` on a fresh clone (with stub framework scripts) runs end-to-end: prepares the cache, creates a campaign, fills every cell with `status: "error"` and `error_reason: "nonzero_exit"`, and generates a report.
- Workspace lifecycle: layer 1 / 2 / 3 build and rebuild on the documented triggers. Re-running `eval-prepare` after no changes does no work. Venv fingerprint is recorded before and after each cell run.
- Pipeline: every typed `error_reason` is reachable and recorded correctly via the fake-framework integration suite, in accordance with the precedence table. Schema-validity violations of agent `output` are non-fatal. Diff derivation does not mutate `<cell>/repo/.git/index`.
- Storage: `runs/CURRENT` symlink, campaign `manifest.json`, lockfile semantics (refuse / reclaim) work as specified. `stdout.log` is always present; `response.json` is present iff stdout parsed cleanly.
- CLI: every verb in the table runs and does what its row says. Targeted `--framework` / `--case` filtering on `eval-all` works. `eval-all` rejects override flags in an existing campaign with a helpful message; `eval` accepts them per-cell. Re-running `eval <fw> <case>` overwrites that cell.
- Configuration overrides: campaign-level overrides are recorded in `manifest.json#config_overrides` and frozen; per-cell `meta.effective_config` records what actually ran with its source.
- Environments: `agent_env` includes declared framework keys; `test_env` excludes them. Tests assert this distinction.
- Reporting: `report.md` regenerates idempotently from cell artifacts. Cells with cell-level overrides are visibly marked.
- Tests: unit suite passes without `uv`; integration suite passes with `uv` available and exercises every `error_reason` and every scoring field.
- The harness never imports framework code; it only invokes per-framework entry scripts as subprocesses.
- The `repo/` worktree is left in place after each cell run; deviation from `task-spec.md` is documented in "Compatibility."

## Open Implementation Details

To resolve during the implementation plan rather than now:

- Exact `uv sync` flag combination for layer 2 venv build; verify that `UV_PROJECT_ENVIRONMENT` plus a project pointer is the cleanest invocation.
- Hashing algorithm for fixture content hash and lock hash (likely BLAKE2 over a sorted file list); same algorithm for the venv fingerprint over `*.dist-info/` directory listings.
- JSON schema files: inline as Python dicts vs. shipped as `.schema.json`.
- Whether the `cases` verb reads `cases/*.json` directly or goes through `discovery.py`.
- Whether report cell-level override marker should be `*` next to the framework name vs. a separate column; defer until first real run.
