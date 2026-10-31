# Shared Jev → Gemini evidence gate and headless workflow runner

`scripts/run_workflow.py` and `scripts/jev_review_gate.py` provide stdlib-only orchestration and evidence gating. Neither authenticates arbitrary logs, proves financial mathematics, or authorizes live actions.

## One-command headless workflow (`scripts/run_workflow.py`)

```bash
python3 scripts/run_workflow.py [packet.txt] \
  --id sample-1 --kind task \
  --workspace /path/to/workspace \
  --scope port_spec.py tests/test_port_spec.py \
  --verify-cmd "python3 -B -m unittest discover -s tests -v" \
  --key-slot 1 --output-dir .artifacts/run-1
```

- **Worker execution & permissions:** Generates task-local `worker-settings.json` with `toolPermission: "request-review"`, `allowNonWorkspaceAccess: false`, explicit `write_file`/`edit_file` rules for declared scope paths, explicit `command` rules for `--verify-cmd`, and denies `read_url`, `execute_url`, `mcp`, and subagent delegation.
- **Metadata & fail-closed checks:** Requires `agy` exit 0, `init.model == "gemini-4-argon-high"`, `init.permission_mode == "request-review"`, `init.effort == "high"` when present (if omitted by CLI, records verification via `--effort high` plus the `-high` model slug), `result.status == "SUCCESS"`, non-empty response, and no tool denial or timeout. Fails closed as `BLOCKED` on any violation.
- **Mechanical checks & rework:** Snapshots workspace files before execution, verifies changed paths stay inside `--scope`, runs `--verify-cmd` (requiring exit 0 and `passed > 0`), and checks whitespace via `git diff --no-index --check`. On mechanical check failure or reviewer `REJECT`, sends a concise rework prompt (`<=400` tokens) back to the worker (resuming the worker conversation via `--resume <id>` by default) for at most **two unsuccessful rework loops**.
- **Cumulative usage accounting:** Resumed `agy` conversations report cumulative counters. `run_workflow.py` retains `raw_terminal_usage` snapshots and computes per-step deltas without double counting. `cache_read_tokens` is reported separately from `input_tokens` and `total_tokens`.
- **Outputs & exit codes:** Writes `workflow-result.json` and compact `proof-pack.txt`. Exit codes: `0` PASS, `1` REJECT, `2` BLOCKED, `3` PENDING_REVIEW.

## Standalone Jev → Gemini review gate (`scripts/jev_review_gate.py`)

```bash
python3 scripts/jev_review_gate.py packet.json --output-dir NEW_DIR --key-slot 1
```

### Packet input schema

```json
{
  "schema_version": 1,
  "id": "sample-1",
  "kind": "task",
  "objective": "Fix the bounded parser seam",
  "worker": {
    "model": "gemini-4-argon-high",
    "effort": "high",
    "exit_code": 0,
    "status": "SUCCESS",
    "response": "Nonempty actual worker response",
    "tool_denied": false
  },
  "checks": [
    {"kind": "tests", "command": "python3 -m unittest", "exit_code": 0, "passed": 8, "output": "Actual output"},
    {"kind": "scope", "command": "compare source snapshots", "exit_code": 0, "output": "Actual changed paths"},
    {"kind": "whitespace", "command": "git diff --check", "exit_code": 0, "output": "exit 0, no whitespace errors"}
  ],
  "evidence": {
    "focused_diff": "Actual unified diff",
    "verification_output": "Actual commands, output and exit codes",
    "summary": "Bounded change and residual limitations",
    "source_hashes": {"port_spec.py": "<sha256>"}
  },
  "open_questions": []
}
```

For `kind: "quant"`, include `quant_contract` with non-empty `data_provenance`, `friction`, `causality`, and `parity` strings (placeholders like `unknown`, `N/A`, or `tbd` block). Optional `min_reviewers` (`1..4`) can raise the reviewer count, never lower the quant minimum of 2. Requiring a stronger model is a separate obligation; a fresh context of `gemini-4-argon-high` does not qualify as a stronger tier.

### Triage efficiency, credential safety, and routing

- **Targeted Jev state (`build_jev_state`):** Sends Jev (`jev-1.13.0`) only `objective`, `kind`, `summary`, `verification_output`, compact check statuses, `open_questions`, `quant_contract` (if quant), and cryptographic hashes (`diff_sha256`, `packet_sha256`, `source_hashes`) linking the exact reviewed code, omitting raw diff bodies.
- **Deduplicated reviewer payload (`build_reviewer_payload`):** Sends each fresh `gemini-4-argon-high --effort high` reviewer the full `evidence` (`focused_diff`, `verification_output`, `summary`, `source_hashes`), check metadata, `open_questions`, `jev` triage, and `route`, omitting duplicated `worker.response` and `checks[].output`.
- **Credential redaction:** Blocks and redacts real secrets (`apikey_...`, `sk-...`, `Bearer ...`, `api_key`, `password`, `secret`, `authorization`, and secret `token=`/`"token":` assignments) while ignoring ordinary code identifiers and type annotations such as `token: str` or `token = part.strip()`.
- **Routing (`jev-review-v1`):** Requires `adequate` label, confidence `>=0.85`, margin `>=0.20`, concern `<0.15`, and no `open_questions` for `standard_review`; otherwise routes to `escalated_review`. Both keep mandatory independent reviewers (1 task, 2 quant) with distinct `conversation_id`s, `request-review` permissions, and no tool calls. `PASS` requires empty `findings` and explicit resolutions for all `open_questions`.
