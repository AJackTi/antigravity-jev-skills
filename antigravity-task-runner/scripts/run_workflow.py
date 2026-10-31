#!/usr/bin/env python3
"""Headless workflow runner: Gemini worker -> mechanical checks -> Jev triage -> fresh Gemini review(s) -> bounded rework."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
GATE_PATH = SCRIPT_DIR / 'jev_review_gate.py'
_spec = importlib.util.spec_from_file_location('jev_review_gate', GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

GEMINI_MODEL = gate.GEMINI_MODEL
JEV_MODEL = gate.JEV_MODEL
MAX_REWORK_LIMIT = 2
MAX_HANDOFF_CHARS = 1500
MAX_CONTRACT_CHARS = 8000
MAX_DIFF_CHARS = 12000
PROTECTED_PATH_RE = re.compile(
    r'(?i)(?:^|/)(?:\.env(?:\..+)?|credentials?\b.*|secrets?\b.*|configs?/live/.*|.*exchange[_-]?endpoint.*|.*live[_-]?config.*)$'
)
DENIAL_PATTERNS = (
    'permission check failed',
    'user denied permission',
    'permission denied',
    'tool denied',
    'print timeout',
    'timed out',
    'quota exceeded',
    'authentication failed',
)
BLOCKED_RESPONSE_RE = re.compile(r'(?im)^\s*(?:verdict|status|outcome)?\s*[:=-]?\s*BLOCKED\b')
UNITTEST_RAN_RE = re.compile(r'\bRan\s+(\d+)\s+tests?\b')
PYTEST_PASSED_RE = re.compile(r'\b(\d+)\s+passed\b')
GO_PASS_RE = re.compile(r'(?m)^--- PASS:')
CONFLICT_MARKER_RE = re.compile(r'(?m)^(?:<{7}|={7}|>{7})(?:\s|$)')
SENTENCE_SPLIT_RE = re.compile(r'\.\s+(?=[A-Z])')
FILE_TOKEN_RE = re.compile(r'[A-Za-z0-9_./-]+(?:\.[A-Za-z0-9_-]+)+')


def estimate_tokens(text: str) -> int:
    text = (text or '').strip()
    if not text:
        return 0
    by_chars = (len(text) + 3) // 4
    by_words = int(len(text.split()) * 1.3)
    return max(1, min(by_chars, max(by_words, by_chars)))


def clamp_handoff(text: str, max_chars: int = MAX_HANDOFF_CHARS) -> str:
    text = gate.redact(text.strip())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + '\n...[truncated]'


def parse_packet_metadata(packet_text: str) -> dict[str, object]:
    """Extract objective, dotted scope file paths, and verify commands without corrupting dotted names."""
    objective = ''
    scope_files: list[str] = []
    verify_cmds: list[str] = []
    for raw_line in packet_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('TASK:') and not objective:
            body = line.removeprefix('TASK:').strip()
            objective = SENTENCE_SPLIT_RE.split(body, maxsplit=1)[0].rstrip('.').strip()
        elif not objective and not line.startswith('#'):
            objective = SENTENCE_SPLIT_RE.split(line, maxsplit=1)[0].rstrip('.').strip()
        if line.startswith('WRITE SCOPE:'):
            body = line.removeprefix('WRITE SCOPE:').strip()
            first_sentence = SENTENCE_SPLIT_RE.split(body, maxsplit=1)[0]
            for raw_token in FILE_TOKEN_RE.findall(first_sentence):
                token = raw_token.strip('.,;:()[]{}"\'')
                if token and '.' in Path(token).name and not token.startswith('.') and token not in scope_files:
                    scope_files.append(token)
            cmd_match = re.search(r'authorized shell command is exactly:\s*(.+)$', body)
            if cmd_match:
                verify_cmds.append(cmd_match.group(1).strip().rstrip('.'))
        elif 'authorized shell command is exactly:' in line:
            cmd_match = re.search(r'authorized shell command is exactly:\s*(.+)$', line)
            if cmd_match:
                verify_cmds.append(cmd_match.group(1).strip().rstrip('.'))
    return {'objective': objective, 'scope': scope_files, 'verify_cmds': verify_cmds}


def resolve_full_contract(packet_text: str | None, objective: str) -> str:
    """Return the complete verbatim task contract once without silent truncation."""
    pt = (packet_text or '').strip()
    obj = (objective or '').strip()
    if pt:
        if obj and obj not in pt:
            return f'{obj}\n\n{pt}'
        return pt
    return obj


def build_initial_prompt(packet_text: str | None, objective: str, scope_files: list[str], verify_cmds: list[str]) -> str:
    contract = resolve_full_contract(packet_text, objective)
    if len(contract) > MAX_CONTRACT_CHARS:
        raise ValueError('task contract exceeds maximum bounded size; split seam instead of truncating')
    scope_str = ', '.join(scope_files)
    cmd_lines = ', '.join(f'`{c.strip()}`' for c in verify_cmds if c.strip())
    verify_rule = (
        f'Execute each authorized verification command literally and separately ({cmd_lines}); '
        'forbid extra shell discovery or command chaining (no ls/pwd/cd/prefixes/suffixes/&&/|). Do not loosen ACLs.'
        if cmd_lines
        else 'Read-only audit: do not run shell commands; root coordinator performs mechanical checks. Do not loosen ACLs.'
    )
    if packet_text and packet_text.strip():
        return f'{contract}\n\nOPERATIONAL RULES: Edit only {scope_str}. {verify_rule}'
    return (
        f'TASK: {contract}\n'
        f'WRITE SCOPE: only {scope_str}. No network, credentials, unrelated files, or delegation.\n'
        f'VERIFY: {verify_rule} Stop as BLOCKED on permission denial or unavailable verification.\n'
        'STOP: Deliver working files and verification proof. Final response <=350 tokens: '
        'Understanding, Plan (<=5 steps), Files, Change, Verification, Residual risk.'
    )


def build_rework_prompt(
    rework_index: int,
    max_reworks: int,
    objective: str,
    scope_files: list[str],
    verify_cmds: list[str],
    defects: list[str],
    open_questions: list[str] | None = None,
) -> str:
    scope_str = ', '.join(scope_files)
    cmd_lines = ', '.join(f'`{c.strip()}`' for c in verify_cmds if c.strip())
    defect_lines = '; '.join(d.strip() for d in defects if d and d.strip())[:700]
    q_lines = f"\nOPEN QUESTIONS TO RESOLVE: {'; '.join(open_questions)[:180]}" if open_questions else ''
    prompt = (
        f'REWORK {rework_index}/{max_reworks}: {objective.strip()[:240]}\n'
        f'WRITE SCOPE: only {scope_str}.\n'
        f'DEFECTS: {defect_lines}{q_lines}\n'
        f'ACTION: Fix every defect within scope. Execute each verification command literally and separately ({cmd_lines}) '
        'with no extra shell discovery or chaining (no ls/pwd/cd/prefixes/suffixes/&&/|). Do not loosen ACLs or touch '
        'out-of-scope files. Return <=350 tokens: Understanding, Plan, Files, Change, Verification, Residual risk.'
    )
    return clamp_handoff(prompt)


def build_worker_settings(workspace: Path, scope_files: list[str], verify_cmds: list[str]) -> dict[str, object]:
    workspace = workspace.resolve()
    if not scope_files:
        raise ValueError('nonempty explicit or resolved write scope required; refusing whole-workspace write permissions')
    allow = ['read_file(*)']
    for rel in scope_files:
        raw_rel = str(rel).strip()
        if not raw_rel or Path(raw_rel).is_absolute():
            raise ValueError(f'invalid write scope entry: {rel!r}')
        clean_rel = Path(raw_rel).as_posix()
        resolved_target = (workspace / clean_rel).resolve()
        try:
            resolved_target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f'write scope escapes workspace: {rel!r}') from exc
        abs_target = str(resolved_target)
        for pattern in (
            f'write_file({clean_rel})',
            f'write_file({abs_target})',
            f'edit_file({clean_rel})',
            f'edit_file({abs_target})',
        ):
            if pattern not in allow:
                allow.append(pattern)
    for cmd in verify_cmds:
        cmd_clean = cmd.strip()
        if cmd_clean:
            for pattern in (f'command({cmd_clean})', f'run_command({cmd_clean})'):
                if pattern not in allow:
                    allow.append(pattern)
    return {
        'toolPermission': 'request-review',
        'allowNonWorkspaceAccess': False,
        'permissions': {
            'allow': allow,
            'deny': [
                'read_url(*)',
                'execute_url(*)',
                'mcp(*)',
                'invoke_subagent(*)',
                'define_subagent(*)',
            ],
        },
    }


def snapshot_workspace(workspace: Path) -> dict[str, bytes]:
    workspace = workspace.resolve()
    files: dict[str, bytes] = {}
    if not workspace.is_dir():
        return files
    for path in sorted(workspace.rglob('*')):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(workspace).parts
        if any(part in {'.git', '__pycache__', '.pytest_cache', '.mypy_cache'} or part.endswith('.pyc') for part in rel_parts):
            continue
        rel = path.relative_to(workspace).as_posix()
        files[rel] = path.read_bytes()
    return files


def file_hashes(snapshot: dict[str, bytes], subset: list[str] | None = None) -> dict[str, str]:
    keys = sorted(subset) if subset is not None else sorted(snapshot)
    return {k: hashlib.sha256(snapshot[k]).hexdigest() for k in keys if k in snapshot}


def compute_changed_files(before: dict[str, bytes], after: dict[str, bytes]) -> tuple[list[str], list[str]]:
    changed: list[str] = []
    deleted: list[str] = []
    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            deleted.append(rel)
        elif rel not in before or before[rel] != after[rel]:
            changed.append(rel)
    return changed, deleted


def build_focused_diff(before: dict[str, bytes], after: dict[str, bytes], changed_files: list[str], max_chars: int = MAX_DIFF_CHARS) -> str:
    chunks: list[str] = []
    for rel in changed_files:
        old_text = before.get(rel, b'').decode('utf-8', errors='replace').splitlines()
        new_text = after.get(rel, b'').decode('utf-8', errors='replace').splitlines()
        diff_lines = list(
            difflib.unified_diff(
                old_text,
                new_text,
                fromfile=f'a/{rel}' if rel in before else '/dev/null',
                tofile=f'b/{rel}',
                lineterm='',
            )
        )
        if diff_lines:
            chunks.append('\n'.join(diff_lines))
    return '\n'.join(chunks).strip()


def parse_test_count(output: str, exit_code: int) -> int:
    if exit_code != 0:
        return 0
    m = UNITTEST_RAN_RE.search(output)
    if m:
        if re.search(r'(?m)^FAILED\b', output):
            return 0
        return int(m.group(1))
    m = PYTEST_PASSED_RE.search(output)
    if m:
        return int(m.group(1))
    go_passes = len(GO_PASS_RE.findall(output))
    if go_passes > 0:
        return go_passes
    if re.search(r'\bOK\b', output):
        return 1
    return 0


def run_mechanical_checks(
    workspace: Path,
    baseline_snapshot: dict[str, bytes],
    scope_files: list[str],
    verify_cmds: list[str],
) -> dict[str, object]:
    current_snapshot = snapshot_workspace(workspace)
    changed, deleted = compute_changed_files(baseline_snapshot, current_snapshot)
    all_touched = sorted(set(changed) | set(deleted))
    scope_set = {Path(p).as_posix() for p in scope_files} if scope_files else set()
    out_of_scope = [p for p in all_touched if scope_set and p not in scope_set]
    missing_declared = [p for p in sorted(scope_set) if p not in current_snapshot or not current_snapshot[p].strip()]

    if out_of_scope:
        scope_exit = 1
        scope_out = f'Out-of-scope files modified: {", ".join(out_of_scope)}'
    elif not changed:
        scope_exit = 1
        scope_out = 'No workspace files were created or modified by worker'
    elif missing_declared:
        scope_exit = 1
        scope_out = f'Missing or empty declared target files: {", ".join(missing_declared)}'
    else:
        scope_exit = 0
        scope_out = f'Changed files within declared scope ({len(changed)}): {", ".join(changed)}'

    scope_check = {
        'kind': 'scope',
        'command': 'compare workspace snapshot against declared write scope',
        'exit_code': scope_exit,
        'output': scope_out,
    }

    # Run verification commands
    test_outputs: list[str] = []
    test_exit = 0
    total_passed = 0
    cmds = verify_cmds or ['python3 -B -m unittest discover -s tests -v']
    for cmd in cmds:
        try:
            argv = shlex.split(cmd)
            res = subprocess.run(argv, cwd=workspace, text=True, capture_output=True, timeout=120, check=False)
            combined = ((res.stdout or '') + ('\n' + res.stderr if res.stderr else '')).strip()
            test_outputs.append(f'$ {cmd} (exit {res.returncode})\n{combined}'.strip())
            if res.returncode != 0:
                test_exit = res.returncode
            else:
                total_passed += parse_test_count(combined, res.returncode)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            test_exit = 2
            test_outputs.append(f'$ {cmd} failed to execute: {exc}')

    if test_exit == 0 and total_passed <= 0:
        test_exit = 1
    tests_check = {
        'kind': 'tests',
        'command': ' && '.join(cmds),
        'exit_code': test_exit,
        'passed': total_passed,
        'output': '\n\n'.join(test_outputs).strip() or 'no test output',
    }

    # Whitespace check across changed files
    ws_issues: list[str] = []
    raw_ws_exits: list[int] = []
    for rel in changed:
        target = workspace / rel
        content = current_snapshot[rel].decode('utf-8', errors='replace')
        if CONFLICT_MARKER_RE.search(content):
            ws_issues.append(f'{rel}: merge conflict marker detected')
        for idx, line in enumerate(content.splitlines(), start=1):
            if line.endswith((' ', '\t', '\r')):
                ws_issues.append(f'{rel}:{idx}: trailing whitespace')
                break
        try:
            ws_res = subprocess.run(
                ['git', 'diff', '--no-index', '--check', '--', '/dev/null', str(target)],
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            raw_ws_exits.append(ws_res.returncode)
            diag = ((ws_res.stdout or '') + (ws_res.stderr or '')).strip()
            if diag or ws_res.returncode not in (0, 1):
                ws_issues.append(diag or f'{rel}: git diff --check exited {ws_res.returncode}')
        except FileNotFoundError:
            raw_ws_exits.append(0)
        except subprocess.TimeoutExpired:
            ws_issues.append(f'{rel}: git diff --check timed out')

    if not changed:
        ws_exit = 1
        ws_out = 'No changed files to check for whitespace'
    elif ws_issues:
        ws_exit = 2
        ws_out = '; '.join(ws_issues)
    else:
        ws_exit = 0
        ws_out = f'exit 0, no whitespace errors across {len(changed)} file(s) (raw git diff --no-index --check exits: {raw_ws_exits})'

    whitespace_check = {
        'kind': 'whitespace',
        'command': 'git diff --no-index --check',
        'exit_code': ws_exit,
        'output': ws_out,
    }

    focused_diff = build_focused_diff(baseline_snapshot, current_snapshot, changed, max_chars=MAX_DIFF_CHARS)
    diff_chars = len(focused_diff)
    oversized_diff = diff_chars > MAX_DIFF_CHARS
    oversized_diff_reason = (
        f'focused diff length ({diff_chars} chars) exceeds maximum bounded review budget ({MAX_DIFF_CHARS} chars); '
        'split seam instead of truncating'
        if oversized_diff
        else None
    )
    checks = [tests_check, scope_check, whitespace_check]
    all_passed = all(c['exit_code'] == 0 for c in checks) and total_passed > 0 and not out_of_scope and not oversized_diff
    return {
        'all_passed': all_passed,
        'out_of_scope': out_of_scope,
        'oversized_diff': oversized_diff,
        'oversized_diff_reason': oversized_diff_reason,
        'diff_chars': diff_chars,
        'max_diff_chars': MAX_DIFF_CHARS,
        'changed_files': changed,
        'deleted_files': deleted,
        'checks': checks,
        'snapshot': current_snapshot,
        'source_hashes': file_hashes(current_snapshot, changed),
        'focused_diff': focused_diff,
    }


def compute_usage_delta(
    raw_usage: dict[str, object] | None,
    prev_raw_usage: dict[str, object] | None,
    default_basis: str,
) -> tuple[dict[str, int], dict[str, int], str]:
    raw_dict = raw_usage if isinstance(raw_usage, dict) else {}
    keys = ('input_tokens', 'output_tokens', 'thinking_tokens', 'cache_read_tokens', 'total_tokens')
    clean_raw: dict[str, int] = {}
    for k in keys:
        val = raw_dict.get(k, 0)
        clean_raw[k] = int(val) if type(val) in (int, float) and val >= 0 else 0
    if clean_raw['total_tokens'] == 0 and (clean_raw['input_tokens'] or clean_raw['output_tokens']):
        clean_raw['total_tokens'] = clean_raw['input_tokens'] + clean_raw['output_tokens']

    if isinstance(prev_raw_usage, dict):
        delta: dict[str, int] = {}
        for k in keys:
            prev_val = prev_raw_usage.get(k, 0)
            prev_int = int(prev_val) if type(prev_val) in (int, float) and prev_val >= 0 else 0
            delta[k] = max(0, clean_raw[k] - prev_int)
        if delta['total_tokens'] == 0 and (delta['input_tokens'] or delta['output_tokens']):
            delta['total_tokens'] = delta['input_tokens'] + delta['output_tokens']
        basis = 'difference of terminal usage counters in the same resumed worker conversation'
    else:
        delta = dict(clean_raw)
        basis = default_basis

    return delta, clean_raw, basis


def run_worker_attempt(
    workspace: Path,
    attempt_dir: Path,
    prompt: str,
    scope_files: list[str],
    verify_cmds: list[str],
    timeout_seconds: int,
    resume_conversation_id: str | None = None,
    prev_raw_by_cid: dict[str, dict[str, int]] | None = None,
) -> dict[str, object]:
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = attempt_dir / 'worker-settings.json'
    settings = build_worker_settings(workspace, scope_files, verify_cmds)
    gate.save(settings_path, settings)
    (attempt_dir / 'worker-prompt.txt').write_text(prompt, encoding='utf-8')

    try:
        executable = gate.resolve_executable('agy')
    except OSError:
        gate.save(
            attempt_dir / 'worker-invocation.json',
            {'requested_executable': 'agy', 'state': 'UNAVAILABLE', 'model': GEMINI_MODEL, 'effort': 'high', 'exit_code': None},
        )
        return {
            'ok': False,
            'blocked': True,
            'reason': 'agy executable unavailable',
            'exit_code': None,
            'tool_denied': False,
        }

    argv = [
        executable,
        '--model', GEMINI_MODEL,
        '--effort', 'high',
        '--settings', str(settings_path),
        '--print-timeout', f'{int(timeout_seconds)}s',
        '--output-format', 'stream-json',
    ]
    if resume_conversation_id:
        argv.extend(['--conversation', resume_conversation_id])
    argv.extend(['--print', prompt])

    gate.save(
        attempt_dir / 'worker-invocation.json',
        {
            'argv': argv,
            'state': 'STARTING',
            'model': GEMINI_MODEL,
            'effort': 'high',
            'resumed_from': resume_conversation_id,
            'prompt_tokens_est': estimate_tokens(prompt),
        },
    )

    t0 = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=workspace, text=True, capture_output=True, timeout=timeout_seconds + 20, check=False)
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - t0, 2)
        out_txt = exc.stdout.decode(errors='replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        err_txt = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
        (attempt_dir / 'worker.stdout.jsonl').write_text(gate.redact(out_txt), encoding='utf-8')
        (attempt_dir / 'worker.stderr.txt').write_text(gate.redact(err_txt), encoding='utf-8')
        gate.save(
            attempt_dir / 'worker-timeout.json',
            {'timed_out': True, 'exit_code': None, 'model_argument': GEMINI_MODEL, 'effort_argument': 'high', 'duration_seconds': duration},
        )
        return {
            'ok': False,
            'blocked': True,
            'reason': 'worker timed out',
            'exit_code': None,
            'tool_denied': False,
            'duration_seconds': duration,
        }

    duration = round(time.monotonic() - t0, 2)
    (attempt_dir / 'worker.stdout.jsonl').write_text(gate.redact(result.stdout), encoding='utf-8')
    (attempt_dir / 'worker.stderr.txt').write_text(gate.redact(result.stderr), encoding='utf-8')
    gate.save(
        attempt_dir / 'worker-invocation.json',
        {
            'argv': argv,
            'state': 'FINISHED',
            'exit_code': result.returncode,
            'duration_seconds': duration,
            'resumed_from': resume_conversation_id,
        },
    )

    try:
        events = [gate.strict_json(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError:
        return {
            'ok': False,
            'blocked': True,
            'reason': 'malformed worker JSONL stream',
            'exit_code': result.returncode,
            'tool_denied': False,
            'duration_seconds': duration,
        }

    if not events or any(
        not isinstance(e, dict) or any(k in e and not isinstance(e[k], dict) for k in ('init', 'result', 'step_update'))
        for e in events
    ):
        return {
            'ok': False,
            'blocked': True,
            'reason': 'invalid or empty worker event stream',
            'exit_code': result.returncode,
            'tool_denied': False,
            'duration_seconds': duration,
        }

    init = next((e['init'] for e in events if e.get('event') == 'init'), {})
    terminal = next((e['result'] for e in reversed(events) if e.get('event') == 'result'), {})
    usage_updates = sum(
        1 for e in events if isinstance(e.get('usage'), dict) or isinstance(e.get('result', {}).get('usage'), dict)
    )

    tool_denied = bool(terminal.get('tool_denied') is True)
    for ev in events:
        if ev.get('tool_denied') is True:
            tool_denied = True
        step = ev.get('step_update')
        if isinstance(step, dict):
            if step.get('denied') is True or str(step.get('status', '')).upper() in ('DENIED', 'BLOCKED'):
                tool_denied = True
            if str(step.get('status', '')).upper() == 'ERROR':
                err_msg = f"{step.get('error', '')} {step.get('output', '')}".lower()
                if any(pat in err_msg for pat in DENIAL_PATTERNS):
                    tool_denied = True

    stderr_lower = (result.stderr or '').lower()
    if any(pat in stderr_lower for pat in DENIAL_PATTERNS):
        tool_denied = True

    model_actual = init.get('model')
    effort_field = init.get('effort', 'high')
    perm_mode = init.get('permission_mode')
    status = terminal.get('status')
    response_text = str(terminal.get('response') or '').strip()
    cid = terminal.get('conversation_id')

    prev_map = prev_raw_by_cid if isinstance(prev_raw_by_cid, dict) else {}
    prev_raw = prev_map.get(cid) if isinstance(cid, str) and cid in prev_map else None
    delta_usage, raw_usage, usage_basis = compute_usage_delta(
        terminal.get('usage'),
        prev_raw,
        'initial CLI conversation counter',
    )
    if isinstance(cid, str) and cid.strip():
        prev_map[cid] = raw_usage

    effort_verification = (
        'init effort field'
        if 'effort' in init
        else 'explicit --effort high plus init.model High slug; init omits separate effort'
    )

    block_reasons: list[str] = []
    if result.returncode != 0:
        block_reasons.append(f'worker exited with code {result.returncode}')
    if model_actual != GEMINI_MODEL:
        block_reasons.append(f'unexpected worker model: {model_actual!r}')
    if effort_field != 'high':
        block_reasons.append(f'unexpected worker effort: {effort_field!r}')
    if perm_mode and perm_mode != 'request-review':
        block_reasons.append(f'unexpected worker permission_mode: {perm_mode!r}')
    if status != 'SUCCESS':
        block_reasons.append(f'worker terminal status {status!r}')
    if tool_denied:
        block_reasons.append('worker encountered tool permission denial or timeout')
    if not response_text:
        block_reasons.append('worker returned empty response (possible silent denial/stop)')
    if not isinstance(cid, str) or not cid.strip():
        block_reasons.append('worker conversation_id missing')
    if response_text and BLOCKED_RESPONSE_RE.search(response_text):
        block_reasons.append('worker explicitly reported BLOCKED')

    worker_record = {
        'ok': len(block_reasons) == 0,
        'blocked': len(block_reasons) > 0,
        'reason': '; '.join(block_reasons) if block_reasons else None,
        'model': model_actual,
        'effort': 'high' if effort_field == 'high' else effort_field,
        'effort_verification': effort_verification,
        'permission_mode': perm_mode or 'request-review',
        'conversation_id': cid,
        'exit_code': result.returncode,
        'status': status,
        'tool_denied': tool_denied or not bool(response_text),
        'response': response_text,
        'duration_seconds': duration,
        'usage': delta_usage,
        'raw_terminal_usage': raw_usage,
        'usage_basis': usage_basis,
        'usage_updates_observed': usage_updates,
    }
    gate.save(attempt_dir / 'worker-result.json', worker_record)
    return worker_record


def summarize_worker_response(response_text: str, max_chars: int = 600) -> str:
    cleaned = gate.redact(response_text.strip())
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    compact = ' | '.join(lines)
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 15].rstrip() + '...'


def aggregate_external_usage(external_steps: list[dict[str, object]]) -> dict[str, object]:
    gemini_in = 0
    gemini_out = 0
    gemini_thinking = 0
    gemini_cache_read = 0
    gemini_total = 0
    jev_in = 0
    jev_out = 0
    jev_total = 0
    updates = 0

    for step in external_steps:
        usage = step.get('usage') if isinstance(step.get('usage'), dict) else {}
        updates += int(step.get('usage_updates_observed', 1) or 1)
        if step.get('model') == JEV_MODEL or step.get('role') == 'jev':
            i = int(usage.get('input_tokens', 0) or 0)
            o = int(usage.get('output_tokens', 0) or 0)
            t = int(usage.get('total_tokens', i + o) or (i + o))
            jev_in += i
            jev_out += o
            jev_total += t
        else:
            i = int(usage.get('input_tokens', 0) or 0)
            o = int(usage.get('output_tokens', 0) or 0)
            th = int(usage.get('thinking_tokens', 0) or 0)
            cr = int(usage.get('cache_read_tokens', 0) or 0)
            t = int(usage.get('total_tokens', i + o) or (i + o))
            gemini_in += i
            gemini_out += o
            gemini_thinking += th
            gemini_cache_read += cr
            gemini_total += t

    return {
        'gemini': {
            'input_tokens': gemini_in,
            'uncached_input_tokens': gemini_in,
            'cache_read_tokens': gemini_cache_read,
            'gross_input_including_cache_read_tokens': gemini_in + gemini_cache_read,
            'output_tokens': gemini_out,
            'thinking_tokens': gemini_thinking,
            'total_tokens': gemini_total,
        },
        'jev': {
            'input_tokens': jev_in,
            'output_tokens': jev_out,
            'total_tokens': jev_total,
        },
        'combined_non_cache_total_tokens': gemini_total + jev_total,
        'usage_updates_observed': updates,
    }


def format_proof_pack(report: dict[str, object]) -> str:
    scope_str = ', '.join(report.get('source_files', [])) or 'none'
    hashes_str = ', '.join(f"{k}:{v[:12]}" for k, v in sorted((report.get('source_hashes') or {}).items())) or 'none'
    rev_summaries = []
    for idx, rev in enumerate(report.get('reviewers', []), start=1):
        rev_summaries.append(
            f"R{idx}({rev.get('conversation_id')}):{rev.get('verdict')}({rev.get('confidence')}%)"
        )
    rev_str = ', '.join(rev_summaries) if rev_summaries else 'none'
    jev_info = report.get('jev_assessment') or {}
    jev_str = (
        f"route={report.get('jev_route')} label={jev_info.get('label')} "
        f"conf={jev_info.get('confidence')} concern={jev_info.get('concern_probability')}"
        if jev_info
        else f"route={report.get('jev_route')} error={report.get('jev_error')}"
    )
    usage_tot = report.get('usage_totals', {})
    gem_u = usage_tot.get('gemini', {})
    jev_u = usage_tot.get('jev', {})
    lines = [
        f"TASK: {report.get('id')} — {report.get('task')}",
        f"VERDICT: {report.get('verdict')} (rework_loops={report.get('rework_loops')})",
        f"SCOPE: {scope_str}",
        f"HASHES: {hashes_str} (unchanged_during_review={report.get('source_hashes_unchanged_during_chatgpt_verification_and_review')})",
        f"CHANGE: {report.get('summary') or 'none'}",
        f"TESTS: `{report.get('coordinator_test_command')}` exit={report.get('test_exit_code')} passed={report.get('unique_tests_passed')}",
        f"JEV: {jev_str}",
        f"REVIEW: {rev_str}",
        f"USAGE: gemini_total={gem_u.get('total_tokens', 0)} (in={gem_u.get('input_tokens', 0)}, out={gem_u.get('output_tokens', 0)}, cache_read={gem_u.get('cache_read_tokens', 0)}) jev_total={jev_u.get('total_tokens', 0)}",
        f"RISK: {report.get('residual_risk') or '; '.join(report.get('reasons', [])) or 'Bounded scope only'}",
    ]
    return clamp_handoff('\n'.join(lines), max_chars=1400) + '\n'


def run_workflow(
    *,
    task_id: str,
    objective: str,
    workspace: Path,
    output_dir: Path,
    kind: str = 'task',
    packet_text: str | None = None,
    scope_files: list[str] | None = None,
    verify_cmds: list[str] | None = None,
    key_slot: int | None = None,
    min_reviewers: int | None = None,
    max_reworks: int = MAX_REWORK_LIMIT,
    worker_timeout: int = 300,
    open_questions: list[str] | None = None,
    quant_contract: dict[str, str] | None = None,
    require_stronger_reviewer: bool = False,
    stronger_reviewer_model: str | None = None,
    authorized_protected: bool = False,
    triage_only: bool = False,
    resume_worker: bool = True,
) -> dict[str, object]:
    workspace = Path(workspace).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

    parsed_meta = parse_packet_metadata(packet_text or '')
    effective_objective = (objective or str(parsed_meta['objective']) or '').strip()
    full_contract = resolve_full_contract(packet_text, effective_objective)
    effective_scope = [str(p).strip() for p in (scope_files if scope_files is not None else parsed_meta['scope']) if str(p).strip()]
    effective_verify = list(verify_cmds if verify_cmds is not None else parsed_meta['verify_cmds'])
    if not effective_verify:
        effective_verify = ['python3 -B -m unittest discover -s tests -v']
    effective_slot = key_slot if key_slot in (1, 2, 3) else (2 if kind == 'quant' else 1)
    effective_max_reworks = max(0, min(int(max_reworks), MAX_REWORK_LIMIT))
    questions = [q.strip() for q in (open_questions or []) if isinstance(q, str) and q.strip()]

    # Pre-flight safety / authorization / quant checks
    preflight_blockers: list[str] = []
    if kind not in ('task', 'quant'):
        preflight_blockers.append('kind must be task or quant')
    if not effective_objective or not full_contract:
        preflight_blockers.append('missing objective')
    if len(full_contract) > MAX_CONTRACT_CHARS:
        preflight_blockers.append(
            f'task contract length ({len(full_contract)} chars) exceeds maximum bounded budget ({MAX_CONTRACT_CHARS}); split seam instead of truncating'
        )
    if not effective_scope:
        preflight_blockers.append('missing explicit or resolved write scope; refusing whole-workspace write permissions')
    else:
        for rel in effective_scope:
            if Path(rel).is_absolute():
                preflight_blockers.append(f'write scope must be relative to workspace: {rel}')
            else:
                try:
                    (workspace / rel).resolve().relative_to(workspace)
                except ValueError:
                    preflight_blockers.append(f'write scope escapes workspace: {rel}')
    if int(max_reworks) > MAX_REWORK_LIMIT or int(max_reworks) < 0:
        preflight_blockers.append(f'max_reworks must be in 0..{MAX_REWORK_LIMIT}')
    if gate.contains_secret(full_contract):
        preflight_blockers.append('possible credential in task packet; redact before delegation')
    if not authorized_protected:
        for rel in effective_scope:
            if PROTECTED_PATH_RE.search(Path(rel).as_posix()):
                preflight_blockers.append(f'protected path requires explicit user authorization: {rel}')
    if kind == 'quant':
        if not isinstance(quant_contract, dict) or not all(
            isinstance(quant_contract.get(k), str)
            and quant_contract[k].strip()
            and quant_contract[k].strip().lower() not in {'unknown', 'n/a', 'na', 'tbd', 'todo', 'not provided', 'not known'}
            for k in ('data_provenance', 'friction', 'causality', 'parity')
        ):
            preflight_blockers.append('missing quant data/friction/causality/parity contract')
    if require_stronger_reviewer:
        preflight_blockers.append(
            'stronger reviewer required; this runner only executes pinned gemini-4-argon-high reviewers '
            '(same model in a fresh context is not stronger, and a caller-supplied model name is not verified execution proof)'
        )

    if preflight_blockers:
        blocked_report = {
            'verdict': 'BLOCKED',
            'id': task_id,
            'kind': kind,
            'task': effective_objective,
            'workspace': str(workspace),
            'source_files': [],
            'source_hashes': {},
            'source_hashes_unchanged_during_chatgpt_verification_and_review': True,
            'source_or_tests_written_by_chatgpt': 0,
            'worker_and_reviewer_model': GEMINI_MODEL,
            'effort': 'high',
            'effort_verification': 'preflight blocked before model invocation',
            'rework_loops': 0,
            'max_reworks': effective_max_reworks,
            'unique_tests_passed': 0,
            'coordinator_test_command': ' && '.join(effective_verify),
            'test_exit_code': 2,
            'gate_exit_code': 2,
            'jev_route': 'blocked',
            'jev_assessment': None,
            'reasons': preflight_blockers,
            'reviewers': [],
            'stronger_reviewer_required': bool(kind == 'quant' or require_stronger_reviewer),
            'same_model_is_stronger': False,
            'external_steps': [],
            'usage_totals': aggregate_external_usage([]),
            'attempts': [],
            'residual_risk': '; '.join(preflight_blockers),
        }
        blocked_report['proof_pack'] = format_proof_pack(blocked_report)
        gate.save(output_dir / 'workflow-result.json', blocked_report)
        (output_dir / 'proof-pack.txt').write_text(blocked_report['proof_pack'], encoding='utf-8')
        return blocked_report

    baseline_snapshot = snapshot_workspace(workspace)
    prev_raw_by_cid: dict[str, dict[str, int]] = {}
    worker_cid: str | None = None
    external_steps: list[dict[str, object]] = []
    attempts_log: list[dict[str, object]] = []
    rework_loops = 0
    pending_defects: list[str] = []

    final_verdict = 'BLOCKED'
    final_reasons: list[str] = []
    final_mech: dict[str, object] = {}
    final_gate: dict[str, object] = {}
    final_summary = ''
    final_residual_risk = ''
    hashes_unchanged = True
    last_effort_verification = 'explicit --effort high plus init.model High slug; init omits separate effort'

    for attempt_idx in range(effective_max_reworks + 1):
        attempt_num = attempt_idx + 1
        attempt_dir = output_dir / f'attempt-{attempt_num}'
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if attempt_idx == 0:
            prompt = build_initial_prompt(packet_text, effective_objective, effective_scope, effective_verify)
            step_label = 'Gemini implementation'
        else:
            prompt = build_rework_prompt(
                attempt_idx,
                effective_max_reworks,
                effective_objective,
                effective_scope,
                effective_verify,
                pending_defects,
                questions,
            )
            step_label = f'Gemini rework {attempt_idx}'

        worker_res = run_worker_attempt(
            workspace=workspace,
            attempt_dir=attempt_dir,
            prompt=prompt,
            scope_files=effective_scope,
            verify_cmds=effective_verify,
            timeout_seconds=worker_timeout,
            resume_conversation_id=worker_cid if (resume_worker and attempt_idx > 0) else None,
            prev_raw_by_cid=prev_raw_by_cid,
        )

        if isinstance(worker_res.get('effort_verification'), str):
            last_effort_verification = str(worker_res['effort_verification'])

        external_steps.append(
            {
                'step': step_label,
                'role': 'worker',
                'attempt': attempt_num,
                'model': worker_res.get('model') or GEMINI_MODEL,
                'effort': worker_res.get('effort') or 'high',
                'conversation_id': worker_res.get('conversation_id'),
                'usage': worker_res.get('usage', {}),
                'usage_basis': worker_res.get('usage_basis', 'initial CLI conversation counter'),
                'raw_terminal_usage': worker_res.get('raw_terminal_usage', {}),
                'usage_updates_observed': worker_res.get('usage_updates_observed', 0),
                'exit_code': worker_res.get('exit_code'),
                'duration_seconds': worker_res.get('duration_seconds', 0.0),
                'status': 'BLOCKED' if worker_res.get('blocked') else 'SUCCESS',
                'reason': worker_res.get('reason'),
            }
        )

        if worker_res.get('blocked') or not worker_res.get('ok'):
            final_verdict = 'BLOCKED'
            final_reasons = [str(worker_res.get('reason') or 'worker execution blocked')]
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'worker',
                    'status': 'BLOCKED',
                    'reasons': final_reasons,
                    'artifact_dir': str(attempt_dir),
                }
            )
            break

        worker_cid = str(worker_res['conversation_id'])
        final_summary = summarize_worker_response(str(worker_res.get('response') or ''))

        # Run coordinator mechanical verification
        mech = run_mechanical_checks(workspace, baseline_snapshot, effective_scope, effective_verify)
        final_mech = mech
        gate.save(attempt_dir / 'mechanical-checks.json', {k: v for k, v in mech.items() if k != 'snapshot'})

        # Out-of-scope file modifications fail closed immediately as BLOCKED
        if mech['out_of_scope']:
            final_verdict = 'BLOCKED'
            final_reasons = [f"scope violation: worker modified out-of-scope files: {', '.join(mech['out_of_scope'])}"]
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'mechanical_checks',
                    'status': 'BLOCKED',
                    'reasons': final_reasons,
                    'artifact_dir': str(attempt_dir),
                }
            )
            break

        # Oversized diffs fail closed immediately as BLOCKED before Jev or any reviewer call
        if mech.get('oversized_diff'):
            final_verdict = 'BLOCKED'
            final_reasons = [
                str(
                    mech.get('oversized_diff_reason')
                    or f"focused diff length ({mech.get('diff_chars')} chars) exceeds maximum bounded review budget ({MAX_DIFF_CHARS} chars); split seam instead of truncating"
                )
            ]
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'mechanical_checks',
                    'status': 'BLOCKED',
                    'reasons': final_reasons,
                    'artifact_dir': str(attempt_dir),
                }
            )
            break

        if not mech['all_passed']:
            failed_descriptions = [
                f"{c['kind']} check failed (exit {c['exit_code']}): {c['output'][:350]}"
                for c in mech['checks']
                if c['exit_code'] != 0 or (c['kind'] == 'tests' and c.get('passed', 0) <= 0)
            ]
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'mechanical_checks',
                    'status': 'FAILED',
                    'reasons': failed_descriptions,
                    'artifact_dir': str(attempt_dir),
                }
            )
            if rework_loops < effective_max_reworks:
                rework_loops += 1
                pending_defects = failed_descriptions
                continue
            final_verdict = 'REJECT'
            final_reasons = ['mechanical verification failed after maximum rework loops', *failed_descriptions]
            break

        # Mechanical checks passed -> build packet and invoke shared Jev -> fresh Gemini review gate
        pre_review_hashes = dict(mech['source_hashes'])
        packet: dict[str, object] = {
            'schema_version': 1,
            'id': f'{task_id}-attempt-{attempt_num}',
            'kind': kind,
            'objective': full_contract,
            'worker': {
                'model': worker_res['model'],
                'effort': worker_res['effort'],
                'exit_code': worker_res['exit_code'],
                'status': worker_res['status'],
                'response': worker_res['response'],
                'tool_denied': False,
            },
            'checks': mech['checks'],
            'evidence': {
                'focused_diff': mech['focused_diff'],
                'verification_output': next(c['output'] for c in mech['checks'] if c['kind'] == 'tests'),
                'summary': final_summary,
                'source_hashes': pre_review_hashes,
            },
            'open_questions': questions,
        }
        if min_reviewers is not None:
            packet['min_reviewers'] = max(2 if kind == 'quant' else 1, int(min_reviewers))
        elif kind == 'quant':
            packet['min_reviewers'] = 2
        if kind == 'quant' and quant_contract:
            packet['quant_contract'] = quant_contract

        gate_dir = attempt_dir / 'review-gate'
        gate_res = gate.run(packet, gate_dir, slot=effective_slot, triage_only=triage_only)
        final_gate = gate_res

        # Record Jev step in external_steps
        jev_u = gate_res.get('jev_usage') if isinstance(gate_res.get('jev_usage'), dict) else {}
        j_in = int(jev_u.get('input_tokens', 0) or 0)
        j_out = int(jev_u.get('output_tokens', 0) or 0)
        j_tot = int(jev_u.get('total_tokens', j_in + j_out) or (j_in + j_out))
        external_steps.append(
            {
                'step': 'Jev assessment',
                'role': 'jev',
                'attempt': attempt_num,
                'model': JEV_MODEL,
                'usage': {'input_tokens': j_in, 'output_tokens': j_out, 'total_tokens': j_tot},
                'usage_basis': 'API input/output fields; total calculated as their sum',
                'exit_code': 0 if gate_res.get('jev_assessment') and not gate_res.get('jev_error') else 2,
            }
        )

        # Record fresh reviewer steps and verify reviewer context IDs != worker context ID
        reviewer_reused_worker = False
        for rev_idx, rev in enumerate(gate_res.get('reviewers', []), start=1):
            rev_cid = rev.get('conversation_id')
            if rev_cid == worker_cid:
                reviewer_reused_worker = True
            rev_delta, rev_raw, rev_basis = compute_usage_delta(
                rev.get('usage'),
                None,
                'fresh reviewer conversation CLI counter',
            )
            external_steps.append(
                {
                    'step': f'Gemini independent review {rev_idx}',
                    'role': 'reviewer',
                    'attempt': attempt_num,
                    'model': rev.get('model') or GEMINI_MODEL,
                    'effort': rev.get('effort') or 'high',
                    'conversation_id': rev_cid,
                    'usage': rev_delta,
                    'raw_terminal_usage': rev_raw,
                    'usage_basis': rev_basis,
                    'usage_updates_observed': rev.get('usage_updates_observed', 1),
                    'exit_code': rev.get('exit_code', 0),
                    'verdict': rev.get('answer', {}).get('verdict'),
                }
            )

        # Verify workspace files were not mutated during Jev/Gemini review
        post_review_snapshot = snapshot_workspace(workspace)
        post_review_hashes = file_hashes(post_review_snapshot, mech['changed_files'])
        if post_review_hashes != pre_review_hashes or set(post_review_snapshot) != set(mech['snapshot']):
            hashes_unchanged = False
            final_verdict = 'BLOCKED'
            final_reasons = ['workspace files changed during verification/review gate']
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'review_gate',
                    'status': 'BLOCKED',
                    'reasons': final_reasons,
                    'artifact_dir': str(gate_dir),
                }
            )
            break

        if reviewer_reused_worker:
            final_verdict = 'BLOCKED'
            final_reasons = ['reviewer reused worker conversation_id; review is not independent']
            attempts_log.append(
                {
                    'attempt': attempt_num,
                    'stage': 'review_gate',
                    'status': 'BLOCKED',
                    'reasons': final_reasons,
                    'artifact_dir': str(gate_dir),
                }
            )
            break

        gate_verdict = str(gate_res.get('verdict', 'BLOCKED'))
        attempts_log.append(
            {
                'attempt': attempt_num,
                'stage': 'review_gate',
                'status': gate_verdict,
                'route': gate_res.get('route'),
                'reasons': gate_res.get('reasons', []),
                'artifact_dir': str(gate_dir),
            }
        )

        if gate_verdict == 'PASS':
            final_verdict = 'PASS'
            final_reasons = list(gate_res.get('reasons', []))
            risks = [
                r.get('answer', {}).get('residual_risk', '').strip()
                for r in gate_res.get('reviewers', [])
                if r.get('answer', {}).get('residual_risk')
            ]
            final_residual_risk = '; '.join(dict.fromkeys(r for r in risks if r)) or 'Scoped verification passed.'
            break

        if gate_verdict == 'REJECT':
            findings = [
                str(f).strip()
                for r in gate_res.get('reviewers', [])
                for f in r.get('answer', {}).get('findings', [])
                if str(f).strip()
            ]
            if rework_loops < effective_max_reworks:
                rework_loops += 1
                pending_defects = findings or ['Reviewer rejected implementation; strengthen edge-case handling and tests.']
                continue
            final_verdict = 'REJECT'
            final_reasons = findings or ['Reviewer rejected after maximum rework loops']
            final_residual_risk = '; '.join(final_reasons)
            break

        # BLOCKED or PENDING_REVIEW
        final_verdict = gate_verdict
        final_reasons = [
            x
            for x in [
                *(gate_res.get('reasons') or []),
                gate_res.get('jev_error'),
                gate_res.get('review_error'),
            ]
            if x
        ]
        final_residual_risk = '; '.join(final_reasons) or f'Gate returned {gate_verdict}'
        break

    tests_check_final = next((c for c in final_mech.get('checks', []) if c.get('kind') == 'tests'), {})
    reviewers_summary = [
        {
            'conversation_id': r.get('conversation_id'),
            'model': r.get('model'),
            'effort': r.get('effort'),
            'effort_verification': r.get('effort_verification'),
            'verdict': r.get('answer', {}).get('verdict'),
            'confidence': r.get('answer', {}).get('confidence'),
            'findings': r.get('answer', {}).get('findings', []),
            'resolved_questions': r.get('answer', {}).get('resolved_questions', []),
            'residual_risk': r.get('answer', {}).get('residual_risk', ''),
            'usage': r.get('usage', {}),
        }
        for r in (final_gate.get('reviewers') or [])
    ]

    gate_exit_map = {'PASS': 0, 'REJECT': 1, 'BLOCKED': 2, 'PENDING_REVIEW': 3}
    report = {
        'verdict': final_verdict,
        'id': task_id,
        'kind': kind,
        'task': effective_objective,
        'workspace': str(workspace),
        'source_files': final_mech.get('changed_files', []),
        'source_hashes': final_mech.get('source_hashes', {}),
        'source_hashes_unchanged_during_chatgpt_verification_and_review': hashes_unchanged,
        'source_or_tests_written_by_chatgpt': 0,
        'worker_and_reviewer_model': GEMINI_MODEL,
        'effort': 'high',
        'effort_verification': last_effort_verification,
        'rework_loops': rework_loops,
        'max_reworks': effective_max_reworks,
        'unique_tests_passed': int(tests_check_final.get('passed', 0) or 0),
        'coordinator_test_command': ' && '.join(effective_verify),
        'test_exit_code': int(tests_check_final.get('exit_code', 2) if tests_check_final else 2),
        'gate_exit_code': gate_exit_map.get(final_verdict, 2),
        'jev_route': final_gate.get('route', 'blocked' if final_verdict == 'BLOCKED' else None),
        'jev_assessment': final_gate.get('jev_assessment'),
        'jev_error': final_gate.get('jev_error'),
        'reasons': final_reasons,
        'summary': final_summary,
        'residual_risk': final_residual_risk or ('; '.join(final_reasons) if final_reasons else 'Bounded scope only'),
        'reviewers': reviewers_summary,
        'stronger_reviewer_required': bool(kind == 'quant' or require_stronger_reviewer),
        'same_model_is_stronger': False,
        'external_steps': external_steps,
        'usage_totals': aggregate_external_usage(external_steps),
        'attempts': attempts_log,
    }
    report['proof_pack'] = format_proof_pack(report)
    report = gate.sanitize(report)
    gate.save(output_dir / 'workflow-result.json', report)
    (output_dir / 'proof-pack.txt').write_text(report['proof_pack'], encoding='utf-8')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet_file', nargs='?', type=Path, help='Optional path to task packet file')
    parser.add_argument('--packet-file', dest='packet_file_opt', type=Path, help='Path to task packet file')
    parser.add_argument('--id', default='task-1', help='Task identifier')
    parser.add_argument('--objective', default='', help='One-line bounded objective')
    parser.add_argument('--kind', choices=('task', 'quant'), default='task')
    parser.add_argument('--workspace', type=Path, default=Path('.'), help='Target task workspace')
    parser.add_argument('--output-dir', type=Path, required=True, help='New output directory for workflow artifacts')
    parser.add_argument('--scope', nargs='*', default=None, help='Allowed relative write scope paths')
    parser.add_argument('--verify-cmd', action='append', default=None, help='Exact verification command(s)')
    parser.add_argument('--key-slot', type=int, choices=(1, 2, 3), default=None)
    parser.add_argument('--min-reviewers', type=int, default=None)
    parser.add_argument('--max-reworks', type=int, default=MAX_REWORK_LIMIT)
    parser.add_argument('--worker-timeout', type=int, default=300)
    parser.add_argument('--open-question', action='append', default=None)
    parser.add_argument('--quant-contract', type=str, default=None, help='JSON string or path to JSON quant contract')
    parser.add_argument('--require-stronger-reviewer', action='store_true')
    parser.add_argument('--stronger-reviewer-model', default=None)
    parser.add_argument('--authorized-protected', action='store_true')
    parser.add_argument('--triage-only', action='store_true')
    parser.add_argument('--no-resume-worker', action='store_true')
    parser.add_argument('--print-pack', action='store_true', help='Print compact proof pack text instead of JSON summary')
    args = parser.parse_args()

    packet_path = args.packet_file_opt or args.packet_file
    try:
        packet_text = packet_path.read_text(encoding='utf-8') if packet_path else None
        quant_contract = None
        if args.quant_contract:
            qc_path = Path(args.quant_contract)
            raw_qc = qc_path.read_text(encoding='utf-8') if qc_path.is_file() else args.quant_contract
            quant_contract = gate.strict_json(raw_qc)
        result = run_workflow(
            task_id=args.id,
            objective=args.objective,
            workspace=args.workspace,
            output_dir=args.output_dir,
            kind=args.kind,
            packet_text=packet_text,
            scope_files=args.scope,
            verify_cmds=args.verify_cmd,
            key_slot=args.key_slot,
            min_reviewers=args.min_reviewers,
            max_reworks=args.max_reworks,
            worker_timeout=args.worker_timeout,
            open_questions=args.open_question,
            quant_contract=quant_contract,
            require_stronger_reviewer=args.require_stronger_reviewer,
            stronger_reviewer_model=args.stronger_reviewer_model,
            authorized_protected=args.authorized_protected,
            triage_only=args.triage_only,
            resume_worker=not args.no_resume_worker,
        )
    except (OSError, ValueError, TypeError):
        print('Cannot execute workflow or create new output directory; no credentials displayed.', file=sys.stderr)
        return 2

    if args.print_pack:
        sys.stdout.write(result['proof_pack'])
    else:
        compact_out = {
            k: result[k]
            for k in (
                'id',
                'verdict',
                'jev_route',
                'rework_loops',
                'unique_tests_passed',
                'source_files',
                'source_hashes',
                'usage_totals',
                'reasons',
            )
        }
        print(json.dumps(compact_out, ensure_ascii=False))
    return {'PASS': 0, 'REJECT': 1, 'BLOCKED': 2, 'PENDING_REVIEW': 3}[result['verdict']]


if __name__ == '__main__':
    sys.exit(main())
