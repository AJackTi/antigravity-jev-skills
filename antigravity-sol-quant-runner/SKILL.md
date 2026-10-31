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

Run the full quant worker, mechanical reconciliation, `$jev` (`jev-1.13.0`) triage, and **two fresh `gemini-4-argon-high --effort high` independent reviews** with one command:

```bash
python3 ../antigravity-task-runner/scripts/run_workflow.py quant-packet.txt \
  --id quant-1 --kind quant --key-slot 2 \
  --workspace /path/to/workspace \
  --scope strategies/funding.py tests/test_funding.py \
  --verify-cmd "python3 -B -m unittest discover -s tests -v" \
  --quant-contract quant-contract.json \
  --output-dir .artifacts/quant-run
```

- Both `standard_review` and `escalated_review` require at least **two independent `gemini-4-argon-high --effort high` reviewers** with distinct context IDs; this minimum cannot be lowered.
- A fresh context of `gemini-4-argon-high` is independent, **not** a stronger reviewer; project stronger-review obligations cannot be waived.
- Normalize audit packs with `python3 scripts/build_quant_audit_pack.py result.json --output audit-pack.txt` (downgrades proofless `PASS` to `BLOCKED`) and log runs via `../antigravity-task-runner/scripts/record_run.py`.
- Read [references/quant-audit.md](references/quant-audit.md) for minimum audit gates, search-aware inference, reviewer questions, and the Targeted Quant Audit Pack format, and [../antigravity-task-runner/references/jev-gate.md](../antigravity-task-runner/references/jev-gate.md) for the shared gate contract.
