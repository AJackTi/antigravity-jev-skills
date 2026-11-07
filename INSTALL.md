# Installation

## 1. Dependencies

Use macOS/Linux and Python 3.10+. Install/authenticate `agy` and Codex CLI separately. AGY must expose `gemini-4-argon-high` or `claude-opus-5-5-high` with High effort. Default scoped final review uses existing ChatGPT authentication and `gpt-6.1-sol`. Its catalog requires Superpowers 7.0.1; this bundle does not install that plugin or the engineering guides referenced by the global policy.

Check native authentication/availability, including `agy --print /usage`; availability is not a future quota guarantee.

## 2. Verify and register

For a downloaded bundle:

```bash
shasum -a 256 -c antigravity-jev-skills-20261107.zip.sha256
unzip antigravity-jev-skills-20261107.zip
cd antigravity-jev-skills
python3 -B install.py
python3 -B install.py --install
```

A Git clone starts at `python3 -B install.py`. For an isolated trial use `--install --target-root /tmp/agy-jev-trial`. The installer registers three skill symlinks and `jev`/`jev-pool` launchers; it preserves existing real files/directories. `--replace-links` replaces owned symlinks only. Keep the source folder in place, add `~/.local/bin` to PATH, and check `jev --help` and `jev-pool --help`.

Manually review/merge `global-config/AGENTS.md`, preserving existing rules and adapting dependency paths. It is not installed automatically. Reopen Codex to refresh instructions; they do not enforce routing in every application.

## 3. Private credentials

Workflows use `jev-pool`. Put TypeSafe keys outside this repo in `~/.config/typesafe/keys`: real owner-only directory (`0700`), private regular `*.key` files (`0600`), no symlinks. Enter values privately without shell-history literals. Lexicographic filenames determine slots: task defaults to 1, quant to 2; with one key, use `--key-slot 1` for quant. Standalone `jev auth set` or `TYPESAFE_API_KEY` alone is insufficient.

Optional APIs accept existing private files via `ZAI_API_KEY_FILE` or `OPENROUTER_API_KEY_FILE`. Defaults check `~/.config/ai-router/<provider>.key`, then `credentials.json`. Installation includes/changes no keys.

## 4. Routing and review

| WORKFLOW_WORKER | Model selection |
|---|---|
| `agy` (default) | `AGY_MODEL`: either High model above |
| `devin` | `swe-1-6-slow` |
| `opencode` | Exact free `OPENCODE_MODEL=provider/model` |
| `zai` | `glm-4.5-flash` |
| `openrouter` | Exact `OPENROUTER_MODEL` ending `:free`, zero pricing |

Pi is not integrated. Coordinator fallback to smaller/free routes follows clean quota/auth/availability/timeout failures, sharing the initial-plus-two-extra ledger. There is no automatic runtime switching or safety/proof/permission bypass. Main Codex is last resort outside the worker adapter. Jev 1.13.0, independent High review (plus quant obligations), and fresh scoped Codex final review remain required. The 100k-token/180-second per-call discipline needs coordinator supervision, not an SDK hard token cap.

Direct API final review needs a private `WORKFLOW_REVIEW_CONTEXT_FILE` outside the workspace, binding final hashes and fresh literal preflight. The two-phase procedure is in `antigravity-task-runner/references/routing.md`; `--chatgpt-review off` alone never authorizes completion.

## 5. Isolated sample

Use fresh workspace/output paths:

```bash
mkdir -p /tmp/agy-demo-ws
cat > /tmp/agy-demo-packet.txt <<'EOF'
TASK: Add greet.py with hello(name) returning "Hi, <name>!" and test_greet.py unit tests.
WRITE SCOPE: greet.py, test_greet.py.
VERIFY: python3 -B -m unittest discover -v
EOF
AGY_MODEL=gemini-4-argon-high python3 -B ~/.agents/skills/antigravity-task-runner/scripts/run_workflow.py /tmp/agy-demo-packet.txt   --workspace /tmp/agy-demo-ws --id demo --scope greet.py test_greet.py   --verify-cmd "python3 -B -m unittest discover -v" --key-slot 1   --worker-timeout 180 --max-reworks 0 --output-dir /tmp/agy-demo-out
```

Require `workflow-final-result.json` PASS, native `workflow-result.json` PASS, and retained `proof-pack.txt`. On BLOCKED inspect the reason; never bypass gates. Provenance: `THIRD_PARTY_NOTICES.md`.
