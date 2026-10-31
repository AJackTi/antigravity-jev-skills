# Installation & Operations Guide

## 1. Prerequisites & CLI Authentication

- **OS & Python:** macOS or Linux, **Python 3.10+**.
- **Antigravity CLI (`agy`):** Install and authenticate the official CLI separately: https://antigravity.google/docs/getting-started/
- **Verify exact model pin:** Confirm `agy models` exposes `gemini-4-argon-high` (workers and fresh reviewers hard-pin `--model gemini-4-argon-high --effort high` with no fallback):
  ```bash
  agy --help
  agy models
  ```
- **TypeSafe AI:** Account and API key per https://docs.typesafe.ai.
- **Codex:** Reads global conventions at `~/.codex/AGENTS.md`.

## 2. Obtain Source & Verify Archive

- **Clone repository:**
  ```bash
  git clone https://github.com/AJackTi/antigravity-jev-skills.git antigravity-jev-skills
  cd antigravity-jev-skills
  ```
- **Or download ZIP + `.sha256` from the latest Release (`https://github.com/AJackTi/antigravity-jev-skills/releases/latest`), verify SHA-256, and unzip:**
  ```bash
  shasum -a 256 -c antigravity-jev-skills-*.zip.sha256
  unzip antigravity-jev-skills-*.zip
  cd antigravity-jev-skills
  ```

## 3. Verify Manifest & Register Symlinks

1. **Verify file hashes against `manifest.json`** (offline):
   ```bash
   python3 -B install.py
   ```
2. **Register user-level symlinks** (skills in `~/.agents/skills/`; `jev` and `jev-pool` in `~/.local/bin/`):
   ```bash
   python3 -B install.py --install
   ```
- **Symlink persistence:** Creates symlinks only; does not copy skills or auto-activate `global-config/AGENTS.md`. Keep the extracted/cloned folder in place.
- **Collision preservation:** Existing regular files, real directories, and unrelated config are preserved. `--replace-links` replaces user-owned symlinks only and never overwrites real directories or files.
- **Isolated trial:** Test in an isolated root before modifying an active global skills setup:
  ```bash
  python3 -B install.py --install --target-root /tmp/agy-jev-trial
  ```

## 4. Configure `PATH`

Add `~/.local/bin` (or `<target-root>/.local/bin`) to `PATH` in `~/.zshrc` or `~/.bashrc` and your current shell, then check the CLIs offline:

```bash
export PATH="$HOME/.local/bin:$PATH"
jev --help
jev-pool --help
```

## 5. Review & Merge `global-config/AGENTS.md`

- Manually review and merge `global-config/AGENTS.md` into `~/.codex/AGENTS.md`, preserving existing rules. Do not blindly overwrite `~/.codex/AGENTS.md`.
- **Reopen your Codex chat** to refresh cached skills and apply routing rules.
- `AGENTS.md` is an instructional convention, not runtime enforcement; repo or higher-priority instructions take precedence and stop at `BLOCKED` on conflict.

## 6. Configure External Key Pool (`jev-pool`)

- **Pool vs. standalone auth:** Standalone `jev` reads `~/.config/typesafe/api_key` (`0600`) or `TYPESAFE_API_KEY`, whereas `run_workflow.py` calls `jev-pool` directly. Running `jev auth set` alone or setting only `TYPESAFE_API_KEY` is **not sufficient** for workflows.
- **Directory & slots:** `jev-pool` requires a real owner-only directory `~/.config/typesafe/keys` (`0700`, symlinks refused) containing regular owner-only `*.key` files (`0600`). Slots map in lexicographical filename order with no failover on rejection:
  - `task` defaults to **slot `1`** (`key-01.key`).
  - `quant` defaults to **slot `2`** (`key-02.key`). With one key, pass `--key-slot 1` for `quant`.
- **Create `key-01.key` outside the repo** via `getpass` (real-path symlink check, owner-only `0700`/`0600`, `O_EXCL | O_NOFOLLOW`, no existing-key overwrite, no keys in `argv`/history):
  ```bash
  python3 -c '
  import getpass, os
  from pathlib import Path
  d = Path.home() / ".config/typesafe/keys"
  d.parent.mkdir(parents=True, exist_ok=True)
  if d.is_symlink(): raise SystemExit("Refusing symlink directory")
  d.mkdir(mode=0o700, exist_ok=True)
  os.chmod(d, 0o700)
  k = getpass.getpass("TypeSafe API key (slot 1): ").strip()
  assert k and k.isascii(), "Key must be non-empty ASCII"
  fd = os.open(d / "key-01.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
  with os.fdopen(fd, "w", encoding="ascii") as f: f.write(k + "\n")
  '
  ```

## 7. Run Minimal Live Smoke Task

1. **Create an isolated workspace and packet** with the exact scope and test command:
   ```bash
   mkdir -p /tmp/agy-smoke-ws/tests
   cat << 'EOF' > /tmp/agy-smoke-packet.txt
   TASK: Create greet.py with hello(name) returning "Hi, <name>!" and unit test in tests/test_greet.py.
   WRITE SCOPE: greet.py, tests/test_greet.py.
   VERIFY: authorized shell command is exactly: python3 -B -m unittest discover -s tests -v
   EOF
   ```
2. **Run the headless command** (`--output-dir` must be a **new** path outside write scope):
   ```bash
   python3 -B ~/.agents/skills/antigravity-task-runner/scripts/run_workflow.py \
     --packet-file /tmp/agy-smoke-packet.txt \
     --workspace /tmp/agy-smoke-ws \
     --id smoke-1 \
     --scope greet.py tests/test_greet.py \
     --verify-cmd "python3 -B -m unittest discover -s tests -v" \
     --key-slot 1 \
     --output-dir /tmp/agy-smoke-out
   ```
3. **Verify output:** In `/tmp/agy-smoke-out/workflow-result.json` and `/tmp/agy-smoke-out/proof-pack.txt`, confirm exit code `0`, `verdict: "PASS"`, model pin `gemini-4-argon-high` (`effort: "high"`), and passing tests.

## 8. Troubleshooting

If a run fails or stops with `BLOCKED`, read the reported error or `proof-pack.txt` and check CLI authentication and model availability, the requested key slot and permissions, write scope, and a fresh output directory.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for provenance and third-party notices.
