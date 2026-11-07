#!/usr/bin/env python3
"""Headless workflow runner: worker (agy AGY_MODEL, WORKFLOW_WORKER=devin tool-free swe-1-6-slow, WORKFLOW_WORKER=opencode tool-free OPENCODE_MODEL, or WORKFLOW_WORKER=zai|openrouter tool-free direct HTTPS API) -> mechanical checks -> Jev triage -> fresh AGY_MODEL review(s) -> bounded rework."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
GATE_PATH = SCRIPT_DIR / 'jev_review_gate.py'
_spec = importlib.util.spec_from_file_location('jev_review_gate', GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
DEVIN_PATH = SCRIPT_DIR / 'devin_worker.py'
_devin_spec = importlib.util.spec_from_file_location('devin_worker', DEVIN_PATH)
devin = importlib.util.module_from_spec(_devin_spec)
_devin_spec.loader.exec_module(devin)
OPENCODE_PATH = SCRIPT_DIR / 'opencode_worker.py'
_opencode_spec = importlib.util.spec_from_file_location('opencode_worker', OPENCODE_PATH)
opencode = importlib.util.module_from_spec(_opencode_spec)
_opencode_spec.loader.exec_module(opencode)
FREE_API_PATH = SCRIPT_DIR / 'free_api_worker.py'
_free_api_spec = importlib.util.spec_from_file_location('free_api_worker', FREE_API_PATH)
free_api = importlib.util.module_from_spec(_free_api_spec)
_free_api_spec.loader.exec_module(free_api)

GEMINI_MODEL = gate.GEMINI_MODEL  # compatibility alias for the default AGY_MODEL
DEVIN_MODEL = gate.DEVIN_MODEL
JEV_MODEL = gate.JEV_MODEL
MAX_REWORK_LIMIT = 2
MAX_HANDOFF_CHARS = 1500
MAX_CONTRACT_CHARS = 8000
MAX_DIFF_CHARS = 12000
DEFAULT_VERIFY_TIMEOUT = 120
MIN_VERIFY_TIMEOUT = 1
MAX_VERIFY_TIMEOUT = 1800
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
PYTEST_PASSED_RE = re.compile(
    r'(?m)^\s*(?:=+\s*)?(?:\d+\s+(?!failed\b|errors?\b)\w+,\s*)*(\d+)\s+passed\b(?:(?!\s*,\s*\d+\s+(?:failed|errors?)\b)\s*,|\s+in\s+[\d.]+s\b|\s*=+|\s*$)'
)
GO_PASS_RE = re.compile(r'(?m)^--- PASS:')
ANSI_SGR_RE = re.compile(r'\x1b\[[0-9;]*m')
_NK = r'(?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)'
NODE_SUMMARY_LINE_RE = re.compile(rf'^(?P<prefix>ℹ\ufe0f?|#)[ \t]+(?P<key>{_NK})[ \t]+(?P<value>\S+)[ \t]*$')
NODE_COUNTER_CANDIDATE_RE = re.compile(rf'^\s*(?:ℹ\ufe0f?|#)[ \t]*{_NK}\b.*$')
NODE_INLINE_COUNTER_RE = re.compile(rf'(?:ℹ\ufe0f?|#)[ \t]*{_NK}[ \t]*[:=]?\s*[-+]?\d')
NODE_SPEC_LINE_RE = re.compile(r'^\s*[✔✖ℹ]\ufe0f?(?:[ \t]+|$)')
NODE_SPEC_FAIL_LINE_RE = re.compile(r'^\s*✖\ufe0f?(?:[ \t]+|$)')
NODE_TAP_POINT_RE = re.compile(r'^\s*(?:not )?ok[ \t]+\d+(?:[ \t]+[-#].*|[ \t]*)$')
NODE_TAP_MARKER_RE = re.compile(
    r'^(?:TAP version \d+[ \t]*|1\.\.\d+(?:[ \t]+#.*)?[ \t]*|\s*(?:not )?ok[ \t]+\d+(?:[ \t]+[-#].*|[ \t]*)|\s*#[ \t]+Subtest:.*)$'
)
NODE_TAP_FAIL_LINE_RE = re.compile(r'^\s*not ok[ \t]+\d+\b(?![^\n]*#[ \t]*(?i:TODO|SKIP)\b)')
NONNEG_INT_RE = re.compile(r'^\d+$')
NONNEG_FLOAT_RE = re.compile(r'^\d+(?:\.\d+)?$')
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


def _parse_node_test_count(cleaned: str) -> int | None:
    lines = [ln.rstrip('\r') for ln in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    idxs = [i for i, ln in enumerate(lines) if NODE_COUNTER_CANDIDATE_RE.match(ln)]
    if not idxs:
        if '\u2139' in cleaned or any(
            NODE_SPEC_LINE_RE.match(ln)
            or NODE_TAP_MARKER_RE.match(ln)
            or (not NODE_TAP_POINT_RE.match(ln) and NODE_INLINE_COUNTER_RE.search(ln))
            for ln in lines
        ):
            return 0
        return None
    start = idxs[0]
    if idxs != list(range(start, len(lines))):
        return 0
    pfxs: set[str] = set()
    cnts: dict[str, int] = {}
    seen: set[str] = set()
    for ln in lines[start:]:
        m = NODE_SUMMARY_LINE_RE.match(ln)
        if not m or m['key'] in seen:
            return 0
        pfxs.add('ℹ' if m['prefix'].startswith('ℹ') else '#')
        k, v = m['key'], m['value']
        seen.add(k)
        if k == 'duration_ms':
            if not NONNEG_FLOAT_RE.match(v):
                return 0
        elif NONNEG_INT_RE.match(v):
            cnts[k] = int(v)
        else:
            return 0
    if len(pfxs) != 1 or not {'tests', 'pass', 'fail'} <= set(cnts):
        return 0
    pfx = next(iter(pfxs))
    for ln in lines[:start]:
        if (
            NODE_SPEC_FAIL_LINE_RE.match(ln)
            or NODE_TAP_FAIL_LINE_RE.match(ln)
            or (not NODE_TAP_POINT_RE.match(ln) and NODE_INLINE_COUNTER_RE.search(ln))
            or (pfx == '#' and ('\u2139' in ln or NODE_SPEC_LINE_RE.match(ln)))
            or (pfx == 'ℹ' and NODE_TAP_MARKER_RE.match(ln))
        ):
            return 0
    tot, ok, fail = cnts['tests'], cnts['pass'], cnts['fail']
    canc, skip, todo = cnts.get('cancelled', 0), cnts.get('skipped', 0), cnts.get('todo', 0)
    if tot <= 0 or ok <= 0 or ok > tot or fail != 0 or canc != 0 or ok + fail + canc + skip + todo != tot:
        return 0
    return ok


def parse_test_count(output: str, exit_code: int) -> int:
    if type(exit_code) is not int or exit_code != 0 or not isinstance(output, str):
        return 0
    cleaned = ANSI_SGR_RE.sub('', output)
    node_count = _parse_node_test_count(cleaned)
    if node_count is not None:
        return node_count
    m = UNITTEST_RAN_RE.search(cleaned)
    if m:
        if re.search(r'(?m)^FAILED\b', cleaned):
            return 0
        return int(m.group(1))
    m = PYTEST_PASSED_RE.search(cleaned)
    if m:
        return int(m.group(1))
    go_passes = len(GO_PASS_RE.findall(cleaned))
    if go_passes > 0:
        return go_passes
    if not re.search(r'(?m)^FAILED\b', cleaned) and re.search(r'(?m)^\s*OK(?:\s*\([^)\n]*\))?\s*$', cleaned):
        return 1
    return 0


def validate_verify_timeout(verify_timeout: int) -> int:
    if type(verify_timeout) is not int or not MIN_VERIFY_TIMEOUT <= verify_timeout <= MAX_VERIFY_TIMEOUT:
        raise ValueError(f'verify_timeout must be an integer in {MIN_VERIFY_TIMEOUT}..{MAX_VERIFY_TIMEOUT}')
    return verify_timeout


RTK_FILTER_TIMEOUT = 10


def resolve_rtk_executable() -> str | None:
    candidates = [Path(found) for found in (shutil.which('rtk'),) if found]
    candidates.extend((Path('/opt/homebrew/bin/rtk'), Path('/usr/local/bin/rtk'), Path.home() / '.local/bin' / 'rtk'))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def _is_supported_pytest_output(cleaned: str, exit_code: int, passed_count: int) -> bool:
    if type(exit_code) is not int or exit_code != 0 or type(passed_count) is not int or passed_count <= 0 or not cleaned.strip():
        return False
    if _parse_node_test_count(cleaned) is not None or UNITTEST_RAN_RE.search(cleaned) or GO_PASS_RE.search(cleaned):
        return False
    matches = list(PYTEST_PASSED_RE.finditer(cleaned))
    return bool(matches) and not re.search(r'(?m)^FAILED\b', cleaned) and sum(int(m.group(1)) for m in matches) == passed_count


def _parse_rtk_pytest_passed(filtered_text: str) -> int | None:
    cleaned = ANSI_SGR_RE.sub('', filtered_text or '').strip()
    if not cleaned or re.search(r'(?i)\b(?:no tests collected|failed|errors?|traceback)\b', cleaned):
        return None
    stripped = re.sub(r'(?m)^(\s*)Pytest:\s*', r'\1', cleaned)
    if not PYTEST_PASSED_RE.search(stripped):
        return None
    count = parse_test_count(stripped, 0)
    return count if count > 0 else None


def filter_verification_output(
    raw_output: str,
    exit_code: int,
    passed_count: int,
    *,
    attempt_dir: Path | None = None,
) -> dict[str, object]:
    """Postprocess verified passing pytest logs via `rtk pipe -f pytest`; fall back to raw on any mismatch or error."""
    raw_text = str(raw_output or '')
    cleaned = ANSI_SGR_RE.sub('', raw_text)
    effective = raw_text
    rtk_applied = False
    filtered_passed: int | None = None
    rtk_exe: str | None = None
    eligible = _is_supported_pytest_output(cleaned, exit_code, passed_count)
    if type(exit_code) is not int or exit_code != 0 or type(passed_count) is not int or passed_count <= 0:
        reason = 'failing or zero-count test output kept raw'
    elif not eligible:
        reason = 'unsupported test runner format (not pytest); raw output retained'
    else:
        rtk_exe = resolve_rtk_executable()
        if not rtk_exe:
            reason = 'rtk executable unavailable; raw output retained'
        else:
            try:
                res = subprocess.run(
                    [rtk_exe, 'pipe', '-f', 'pytest'],
                    input=raw_text,
                    text=True,
                    capture_output=True,
                    timeout=RTK_FILTER_TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                reason = 'rtk filter timed out; raw output retained'
            except (OSError, ValueError):
                reason = 'rtk filter execution failed; raw output retained'
            else:
                candidate = (res.stdout or '').strip()
                if res.returncode != 0:
                    reason = f'rtk filter exited {res.returncode}; raw output retained'
                elif not candidate:
                    reason = 'rtk filter returned empty output; raw output retained'
                else:
                    filtered_passed = _parse_rtk_pytest_passed(candidate)
                    if filtered_passed is None:
                        reason = 'rtk output unparseable or reports no tests/failure; raw output retained'
                    elif filtered_passed != passed_count:
                        reason = f'rtk filtered passed count ({filtered_passed}) did not match raw passed count ({passed_count}); raw output retained'
                    else:
                        rtk_applied = True
                        effective = candidate
                        reason = 'applied rtk pipe -f pytest with matching passed count'
    raw_b = raw_text.encode('utf-8')
    eff_b = effective.encode('utf-8')
    meta: dict[str, object] = {
        'rtk_applied': rtk_applied,
        'applied': rtk_applied,
        'reason': reason,
        'filter': 'rtk pipe -f pytest' if eligible else None,
        'rtk_executable': rtk_exe,
        'raw_exit_code': exit_code,
        'raw_passed': passed_count,
        'filtered_passed': filtered_passed,
        'raw_chars': len(raw_text),
        'filtered_chars': len(effective),
        'saved_chars': len(raw_text) - len(effective),
        'raw_bytes': len(raw_b),
        'filtered_bytes': len(eff_b),
        'saved_bytes': len(raw_b) - len(eff_b),
        'raw_sha256': hashlib.sha256(raw_b).hexdigest(),
        'filtered_sha256': hashlib.sha256(eff_b).hexdigest(),
        'raw_output': raw_text,
        'filtered_output': effective,
    }
    if attempt_dir is not None:
        attempt_dir = Path(attempt_dir)
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw_path = attempt_dir / 'verification-raw.txt'
        filt_path = attempt_dir / 'verification-filtered.txt'
        raw_path.write_text(gate.redact(raw_text), encoding='utf-8')
        filt_path.write_text(gate.redact(effective), encoding='utf-8')
        meta['raw_artifact'] = str(raw_path)
        meta['filtered_artifact'] = str(filt_path)
    return meta


postprocess_verification_output = filter_verification_output


def run_mechanical_checks(
    workspace: Path,
    baseline_snapshot: dict[str, bytes],
    scope_files: list[str],
    verify_cmds: list[str],
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT,
    attempt_dir: Path | None = None,
) -> dict[str, object]:
    effective_verify_timeout = validate_verify_timeout(verify_timeout)
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
            res = subprocess.run(
                argv,
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=effective_verify_timeout,
                check=False,
            )
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
        test_outputs.append(
            'Diagnostic: no recognized executed-test summary found '
            '(exit 0 with 0 executed tests counted; could be unsupported custom output format or truly no tests). '
            'Run real supported tests (unittest, pytest, Go, or Node), including '
            '`python3 -B -m unittest discover -s tests -v` for static integrity assertions.'
        )
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
    rtk_meta = filter_verification_output(tests_check['output'], test_exit, total_passed, attempt_dir=attempt_dir)
    rtk_summary = {k: v for k, v in rtk_meta.items() if k not in ('raw_output', 'filtered_output')}
    if rtk_meta['rtk_applied']:
        rtk_summary['filtered_summary'] = rtk_meta['filtered_output']
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
        'rtk_verification': rtk_summary,
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
    model = gate.selected_model()  # raises before any external call if unsupported
    settings_path = attempt_dir / 'worker-settings.json'
    settings = build_worker_settings(workspace, scope_files, verify_cmds)
    gate.save(settings_path, settings)
    (attempt_dir / 'worker-prompt.txt').write_text(prompt, encoding='utf-8')

    try:
        executable = gate.resolve_executable('agy')
    except OSError:
        gate.save(
            attempt_dir / 'worker-invocation.json',
            {'requested_executable': 'agy', 'state': 'UNAVAILABLE', 'model': model, 'effort': 'high', 'exit_code': None},
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
        '--model', model,
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
            'model': model,
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
            {'timed_out': True, 'exit_code': None, 'model_argument': model, 'effort_argument': 'high', 'duration_seconds': duration},
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
    if model_actual != model:
        block_reasons.append(f'unexpected worker model: {model_actual!r}')
    if effort_field != 'high':
        block_reasons.append(f'unexpected worker effort: {effort_field!r}')
    if perm_mode != 'request-review':  # missing init.permission_mode also blocks
        block_reasons.append(f'unexpected worker permission_mode: {perm_mode!r}')
    if status != 'SUCCESS':
        block_reasons.append(f'worker terminal status {status!r}')
    finished_reason = f"{terminal.get('finished_reason') or ''} {terminal.get('finish_reason') or ''}".lower()
    if 'timeout' in finished_reason or 'timed_out' in finished_reason:
        block_reasons.append('worker terminal finished_reason reports timeout (BLOCKED even if status is SUCCESS)')
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
        'requested_model': model,
        'model': model_actual,
        'effort': 'high' if effort_field == 'high' else effort_field,
        'effort_verification': effort_verification,
        'permission_mode': perm_mode,
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


def build_gate_checks(checks: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {**c, 'output': f"See evidence.verification_output (utf-8 sha256: {hashlib.sha256(str(c.get('output') or '').encode('utf-8')).hexdigest()})"}
        if c.get('kind') == 'tests'
        else dict(c)
        for c in checks
    ]


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
    devin_totals = {k: 0 for k in devin.FINAL_METRIC_KEYS}
    devin_sessions = 0
    opencode_totals = {k: 0 for k in opencode.OPENCODE_METRIC_KEYS}
    opencode_sessions = 0
    opencode_steps_seen = 0
    opencode_unverified = False
    api_buckets = {
        'zai': {
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
            'cached_tokens': 0,
            'reasoning_tokens': 0,
            'total_steps': 0,
            'sessions': 0,
            'steps_seen': 0,
            'unverified': False,
        },
        'openrouter': {
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'total_tokens': 0,
            'cached_tokens': 0,
            'reasoning_tokens': 0,
            'total_steps': 0,
            'sessions': 0,
            'steps_seen': 0,
            'unverified': False,
        },
    }

    for step in external_steps:
        usage = step.get('usage') if isinstance(step.get('usage'), dict) else {}
        updates += int(step.get('usage_updates_observed', 1) or 1)
        if step.get('provider') == 'devin':
            if usage and all(type(usage.get(k)) is int and usage[k] >= 0 for k in devin.FINAL_METRIC_KEYS):
                devin_sessions += 1
                for k in devin.FINAL_METRIC_KEYS:
                    devin_totals[k] += usage[k]
        elif step.get('provider') == 'opencode':
            opencode_steps_seen += 1
            if (
                usage
                and all(type(usage.get(k)) is int and usage[k] >= 0 for k in opencode.OPENCODE_METRIC_KEYS)
                and usage['input_tokens'] > 0
                and usage['output_tokens'] > 0
                and usage['total_steps'] > 0
                and usage['total_tokens']
                == (
                    usage['input_tokens']
                    + usage['output_tokens']
                    + usage['reasoning_tokens']
                    + usage['cache_read_tokens']
                    + usage['cache_write_tokens']
                )
            ):
                opencode_sessions += 1
                for k in opencode.OPENCODE_METRIC_KEYS:
                    opencode_totals[k] += usage[k]
            else:
                opencode_unverified = True
        elif step.get('provider') in ('zai', 'openrouter'):
            bucket = api_buckets[step.get('provider')]
            bucket['steps_seen'] += 1
            required_keys = ['prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens', 'total_steps']
            if usage and all(k in usage for k in required_keys):
                values = [usage[k] for k in required_keys]
                if all(type(v) is int and v >= 0 for v in values):
                    prompt, completion, total, cached, reasoning, total_steps = values
                    if (
                        prompt > 0
                        and completion > 0
                        and total_steps > 0
                        and total == prompt + completion
                        and cached <= prompt
                        and reasoning <= completion
                    ):
                        bucket['sessions'] += 1
                        bucket['prompt_tokens'] += prompt
                        bucket['completion_tokens'] += completion
                        bucket['total_tokens'] += total
                        bucket['cached_tokens'] += cached
                        bucket['reasoning_tokens'] += reasoning
                        bucket['total_steps'] += total_steps
                    else:
                        bucket['unverified'] = True
                else:
                    bucket['unverified'] = True
            else:
                bucket['unverified'] = True
        elif step.get('model') == JEV_MODEL or step.get('role') == 'jev':
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

    opencode_summary = (
        {k: 'unknown' for k in opencode.OPENCODE_METRIC_KEYS}
        if (opencode_steps_seen > 0 and (opencode_unverified or opencode_sessions == 0))
        else dict(opencode_totals)
    )
    zai_summary = (
        {k: 'unknown' for k in api_buckets['zai'] if k not in ('sessions', 'steps_seen', 'unverified')}
        if (api_buckets['zai']['steps_seen'] > 0 and (api_buckets['zai']['unverified'] or api_buckets['zai']['sessions'] == 0))
        else {k: api_buckets['zai'][k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens', 'total_steps')}
    )
    openrouter_summary = (
        {k: 'unknown' for k in api_buckets['openrouter'] if k not in ('sessions', 'steps_seen', 'unverified')}
        if (api_buckets['openrouter']['steps_seen'] > 0 and (api_buckets['openrouter']['unverified'] or api_buckets['openrouter']['sessions'] == 0))
        else {k: api_buckets['openrouter'][k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cached_tokens', 'reasoning_tokens', 'total_steps')}
    )
    combined_api_total = 0
    if isinstance(zai_summary.get('total_tokens'), int):
        combined_api_total += zai_summary['total_tokens']
    if isinstance(openrouter_summary.get('total_tokens'), int):
        combined_api_total += openrouter_summary['total_tokens']
    combined_api_total_known = not any(
        bucket['steps_seen'] > 0 and (bucket['unverified'] or bucket['sessions'] == 0)
        for bucket in api_buckets.values()
    )
    combined_non_cache_total = (
        gemini_total + jev_total + combined_api_total
        if combined_api_total_known
        else 'unknown'
    )
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
        'devin': {
            'sessions_with_verified_usage': devin_sessions,
            **devin_totals,
            'basis': devin.USAGE_BASIS,
        },
        'opencode': {
            'sessions_with_verified_usage': opencode_sessions,
            **opencode_summary,
            'basis': opencode.OPENCODE_USAGE_BASIS,
        },
        'zai': {
            'sessions_with_verified_usage': api_buckets['zai']['sessions'],
            **zai_summary,
        },
        'openrouter': {
            'sessions_with_verified_usage': api_buckets['openrouter']['sessions'],
            **openrouter_summary,
        },
        'legacy_key_note': "'gemini' is the legacy key for agy usage of the selected AGY_MODEL only; Devin, OpenCode, zai, and openrouter usage appear only under their respective provider keys. API cached tokens are a subset of prompt_tokens and are not double-counted in combined_non_cache_total_tokens.",
        'combined_non_cache_total_tokens': combined_non_cache_total,
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
    dev_u = usage_tot.get('devin', {})
    opc_u = usage_tot.get('opencode', {})
    if report.get('worker_provider') == 'devin':
        usage_line = (
            f"USAGE: worker=devin:{report.get('worker_model')} (ATIF-verified, Normal, 0 tools, no effort claim) "
            f"sessions={dev_u.get('sessions_with_verified_usage', 0)} prompt={dev_u.get('total_prompt_tokens', 0)} "
            f"completion={dev_u.get('total_completion_tokens', 0)} cached={dev_u.get('total_cached_tokens', 0)}; "
            f"reviewer=agy:{report.get('reviewer_model')} high agy_total={gem_u.get('total_tokens', 0)} "
            f"(in={gem_u.get('input_tokens', 0)}, out={gem_u.get('output_tokens', 0)}) jev_total={jev_u.get('total_tokens', 0)}"
        )
    elif report.get('worker_provider') == 'opencode':
        usage_line = (
            f"USAGE: worker=opencode:{report.get('worker_model')} (export-verified, deny-all, 0 tools, no effort claim) "
            f"sessions={opc_u.get('sessions_with_verified_usage', 0)} in={opc_u.get('input_tokens', 'unknown')} "
            f"out={opc_u.get('output_tokens', 'unknown')} reasoning={opc_u.get('reasoning_tokens', 'unknown')} "
            f"cache_read={opc_u.get('cache_read_tokens', 'unknown')} cache_write={opc_u.get('cache_write_tokens', 'unknown')} "
            f"total={opc_u.get('total_tokens', 'unknown')}; "
            f"reviewer=agy:{report.get('reviewer_model')} high agy_total={gem_u.get('total_tokens', 0)} "
            f"(in={gem_u.get('input_tokens', 0)}, out={gem_u.get('output_tokens', 0)}) jev_total={jev_u.get('total_tokens', 0)}"
        )
    else:
        usage_line = (
            f"USAGE: agy_model={report.get('worker_and_reviewer_model')} agy_total={gem_u.get('total_tokens', 0)} "
            f"(in={gem_u.get('input_tokens', 0)}, out={gem_u.get('output_tokens', 0)}, cache_read={gem_u.get('cache_read_tokens', 0)}) "
            f"jev_total={jev_u.get('total_tokens', 0)}"
        )
    ctx_opt = report.get('context_optimization') or {}
    ctx_line = (
        f"CONTEXT: mode={report.get('context_mode')} prompt_chars={report.get('prompt_chars')} "
        f"rtk_applied={ctx_opt.get('rtk_applied', False)} "
        f"verify_bytes={ctx_opt.get('filtered_verification_bytes', 0)}/{ctx_opt.get('raw_verification_bytes', 0)} "
        f"({ctx_opt.get('rtk_reason') or 'not run'})"
    )
    lines = [
        f"TASK: {report.get('id')} — {report.get('task')}",
        f"VERDICT: {report.get('verdict')} (rework_loops={report.get('rework_loops')})",
        f"SCOPE: {scope_str}",
        f"HASHES: {hashes_str} (unchanged_during_review={report.get('source_hashes_unchanged_during_chatgpt_verification_and_review')})",
        f"CHANGE: {report.get('summary') or 'none'}",
        f"TESTS: `{report.get('coordinator_test_command')}` exit={report.get('test_exit_code')} passed={report.get('unique_tests_passed')}",
        ctx_line,
        f"JEV: {jev_str}",
        f"REVIEW: {rev_str}",
        usage_line,
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
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT,
    open_questions: list[str] | None = None,
    quant_contract: dict[str, str] | None = None,
    require_stronger_reviewer: bool = False,
    stronger_reviewer_model: str | None = None,
    authorized_protected: bool = False,
    triage_only: bool = False,
    resume_worker: bool = True,
    worker_provider: str | None = None,
    opencode_model: str | None = None,
    context_mode: str | None = None,
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
    try:
        worker_kind = opencode.resolve_worker(worker_provider)  # WORKFLOW_WORKER: agy (default) | devin | opencode
    except ValueError as exc:  # unknown provider: no worker/Jev/reviewer call
        worker_kind = None
        preflight_blockers.append(str(exc))
    is_devin = worker_kind == 'devin'
    is_opencode = worker_kind == 'opencode'
    is_free_api = worker_kind in ('zai', 'openrouter')
    opencode_full_model: str | None = None
    free_api_full_model: str | None = None
    if is_opencode:
        try:
            opencode_full_model, _, _ = opencode.resolve_opencode_model(opencode_model)
        except (ValueError, opencode.OpenCodeBlocked) as exc:
            preflight_blockers.append(str(exc))
    if is_free_api:
        try:
            if worker_kind == 'zai':
                free_api_full_model = free_api.resolve_zai_model()
            elif worker_kind == 'openrouter':
                free_api_full_model = free_api.resolve_openrouter_model()[0]
        except (ValueError, free_api.FreeApiBlocked) as exc:
            preflight_blockers.append(str(exc))
    try:
        agy_model = gate.selected_model()  # always validated: selects fresh AGY reviewers (and the agy worker)
    except ValueError as exc:  # unsupported AGY_MODEL: no worker/Jev/reviewer call
        agy_model = None
        preflight_blockers.append(str(exc))
    try:
        effective_context_mode = devin.resolve_context_mode(context_mode)  # WORKFLOW_CONTEXT: lean (default) | compact | full
    except ValueError as exc:  # unsupported WORKFLOW_CONTEXT: no worker/Jev/reviewer call
        effective_context_mode = None
        preflight_blockers.append(str(exc))
    is_external_non_agy = is_devin or is_opencode or is_free_api
    identity = {
        'worker_provider': worker_kind,
        'worker_model': (
            DEVIN_MODEL if is_devin else (opencode_full_model if is_opencode else (free_api_full_model if is_free_api else agy_model))
        ) if worker_kind else None,
        'worker_effort': None if is_external_non_agy else 'high',
        'reviewer_provider': 'agy',
        'reviewer_model': agy_model,
        'reviewer_effort': 'high',
        # Legacy key: only meaningful when one agy model is both worker and reviewer.
        'worker_and_reviewer_model': None if is_external_non_agy else agy_model,
        'effort': None if is_external_non_agy else 'high',
        'context_mode': effective_context_mode,
    }
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
    try:
        effective_verify_timeout = validate_verify_timeout(verify_timeout)
    except ValueError as exc:
        effective_verify_timeout = DEFAULT_VERIFY_TIMEOUT
        preflight_blockers.append(str(exc))
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
            f'stronger reviewer required; this runner only executes the selected AGY_MODEL ({agy_model}) as reviewer '
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
            **identity,
            'prompt_chars': None,
            'context_optimization': {
                'context_mode': effective_context_mode,
                'worker_provider': worker_kind,
                'prompt_chars': None,
                'rtk_applied': False,
                'rtk_reason': 'preflight blocked before verification',
                'raw_verification_bytes': 0,
                'filtered_verification_bytes': 0,
                'raw_evidence_authoritative': True,
            },
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
    last_prompt_chars: int | None = None
    last_context_mode = effective_context_mode
    last_effort_verification = (
        'Devin worker: no effort claim (swe-1-6-slow); AGY reviewers: explicit --effort high plus -high slug'
        if is_devin
        else (
            f'OpenCode worker: no effort claim ({opencode_full_model}); AGY reviewers: explicit --effort high plus -high slug'
            if is_opencode
            else 'explicit --effort high plus init.model High slug; init omits separate effort'
        )
    )

    for attempt_idx in range(effective_max_reworks + 1):
        attempt_num = attempt_idx + 1
        attempt_dir = output_dir / f'attempt-{attempt_num}'
        attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if is_free_api:
            step_label = f'free_api_{worker_kind}_{attempt_num}'
            worker_res = free_api.run_free_api_attempt(
                provider=worker_kind,
                model=free_api_full_model,
                workspace=workspace,
                attempt_dir=attempt_dir,
                contract=full_contract,
                scope_files=effective_scope,
                verify_cmds=effective_verify,
                timeout_seconds=worker_timeout,
                feedback=devin.build_feedback(pending_defects) if attempt_idx > 0 else None,
                open_questions=questions,
                attempt=attempt_num,
                max_attempts=effective_max_reworks + 1,
                context_mode=effective_context_mode
            )
            attempt_context_mode = worker_res.get('context_mode', effective_context_mode)
            attempt_prompt_chars = worker_res.get('prompt_chars', 0)
        elif is_devin:
            # Every Devin attempt is a fresh session: original contract + current scoped source + concise feedback.
            step_label = (
                f'Devin worker generation ({DEVIN_MODEL})' if attempt_idx == 0
                else f'Devin worker rework {attempt_idx} ({DEVIN_MODEL}, fresh session)'
            )
            worker_res = devin.run_devin_attempt(
                workspace=workspace,
                attempt_dir=attempt_dir,
                contract=full_contract,
                scope_files=effective_scope,
                verify_cmds=effective_verify,
                timeout_seconds=worker_timeout,
                feedback=devin.build_feedback(pending_defects) if attempt_idx > 0 else None,
                open_questions=questions,
                attempt=attempt_num,
                max_attempts=effective_max_reworks + 1,
                context_mode=effective_context_mode,
            )
            attempt_context_mode = worker_res.get('context_mode') or effective_context_mode
            attempt_prompt_chars = worker_res.get('prompt_chars')
        elif is_opencode:
            # Every OpenCode attempt is a fresh session: original contract + current scoped source + concise feedback.
            step_label = (
                f'OpenCode worker generation ({opencode_full_model})' if attempt_idx == 0
                else f'OpenCode worker rework {attempt_idx} ({opencode_full_model}, fresh session)'
            )
            worker_res = opencode.run_opencode_attempt(
                workspace=workspace,
                attempt_dir=attempt_dir,
                contract=full_contract,
                scope_files=effective_scope,
                verify_cmds=effective_verify,
                timeout_seconds=worker_timeout,
                feedback=devin.build_feedback(pending_defects) if attempt_idx > 0 else None,
                open_questions=questions,
                attempt=attempt_num,
                max_attempts=effective_max_reworks + 1,
                context_mode=effective_context_mode,
                requested_model=opencode_full_model,
            )
            attempt_context_mode = worker_res.get('context_mode') or effective_context_mode
            attempt_prompt_chars = worker_res.get('prompt_chars')
        else:
            if attempt_idx == 0:
                prompt = build_initial_prompt(packet_text, effective_objective, effective_scope, effective_verify)
                step_label = f'Worker implementation ({agy_model})'
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
                step_label = f'Worker rework {attempt_idx} ({agy_model})'

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
            attempt_context_mode = effective_context_mode
            attempt_prompt_chars = len(prompt)

            if isinstance(worker_res.get('effort_verification'), str):
                last_effort_verification = str(worker_res['effort_verification'])

        last_context_mode = str(attempt_context_mode) if attempt_context_mode else effective_context_mode
        if isinstance(attempt_prompt_chars, int):
            last_prompt_chars = attempt_prompt_chars

        if is_free_api:
            external_steps.append({
                'step': step_label,
                'role': 'worker',
                'provider': worker_kind,
                'attempt': attempt_num,
                'requested_model': free_api_full_model,
                'model': worker_res.get('model'),
                'effort': None,
                'context_mode': last_context_mode,
                'prompt_chars': last_prompt_chars,
                'conversation_id': worker_res.get('request_id'),
                'usage': worker_res.get('usage', {}),
                'usage_basis': worker_res.get('usage_basis', ''),
                'usage_updates_observed': worker_res.get('usage_updates_observed', 0),
                'exit_code': worker_res.get('exit_code'),
                'duration_seconds': worker_res.get('duration_seconds', 0.0),
                'status': 'BLOCKED' if worker_res.get('blocked', False) else 'VERIFIED',
                'files_applied': worker_res.get('files_written', []),
                'reason': worker_res.get('reason')
            })
        elif is_devin:
            external_steps.append(
                {
                    'step': step_label,
                    'role': 'worker',
                    'provider': 'devin',
                    'attempt': attempt_num,
                    'requested_model': DEVIN_MODEL,
                    'model': worker_res.get('model'),  # None unless ATIF-verified
                    'effort': None,
                    'context_mode': last_context_mode,
                    'prompt_chars': last_prompt_chars,
                    'conversation_id': worker_res.get('session_id'),
                    'usage': worker_res.get('usage', {}),
                    'usage_basis': worker_res.get('usage_basis', devin.USAGE_BASIS),
                    'usage_updates_observed': worker_res.get('usage_updates_observed', 0),
                    'exit_code': worker_res.get('exit_code'),
                    'duration_seconds': worker_res.get('duration_seconds', 0.0),
                    'status': 'BLOCKED' if worker_res.get('blocked') else 'VERIFIED',
                    'files_applied': worker_res.get('files_written', []),
                    'reason': worker_res.get('reason'),
                }
            )
        elif is_opencode:
            external_steps.append(
                {
                    'step': step_label,
                    'role': 'worker',
                    'provider': 'opencode',
                    'attempt': attempt_num,
                    'requested_model': opencode_full_model,
                    'model': worker_res.get('model'),  # None unless stream+export-verified
                    'effort': None,
                    'context_mode': last_context_mode,
                    'prompt_chars': last_prompt_chars,
                    'conversation_id': worker_res.get('session_id'),
                    'usage': worker_res.get('usage', {}),
                    'usage_basis': worker_res.get('usage_basis', opencode.OPENCODE_USAGE_BASIS),
                    'usage_updates_observed': worker_res.get('usage_updates_observed', 0),
                    'exit_code': worker_res.get('exit_code'),
                    'export_exit_code': worker_res.get('export_exit_code'),
                    'duration_seconds': worker_res.get('duration_seconds', 0.0),
                    'status': 'BLOCKED' if worker_res.get('blocked') else 'VERIFIED',
                    'files_applied': worker_res.get('files_written', []),
                    'reason': worker_res.get('reason'),
                }
            )
        else:
            external_steps.append(
                {
                    'step': step_label,
                    'role': 'worker',
                    'provider': 'agy',
                    'attempt': attempt_num,
                    'model': worker_res.get('model') or agy_model,
                    'effort': worker_res.get('effort') or 'high',
                    'context_mode': last_context_mode,
                    'prompt_chars': last_prompt_chars,
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
                    'context_mode': last_context_mode,
                    'prompt_chars': last_prompt_chars,
                    'reasons': final_reasons,
                    'artifact_dir': str(attempt_dir),
                }
            )
            break

        worker_cid = str(worker_res['request_id'] if is_free_api else (worker_res['session_id'] if (is_devin or is_opencode) else worker_res['conversation_id']))
        final_summary = summarize_worker_response(str(worker_res.get('response') or ''))

        # Run coordinator mechanical verification
        mech = run_mechanical_checks(
            workspace,
            baseline_snapshot,
            effective_scope,
            effective_verify,
            verify_timeout=effective_verify_timeout,
            attempt_dir=attempt_dir,
        )
        final_mech = mech
        rtk_attempt = mech.get('rtk_verification', {})
        gate.save(
            attempt_dir / 'context-optimization.json',
            {
                'attempt': attempt_num,
                'worker_provider': worker_kind,
                'context_mode': last_context_mode,
                'prompt_chars': last_prompt_chars,
                'rtk': rtk_attempt,
            },
        )
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

        # Mechanical checks passed -> build packet and invoke shared Jev -> fresh AGY_MODEL review gate
        pre_review_hashes = dict(mech['source_hashes'])
        if is_free_api:
            packet_worker = {
                'provider': worker_res['provider'],
                'model': worker_res['model'],
                'exit_code': worker_res['exit_code'],
                'proof_status': worker_res['proof_status'],
                'request_id': worker_res['request_id'],
                'response': worker_res['response'],
                'tool_calls': worker_res.get('tool_calls'),
                'tool_denied': worker_res.get('tool_denied'),
                'provider_id': worker_res.get('provider_id'),
                'model_id': worker_res.get('model_id')
            }
            worker_proof = dict(worker_res['worker_proof'])
        elif is_devin:
            packet_worker: dict[str, object] = {
                'provider': 'devin',
                'model': worker_res['model'],
                'exit_code': worker_res['exit_code'],
                'proof_status': worker_res['proof_status'],
                'response': worker_res['response'],
                'tool_denied': False,
                'tool_calls': worker_res['tool_calls'],
            }
            worker_proof: dict[str, object] = {
                'source': 'coordinator-parsed Devin ATIF export and stdout (not worker narrative)',
                'provider': 'devin',
                'requested_model': DEVIN_MODEL,
                'actual_generation_model': worker_res['model'],
                'agent_model_name': worker_res.get('agent_model_name'),
                'permission_mode': worker_res.get('permission_mode'),
                'schema_version': worker_res.get('schema_version'),
                'exit_code': worker_res['exit_code'],
                'tool_calls': worker_res['tool_calls'],
                'tool_denied': False,
                'stdout_matches_export': worker_res.get('stdout_matches_export'),
                'stdout_normalization': worker_res.get('stdout_normalization'),
                'session_id': worker_res.get('session_id'),
                'generation_steps': worker_res.get('generation_steps'),
                'applied_files': worker_res.get('files_written', []),
                'application': 'coordinator mechanically applied model-authored UTF-8 bytes to exact declared paths after proof checks',
            }
        elif is_opencode:
            packet_worker = {
                'provider': 'opencode',
                'requested_model': worker_res['requested_model'],
                'model': worker_res['model'],
                'provider_id': worker_res['provider_id'],
                'model_id': worker_res['model_id'],
                'exit_code': worker_res['exit_code'],
                'export_exit_code': worker_res['export_exit_code'],
                'proof_status': worker_res['proof_status'],
                'response': worker_res['response'],
                'tool_denied': False,
                'tool_calls': worker_res['tool_calls'],
            }
            worker_proof = {
                'source': 'coordinator-parsed OpenCode JSON stream, session export, and isolated config (not worker narrative)',
                'provider': 'opencode',
                'requested_model': worker_res['requested_model'],
                'actual_generation_model': worker_res['model'],
                'provider_id': worker_res['provider_id'],
                'model_id': worker_res['model_id'],
                'exit_code': worker_res['exit_code'],
                'export_exit_code': worker_res['export_exit_code'],
                'tool_calls': worker_res['tool_calls'],
                'tool_denied': False,
                'stream_matches_export': worker_res['stream_matches_export'],
                'finish_reason': worker_res['finish_reason'],
                'session_id': worker_res['session_id'],
                'generation_steps': worker_res['generation_steps'],
                'config_proof_status': worker_res['config_proof_status'],
                'config_proof': worker_res['config_proof'],
                'config_verified': worker_res['config_verified'],
                'isolated_xdg': worker_res['isolated_xdg'],
                'pure_mode': worker_res['pure_mode'],
                'prelaunch_config_sha256': worker_res['prelaunch_config_sha256'],
                'postrun_config_sha256': worker_res['postrun_config_sha256'],
                'raw_stdout_sha256': worker_res['raw_stdout_sha256'],
                'export_sha256': worker_res['export_sha256'],
                'final_response_sha256': worker_res['final_response_sha256'],
                'usage': worker_res['usage'],
                'applied_files': worker_res.get('files_written', []),
                'application': 'coordinator mechanically applied model-authored UTF-8 bytes to exact declared paths after proof checks',
            }
        else:
            packet_worker = {
                'model': worker_res['model'],
                'effort': worker_res['effort'],
                'exit_code': worker_res['exit_code'],
                'status': worker_res['status'],
                'response': worker_res['response'],
                'tool_denied': False,
            }
            worker_proof = {
                'source': 'coordinator-parsed agy init/result metadata (not worker narrative)',
                'requested_model': worker_res.get('requested_model'),
                'actual_init_model': worker_res['model'],
                'permission_mode': worker_res.get('permission_mode'),
                'effort': worker_res['effort'],
                'effort_verification': worker_res.get('effort_verification'),
                'status': worker_res['status'],
                'exit_code': worker_res['exit_code'],
                'tool_denied': bool(worker_res.get('tool_denied')),
                'conversation_id': worker_res.get('conversation_id'),
            }
        packet: dict[str, object] = {
            'schema_version': 1,
            'id': f'{task_id}-attempt-{attempt_num}',
            'kind': kind,
            'objective': full_contract,
            'worker': packet_worker,
            'checks': build_gate_checks(mech['checks']),
            'evidence': {
                'focused_diff': mech['focused_diff'],
                'verification_output': next(c['output'] for c in mech['checks'] if c['kind'] == 'tests'),
                'summary': final_summary,
                'summary_label': 'short redacted excerpt of the worker handoff; not the authoritative full response',
                'full_worker_handoff': worker_res['response'],
                'worker_proof': worker_proof,
                'source_workspace': str(workspace),
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
                'step': 'Jev đánh giá',
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
                    'step': f'Independent review {rev_idx} ({agy_model})',
                    'role': 'reviewer',
                    'provider': 'agy',
                    'attempt': attempt_num,
                    'model': rev.get('model') or agy_model,
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

        # Verify workspace files were not mutated during Jev/reviewer gate
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
    rtk_final = final_mech.get('rtk_verification', {}) if isinstance(final_mech.get('rtk_verification'), dict) else {}
    identity['context_mode'] = last_context_mode
    context_opt_summary = {
        'context_mode': last_context_mode,
        'worker_provider': worker_kind,
        'prompt_chars': last_prompt_chars,
        'rtk_applied': bool(rtk_final.get('rtk_applied', False)),
        'rtk_reason': rtk_final.get('reason', 'not run'),
        'raw_verification_chars': int(rtk_final.get('raw_chars', 0) or 0),
        'filtered_verification_chars': int(rtk_final.get('filtered_chars', 0) or 0),
        'saved_verification_chars': int(rtk_final.get('saved_chars', 0) or 0),
        'raw_verification_bytes': int(rtk_final.get('raw_bytes', 0) or 0),
        'filtered_verification_bytes': int(rtk_final.get('filtered_bytes', 0) or 0),
        'saved_verification_bytes': int(rtk_final.get('saved_bytes', 0) or 0),
        'raw_verification_sha256': rtk_final.get('raw_sha256'),
        'filtered_verification_sha256': rtk_final.get('filtered_sha256'),
        'raw_passed': int(rtk_final.get('raw_passed', 0) or 0),
        'filtered_passed': rtk_final.get('filtered_passed'),
        'raw_artifact': rtk_final.get('raw_artifact'),
        'filtered_artifact': rtk_final.get('filtered_artifact'),
        'filtered_summary': rtk_final.get('filtered_summary'),
        'raw_evidence_authoritative': True,
        'rtk': rtk_final,
    }
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
        **identity,
        'prompt_chars': last_prompt_chars,
        'context_optimization': context_opt_summary,
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


CHATGPT_REVIEW_PATH = SCRIPT_DIR / 'chatgpt_review.py'
CHATGPT_REVIEW_MODES = ('off', 'baseline', 'scoped')
_chatgpt_review_module = None


def load_chatgpt_review():
    global _chatgpt_review_module
    if _chatgpt_review_module is None:
        cr_spec = importlib.util.spec_from_file_location('chatgpt_review_helper', CHATGPT_REVIEW_PATH)
        if cr_spec is None or cr_spec.loader is None:
            raise ValueError('BLOCKED: chatgpt_review.py helper unavailable')
        mod = importlib.util.module_from_spec(cr_spec)
        cr_spec.loader.exec_module(mod)
        _chatgpt_review_module = mod
    return _chatgpt_review_module


def _chatgpt_review_child_dir(output_dir: Path) -> Path:
    wf_resolved = Path(output_dir).resolve()
    return wf_resolved.parent / f'{wf_resolved.name}-chatgpt-review'


def _preflight_chatgpt_review_paths(workspace: Path, output_dir: Path) -> Path:
    if os.path.islink(workspace) or os.path.islink(output_dir):
        raise ValueError('BLOCKED: workspace and output_dir must not be symlinks')
    ws_resolved = Path(workspace).resolve()
    if not ws_resolved.is_dir():
        raise ValueError('BLOCKED: workspace is not an existing directory')
    if os.path.lexists(output_dir):
        raise ValueError('BLOCKED: output_dir must not already exist')
    wf_resolved = Path(output_dir).resolve()
    if wf_resolved == ws_resolved or ws_resolved in wf_resolved.parents or wf_resolved in ws_resolved.parents:
        raise ValueError('BLOCKED: --output-dir must be outside --workspace when --chatgpt-review is enabled')
    child_dir = _chatgpt_review_child_dir(wf_resolved)
    if os.path.lexists(child_dir):
        raise ValueError('BLOCKED: exclusive ChatGPT review output directory must not already exist')
    if child_dir == ws_resolved or ws_resolved in child_dir.parents or child_dir in ws_resolved.parents:
        raise ValueError('BLOCKED: ChatGPT review output directory must be outside --workspace')
    return child_dir


def format_final_workflow_summary(final_report: dict[str, object]) -> str:
    hashes_map = final_report.get('source_hashes') if isinstance(final_report.get('source_hashes'), dict) else {}
    hashes_str = ', '.join(f'{k}:{str(v)[:12]}' for k, v in sorted(hashes_map.items())) or 'none'
    codex_u = final_report.get('codex_usage') if isinstance(final_report.get('codex_usage'), dict) else {}
    prov_u = final_report.get('provider_io_usage') if isinstance(final_report.get('provider_io_usage'), dict) else {}
    lines = [
        f"TASK: {final_report.get('id')} ({final_report.get('kind')})",
        (
            f"VERDICT: {final_report.get('verdict')} "
            f"(pipeline={final_report.get('pipeline_verdict')}, "
            f"chatgpt_review={final_report.get('chatgpt_review_verdict') or 'SKIPPED'}, "
            f"mode={final_report.get('chatgpt_review_mode')})"
        ),
        (
            f"HASHES: {hashes_str} "
            f"(unchanged={final_report.get('source_hashes_unchanged_during_chatgpt_verification_and_review')})"
        ),
        (
            f"CODEX_STAGE_TOKENS: in={codex_u.get('input_tokens', 'unknown')} "
            f"(cached_subset={codex_u.get('cached_input_tokens', 'unknown')}, "
            f"cache_write={codex_u.get('cache_write_input_tokens', 'unknown')}, "
            f"uncached={codex_u.get('uncached_input_tokens', 'unknown')}) "
            f"out={codex_u.get('output_tokens', 'unknown')} "
            f"(reasoning_subset={codex_u.get('reasoning_output_tokens', 'unknown')}) "
            f"total={codex_u.get('total_tokens', 'unknown')}"
        ),
        (
            f"PROVIDER_IO_TOKENS: total={prov_u.get('provider_io_total_tokens', 'unknown')} "
            f"(devin_io={prov_u.get('devin_io_tokens', 'unknown')}, "
            f"opencode_io={prov_u.get('opencode_io_tokens', 'unknown')}, "
            f"jev_io={prov_u.get('jev_io_tokens', 'unknown')}, "
            f"agy_io={prov_u.get('agy_io_tokens', 'unknown')})"
        ),
        f"ALL_IN_TOTAL_TOKENS: {final_report.get('all_in_total_tokens', 'unknown')}",
        f"REASONS: {'; '.join(final_report.get('reasons') or []) or 'none'}",
        f"RISK: {final_report.get('residual_risk')}",
    ]
    return '\n'.join(lines) + '\n'


def _finalize_with_chatgpt_review(
    result: dict[str, object],
    *,
    workspace: Path,
    output_dir: Path,
    profile: str,
    child_dir: Path,
) -> dict[str, object]:
    pipeline_verdict = str(result.get('verdict') or 'BLOCKED')
    base_usage_totals = dict(result.get('usage_totals') or {}) if isinstance(result.get('usage_totals'), dict) else {}
    base_reasons = [str(r) for r in (result.get('reasons') or []) if str(r).strip()]

    if pipeline_verdict != 'PASS':
        effective_verdict = pipeline_verdict if pipeline_verdict in ('REJECT', 'BLOCKED', 'PENDING_REVIEW') else 'BLOCKED'
        final_report: dict[str, object] = {
            'id': result.get('id'),
            'kind': result.get('kind'),
            'verdict': effective_verdict,
            'pipeline_verdict': pipeline_verdict,
            'chatgpt_review_mode': profile,
            'chatgpt_review_invoked': False,
            'chatgpt_review_status': 'SKIPPED_NON_PASS_PIPELINE',
            'chatgpt_review_verdict': None,
            'chatgpt_review_confidence': None,
            'chatgpt_review_output_dir': None,
            'jev_route': result.get('jev_route'),
            'rework_loops': result.get('rework_loops'),
            'unique_tests_passed': result.get('unique_tests_passed'),
            'context_mode': result.get('context_mode'),
            'prompt_chars': result.get('prompt_chars'),
            'context_optimization': result.get('context_optimization'),
            'source_files': list(result.get('source_files') or []),
            'source_hashes': dict(result.get('source_hashes') or {}),
            'source_hashes_unchanged_during_chatgpt_verification_and_review': bool(
                result.get('source_hashes_unchanged_during_chatgpt_verification_and_review') is True
            ),
            'source_hashes_unchanged_during_chatgpt_review': None,
            'usage_totals': base_usage_totals,
            'codex_usage': None,
            'provider_io_usage': None,
            'all_in_total_tokens': 'unknown',
            'findings': [],
            'reasons': base_reasons,
            'residual_risk': result.get('residual_risk'),
        }
        final_report['summary_text'] = format_final_workflow_summary(final_report)
        final_report = gate.sanitize(final_report)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        gate.save(Path(output_dir) / 'workflow-final-result.json', final_report)
        return final_report

    gpt_res: dict[str, object] | None = None
    helper_error: str | None = None
    try:
        cr_mod = load_chatgpt_review()
        raw_res = cr_mod.run_review(
            workflow_dir=output_dir,
            workspace=workspace,
            output_dir=child_dir,
            profile=profile,
        )
        if isinstance(raw_res, dict):
            gpt_res = raw_res
        else:
            helper_error = 'BLOCKED: ChatGPT review helper returned non-object result'
    except Exception as exc:
        helper_error = f'BLOCKED: ChatGPT review helper raised exception: {exc}'

    def _is_pos_int(val: object) -> bool:
        return type(val) is int and val > 0

    def _is_nonneg_int(val: object) -> bool:
        return type(val) is int and val >= 0

    codex_usage = gpt_res.get('codex_usage') if isinstance(gpt_res, dict) else None
    provider_io_usage = gpt_res.get('provider_io_usage') if isinstance(gpt_res, dict) else None
    all_in_total = gpt_res.get('all_in_total_tokens') if isinstance(gpt_res, dict) else 'unknown'

    valid_usage = (
        isinstance(codex_usage, dict)
        and _is_pos_int(codex_usage.get('input_tokens'))
        and _is_nonneg_int(codex_usage.get('cached_input_tokens'))
        and _is_pos_int(codex_usage.get('output_tokens'))
        and _is_pos_int(codex_usage.get('total_tokens'))
        and codex_usage.get('total_tokens') == codex_usage.get('input_tokens') + codex_usage.get('output_tokens')
        and isinstance(provider_io_usage, dict)
        and _is_pos_int(provider_io_usage.get('provider_io_total_tokens'))
        and _is_pos_int(all_in_total)
        and all_in_total == codex_usage.get('total_tokens') + provider_io_usage.get('provider_io_total_tokens')
    )
    hashes_unchanged_gpt = bool(
        isinstance(gpt_res, dict)
        and gpt_res.get('source_hashes_unchanged_during_chatgpt_review') is True
        and isinstance(gpt_res.get('source_hashes'), dict)
        and gpt_res.get('source_hashes') == result.get('source_hashes')
    )
    gpt_status = str(gpt_res.get('status') or 'BLOCKED') if isinstance(gpt_res, dict) else 'BLOCKED'
    gpt_verdict = str(gpt_res.get('verdict') or 'BLOCKED') if isinstance(gpt_res, dict) else 'BLOCKED'
    gpt_findings = [str(f) for f in (gpt_res.get('findings') or []) if str(f).strip()] if isinstance(gpt_res, dict) else []
    gpt_reasons = [str(r) for r in (gpt_res.get('reasons') or []) if str(r).strip()] if isinstance(gpt_res, dict) else []

    combined_reasons = list(base_reasons)
    for item in (*gpt_reasons, *gpt_findings):
        if item not in combined_reasons:
            combined_reasons.append(item)
    if helper_error and helper_error not in combined_reasons:
        combined_reasons.append(helper_error)
    if not valid_usage:
        msg = 'BLOCKED: missing or incomplete ChatGPT/provider token usage'
        if msg not in combined_reasons:
            combined_reasons.append(msg)
    if not hashes_unchanged_gpt:
        msg = 'BLOCKED: source hashes missing or changed during ChatGPT review'
        if msg not in combined_reasons:
            combined_reasons.append(msg)

    if (
        helper_error is None
        and valid_usage
        and hashes_unchanged_gpt
        and gpt_status == 'COMPLETED'
        and gpt_verdict in ('PASS', 'REJECT')
    ):
        effective_verdict = gpt_verdict
    else:
        effective_verdict = 'BLOCKED'
        all_in_total = 'unknown'

    combined_usage_totals = {
        **base_usage_totals,
        'codex': codex_usage,
        'provider_io': provider_io_usage,
        'all_in_total_tokens': all_in_total,
    }
    residual_risk = (
        str(gpt_res.get('residual_risk'))
        if isinstance(gpt_res, dict) and isinstance(gpt_res.get('residual_risk'), str) and gpt_res['residual_risk'].strip()
        else ('; '.join(combined_reasons) if combined_reasons else str(result.get('residual_risk') or 'Bounded scope only'))
    )
    final_report = {
        'id': result.get('id'),
        'kind': result.get('kind'),
        'verdict': effective_verdict,
        'pipeline_verdict': pipeline_verdict,
        'chatgpt_review_mode': profile,
        'chatgpt_review_invoked': True,
        'chatgpt_review_status': gpt_status,
        'chatgpt_review_verdict': gpt_verdict,
        'chatgpt_review_confidence': gpt_res.get('confidence') if isinstance(gpt_res, dict) else None,
        'chatgpt_review_output_dir': str(child_dir),
        'jev_route': result.get('jev_route'),
        'rework_loops': result.get('rework_loops'),
        'unique_tests_passed': result.get('unique_tests_passed'),
        'context_mode': result.get('context_mode'),
        'prompt_chars': result.get('prompt_chars'),
        'context_optimization': result.get('context_optimization'),
        'source_files': list(result.get('source_files') or []),
        'source_hashes': dict(result.get('source_hashes') or {}),
        'source_hashes_unchanged_during_chatgpt_verification_and_review': bool(
            result.get('source_hashes_unchanged_during_chatgpt_verification_and_review') is True
            and hashes_unchanged_gpt
        ),
        'source_hashes_unchanged_during_chatgpt_review': hashes_unchanged_gpt,
        'usage_totals': combined_usage_totals,
        'codex_usage': codex_usage,
        'provider_io_usage': provider_io_usage,
        'all_in_total_tokens': all_in_total,
        'findings': gpt_findings,
        'reasons': combined_reasons,
        'residual_risk': residual_risk,
    }
    final_report['summary_text'] = format_final_workflow_summary(final_report)
    final_report = gate.sanitize(final_report)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    gate.save(Path(output_dir) / 'workflow-final-result.json', final_report)
    return final_report


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        '--verify-timeout',
        type=int,
        default=DEFAULT_VERIFY_TIMEOUT,
        help='Verification command timeout in seconds (1..1800)',
    )
    parser.add_argument('--open-question', action='append', default=None)
    parser.add_argument('--quant-contract', type=str, default=None, help='JSON string or path to JSON quant contract')
    parser.add_argument('--require-stronger-reviewer', action='store_true')
    parser.add_argument('--stronger-reviewer-model', default=None)
    parser.add_argument('--authorized-protected', action='store_true')
    parser.add_argument('--triage-only', action='store_true')
    parser.add_argument('--no-resume-worker', action='store_true')
    parser.add_argument('--context-mode', default=None, help='Prompt context mode: lean (default), compact, or full')
    parser.add_argument(
        '--chatgpt-review',
        choices=CHATGPT_REVIEW_MODES,
        default='scoped',
        help='Native ChatGPT final review mode: scoped (default), off, or baseline',
    )
    parser.add_argument('--print-pack', action='store_true', help='Print compact proof pack text instead of JSON summary')
    args = parser.parse_args(argv)

    packet_path = args.packet_file_opt or args.packet_file
    try:
        review_child_dir = (
            _preflight_chatgpt_review_paths(args.workspace, args.output_dir)
            if args.chatgpt_review != 'off'
            else None
        )
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
            verify_timeout=args.verify_timeout,
            open_questions=args.open_question,
            quant_contract=quant_contract,
            require_stronger_reviewer=args.require_stronger_reviewer,
            stronger_reviewer_model=args.stronger_reviewer_model,
            authorized_protected=args.authorized_protected,
            triage_only=args.triage_only,
            resume_worker=not args.no_resume_worker,
            context_mode=args.context_mode,
        )
    except (OSError, ValueError, TypeError):
        print('Cannot execute workflow or create new output directory; no credentials displayed.', file=sys.stderr)
        return 2

    if args.chatgpt_review != 'off':
        final_result = _finalize_with_chatgpt_review(
            result,
            workspace=args.workspace,
            output_dir=args.output_dir,
            profile=args.chatgpt_review,
            child_dir=review_child_dir or _chatgpt_review_child_dir(args.output_dir),
        )
        if args.print_pack:
            sys.stdout.write(str(final_result['summary_text']))
        else:
            compact_final = {
                k: final_result[k]
                for k in (
                    'id',
                    'verdict',
                    'pipeline_verdict',
                    'chatgpt_review_mode',
                    'chatgpt_review_invoked',
                    'chatgpt_review_verdict',
                    'jev_route',
                    'rework_loops',
                    'unique_tests_passed',
                    'context_mode',
                    'prompt_chars',
                    'context_optimization',
                    'source_files',
                    'source_hashes',
                    'usage_totals',
                    'codex_usage',
                    'provider_io_usage',
                    'all_in_total_tokens',
                    'findings',
                    'reasons',
                )
            }
            print(json.dumps(compact_final, ensure_ascii=False))
        return {'PASS': 0, 'REJECT': 1, 'BLOCKED': 2, 'PENDING_REVIEW': 3}.get(str(final_result['verdict']), 2)

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
                'context_mode',
                'prompt_chars',
                'context_optimization',
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
