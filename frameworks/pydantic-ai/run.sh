#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec uv run --quiet python adapter.py "$@"
