# Pydantic-AI Framework Adapter

Source: TODO-9d4a65e1

## Goal

Replace the v1 stub at `frameworks/pydantic-ai/` with a working Pydantic-AI adapter that satisfies the shared software-bugfix benchmark contract. The adapter reads a JSON request envelope from stdin, runs a Pydantic-AI agent against the per-cell worktree, edits the worktree in place to fix the failing test, and writes a single contract-compliant JSON response envelope to stdout. It is one of two real (non-stub) adapters being specced concurrently — alongside the deepagents adapter — both targeting the same harness/contract surface but using the framework-native idioms appropriate to each.

## Context

The harness (`evals/`), shared contract (`shared/contract.md`), and task spec (`shared/task-spec.md`) are stable. Every directory under `frameworks/` is currently a v1 stub whose `run.sh` exits non-zero; this spec covers the pydantic-ai dir.

The harness invokes the adapter as a subprocess: it spawns the entry command declared in `frameworks/pydantic-ai/manifest.json` with `cwd=frameworks/pydantic-ai/`, pipes the request envelope to stdin, captures stdout/stderr to per-cell logs, and enforces an outer timeout. Adapter env is built by `build_agent_env`: only the manifest's declared keys are forwarded from the parent process and `.env`, plus a small allowlist (`HOME`, `LANG`, `TERM`, `PATH`); `PATH` is prepended with the case venv's `bin/` so harness-prepared dependencies (e.g. `pytest` for `py-parse-duration-001`) are discoverable from the agent's child shells. The harness owns worktree lifecycle, canonical-diff derivation against pristine HEAD, visible/hidden test reruns, edit-constraint checks, and scoring — the adapter is responsible only for running the agent loop and emitting a valid envelope.

Pydantic-AI is a Python agent framework by the Pydantic team. Its distinguishing properties for this task:

- **Native structured output** — `Agent(..., output_type=PydanticBaseModel)` makes the agent's final response a validated instance of a Pydantic model with no separate `submit_report` tool dance and no `response_format` plumbing for the adapter to manage.
- **Native tool registration** — `@agent.tool_plain` (no context) and `@agent.tool` (with `RunContext[Deps]`) decorate Python functions; arg shape is Pydantic-validated automatically.
- **Provider-flexible** — string IDs like `'anthropic:claude-sonnet-4-6'` route through pydantic-ai's known-model registry and switch providers; for v1 we constrain to Anthropic (see Constraints).
- **No built-in filesystem or shell tools** — unlike deepagents, the adapter author must wire the file-inspection / search / edit / test-execution / diff-inspection capabilities itself.
- **Trace and usage exposed on the result** — `result.all_messages()` returns `ModelRequest` / `ModelResponse` objects whose parts include `ToolCallPart` / `ToolReturnPart` / `TextPart` plus per-response `RequestUsage`; `result.usage()` returns a cumulative `RunUsage(input_tokens, output_tokens, requests)`. These shapes map cleanly onto the contract's `trace.steps` / `trace.tokens` / `trace.latency_ms`.

The bootstrap case (`py-parse-duration-001`) ships a visible test (`pytest -q tests/test_parse_duration.py`) and a hidden test (`pytest -q tests/test_parse_duration_extended.py`); the hidden test is intentionally designed to catch under-fixes. The case venv is harness-owned and pre-pinned through `UV_PROJECT_ENVIRONMENT` and the `PATH` prefix; cell-time mutations to the venv are detected and reported as `venv_mutated`.

## Requirements

- `frameworks/pydantic-ai/run.sh` reads exactly one JSON request envelope from stdin and writes exactly one JSON response envelope (matching `shared/contract.md`) to stdout, then exits.
- `frameworks/pydantic-ai/manifest.json` declares `entry`, an idempotent `setup` command, `env: ["ANTHROPIC_API_KEY"]`, and `model: "claude-sonnet-4-6"`.
- Pydantic-AI-specific dependencies are managed under `frameworks/pydantic-ai/` (`pyproject.toml` plus a lockfile), not shared with the harness or other framework directories.
- The agent loop honors `shared/task-spec.md`'s required capabilities — file inspection, file search, file editing, test execution, diff inspection — all rooted at `input.repo_path`.
- The agent's structured output is produced via `Agent(..., output_type=AgentReport)` where `AgentReport` is a Pydantic `BaseModel` with the contract's required fields (`root_cause`, `summary`, `changed_files`, `tests_run`, `evidence`, `confidence`). No `submit_report` tool exists. The output model must not declare any of the forbidden top-level keys (`fixed`, `not_fixed`, `status`).
- The tool surface is **hybrid**: filesystem capabilities (file read, file write/edit, listing, glob, grep) are individual Python `@agent.tool_plain` functions; test execution and `git diff` go through one `run_shell` tool that calls `subprocess.run(..., cwd=input.repo_path, env=...)`. Filesystem-tool path arguments are anchored at `input.repo_path` and rejected if they escape it.
- All adapter logging and Pydantic-AI internal output goes to stderr; stdout carries only the final envelope.
- The response envelope's `output` block is the validated `AgentReport` instance (serialized via `.model_dump()`) and contains none of the forbidden keys.
- The response envelope's `trace` block contains `steps`, `tokens` (`{input, output}` ints), and `latency_ms` — populated from `result.all_messages()` and `result.usage()`. Tokens are omitted (handled by harness fallback) only if the provider returns no usage data.
- The adapter respects `config.timeout_s` (best-effort: aim to complete and emit envelope before harness hard-kill) and `config.max_steps` (mapped onto pydantic-ai's `UsageLimits.request_limit` with a small multiplier — see Open Questions).
- On any failure path inside the adapter (model error, usage-limit exceeded, output-type validation error, tool-tier exception bubbling up), the adapter emits a contract-compliant envelope with `error` populated and exits non-zero. It must not emit malformed JSON or partial output.
- `frameworks/pydantic-ai/README.md` documents: setup command, model choice, required env vars (`ANTHROPIC_API_KEY`), the hybrid tool surface, the `output_type=AgentReport` structured-output approach, and the Anthropic-only constraint.

## Constraints

- Anthropic-only for v1. Default model is `claude-sonnet-4-6`. Other providers (OpenAI, Gemini, Mistral, etc., all natively supported by pydantic-ai) are deferred — the README must say so. The adapter may accept a `config.model` that names another Anthropic model variant, but is not required to support cross-provider overrides.
- No new sandbox / virtualization dependencies. The adapter must run with only direct subprocess access for the `run_shell` tool.
- No changes to harness internals (`evals/`), the shared contract (`shared/`), case manifests (`cases/`), or fixtures (`fixtures/`). Edits scoped to `frameworks/pydantic-ai/`.
- The `frameworks/pydantic-ai/` directory remains independent of other framework directories — no shared lockfile or shared Python package.
- The agent must not commit, reset, or otherwise mutate `.git/` in `input.repo_path`. Diff derivation is the harness's responsibility and uses a temporary index against pristine HEAD.
- The case venv (`<cache>/<case_id>.venv`) is harness-owned and fingerprinted before/after each cell. The adapter must not invoke commands that mutate it (e.g., `pip install` / `uv sync` into that venv). Tests that need the case venv use the harness-prepared `PATH` only.

## Approach

**Chosen approach:** A single Python entry script invoked by `run.sh` (which `cd`s into `frameworks/pydantic-ai/` and shells out to `uv run python <entry>`). The agent is constructed as `Agent(model='anthropic:claude-sonnet-4-6', output_type=AgentReport, instructions=<task-tailored prompt>)` with a hybrid tool surface registered via `@agent.tool_plain`: narrow Python filesystem tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`) plus a single `run_shell(command: str)` tool that does `subprocess.run(..., cwd=input.repo_path, env=...)`. Output extraction uses pydantic-ai's native `output_type=` — no `submit_report` tool exists.

The adapter:

1. Reads the request envelope from stdin and validates the required `input.*` and `config.*` fields.
2. Defines `AgentReport(BaseModel)` with the contract-required fields; constructs the `Agent` with `model=<config.model normalized to 'anthropic:<id>'>`, `output_type=AgentReport`, and `instructions=<system prompt>` that surfaces the bugfix framing, the edit constraints, and the available tool surface.
3. Registers the hybrid tool surface against the agent. Filesystem-tool path arguments are validated to stay within `input.repo_path`. The `run_shell` tool runs with `cwd=input.repo_path` and the agent's env (with case-venv `bin/` already on `PATH`).
4. Calls `agent.run_sync(<single user message that includes failing_test_command, failure_output, edit_constraints>, usage_limits=UsageLimits(request_limit=<from config.max_steps>))`.
5. Captures `result.all_messages()` and `result.usage()` and converts them to the contract's `trace.steps` / `trace.tokens` / `trace.latency_ms` shape.
6. Emits the final envelope to stdout — `{task_id, output: <AgentReport.model_dump()>, trace: <converted history>, error: null}` — and exits 0. On any failure path, emits an envelope with `error` populated and exits non-zero, per the contract.

**Why this over alternatives:**

The `output_type=AgentReport` path is pydantic-ai's strongest idiom and gives stronger guarantees than the deepagents `submit_report` pattern: structured output is enforced by the framework before the run returns, not by watching for a tool call. There's no provider-side `response_format` plumbing to break here because pydantic-ai handles it internally.

The hybrid tool surface (filesystem tools + one `run_shell`) gives Pydantic-validated arg shapes for the structured ops while preserving shell composability for tests, `git diff`, and anything we can't anticipate. It mirrors the effective shape of deepagents' `LocalShellBackend` so the two adapters are comparable at the capability level even with different idioms.

**Considered and rejected:**

- *`submit_report`-tool pattern (mirror deepagents)*: rejected. Pydantic-ai has no `response_format`-fights-the-loop problem to work around; the native `output_type` is strictly cleaner here. Maintaining structural uniformity across adapters is not worth giving up the framework's idiomatic output guarantee.
- *Shell-only tool surface (single `run_shell`)*: rejected. Smaller surface but worse trace (every step an opaque shell string), no per-tool arg validation, more fork/exec overhead per step, and edit-constraint hygiene would rely on the model's good behavior alone.
- *Narrow specialized tools only (no `run_shell`)*: rejected. No clean way to invoke `failing_test_command` (which is already a shell string) and `git diff` is most natural through the shell. Wrapping test execution as a narrow tool would just introduce another tool shape the model has to learn.
- *Multi-provider routing in v1*: rejected to keep parallel scope with the deepagents adapter. Pydantic-ai supports it natively; expanding scope here would diverge with no acceptance benefit.
- *Post-loop extraction LLM*: rejected. Doubles model spend, risks disagreement with the agent's actual edits, and is the worst option when `output_type=` already exists.

## Acceptance Criteria

- After `just eval-prepare` and `just eval-new`, `just eval pydantic-ai py-parse-duration-001` produces `runs/CURRENT/pydantic-ai/py-parse-duration-001/` with `meta.json`, `scoring.json`, `request.json`, `response.json`, `stdout.log`, `stderr.log`, `diff.patch`, `visible_test.json`, and `hidden_test.json`.
- `meta.json.status == "ok"` and `meta.json.error_reason == null`.
- `scoring.json.schema_validity == true` (envelope and `output` block both validate against `shared/contract.md` and `shared/task-spec.md`).
- `scoring.json.visible_test_outcome == "pass"` on `py-parse-duration-001`. Single-shot runs may show model-side nondeterminism; the adapter must not be the source of flakiness.
- `scoring.json.hidden_test_outcome == "pass"` on `py-parse-duration-001`.
- `scoring.json.edit_constraint_compliance` is clean (`disallowed_violations == []`, `allowed_violations == []`, `over_max_changed_files == false`).
- `scoring.json.minimality.changed_files <= 5` and the change set is plausibly minimal (one or two files; not a sweeping rewrite).
- `scoring.json.trace_quality` step list shows a sensible debugging workflow — at least one file read before edits, at least one test run, non-trivial step count.
- `meta.json.venv_mutated == false` (case venv untouched by the agent run).
- `frameworks/pydantic-ai/README.md` exists and covers setup, model, env vars, the `output_type=AgentReport` pattern, the hybrid tool surface, and the Anthropic-only constraint.
- Existing harness tests in `evals/tests/` continue to pass (no harness changes; adapter changes alone should not affect them).

## Non-Goals

- Implementing other framework adapters. Each is independent follow-on work.
- Changes to `evals/`, `shared/`, `cases/`, or `fixtures/`.
- Provider-flexible adapters (OpenAI, Gemini, etc.). Anthropic-only for v1, even though pydantic-ai supports them natively.
- Sandbox / virtualized execution.
- Reaching any particular pass rate on the SWE-bench-style cases (`psf__requests-1921`, `pylint-dev__pylint-7080`, `pytest-dev__pytest-7571`); their pass rate is a benchmark *measurement*, not an adapter acceptance criterion.
- A leaderboard-style aggregate score across cases (deferred per `shared/task-spec.md`).
- Adapter-level unit tests. The harness owns end-to-end coverage; adapter correctness is verified via cell runs.

## Open Questions

- `config.max_steps` ↔ pydantic-ai usage-limits mapping. Pydantic-ai exposes `UsageLimits(request_limit=..., total_tokens_limit=...)` rather than a strict step counter. `request_limit` is the closest analog and counts model requests; the implementer should pick a small multiplier (e.g. `max_steps * 2`) and tune once a real run lands. If `request_limit` proves a poor fit, fall back to `agent.iter()` and count steps manually.
- The exact pydantic-ai version pin. The latest stable 1.x release is the default expectation; the implementer should pick a single release and lock it in `uv.lock` rather than tracking `main`. `result.usage()`'s token field names have evolved across versions (e.g. `input_tokens`/`output_tokens` vs older `request_tokens`/`response_tokens`), so the trace-mapping code needs to read the fields the pinned version actually exposes.
- Manifest model normalization. The harness manifest holds `model: "claude-sonnet-4-6"` (no provider prefix), but pydantic-ai expects `'anthropic:claude-sonnet-4-6'`. The adapter prepends the `anthropic:` prefix when normalizing `config.model`. Confirm this normalization is purely additive and does not need to surface as a manifest schema change.
