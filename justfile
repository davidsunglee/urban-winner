_default:
    @just --list

# List framework dirs.
frameworks:
    cd evals && uv run python -m evals frameworks

# List discovered cases.
cases:
    cd evals && uv run python -m evals cases

# Build per-case bare repos and venvs; run framework setups. Idempotent.
eval-prepare *flags:
    cd evals && uv run python -m evals eval-prepare {{flags}}

# Create a new campaign (runs/<ts>/) and repoint runs/CURRENT.
eval-new *flags:
    cd evals && uv run python -m evals eval-new {{flags}}

# Fill missing cells in runs/CURRENT (auto-runs prepare + new if needed).
eval-all *flags:
    cd evals && uv run python -m evals eval-all {{flags}}

# Run/rerun a single cell.
eval framework case *flags:
    cd evals && uv run python -m evals eval {{framework}} {{case}} {{flags}}

# Print matrix of filled / missing / error per cell in CURRENT.
eval-status:
    cd evals && uv run python -m evals eval-status

# Regenerate runs/CURRENT/report.md.
eval-report:
    cd evals && uv run python -m evals eval-report

# Wipe .runs-cache/.
eval-clean-cache:
    cd evals && uv run python -m evals eval-clean-cache

# Wipe runs/.
eval-clean-runs:
    cd evals && uv run python -m evals eval-clean-runs
