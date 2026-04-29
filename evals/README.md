# evals

Framework-agnostic eval harness. Discovers framework manifests in `../frameworks/<name>/manifest.json`, invokes them through the contract in `../shared/contract.md`, and scores them against `../shared/task-spec.md`.

## Run

Top-level orchestration is in the repo-root `justfile`:

```sh
just eval-prepare          # build per-case caches and run framework setups
just eval-new              # create a fresh campaign
just eval-all              # run every (framework, case) cell in the current campaign
just eval <fw> <case>      # run/rerun one cell
just eval-status           # matrix of done/missing/error
just eval-report           # regenerate runs/CURRENT/report.md
```

Override flags `--model <id>`, `--timeout-s <n>`, `--max-steps <n>` are accepted on `eval-new` (campaign-level) and `eval` (cell-level). `eval-all` rejects them inside an existing campaign — start a new campaign with `eval-new --model X` instead.

## Layout

- `evals/__main__.py` — CLI entry (`python -m evals <verb>`)
- `evals/cli.py` — subcommand dispatch
- `evals/discovery.py` — find frameworks and cases
- `evals/workspace.py` — bare git, venv, per-cell worktree (layers 1, 2, 3)
- `evals/setup.py` — framework setup runner (`.ok` / `.fail` sentinels)
- `evals/runner.py` — one cell: build request, spawn, capture, classify
- `evals/pipeline.py` — temp-index diff, test reruns, edit constraint, scoring, atomic meta sentinel
- `evals/campaign.py` — campaign creation, CURRENT pointer, lockfile
- `evals/status.py` — matrix renderer
- `evals/report.py` — markdown report
- `evals/env.py` — `.env` loading, agent_env / test_env / setup_env
- `evals/schemas.py` — bespoke validators for framework / case / envelope / agent output

Tests live in `tests/`; integration tests are gated by `pytest -m integration`.
