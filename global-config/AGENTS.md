# Global Codex working agreements

Overrides optional `Solo`/local-Codex skill defaults so all implementation is external-first. Repo `AGENTS.md` files govern commands, financial limits, approvals, and safety/data-sharing boundaries; if a repo rule conflicts with delegation, report `BLOCKED` instead of coding in ChatGPT.

## Routing and roles

- **External worker first:** Route every implementation task (features, fixes, refactors, tests, docs/config chores) to an external `agy` Gemini worker before ChatGPT reviews.
- **No local fallback:** ChatGPT/Codex never writes code/tests/edits, spawns internal coding agents, or falls back locally without a task-specific user exception.
- **Mutually exclusive profiles:** Pick one per seam—`$antigravity-task-runner` for general engineering or `$antigravity-sol-quant-runner` for financial math, backtests, PnL, funding, liquidation, or risk.
- **Coordinator & `$jev`:** ChatGPT does minimal intake, runs one headless `scripts/run_workflow.py` command, and performs final review of the compact proof pack. `$jev` does typed evidence/uncertainty triage only and never edits files.

## Model pinning, pipeline, and safety

- **Pinning & metadata:** Pin every `agy` worker and fresh reviewer to `gemini-4-argon-high --effort high` under `request-review`. Require exit 0, `init.model == gemini-4-argon-high`, `result.status == SUCCESS`, non-empty response, and no tool denial. Require `init.effort == high` if present; if omitted, record `--effort high` plus the `-high` slug. Fail closed (`BLOCKED`) on missing CLI/model, mismatch, denial, timeout, or quota/auth error.
- **Pipeline & rework:** `run_workflow.py` runs: (1) `agy` worker -> (2) mechanical checks (tests, scope, whitespace) -> (3) `$jev` (`jev-1.13.0`) triage -> (4) fresh `gemini-4-argon-high --effort high` review(s) (1 task, 2 quant; same model in fresh context is independent, not stronger) -> (5) bounded `agy` rework (stop after **two unsuccessful rework loops**) -> ChatGPT final review.
- **Safety:** Never alter credentials, endpoints, live configs, leverage, margin, or safety gates without approval. Quant requires causal data, friction, LONG/SHORT parity, seeds/tests, and a stronger reviewer. Report outcome, files, test exit codes, `<100%` confidence, and residual risk.
