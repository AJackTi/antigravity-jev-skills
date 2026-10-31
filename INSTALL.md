# Installation & Operations Guide

## 1. Prerequisites & CLI Authentication

- **OS & Python:** macOS or Linux, **Python 3.10+**.
- **Antigravity CLI (`agy`):** Install and authenticate the official CLI separately: https://antigravity.google/docs/getting-started/
- **Verify exact model pin:** Confirm `agy models` exposes `gemini-4-argon-high` (no fallback models; workers and fresh reviewers hard-pin `--model gemini-4-argon-high --effort high` and retain normal permissions):
  ```bash
  agy --help
  agy models
  ```
- **TypeSafe AI:** Account and API key per https://docs.typesafe.ai.
- **Codex:** Reads global conventions at `~/.codex/AGENTS.md`.

## 2. Obtain Source & Verify Archive

Keep all three skill folders (`antigravity-task-runner`, `antigravity-sol-quant-runner`, `jev-calibrated-decisions`) adjacent in the same parent directory so `antigravity-task-runner` and `antigravity-sol-quant-runner` share `antigravity-task-runner/scripts/jev_review_gate.py`.

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

1. **Verify file hashes against `manifest.json`** (offline, no network or credentials; if covered files change, regenerate an authentic `manifest.json` rather than forging hashes):
   ```bash
   python3 -B install.py
   ```
2. **Register user-level symlinks** (`$antigravity-task-runner`, `$antigravity-sol-quant-runner`, `$jev` in `~/.agents/skills/`; `jev`, `jev-pool` in `~/.local/bin/`):
   ```bash
   python3 -B install.py --install
   ```
- **Symlink persistence:** Creates symlinks only; does not copy skills or auto-activate `global-config/AGENTS.md`. Keep the extracted/cloned folder in place.
- **Collision preservation:** Existing regular files, real directories, and unrelated config are preserved. `--replace-links` replaces user-owned symlinks only and never overwrites real directories or files.
- **Isolated trial:** On machines with an active global skills setup, do not overwrite active skills in place; test in an isolated root first:
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

- Manually review and merge `global-config/AGENTS.md` into `~/.codex/AGENTS.md`, preserving existing rules. Do not blindly overwrite `~/.codex/AGENTS.md` or modify keys/privileged settings.
- **Reopen your Codex chat** so it refreshes its cached skills inventory (`$antigravity-task-runner`, `$antigravity-sol-quant-runner`, `$jev`) and applies routing rules.
- `AGENTS.md` is an instructional convention, not runtime enforcement; repo or higher-priority instructions take precedence and stop at `BLOCKED` on conflict.

## 6. Configure External Key Pool (`jev-pool`)

- **Pool vs. standalone auth:** Standalone `jev` reads `~/.config/typesafe/api_key` (`0600`) or `TYPESAFE_API_KEY`, whereas `run_workflow.py` calls `jev-pool` directly. Running `jev auth set` alone or setting only `TYPESAFE_API_KEY` is **not sufficient** for workflows.
- **Directory & slots:** `jev-pool` requires a real owner-only directory `~/.config/typesafe/keys` (`0700`, symlinks refused) containing regular owner-only `*.key` files (`0600`). No keys, credentials, or logs are included in this bundle. Slots map in lexicographical filename order with no failover on rejection:
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
2. **Run the headless command** (`--output-dir` must be a **new** path outside write scope; consumes real Antigravity and TypeSafe API quota):
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
3. **Verify proof metadata:** In `/tmp/agy-smoke-out/workflow-result.json` and `/tmp/agy-smoke-out/proof-pack.txt`, confirm exit code `0`, `verdict: "PASS"`, model pin `gemini-4-argon-high` (`effort: "high"`), distinct worker/reviewer `conversation_id` values (same-model fresh sessions provide independence only, not a stronger model), and passing executed tests (config-only edits cannot achieve `PASS`).

## 8. Offline Tests, `BLOCKED` Conditions & Advanced Pointers

- **Offline regression tests** (from each skill directory; offline checks do not guarantee online model availability or live accuracy):
  ```bash
  # antigravity-task-runner (36 unit tests + 6 replay fixtures: 3 task, 3 quant)
  python3 -B -m unittest discover -s tests -v
  python3 -B scripts/replay_fixtures.py
  # jev-calibrated-decisions (48 unit tests)
  python3 -B -m unittest discover -s tests -v
  ```
- **Common `BLOCKED` conditions:** missing `agy` or `gemini-4-argon-high` pin; auth/quota errors; missing key slot or invalid key-pool permissions/symlinks (`0700`/`0600`); out-of-scope edits, path conflicts, or an existing `--output-dir`; diff `> 12000` chars; contract `> 8000` chars; or exceeding **2 unsuccessful rework rounds** after `REJECT`.
- **Advanced pointers:**
  - **Quant mode (`--kind quant`):** Requires `--quant-contract` (data provenance, lookahead-free causality, friction, `LONG`/`SHORT` parity, seeds, accounting reconciliation) and at least 2 fresh Gemini reviews; audit output is not live-trading approval. See `antigravity-sol-quant-runner/SKILL.md` and `antigravity-sol-quant-runner/references/quant-audit.md`.
  - **Images & `Box`:** Require per-request `--enable-images`, and `Box` uses an explicit `jev-1.14.0+` model; experimental with no inherited production-accuracy or trading approval. See `jev-calibrated-decisions/references/images.md`.

## Provenance & Notices

- **Original client & orchestration:** The repository owner confirms authorship and redistribution rights for `jev-calibrated-decisions/scripts/jev.py` (derived from https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f) and local orchestration code (owner statement dated 2026-10-31, recorded as stated and not an independent legal determination).
- **Upstream TypeSafe AI MIT license:** Official TypeSafe AI skill documentation is retained separately under MIT in [licenses/typesafe-ai-skills-MIT.txt](licenses/typesafe-ai-skills-MIT.txt) (applies only to upstream TypeSafe AI portions; no upstream endorsement is implied).
- **No root license grant:** No root `LICENSE` is granted for original repository code. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
