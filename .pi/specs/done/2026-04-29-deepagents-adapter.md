# DeepAgents Framework Adapter

Source: TODO-1342fa54

## Goal

Replace the v1 stub at `frameworks/deepagents/` with a working DeepAgents adapter that satisfies the shared software-bugfix benchmark contract. The adapter reads a JSON request envelope from stdin, runs a real DeepAgents loop against the per-cell worktree, edits the worktree in place to fix the failing test, and writes a single contract-compliant JSON response envelope to stdout. This is the first real (non-stub) adapter in the repo; it sets the wiring pattern other framework dirs will follow but is independent of them.

## Context

The harness (`evals/`) and the shared contract (`shared/contract.md`, `shared/task-spec.md`) are stable. Every other directory under `frameworks/` is a v1 stub whose `run.sh` exits non-zero. DeepAgents will be the first real adapter.

The harness invokes the adapter as a subprocess: it spawns the entry command declared in `frameworks/deepagents/manifest.json` with `cwd=frameworks/deepagents/`, pipes the request envelope to stdin, captures stdout and stderr to per-cell logs, and enforces an outer timeout. Adapter env is built by `build_agent_env`: only the manifest's declared keys are forwarded from the parent process and `.env`, plus a small allowlist (`HOME`, `LANG`, `TERM`, `PATH`); `PATH` is prepended with the case venv's `bin/` so harness-prepared dependencies (e.g. `pytest` for `py-parse-duration-001`) are discoverable from the agent's child shells. The harness owns worktree lifecycle, canonical-diff derivation against pristine HEAD, visible/hidden test reruns, edit-constraint checks, and scoring — the adapter is responsible only for running the agent loop and emitting a valid envelope.

DeepAgents is a Python library on top of LangGraph. Out of the box it ships a configured agent with built-in filesystem tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`), planning (`write_todos`), sub-agent delegation (`task`), context summarization, and an optional `execute` shell tool. Tool availability is gated by the configured **backend** passed to `create_deep_agent(...)`:

- `StateBackend` — virtual in-memory FS, no shell. Default.
- `FilesystemBackend(root_dir=...)` — real local FS, rooted at `root_dir`. Filesystem only.
- `LocalShellBackend` — real local FS plus the `execute` tool, runs commands directly via subprocess with the process owner's permissions; explicit opt-in. Per the deepagents threat model, it inherits the parent process env by default and supports configurable timeouts and output capping.
- Sandbox backends (Modal, Daytona, OpenSandbox) — full FS + shell inside an isolated runtime. Out of scope here.

The bootstrap case (`py-parse-duration-001`) ships a visible test (`pytest -q tests/test_parse_duration.py`) and a hidden test (`pytest -q tests/test_parse_duration_extended.py`); the hidden test is intentionally designed to catch three under-fixes (adding only `s` to UNITS, defensive `UNITS.get(..., 1)` in parser, hardcoding the failing input). The case venv is harness-owned and pre-pinned through `UV_PROJECT_ENVIRONMENT` and the `PATH` prefix; cell-time mutations to the venv are detected and reported as `venv_mutated`.

## Requirements

- `frameworks/deepagents/run.sh` reads exactly one JSON request envelope from stdin and writes exactly one JSON response envelope (matching `shared/contract.md`) to stdout, then exits.
- `frameworks/deepagents/manifest.json` declares `entry`, an idempotent `setup` command, `env: ["ANTHROPIC_API_KEY"]`, and `model: "claude-sonnet-4-6"`.
- DeepAgents-specific dependencies are managed under `frameworks/deepagents/` (`pyproject.toml` plus a lockfile), not shared with the harness or other framework directories.
- The agent loop honors `shared/task-spec.md`'s required capabilities — file inspection, file search, file editing, test execution, diff inspection — all rooted at `input.repo_path`.
- Agent-side test invocations and `git diff` run with the working directory set to `input.repo_path`.
- All adapter logging and DeepAgents-internal output goes to stderr; stdout carries only the final envelope.
- The response envelope's `output` block contains `root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, and `confidence` and contains none of the forbidden keys (`fixed`, `not_fixed`, `status`).
- The response envelope's `trace` block contains `steps`, `tokens` (`{input, output}` ints), and `latency_ms` — populated from the DeepAgents message history and AIMessage usage metadata when the provider reports them; tokens are omitted (handled by harness fallback) only when the provider does not report usage.
- The adapter respects `config.timeout_s` (best-effort: aim to complete and emit envelope before harness hard-kill) and `config.max_steps` (mapped to LangGraph `recursion_limit`).
- On any failure path inside the adapter (model error, recursion-limit hit, no `submit_report` call observed), the adapter emits a contract-compliant envelope with `error` populated and exits non-zero. It must not emit malformed JSON or partial output.
- `frameworks/deepagents/README.md` documents: setup command, model choice, required env vars (`ANTHROPIC_API_KEY`), how filesystem and shell tools are wired (which backend), how the agent submits its structured report, and the contingency around `LocalShellBackend` cwd control.

## Constraints

- Anthropic-only for v1. Default model is `claude-sonnet-4-6`. OpenAI and other providers are deferred — the README must say so. The adapter may accept a `config.model` that names another Anthropic model variant, but is not required to support cross-provider overrides.
- No new sandbox / virtualization dependencies (Modal, Daytona, OpenSandbox). The adapter must run with only direct subprocess access.
- No changes to harness internals (`evals/`), the shared contract (`shared/`), case manifests (`cases/`), or fixtures (`fixtures/`). Edits scoped to `frameworks/deepagents/`.
- The `frameworks/deepagents/` directory remains independent of other framework directories — no shared lockfile or shared Python package.
- The agent must not commit, reset, or otherwise mutate `.git/` in `input.repo_path`. Diff derivation is the harness's responsibility and uses a temporary index against pristine HEAD.
- The case venv (`<cache>/<case_id>.venv`) is harness-owned and fingerprinted before/after each cell. The adapter must not invoke commands that mutate it (e.g., `pip install` / `uv sync` into that venv). Tests that need the case venv use the harness-prepared `PATH` only.

## Approach

**Chosen approach: `LocalShellBackend` (Approach A) for both filesystem and shell tools, constructed with `cwd=input.repo_path`. The agent submits its final structured payload via a dedicated `submit_report` tool that the adapter watches for in the message stream.**

The adapter is one Python entry script invoked by `run.sh` (which `cd`s into `frameworks/deepagents/` and shells out to `uv run python <entry>`). It:

1. Reads the request envelope from stdin and validates the required `input.*` and `config.*` fields.
2. Constructs a DeepAgents agent via `create_deep_agent(model=init_chat_model("anthropic:..."), backend=LocalShellBackend(<cwd-pinned-to-input.repo_path>, ...), tools=[submit_report], system_prompt=<task-tailored prompt>)`. The prompt surfaces the bugfix task framing, the `submit_report` contract, and the edit constraints.
3. Invokes the agent with a single user message that surfaces `failing_test_command`, `failure_output`, and `edit_constraints`. Configures LangGraph `recursion_limit` from `config.max_steps` (with a small multiplier — see Open Questions).
4. Captures the agent's tool calls, model calls, and token usage from the returned message list and converts them to the contract's `trace.steps` / `trace.tokens` / `trace.latency_ms` shape.
5. On `submit_report` invocation, captures the structured args (Pydantic-validated by the `@tool` decorator). If the agent calls `submit_report` more than once, the last call wins.
6. Emits the final envelope to stdout — `{task_id, output: <submit_report args>, trace: <converted history>, error: null}` — and exits 0. On any failure path, emits an envelope with `error` populated and exits non-zero, per the contract.

**Why this over alternatives:**

`LocalShellBackend` gives us deepagents' canonical tool surface (`read_file`/`write_file`/`edit_file`/`ls`/`glob`/`grep`/`execute`) and the system-prompt scaffolding deepagents already trains on, with one constructor instead of a hand-rolled tool list. `submit_report` is robust against provider-side feature variation (no `response_format` plumbing risk), Pydantic-validates the structured fields up front, and matches DeepAgents' callable-tool convention.

The fallback path is small enough to pre-document: if `LocalShellBackend` does not expose a working-directory knob (or its env propagation conflicts with the harness's `build_agent_env`), the adapter swaps to `FilesystemBackend(root_dir=input.repo_path)` plus a custom `@tool def shell(command: str)` that calls `subprocess.run(..., cwd=input.repo_path, env=...)`. This is roughly a one-line backend swap and ~15 lines of shell-tool code; it does not change the overall design and is in scope for the same implementation pass.

**Considered and rejected:**

- *Approach B (`FilesystemBackend` + custom `shell` tool) as the primary*: viable, but loses deepagents' canonical `execute` system-prompt scaffolding. Held in reserve as the contingency rather than chosen as default because A is strictly simpler when its cwd knob exists.
- *Approach C (hybrid: `LocalShellBackend` plus separate `pytest` and `git_diff` narrow tools)*: more tool surface for ambiguous benefit; the threat model is dominated by `edit_file` + arbitrary-shell, not by tool naming, so narrowing the shell tool would be cosmetic.
- *Output extraction via `response_format` / `model.with_structured_output(...)`*: risks fighting deepagents' middleware; structured-output features may not propagate cleanly through the loop.
- *Output extraction via post-loop extraction LLM call*: doubles model spend per cell and risks the extraction call disagreeing with the agent's actual edits.
- *Sandbox backend (Modal, Daytona, OpenSandbox)*: out of scope per user preference for "safe but no sandbox overhead"; isolation is already provided by the harness's per-cell worktree lifecycle.

## Acceptance Criteria

- After `just eval-prepare` and `just eval-new`, `just eval deepagents py-parse-duration-001` produces `runs/CURRENT/deepagents/py-parse-duration-001/` with `meta.json`, `scoring.json`, `request.json`, `response.json`, `stdout.log`, `stderr.log`, `diff.patch`, `visible_test.json`, and `hidden_test.json`.
- `meta.json.status == "ok"` and `meta.json.error_reason == null`.
- `scoring.json.schema_validity == true` (envelope and `output` block both validate against `shared/contract.md` and `shared/task-spec.md`).
- `scoring.json.visible_test_outcome == "pass"` on `py-parse-duration-001`. Single-shot runs may show model-side nondeterminism; the adapter must not be the source of flakiness.
- `scoring.json.hidden_test_outcome == "pass"` on `py-parse-duration-001`.
- `scoring.json.edit_constraint_compliance` is clean (`disallowed_violations == []`, `allowed_violations == []`, `over_max_changed_files == false`).
- `scoring.json.minimality.changed_files <= 5` and the change set is plausibly minimal (one or two files; not a sweeping rewrite).
- `scoring.json.trace_quality` step list shows a sensible debugging workflow — at least one file read before edits, at least one test run, non-trivial step count.
- `meta.json.venv_mutated == false` (case venv untouched by the agent run).
- `frameworks/deepagents/README.md` exists and covers setup, model, env vars, tool wiring, the `submit_report` contract, and the `LocalShellBackend` cwd contingency.
- Existing harness tests in `evals/tests/` continue to pass (no harness changes; adapter changes alone should not affect them).

## Non-Goals

- Implementing other framework adapters. Each is independent follow-on work.
- Changes to `evals/`, `shared/`, `cases/`, or `fixtures/`.
- Provider-flexible adapters (OpenAI, Bedrock, etc.). Anthropic-only for v1.
- Sandbox / virtualized execution (Modal, Daytona, OpenSandbox).
- Reaching any particular pass rate on the SWE-bench-style cases (`psf__requests-1921`, `pylint-dev__pylint-7080`, `pytest-dev__pytest-7571`); their pass rate is a benchmark *measurement*, not an adapter acceptance criterion.
- A leaderboard-style aggregate score across cases (deferred per `shared/task-spec.md`).
- Adapter-level unit tests. The harness owns end-to-end coverage; adapter correctness is verified via cell runs.

## Open Questions

- Does `LocalShellBackend.__init__` accept a `root_dir` / `cwd` parameter that pins the cwd of `execute` invocations to `input.repo_path`? If yes, Approach A as written. If no, fall back to Approach B (`FilesystemBackend` + custom `shell` tool) without revisiting the design. The implementer should verify by inspecting the installed `deepagents` source before writing the agent loop.
- `LocalShellBackend`'s default env propagation inherits the parent process env. Confirm that `build_agent_env`'s outputs (`PATH` with case venv prepended, `ANTHROPIC_API_KEY`) flow through to subprocess `pytest` invocations as expected; if not, the shell-tool path needs an explicit `env=os.environ.copy()` on the call site.
- LangGraph `recursion_limit` ↔ `config.max_steps` mapping. `recursion_limit` counts graph steps, not strictly tool calls, so a small multiplier is suggested (e.g. `max_steps * 2`); exact mapping may need tuning once a real run lands.
