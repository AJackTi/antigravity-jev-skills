# Quant audit gates, search-aware inference, and audit pack format

Use this reference alongside `SKILL.md` when designing backtests, auditing trading logic, or assembling a Targeted Quant Audit Pack.

## Research hygiene and evaluation

- Freeze the hypothesis, dataset version, universe, split dates, costs, random seed, and acceptance criteria before parameter search. Keep a holdout that is never used for tuning.
- Maintain replayable fixtures for leakage, fill ordering, funding boundaries, liquidation, empty data, and both directions (`python3 ../antigravity-task-runner/scripts/replay_fixtures.py`).
- Separate discovery from confirmation: a promising result is a candidate until it survives a predeclared OOS run, conservative-cost stress, and fresh audit contexts. Never tune and certify in the same context.
- Log dataset/config/code versions, random seeds, trade counts, and exact commands. If results cannot be reproduced, the verdict is `BLOCKED`.

## Minimum audit gates

- **Causality:** Signal and execution use information available at decision time; define bar-close/intrabar semantics, test by removing or perturbing future rows, and inspect fills around gaps. Searching for `shift(-1)` alone is not a leakage proof.
- **Friction:** Include configured maker/taker fees, slippage, funding, spread, latency/fill assumptions, and liquidation behavior. State every assumption explicitly.
- **Generalization:** Report IS/validation/OOS, trade count, confidence interval or bootstrap/Monte Carlo method, parameter sensitivity, and drawdown. Never apply a universal Sharpe, degradation, correlation, or ruin cutoff without a project-specific rationale. For every metric, state the denominator and definition (annualization frequency, net vs gross, funding inclusion).
- **Baselines:** Compare against buy-and-hold, flat/no-trade, and a simple benchmark on identical timestamps and costs; report whether the edge survives.
- **Direction/risk parity:** Check LONG and SHORT, 1x and configured maximum leverage, empty/short samples, gaps, and partial/forced exits.
- **Stability:** Rerun with nearby parameters, alternate periods/instruments, and resampled trade order; flag results driven by a handful of trades.
- **Reproducibility:** Pin dataset/config/seed/version and include exact command plus exit code.

## Search-aware inference

- Record every candidate, parameter trial, feature variant, and failed run—not only the winner. "Best of N" performance is selection-biased even when labelled OOS.
- Keep the final holdout untouched until search is frozen. If inspected during tuning, relabel it validation and acquire a new holdout.
- Prefer walk-forward or rolling evaluation for time series; report overlap, embargo/gap rules, and regime coverage. Use selection-aware metrics or confidence intervals when the search space is large.

## Targeted Quant Audit Pack (`<=1,500` tokens)

Generate and validate with `python3 scripts/build_quant_audit_pack.py result.json --output audit-pack.txt`:

```text
TASK/HYPOTHESIS: <one sentence>
MATH DIFF: <only changed signal, sizing, risk, or ledger equations>
DATA: <source, periods, split, universe, seed>
FRICTION: <fees, slippage, funding, spread, latency assumptions>
RESULTS: <IS | validation | OOS: trades, return, Sharpe/Sortino, MDD, costs>
LEAKAGE TEST: <method and result>
PARITY: <LONG/SHORT, leverage, exit-path checks>
PROOF: <commands, exit codes, test/assertion counts>
VERDICT: PASS / REJECT / BLOCKED + calibrated confidence and residual risk
```

### Mandatory reviewer questions

1. Could the same result be produced with a one-bar delay or conservative fill?
2. Does the edge remain after every stated cost and a plausible worse-cost stress case?
3. Is the result concentrated in one symbol, regime, parameter corner, or a handful of trades?
4. Can the ledger reconcile positions, fees, funding, realized PnL, and equity on every exit path?
