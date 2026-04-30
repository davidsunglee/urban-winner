**Reviewer:** openai-codex/gpt-5.5 via pi

### Strengths
- The adapter implements the stdin/stdout contract cleanly: success and error paths emit the expected envelope shape, stdout is limited to the JSON response, and malformed-input smoke testing returns a contract-valid error envelope.
- The implementation chose and documented a concrete DeepAgents backend path, uses `LocalShellBackend(root_dir=..., virtual_mode=True)` to root filesystem tools, and adds regression tests for absolute-path and `..` traversal escapes.
- End-to-end evidence is strong: `runs/CURRENT/deepagents/py-parse-duration-001/` has `meta.status == "ok"`, `schema_validity == true`, visible and hidden tests passing, one changed file, and `venv_mutated == false`.
- Trace conversion handles multiple tool calls in one AI message by matching `ToolMessage.tool_call_id`, which is a useful robustness improvement over a simple last-step association.
- The harness regression suite passed locally (`cd evals && uv run pytest -q`: 218 passed), and the framework-local adapter tests passed (`cd frameworks/deepagents && uv run --quiet python -m unittest -q`: 4 passed).

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
- File: `frameworks/deepagents/adapter.py:138-166`
  - What's wrong: `_build_shell_env()` starts from `os.environ.copy()` and passes that full environment to `LocalShellBackend`, so the agent-controlled `execute` tool inherits `ANTHROPIC_API_KEY` and any other adapter-process secrets. I verified this by setting `ANTHROPIC_API_KEY=review-secret` and running `adapter._build_backend(...).execute('printf %s "$ANTHROPIC_API_KEY"')`, which returned the secret.
  - Why it matters: Shell commands are model-controlled and their output is captured in trace/run artifacts. A benign or compromised agent can accidentally or intentionally expose API credentials via `env`, `printenv`, shell expansion, or writes into the worktree.
  - How to fix: Build a minimal shell environment instead of copying the whole adapter environment. Preserve only what tests need (`HOME`/`LANG`/`TERM`, sanitized `PATH`, `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`) and explicitly remove provider/API-token variables before constructing `LocalShellBackend`. The model client can read `ANTHROPIC_API_KEY` from the adapter process environment without forwarding it to the shell tool.

#### Minor (Nice to Have)
- File: `frameworks/deepagents/test_adapter.py:1`
  - What's wrong: The new framework-local regression tests are not run by the plan's documented verification command (`cd evals && uv run pytest`) and are only mentioned in the file docstring.
  - Why it matters: These tests cover important adapter invariants, but future maintainers or CI jobs can easily miss them.
  - How to fix: Add a documented just/CI step for `cd frameworks/deepagents && uv run --quiet python -m unittest -q`, or include the command in `frameworks/deepagents/README.md` under a local testing section.

### Recommendations
- Sanitize the shell-tool environment before merge; this is the main production-readiness gap despite the adapter meeting the functional benchmark.
- Keep the current end-to-end artifact checks as release evidence for this adapter, since they validate contract shape, worktree mutation, test outcomes, and trace quality together.

### Assessment

**Ready to merge: With fixes**

**Reasoning:** Functionally, the adapter satisfies the plan and the target DeepAgents cell passes end-to-end. The remaining blocker is reducing the shell environment so model-controlled commands cannot expose provider credentials in persisted artifacts.
