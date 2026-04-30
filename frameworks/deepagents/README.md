# deepagents

DeepAgents (Python, on top of LangGraph) — adapter for the agent shootout's software-bugfix benchmark.

## Model

- Default: `claude-sonnet-4-6` (Anthropic).
- Anthropic-only for v1. Cross-provider overrides (OpenAI, Bedrock, etc.) are deferred — see `.pi/specs/2026-04-29-deepagents-adapter.md`.
- The harness may pass another Anthropic model via `--model claude-...`; the adapter forwards it as `init_chat_model("anthropic:<model>")` without further validation.

## Setup

- Manifest setup command: `uv sync --frozen` (run once by the harness during `just eval-prepare`).
- Dependencies: `deepagents`, `langchain`, `langchain-anthropic`. Locked in `uv.lock`.
- Python: `>= 3.11` (this directory pins `3.12` via `.python-version`).

## Environment variables

- `ANTHROPIC_API_KEY` — required. Forwarded by the harness from the calling shell or repo-root `.env`.

## Tool wiring

- **Filesystem and shell**: see the comment at the top of `adapter.py` for which deepagents backend was chosen at implementation time.
  - **Approach A** (default): `LocalShellBackend(root_dir=input.repo_path, virtual_mode=True, env=...)`. The agent gets DeepAgents' canonical tool set: `ls`, `glob`, `grep`, `read_file`, `write_file`, `edit_file`, `execute`. With `virtual_mode=True`, the filesystem tools treat paths as virtual paths under the repo root: absolute-looking paths (`/parse_duration/parser.py`) and relative paths (`parse_duration/parser.py`) both resolve under `input.repo_path`, and `..`/`~` traversal is rejected. Host absolute paths like `/Users/...` no longer escape the worktree. The `execute` shell tool still has `cwd=input.repo_path`; deepagents' `virtual_mode` does not restrict shell-spawned processes by design, so the agent prompt forbids commands that mutate `.git/` or the harness-owned case venv.
  - **Approach B** (contingency): `FilesystemBackend(root_dir=input.repo_path)` plus a custom `@tool def shell(command)` that calls `subprocess.run(..., cwd=input.repo_path, env=os.environ.copy())` with a per-call timeout derived from the adapter's remaining `config.timeout_s` deadline. Used when `LocalShellBackend` does not expose a usable working-directory knob, does not forward it to subprocess, or its filesystem tools are not rooted by it.
- **Soft deadline**: the adapter derives a soft deadline from `config.timeout_s` (`max(5.0, timeout_s - 5.0)` seconds) and arms a `SIGALRM`-based timer around `agent.invoke`. On timeout the adapter raises an internal exception, catches it, and emits a contract-valid envelope with `error.message` set — *before* the harness's outer hard-kill fires. Approach B's shell tool also caps `subprocess.run` timeout to the remaining deadline.
- **Structured report**: a callable tool `submit_report` whose Pydantic schema mirrors `shared/task-spec.md`'s `output` block (`root_cause`, `summary`, `changed_files`, `tests_run[]`, `evidence`, `confidence`). The agent must call it once at the end of the run; if it calls more than once, last-call-wins. If it never calls it, the adapter emits an envelope with `error.message = "agent did not call submit_report"` and exits non-zero.

## Capabilities (per `shared/task-spec.md`)

- File inspection — `ls`, `read_file`.
- File search — `glob`, `grep`.
- File editing — `write_file`, `edit_file`.
- Test execution — `execute` (Approach A) or `shell` (Approach B).
- Diff inspection — `git diff` via the same shell/execute tool.

## LocalShellBackend cwd & env contingency

`LocalShellBackend` does not inherit the parent process env unless configured. The adapter passes an explicit shell env built from `os.environ.copy()`, with `<case-venv>/bin` forced to the front of `PATH`, `UV_PROJECT_ENVIRONMENT=<case-venv>`, `UV_NO_SYNC=1`, `PYTHONPATH=<repo>/src:<repo>`, and `PYTHONDONTWRITEBYTECODE=1`. This keeps agent-side `pytest`/`git` invocations on the harness-prepared case environment while preventing `uv run` from syncing into that shared venv or Python from adding `__pycache__` files to the submitted diff.

`run.sh` also protects the case venv from the adapter runtime itself: it preserves the harness-provided `UV_PROJECT_ENVIRONMENT` as `AGENT_HARNESS_CASE_VENV`, unsets `UV_PROJECT_ENVIRONMENT`, then starts the adapter with this directory's own frozen `uv` environment.

## Constraints honored by the agent

- The agent does not commit, reset, or otherwise mutate `.git/` in `input.repo_path` — diff derivation is the harness's responsibility.
- The agent does not run `pip install`, `uv sync`, `uv add`, or any command that would mutate the harness-owned case venv. Tests use the venv on `PATH` with `UV_NO_SYNC=1` and the worktree exposed through `PYTHONPATH`.
- The agent respects `edit_constraints.disallowed_paths` (gitignore-style globs blocking edits to tests, fixtures, lockfiles, `.git/`, etc.).
