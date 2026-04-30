#!/bin/sh
set -eu
cd "$(dirname "$0")"

# The harness uses UV_PROJECT_ENVIRONMENT to point test commands at the
# case-owned venv. Don't let this adapter's own `uv run` consume that venv
# (uv would sync pydantic-ai dependencies into the harness-owned case venv
# and trip venv mutation checks). Preserve it under an adapter-private name
# so the adapter can reconstruct the test env for the agent's run_shell tool.
if [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then
    export AGENT_HARNESS_CASE_VENV="$UV_PROJECT_ENVIRONMENT"
    unset UV_PROJECT_ENVIRONMENT
fi

exec uv run --quiet python adapter.py "$@"
