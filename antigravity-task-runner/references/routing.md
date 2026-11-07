# Canonical Worker Routing & Fallback Policy

> **Precedence:** This policy supersedes conflicting legacy provider lists, wait-only instructions, and absolute local-Codex prohibitions across skill, reference, quant, and global docs. All unrelated security, financial, quant, retry-ledger, and reviewer rules remain mandatory.

## 1. Supported `run_workflow.py` Workers & Models

- **Allowed Workers:** `WORKFLOW_WORKER=agy|devin|opencode|zai|openrouter` (default `agy`; any other value is `BLOCKED`).
- **AGY Models:** `AGY_MODEL` permits only `claude-opus-5-5-high` or `gemini-4-argon-high`. Fresh independent reviewers always run at High effort in `request-review` mode.
- **Z.ai Direct API (`WORKFLOW_WORKER=zai`):** Selects `ZAI_MODEL=glm-4.5-flash`.
- **OpenRouter Direct Free API (`WORKFLOW_WORKER=openrouter`):** Requires an exact `OPENROUTER_MODEL` ending in `:free`, live zero `prompt`/`completion`/`request` pricing assurance, and enforces request parameters `max_price: 0` across all three fields (`prompt`, `completion`, `request`), `allow_fallbacks: false`, `require_parameters: true`, `response_format: {"type": "json_object"}`, `max_tokens: 4096`, and `reasoning` disabled.
- **Direct API Adapter Contract (`zai`, `openrouter`):** Zero tools, strict raw JSON `{"handoff", "files"}` output (every `files` value a string), guarded official HTTPS endpoints only, no redirects, private existing key files (`ZAI_API_KEY_FILE` and `OPENROUTER_API_KEY_FILE` optional existing paths; never literal secrets), exact model/finish/usage/source SHA hashes, atomic file application, and no invented AGY metadata.
- **API Token Accounting:** Usage equals `prompt + completion` tokens; cached and reasoning token subsets are not re-added.
- **Verified Sample Proof (Non-Guarantee):** `OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free` verified in `1105` tokens / `4.03s` / `HTTP 200` / `5` unit tests / `Jev 1.13.0` + High + default scoped Codex final review `PASS` (Gemini and Zai workers do not call the NVIDIA model; sample proof is not a future availability promise). Do not hard-code transient runtime quotas or shared-pool `429` (`upstream_provider_shared_pool`) responses into policy.

## 2. External-First Routing & Quota Fallback

- **External-First Default:** Prefer external CLI and direct free API workers first.
- **Coordinator Fallback:** On an actual, clean, recoverable availability, quota, auth, or timeout failure, the coordinator may switch across authenticated permitted smaller/free external CLIs and direct free APIs (`zai`, `openrouter` `:free`) using exact pinned models and native checks without re-asking the user for the same quota-fallback permission.
- **Single-Attempt CLI Execution:** `run_workflow.py` implements no automatic runtime provider switching or hooks; each CLI invocation performs one permitted attempt, and the coordinator selects the next eligible route if ledger capacity remains.
- **Ledger & Call Budgets:** Every provider switch counts as a used execution. Preserve the original baseline SHA and aggregate diff across attempts. Cap task-wide extra executions at `2` unless the user explicitly increases the ledger. Enforce `100k` tokens (including cache) and `180s` per call, with no background quota polling, no account/key/billing/paid changes, and no arbitrary retries.

## 3. Last-Resort Local Codex Implementation Exception

- **When Permitted:** Only after all eligible external CLIs and direct free APIs are unavailable may the current main local Codex session implement changes as a user-authorized last resort.
- **Execution Boundaries:** No `WORKFLOW_WORKER=codex` exists in `run_workflow.py` (never fabricate one); this is a coordinator-controlled exception outside the shared worker adapter. Use the actual current configured main Codex model—never fabricate smaller model slugs (any lower Codex model must be natively discovered and verified). No internal coding subagents are authorized.
- **Mandatory Gates:** Requires native usage tracking, `Jev 1.13.0`, independent High review (and quant two-High + stronger review rules where applicable), plus a separate fresh read-only final review. Missing reviewer availability blocks `PASS`.

## 4. Safety Blocks, Defects & Portable Command

- **Terminal Safety Blocks:** Model mismatch, permission/tool denial, unsafe/out-of-scope write, invalid proof, or unverified credentials/output are terminal `BLOCKED` stops—never fall back around them.
- **Code Defects:** Failed unit tests, syntax errors, or schema failures are defects, not quota failures; use bounded existing rework only.
- **Promotion & Claims:** Apply global `~/.codex/AGENTS.md` updates only after source/doc reviews and a real canonical CLI run all `PASS`. Never claim universal automation, 100% confidence, or live/trading approval.

```bash
WORKFLOW_WORKER=openrouter OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free AGY_MODEL=gemini-4-argon-high python3 scripts/run_workflow.py packet.txt --id sample --workspace /path/to/sample --scope normalize.py test_normalize.py --verify-cmd "python3 -B -m unittest discover -v" --worker-timeout 180 --max-reworks 0 --output-dir /outside/workspace/run
```
## Coordinator context for direct API final review

Direct `zai`/`openrouter` review requires `WORKFLOW_REVIEW_CONTEXT_FILE`: an absolute, private owner-only JSON file outside the worker workspace. It records actual user permission and the literal native preflight output; it grants no extra tool, model, or safety permissions. Missing, stale, aliased, mismatched, or secret-bearing context blocks before Codex.

The exact top-level fields are `schema_version` (integer 1), `case_id`, canonical `workspace`, `worker_provider`, exact `worker_model`, final `source_hashes`, `user_instruction`, and `quota_preflight`. Bind these to the successful pipeline's `workflow-result.json` and final source snapshot. The preflight object contains `native_command` (`agy --print /usage`), actual `native_output`, `selected_agy_model`, `selection_basis`, and timezone-aware UTC `checked_at_utc` (at most one hour old, no more than 60 seconds ahead). Its model must match the recorded independent reviewers. Do not put credentials or invented quota values in this file.

Final source hashes become known after generation. A coordinator can run the worker/check/Jev/High pipeline once with the explicit `--chatgpt-review off` override, create the bound context, then run the separate final review below. That override alone never authorizes global promotion: both the original pipeline and this final review must PASS. Normal CLI default remains `scoped`; no worker rerun, runtime hook, or automatic model failover is installed.

```bash
WORKFLOW_REVIEW_CONTEXT_FILE=/outside/workspace/context.json python3 scripts/chatgpt_review.py --workflow-dir /existing/pipeline --workspace /path/to/sample --output-dir /new/final-review --profile scoped --timeout 180
```

Use mode `0600` (or read-only `0400`) for the context file. Keep the original pipeline and failed review artifacts intact. Re-running this review command invokes no implementation worker.
