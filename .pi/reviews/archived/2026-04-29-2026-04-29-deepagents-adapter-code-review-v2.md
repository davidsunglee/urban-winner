**Reviewer:** openai-codex/gpt-5.5 via pi

### Strengths
- `frameworks/deepagents/adapter.py:194-212` wires `LocalShellBackend(root_dir=..., virtual_mode=True, env=...)` into `create_deep_agent`, which satisfies the plan's rooted filesystem and repo-cwd shell requirements while adding useful secret isolation for model-controlled shell commands.
- `frameworks/deepagents/adapter.py:346-427` handles the full success/failure envelope lifecycle: request parsing, `config.max_steps` recursion limit, SIGALRM soft deadline from `config.timeout_s`, success envelope, and contract-valid error envelope with stderr traceback.
- `frameworks/deepagents/adapter.py:242-312` builds a contract-valid trace and correctly associates multiple `ToolMessage` results with their originating tool calls by id, avoiding misleading trace output.
- `frameworks/deepagents/run.sh:5-13` protects the harness-owned case venv from adapter `uv run` while preserving it for test commands, directly addressing the venv-mutation requirement.
- `frameworks/deepagents/test_adapter.py:23-284` adds focused regression coverage for filesystem rooting, trace association, and shell secret isolation; the existing harness suite also passes.
- End-to-end artifacts under `runs/CURRENT/deepagents/py-parse-duration-001/` show `meta.status == "ok"`, `venv_mutated == false`, `scoring.schema_validity == true`, and visible/hidden tests passing.

### Issues

#### Critical (Must Fix)
None found.

#### Important (Should Fix)
None found.

#### Minor (Nice to Have)
None found.

### Recommendations
- Keep the new framework-local unittest command documented in developer notes or CI if this adapter becomes part of regular verification; it covers adapter-specific behavior that the generic harness tests do not exercise.

### Assessment

**Ready to merge: Yes**

**Reasoning:** The implementation satisfies the authoritative plan and shared contract, includes appropriate rooting/env safeguards, and was verified with framework-local tests, manifest validation, the full harness unit suite, and successful end-to-end `deepagents/py-parse-duration-001` artifacts.
