# Release Notes - English Release

English-only release of the Antigravity CLI (`agy`), TypeSafe Jev (`$jev`), and Codex/ChatGPT skills bundle, unifying repository docs in English while preserving runtime behavior and safety.

## Skills
- `antigravity-task-runner`: Bounded-scope task workflow (`scripts/run_workflow.py`) and review gate.
- `antigravity-sol-quant-runner`: Quantitative strategy, data, causality, friction, and risk audit.
- `jev` (`jev-calibrated-decisions`): Typed triage (`Noul`, `Choice`, `Score`, opt-in `Box`/images) and `jev`/`jev-pool` CLIs.

## Model Pins
- `agy` worker & fresh reviews: `--model gemini-4-argon-high --effort high` (no fallback).
- `agy` worker conversation resume: `--conversation <id>` (fixes unsupported `--resume` CLI flag).
- Jev text triage & review gate: `jev-1.13.0` (`jev-1.14.0+` only with `--enable-images`).

## Verify & Install
```bash
python3 -B install.py
python3 -B install.py --install
```
The installer preserves existing real directories and does not activate `global-config/AGENTS.md` policy or the `jev-pool` key pool automatically.

## Links & Provenance
- README: https://github.com/AJackTi/antigravity-jev-skills/blob/main/README.md
- Provenance: Owner-confirmed original code for `jev.py` (https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f) and local orchestration (no root `LICENSE`), with separate TypeSafe AI MIT attribution (`licenses/typesafe-ai-skills-MIT.txt`, `THIRD_PARTY_NOTICES.md`).
