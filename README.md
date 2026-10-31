# Antigravity + Jev Skills

Gemini implements, tests, and fixes code, Jev triages evidence, a fresh Gemini session reviews independently, and ChatGPT coordinates the workflow and performs the final review.

## Included Skills

- `antigravity-task-runner`: Engineering implementation, testing, and verification workflows.
- `antigravity-sol-quant-runner`: Quant strategy, data, friction, and risk auditing.
- `jev`: Typed decisions and calibrated evidence triage.

## Requirements

Requires macOS/Linux with Python 3.10+, an authenticated `agy` CLI exposing `gemini-4-argon-high --effort high`, and a TypeSafe API key for text triage with `jev-1.13.0`.

## Quick Install

```bash
git clone https://github.com/AJackTi/antigravity-jev-skills.git
cd antigravity-jev-skills
python3 -B install.py
python3 -B install.py --install
```

The installer registers symlinks only and preserves existing directories. Installation does not auto-authenticate CLIs or enforce policies across all apps. Key-pool configuration and global Codex-policy setup are covered in the [INSTALL.md](INSTALL.md) installation guide. Release archives are available under [Downloads](https://github.com/AJackTi/antigravity-jev-skills/releases/latest).
