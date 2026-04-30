# pydantic-ai

Pydantic AI (Python) — adapter for the agent shootout's software-bugfix benchmark.

## Model

- Default: `claude-sonnet-4-6` (Anthropic).
- Anthropic-only for v1. Pydantic-AI supports OpenAI, Gemini, Mistral, etc. natively, but provider switching is deferred — see `.pi/specs/2026-04-29-pydantic-ai-adapter.md`.
- The harness may pass another Anthropic model via `--model claude-...`; the adapter prepends the `anthropic:` prefix and forwards it to `Agent(model='anthropic:<model>')` without further validation.

## Setup

- Manifest setup command: `uv sync --frozen` (run once by the harness during `just eval-prepare`).
- Dependency: `pydantic-ai>=1.0,<2.0`. Locked in `uv.lock`.
- Python: `>= 3.11` (this directory pins `3.12` via `.python-version`).

## Environment variables

- `ANTHROPIC_API_KEY` — required. Forwarded by the harness from the calling shell or repo-root `.env`.

## Tool wiring

The agent uses a **hybrid tool surface**:

- **Filesystem (Pydantic-validated `@agent.tool_plain` Python functions)**: `read_file`, `write_file`, `edit_file`, `list_dir`, `glob`, `grep`. Path arguments are anchored at `input.repo_path` — any path that resolves outside the repo is rejected with an error string the model receives back.
- **Shell (`run_shell(command: str)`)**: a single tool that calls `subprocess.run(["/bin/sh", "-c", command], cwd=input.repo_path, env=<reconstructed>, capture_output=True, timeout=60)` and returns `exit_code: <N>` plus the combined stdout+stderr (capped at 1 MiB). The env is rebuilt from a small allowlist (`HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `TZ`, `PATH`) plus `PYTHONPATH=<repo>/src:<repo>` and `PYTHONDONTWRITEBYTECODE=1`. When `run.sh` preserved the harness-provided case venv as `AGENT_HARNESS_CASE_VENV`, the adapter restores `UV_PROJECT_ENVIRONMENT=<case-venv>`, sets `UV_NO_SYNC=1`, and prepends `<case-venv>/bin` to `PATH` so agent-side `pytest`/`uv` see the same case-test environment as harness reruns. Provider/API secrets (`ANTHROPIC_API_KEY`, etc.) needed by the adapter to talk to the model are deliberately not forwarded into the model-controlled shell. This is the path for `pytest`, `git diff`, and anything else that needs shell composability.

Why hybrid: filesystem ops benefit from Pydantic-validated arg shapes and clearer per-step traces; shell composability is preserved for tests and `git diff` so we don't have to invent a separate test-runner tool.

## Structured output

The agent's final response is produced via Pydantic-AI's native `output_type=AgentReport`, where `AgentReport` is a Pydantic `BaseModel` with the contract's six required fields:

```python
class AgentReport(BaseModel):
    root_cause: str
    summary: str
    changed_files: list[str]
    tests_run: list[_TestRun]   # _TestRun = {command, exit_code, summary}
    evidence: str
    confidence: float = Field(ge=0.0, le=1.0)
```

The model validates the structured output before the agent run returns. There is no `submit_report` tool (cf. the deepagents adapter, which uses one). Forbidden top-level keys (`fixed`, `not_fixed`, `status`) are not declared on `AgentReport`, so they cannot appear in the response.

If output validation fails (model returns malformed structured data after Pydantic-AI's internal retries), the adapter catches the exception, emits a contract-valid envelope with `error.message` populated and `output: None`, and exits non-zero.

## Capabilities (per `shared/task-spec.md`)

- File inspection — `list_dir`, `read_file`.
- File search — `glob`, `grep`.
- File editing — `write_file`, `edit_file`.
- Test execution — `run_shell` (e.g. the harness-provided `failing_test_command`).
- Diff inspection — `run_shell git diff`.

## Usage limits

`config.max_steps` is mapped to `UsageLimits(request_limit=max(2, 2 * max_steps))` and passed to `agent.run_sync`. Pydantic-AI's `request_limit` counts model requests rather than tool calls, so a small multiplier is used. If a run hits the limit, Pydantic-AI raises `UsageLimitExceeded`, which the adapter catches and renders as a contract-valid error envelope.

## Constraints honored by the agent

- The agent does not commit, reset, or otherwise mutate `.git/` in `input.repo_path` — diff derivation is the harness's responsibility.
- The agent does not run `pip install`, `uv sync`, `uv add`, or any command that would mutate the harness-owned case venv. Tests use the venv on `PATH` only.
- The agent respects `edit_constraints.disallowed_paths` (gitignore-style globs blocking edits to tests, fixtures, lockfiles, `.git/`, etc.). `disallowed_paths`, `allowed_paths`, and `max_changed_files` are also enforced inside `write_file`/`edit_file`: a violating call returns an `error:` string to the model rather than mutating the worktree.
