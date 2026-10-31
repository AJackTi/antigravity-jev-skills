# Three-skill bundle

Requires Python 3.10+ on macOS/Linux. Extract all three folders together; keep
them adjacent so the quant and task skills share the same review adapter.

Verify file hashes (offline, no credentials or network):

```bash
python3 install.py
```

Register user-level skill/CLI symlinks when ready:

```bash
python3 install.py --install
```

The installer preserves existing regular files/directories and unrelated
configuration. `--replace-links` permits replacing owned symlinks only. Keep
the extracted folder in place. Use `--target-root PATH` for an isolated trial.
Add its `.local/bin` directory to PATH when needed.

Install/authenticate the official Antigravity CLI separately:
https://antigravity.google/docs/getting-started/
Verify `agy models` exposes `gemini-4-argon-high`. Workers/reviewers pin both
`--model gemini-4-argon-high --effort high` and retain normal permissions.

Keys are deliberately external. Configure your TypeSafe key on the destination
machine through a hidden prompt/stdin; never paste it into scripts or commands.
The default file is `~/.config/typesafe/api_key` (0600). Optional pool files are
`~/.config/typesafe/keys/key-01.key` etc. (0600) inside a 0700 directory. No keys,
credentials, user account state or private run logs are included in this ZIP.

Skill calls: `$antigravity-task-runner`, `$antigravity-sol-quant-runner`, `$jev`.
If the app has cached its skills inventory, reopen the chat after installation.

Offline regression checks from each extracted skill directory:

```bash
# antigravity-task-runner
python3 -B -m unittest discover -s tests -v
python3 -B scripts/replay_fixtures.py
# jev-calibrated-decisions
python3 -B -m unittest discover -s tests -v
```

Images/Box: follow `jev-calibrated-decisions/references/images.md`. They require
per-request `--enable-images`; Box uses an explicit 1.14+ model. They are
experimental, with no inherited production-accuracy or trading approval.
