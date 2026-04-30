**Reviewer:** openai-codex/gpt-5.5 via pi

## Strengths

- The adapter implements the subprocess JSON contract cleanly: one request from stdin, one JSON envelope to stdout, stderr-only logging, and non-zero error-envelope handling (`frameworks/pydantic-ai/adapter.py:92`, `frameworks/pydantic-ai/adapter.py:563`, `frameworks/pydantic-ai/adapter.py:655`).
- `AgentReport` matches the required six output fields and constrains `confidence` to `[0.0, 1.0]` without declaring forbidden authoritative fields (`frameworks/pydantic-ai/adapter.py:79`).
- Filesystem tools are rooted at `input.repo_path`, reject path escapes, and enforce edit constraints on `write_file`/`edit_file` (`frameworks/pydantic-ai/adapter.py:146`, `frameworks/pydantic-ai/adapter.py:212`, `frameworks/pydantic-ai/adapter.py:240`).
- `run.sh` correctly preserves the harness case venv for test execution while preventing adapter dependency syncs from mutating that venv (`frameworks/pydantic-ai/run.sh:10`).
- The shell tool reconstructs a narrow test environment, restores the case venv semantics, and avoids forwarding provider secrets into model-controlled commands (`frameworks/pydantic-ai/adapter.py:375`).
- Regression coverage exercises path containment, edit constraints, shell env reconstruction, trace conversion, and error-envelope emission (`frameworks/pydantic-ai/test_adapter.py`).

## Issues

No blocking issues found.

## Recommendations

- Consider adding a success-path unit test for `main()` with a fake Pydantic-AI agent result. Current tests cover most helpers and the error path, while the real success path is covered by the checked run artifact rather than a fast local unit test.

## Assessment

Ready to merge: Yes.

Verification reviewed:
- `cd frameworks/pydantic-ai && uv run --quiet python -m unittest -q` — 14 tests passed.
- Existing E2E cell `runs/2026-04-30T19-42-10/pydantic-ai/py-parse-duration-001/` shows `schema_validity: true`, visible test pass, hidden test pass, no venv mutation, and no edit-constraint violations.
