# Cases

A case is the unit of work the harness loads to evaluate an agent framework. Each case bundles a fixture repository (under `fixtures/<case_id>/`), a manifest in this directory (`<case_id>.json`), and a captured failure-output sidecar (`<case_id>.failure_output.txt`). The contract that defines the manifest schema and harness behavior is `../shared/task-spec.md`.

## How to add a case

1. **Read the contract.** `../shared/task-spec.md` is the source of truth for case-manifest fields, default `edit_constraints`, and what the harness derives vs. what the case author supplies.
2. **Pick a path.**
   - **Synthetic / hand-rolled cases** (e.g. `py-parse-duration-001`) follow the design pattern in `../.pi/specs/2026-04-28-bootstrap-fixture-design.md`. Use these to exercise specific harness paths or to keep an always-fast smoke test in the suite.
   - **SWE-bench Verified cases** follow the conversion pattern in `../.pi/specs/2026-04-28-swebench-fixture-conversion.md` and the worked-example plan in `../.pi/plans/done/2026-04-28-swebench-fixture-conversion.md`. Use these for realistic benchmark cases drawn from public OSS bug reports.
3. **Bootstrap the fixture and write the manifest** per the chosen design. Validate with the same end-to-end checks the existing cases pass: visible test fails as captured (structurally), gold patch makes it pass, hidden command catches at least one plausible under-fix variant where possible (regression-coverage-only is acceptable for cases where no plausible under-fix discriminates against existing tests, documented in `notes`).
4. **Commit one fixture per commit** so reverting a single case is clean. Match the existing commit-message convention: `Fixture: <case_id> — <bug summary>`.

Provenance (SWE-bench instance ID, base_commit SHA, upstream URL, hidden-subset rationale, under-fix patterns, any uv overlays or environmental modifications) lives in the manifest's `notes` field, which is never surfaced to the agent.
