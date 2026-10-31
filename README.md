# Antigravity + TypeSafe Jev Skills Bundle (Installation & Operations Guide)

This source bundle combines **Antigravity CLI (`agy`)**, **TypeSafe Jev (`$jev`)**, and **Codex/ChatGPT** across 3 skills:
- `antigravity-task-runner`: Orchestrates software tasks with a bounded write scope.
- `antigravity-sol-quant-runner`: Audits quantitative strategies, data, friction costs, and risk.
- `jev` (directory `jev-calibrated-decisions`): Performs typed evidence triage (typed triage) and independent judgment.

## Architecture & Operating Principles
The `scripts/run_workflow.py` workflow runs 5 sequential steps:
1. **Gemini implement/fix (`agy` worker)**: Implements or fixes code within the permitted scope.
2. **Mechanical checks**: Runs test commands and checks file scope (`scope`) and whitespace (`whitespace`).
3. **Jev typed triage (`$jev`)**: Uses the `jev-1.13.0` text model to classify evidence completeness and consistency.
4. **Fresh Gemini review(s)**: A fresh independent `agy` session (distinct `conversation_id` from the worker) critiques the diff and test results.
5. **ChatGPT final review**: Under the global external-first policy ([global-config/AGENTS.md](global-config/AGENTS.md)), ChatGPT only orchestrates and conducts final acceptance of `proof-pack.txt`, and **does not write code/tests itself or fall back locally** unless the user grants an explicit per-task exception.

**Model & review constraints:**
- Every `agy` session hard-pins `--model gemini-4-argon-high --effort high` (no fallback models).
- A `task` workflow requires **1** fresh Gemini review session; `quant` requires at least **2** independent fresh Gemini review sessions (up to **2 rework rounds** upon `REJECT`).
- A fresh session of the same model only guarantees **independence**, not a stronger model; obligations requiring a `stronger-reviewer` cannot be spoofed using the same `gemini-4-argon-high` model.

## Step-by-Step Installation Guide

### 1. Prerequisites
- **macOS** or **Linux**, **Python 3.10+**.
- Official **Antigravity CLI (`agy`)** installed and authenticated per the documentation: https://antigravity.google/docs/getting-started/
- Verify the CLI and confirm the exact `gemini-4-argon-high` model is available (fallback to other models is not supported):
  ```bash
  agy --help
  agy models
  ```
- **TypeSafe AI** account and API key (https://docs.typesafe.ai).
- **Codex** reading global conventions at `~/.codex/AGENTS.md`.

### 2. Download Source & Directory Structure
- **Clone from GitHub** into the root directory containing `install.py`:
  ```bash
  git clone <YOUR_REPO_URL> antigravity-jev-skills
  cd antigravity-jev-skills
  ```
- **Or from a `.zip` archive:** Extract the archive (root directory `antigravity-jev-skills`) and `cd antigravity-jev-skills`.
- **Directory structure** (must keep the 3 skill directories side by side in the same parent directory):
  - `antigravity-task-runner/`: `scripts/run_workflow.py` and the shared review gate `scripts/jev_review_gate.py`.
  - `antigravity-sol-quant-runner/`: Quantitative audit skill ([SKILL.md](antigravity-sol-quant-runner/SKILL.md)).
  - `jev-calibrated-decisions/`: Backward-compatible directory name, registered as the `jev` skill and the `jev`, `jev-pool` CLIs.
  - `global-config/AGENTS.md`, `licenses/typesafe-ai-skills-MIT.txt`, [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [INSTALL.md](INSTALL.md), `manifest.json`, `install.py`.

### 3. Integrity Verification (Offline)
- If you downloaded a `.zip` archive with a `.sha256` file, verify the archive hash first:
  ```bash
  shasum -a 256 -c *.zip.sha256
  # Example: shasum -a 256 -c antigravity-jev-skills-20261031.zip.sha256
  ```
- From the root directory, verify all files against `manifest.json`:
  ```bash
  python3 -B install.py
  ```
  *Note:* If any code or documentation covered by the manifest is modified, the packager must regenerate an authentic `manifest.json` — never forge hashes to bypass verification.

### 4. Register Skill & CLI Symlinks
- Create the 3 skill symlinks in `~/.agents/skills/` and 2 launchers (`jev`, `jev-pool`) in `~/.local/bin/`:
  ```bash
  python3 -B install.py --install
  ```
- **Safety principles:**
  - The command only creates symlinks; it **does not copy skills** and **does not auto-activate** `global-config/AGENTS.md`. Keep the cloned directory in its current location.
  - Existing real directories are always preserved; `--replace-links` only replaces user-owned symlinks and never overwrites real directories.
  - **Machines with an active global skills setup:** Do not overwrite or forcibly delete active skills in place. Test in isolation using `--target-root` and migrate deliberately after verification:
    ```bash
    python3 -B install.py --install --target-root /tmp/agy-jev-trial
    ```

### 5. Configure `PATH` & Offline CLI Check
- Add `~/.local/bin` to `PATH` in the current shell (and add it once to `~/.zshrc` / `~/.bashrc` to persist across sessions):
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  jev --help
  jev-pool --help
  ```

### 6. Merge Global Policy (`~/.codex/AGENTS.md`)
- Review and manually merge the contents of [global-config/AGENTS.md](global-config/AGENTS.md) into `~/.codex/AGENTS.md` (preserving existing rules; the installer does not modify this file automatically). Do not blindly overwrite it, and do not modify keys or privileged settings.
- Start a new Codex chat session so it discovers the skills and routes normal natural-language requests. `AGENTS.md` is an instructional convention, not a runtime enforcement mechanism; repo-level rules or higher-priority instructions always take precedence and stop at `BLOCKED` if a conflict occurs.

### 7. Configure Key Pool for `jev-pool`
- The workflow invokes `jev-pool` directly, so running `jev auth set` alone or only setting the `TYPESAFE_API_KEY` environment variable is **not sufficient**.
- `jev-pool` requires a real directory at `~/.config/typesafe/keys` (permissions `0700`, symlinks refused) containing regular `*.key` files owned by you (permissions `0600`). Slots are selected in lexicographical filename order (`task` defaults to slot `1`, `quant` defaults to slot `2`; there is no automatic failover when a key is rejected).
- Safely create `key-01.key` **outside the repo** using Python `getpass` with the `O_EXCL` flag (preserving existing configuration, never overwriting an existing key, and never exposing the key via CLI arguments or shell history):
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
- You may optionally create `key-02.key` and `key-03.key`, but **multiple keys are not required**: if you only have 1 key, pass `--key-slot 1` when running `quant`.

### 8. Run Minimal Verification Task (Smoke Task)
- Create an isolated empty workspace and a `packet.txt` concisely describing the write scope and verification command:
  ```bash
  mkdir -p /tmp/agy-smoke-ws/tests
  cat << 'EOF' > /tmp/agy-smoke-packet.txt
  TASK: Create greet.py with hello(name) returning "Hi, <name>!" and unit test in tests/test_greet.py.
  WRITE SCOPE: greet.py, tests/test_greet.py.
  VERIFY: authorized shell command is exactly: python3 -B -m unittest discover -s tests -v
  EOF
  ```
- Run the single headless command (`--output-dir` must be a **new** directory outside the write scope; this command consumes real API quota and ChatGPT acts only as the orchestrator):
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
- Inspect `/tmp/agy-smoke-out/workflow-result.json` and `/tmp/agy-smoke-out/proof-pack.txt`: confirm exit code `0`, `verdict: "PASS"`, model metadata `gemini-4-argon-high` (`effort: "high"`), and passing executed tests (configuration-only changes will not achieve `PASS`).

### 9. Quantitative Mode (`quant`) & Advanced Documentation
- The `--kind quant` mode requires `--quant-contract` declaring full data provenance (`data_provenance`), lookahead-free causality (`causality`), friction costs (`friction`), `LONG`/`SHORT` symmetry (`parity`), reproducibility seeds, and accounting reconciliation, accompanied by **2** fresh Gemini review sessions; the audit result **is not live-trading approval**. See details in [antigravity-sol-quant-runner/SKILL.md](antigravity-sol-quant-runner/SKILL.md) and [references/quant-audit.md](antigravity-sol-quant-runner/references/quant-audit.md).
- Optional image and `Box` features (`jev-1.14.0+`, enabled via `--enable-images`) are documented in [jev-calibrated-decisions/references/images.md](jev-calibrated-decisions/references/images.md).

### 10. Offline Verification, `BLOCKED` Conditions & Token Counters
- Run offline checks from each skill directory:
  ```bash
  # In antigravity-task-runner/ (36 unit tests + 6 replay cases: 3 task, 3 quant)
  python3 -B -m unittest discover -s tests -v
  python3 -B scripts/replay_fixtures.py
  # In jev-calibrated-decisions/ (48 unit tests)
  python3 -B -m unittest discover -s tests -v
  ```
  *Note:* Offline checks do not guarantee online model availability or accuracy on new live data.
- **Common `BLOCKED` reasons:** missing `agy` or the `gemini-4-argon-high` model; authentication/quota errors; invalid key directory/file permissions (`0700`/`0600`) or missing key slot; path conflicts or an already-existing `--output-dir`; diff `> 12000` characters; contract `> 8000` characters; or exceeding **2 unsuccessful rework rounds**.
- **Usage statistics:** Input/output/cached token counts are technical telemetry only, not billing invoices, and do not guarantee general savings across all tasks.

### 11. Publishing to GitHub & Copyright Notice
- Publish the **contents inside** the `antigravity-jev-skills` directory as the repository root so GitHub renders `README.md` (via the GitHub web UI or Git CLI with `<YOUR_REPO_URL>` and a neutral branch such as `main`; inspect the file list with `git status --short` before `git add`):
  ```bash
  git init -b main
  git status --short
  git add .
  git commit -m "Initial source bundle"
  git remote add origin <YOUR_REPO_URL>
  git push -u origin main
  ```
- Do not commit `*.key` files, sensitive configuration, log/output directories (`*.log`, `*.jsonl`, `workflow-result.json`, `proof-pack.txt`), or `$HOME` directories (check `git status --short --ignored` and `.gitignore` before committing or pushing).
- **Copyright & Redistribution Rights ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)):** Official TypeSafe AI skill documentation is licensed under the MIT License ([licenses/typesafe-ai-skills-MIT.txt](licenses/typesafe-ai-skills-MIT.txt), applying only to upstream TypeSafe AI content). For the `jev-calibrated-decisions/scripts/jev.py` client (derived from Gist https://gist.github.com/AJackTi/6f70237e29767100936e213a22f4340f) and local orchestration code, the repository owner confirms ownership of the original client from the Gist and the right to redistribute their code (user statement dated 2026-10-31; recorded as stated, not an independent legal determination). Do not select a root `LICENSE` or grant a license on the owner's behalf (having no root `LICENSE` means no general reuse license is declared for original code portions), do not apply the MIT license to the entire repository, and do not imply upstream endorsement.
