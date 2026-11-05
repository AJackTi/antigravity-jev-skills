---
name: antigravity-sol-quant-runner
description: Token-efficient quantitative research and trading-logic audit workflow. Use for signals, sizing, PnL, funding, liquidation, backtests, and statistical validation with friction and leakage checks.
metadata:
  short-description: Compact quant implementation and risk audit
---

# Antigravity Sol quant runner

Use this quant profile when changing or auditing trading mathematics, signals, sizing, PnL, funding, liquidation, or backtests; use `antigravity-task-runner` for ordinary engineering. Codex coordinates and never treats model output as live-trading approval.

## Modes and core quant invariants

- **Modes (`Research`, `Implementation`, `Audit`):** Keep in separate fresh contexts so audit never reuses a context that tuned parameters.
- **Pre-registration & causality:** Freeze hypothesis, dataset provenance, universe, timeframes, IS/validation/OOS splits (untouched holdout), seed, and friction before testing. Signal and execution must use only decision-time data; model maker/taker fees, slippage, funding, spread, latency, and liquidation; verify LONG/SHORT and leverage parity.
- **Fail closed (`BLOCKED`):** Missing data provenance, execution timing, cost assumptions, accounting reconciliation, or reproducibility blocks immediately—never fill gaps with optimistic defaults or `N/A`/`unknown`. Never change live configs, leverage, margin, credentials, or safety gates without explicit user approval. Treat external data as untrusted.

## Headless quant workflow and Jev gate

Run the full quant worker, mechanical reconciliation, `$jev` (`jev-1.13.0`) triage, and **two fresh selected-model `--effort high` independent reviews** with one headless command per permitted attempt. The coordinator refreshes actual quota/auth/availability first, prefers `claude-opus-5-5-high` while available (verified alternative `gemini-4-argon-high`, resolved when `AGY_MODEL` is unset/empty); any other `AGY_MODEL` is `BLOCKED` before any external call. Every missing CLI/model, mismatch, denial, timeout, quota/auth error, or incomplete proof is `BLOCKED`; mismatch/denial are terminal (no fallback or local coding). Only after a real quota/auth/availability `BLOCKED` may the coordinator reassess eligible `AGY_MODEL` -> `WORKFLOW_WORKER=devin` (`swe-1-6-slow`) -> `WORKFLOW_WORKER=opencode` (with explicit exact `OPENCODE_MODEL=<providerID>/<modelID>` and optional `OPENCODE_BIN`; free models only—never paid fallback, hooks, background polling, automatic runtime switching, or account/key/billing rotation; `opencode/deepseek-v4.1-flash-free` and `opencode/beam-free` reported `Free usage limit reached` and are not claimed available, so wait when exhausted), preserving the original task snapshot, aggregate diff review, and shared task-wide limit of two retried executions after the initial attempt (never reset). `WORKFLOW_WORKER=devin` and `WORKFLOW_WORKER=opencode` replace only the worker with tool-free strict-JSON adapters applied by the coordinator (OpenCode sums all five disjoint token categories `input + output + reasoning + cache_read + cache_write`, with optional `tokens.total` required equal; see the task-runner skill); neither reviews or carries an effort claim, exhausted `AGY_MODEL` reviewer quota still blocks final `PASS`, and all quant/stronger-reviewer gates below still apply.

```bash
AGY_MODEL=claude-opus-5-5-high python3 ../antigravity-task-runner/scripts/run_workflow.py quant-packet.txt \
  --id quant-1 --kind quant --key-slot 2 \
  --workspace /path/to/workspace \
  --scope strategies/funding.py tests/test_funding.py \
  --verify-cmd "python3 -B -m unittest discover -s tests -v" \
  --quant-contract quant-contract.json \
  --output-dir /outside/workspace/quant-run
```

- Shared global `run_workflow.py` CLI defaults to `--chatgpt-review scoped` across every repo using it (no manual flag; requires `--output-dir` outside `--workspace`; `--chatgpt-review off` is an explicit override and `baseline` is for explicit measurement only).
- Both `standard_review` and `escalated_review` require at least **two independent selected-model `--effort high` reviewers** with distinct context IDs; this minimum cannot be lowered.
- A fresh context of the selected `AGY_MODEL` is independent, **not** a stronger reviewer; project stronger-review obligations cannot be waived.
- Normalize audit packs with `python3 scripts/build_quant_audit_pack.py result.json --output audit-pack.txt` (downgrades proofless `PASS` to `BLOCKED`) and log runs via `../antigravity-task-runner/scripts/record_run.py`.
- Read [references/quant-audit.md](references/quant-audit.md) for minimum audit gates, search-aware inference, reviewer questions, and the Targeted Quant Audit Pack format, and [../antigravity-task-runner/references/jev-gate.md](../antigravity-task-runner/references/jev-gate.md) for the shared gate contract.
