"""Focused offline unit tests for scripts/chatgpt_review.py (mocked Codex subprocess only)."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
REVIEW_SCRIPT = SCRIPT_DIR / 'chatgpt_review.py'
spec = importlib.util.spec_from_file_location('chatgpt_review', REVIEW_SCRIPT)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)

WF_SCRIPT = SCRIPT_DIR / 'run_workflow.py'
wf_spec = importlib.util.spec_from_file_location('run_workflow_for_cr_tests', WF_SCRIPT)
wf = importlib.util.module_from_spec(wf_spec)
wf_spec.loader.exec_module(wf)
gate = wf.gate

SAMPLE_CONTRACT = (
    'TASK: Correct only the loop whitespace predicate in collapse_whitespace to match Python str.isspace '
    'for all 29 Unicode whitespace codepoints; preserve TypeError and AST structure.\n'
    'WRITE SCOPE: text_utils.py only\n'
    'VERIFY: python3 -B -m unittest discover -s tests -v'
)
SAMPLE_DIFF = (
    '--- a/text_utils.py\n'
    '+++ b/text_utils.py\n'
    '@@ -1,5 +1,4 @@\n'
    ' def collapse_whitespace(text: str) -> str:\n'
    '-    if char in " \\t\\n":\n'
    '+    if char.isspace():\n'
    '         return " "\n'
)
SAMPLE_VERIFY_OUT = (
    '$ python3 -B -m unittest discover -s tests -v (exit 0)\n'
    'test_collapse (test_text_utils.TestCollapse.test_collapse) ... ok\n'
    '----------------------------------------------------------------------\n'
    'Ran 10 tests in 0.05s\n\n'
    'OK'
)
SAMPLE_HANDOFF = (
    'Updated whitespace predicate to char.isspace() in collapse_whitespace. '
    'Preserved TypeError and AST structure; all 10 unit tests pass.'
)
SAMPLE_SOURCE = 'def collapse_whitespace(text: str) -> str:\n    return " ".join(text.split())\n'


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def make_superpowers_home(root: Path, mcp_names: tuple[str, ...] = ('playwright', 'github_mcp')) -> Path:
    home = root / 'fake_home'
    skills_root = home / '.codex/plugins/cache/openai-curated-remote/superpowers/7.0.1/skills'
    (skills_root / 'using-superpowers').mkdir(parents=True, exist_ok=True)
    (skills_root / 'verification-before-completion').mkdir(parents=True, exist_ok=True)
    (skills_root / 'using-superpowers/SKILL.md').write_text(
        '---\nname: using-superpowers\n---\n# Using Superpowers\nAlways invoke applicable skills first.\n',
        encoding='utf-8',
    )
    (skills_root / 'verification-before-completion/SKILL.md').write_text(
        '---\nname: verification-before-completion\n---\n# Verification Before Completion\nEvidence before claims.\n',
        encoding='utf-8',
    )
    codex_dir = home / '.codex'
    codex_dir.mkdir(parents=True, exist_ok=True)
    toml_lines = ['model = "gpt-6.1-sol"']
    for name in mcp_names:
        toml_lines.append(f'\n[mcp_servers.{name}]\ncommand = "npx"\nargs = ["-y", "{name}"]\nenv = {{ API_TOKEN = "do-not-log-this-value" }}\n')
    (codex_dir / 'config.toml').write_text('\n'.join(toml_lines), encoding='utf-8')
    return home


def make_fixture_tree(
    root: Path,
    *,
    kind: str = 'task',
    worker_provider: str = 'devin',
    rework_loops: int = 0,
    stronger_reviewer_required: bool = False,
) -> tuple[Path, Path]:
    ws = root / 'workspace'
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'text_utils.py').write_text(SAMPLE_SOURCE, encoding='utf-8')
    src_hash = sha256_text(SAMPLE_SOURCE)
    source_hashes = {'text_utils.py': src_hash}

    wf_dir = root / 'workflow'
    wf_dir.mkdir(parents=True, exist_ok=True)

    selected_agy = gate.selected_model()
    min_rev = 2 if kind == 'quant' else 1
    attempt_num = rework_loops + 1
    attempt_dir = wf_dir / f'attempt-{attempt_num}'
    gate_dir = attempt_dir / 'review-gate'
    gate_dir.mkdir(parents=True, exist_ok=True)

    checks = [
        {'kind': 'tests', 'command': 'python3 -B -m unittest discover -s tests -v', 'exit_code': 0, 'passed': 10, 'output': SAMPLE_VERIFY_OUT},
        {'kind': 'scope', 'command': 'compare workspace snapshot against declared write scope', 'exit_code': 0, 'output': 'Changed files within declared scope (1): text_utils.py'},
        {'kind': 'whitespace', 'command': 'git diff --no-index --check', 'exit_code': 0, 'output': 'exit 0, no whitespace errors'},
    ]
    mech = {
        'all_passed': True,
        'out_of_scope': [],
        'oversized_diff': False,
        'oversized_diff_reason': None,
        'diff_chars': len(SAMPLE_DIFF),
        'max_diff_chars': 12000,
        'changed_files': ['text_utils.py'],
        'deleted_files': [],
        'checks': checks,
        'rtk_verification': {
            'rtk_applied': True,
            'raw_chars': len(SAMPLE_VERIFY_OUT),
            'filtered_chars': 26,
            'raw_sha256': sha256_text(SAMPLE_VERIFY_OUT),
            'filtered_summary': 'Pytest: 10 passed in 0.05s',
            'raw_artifact': '/untrusted/report/path/do-not-follow/verification-raw.txt',
        },
        'source_hashes': dict(source_hashes),
        'focused_diff': SAMPLE_DIFF,
    }

    if worker_provider == 'devin':
        packet_worker = {
            'provider': 'devin',
            'model': gate.DEVIN_MODEL,
            'exit_code': 0,
            'proof_status': 'VERIFIED',
            'response': SAMPLE_HANDOFF,
            'tool_denied': False,
            'tool_calls': 0,
        }
        worker_proof = {
            'source': 'coordinator-parsed Devin ATIF export and stdout (not worker narrative)',
            'provider': 'devin',
            'requested_model': gate.DEVIN_MODEL,
            'actual_generation_model': gate.DEVIN_MODEL,
            'agent_model_name': gate.DEVIN_MODEL_NAME,
            'permission_mode': gate.DEVIN_PERMISSION_MODE,
            'schema_version': gate.DEVIN_ATIF_SCHEMA,
            'exit_code': 0,
            'tool_calls': 0,
            'tool_denied': False,
            'stdout_matches_export': True,
            'stdout_normalization': gate.DEVIN_STDOUT_NORMALIZATION,
            'session_id': 'spiny-notebook',
            'generation_steps': 1,
            'applied_files': ['text_utils.py'],
            'application': 'coordinator mechanically applied model-authored UTF-8 bytes',
        }
    else:
        packet_worker = {
            'model': selected_agy,
            'effort': 'high',
            'exit_code': 0,
            'status': 'SUCCESS',
            'response': SAMPLE_HANDOFF,
            'tool_denied': False,
        }
        worker_proof = {
            'source': 'coordinator-parsed agy init/result metadata (not worker narrative)',
            'requested_model': selected_agy,
            'actual_init_model': selected_agy,
            'permission_mode': 'request-review',
            'effort': 'high',
            'effort_verification': 'init effort field',
            'status': 'SUCCESS',
            'exit_code': 0,
            'tool_denied': False,
            'conversation_id': 'agy-worker-cid-1',
        }

    packet: dict[str, object] = {
        'schema_version': 1,
        'id': f'abba-1-attempt-{attempt_num}',
        'kind': kind,
        'objective': SAMPLE_CONTRACT,
        'worker': packet_worker,
        'checks': checks,
        'evidence': {
            'focused_diff': SAMPLE_DIFF,
            'verification_output': SAMPLE_VERIFY_OUT,
            'summary': SAMPLE_HANDOFF[:120],
            'summary_label': 'short redacted excerpt of the worker handoff; not the authoritative full response',
            'full_worker_handoff': SAMPLE_HANDOFF,
            'worker_proof': worker_proof,
            'source_workspace': '/untrusted/report/path/workspace',
            'source_hashes': dict(source_hashes),
        },
        'open_questions': [],
    }
    if kind == 'quant':
        packet['min_reviewers'] = 2
        packet['quant_contract'] = {
            'data_provenance': 'Causal tick archive',
            'friction': '5 bps taker fee + 2 bps slippage',
            'causality': 'Strict t-1 close lag',
            'parity': 'Symmetric LONG/SHORT entry and exit',
        }

    jev_assessment = {'label': 'adequate', 'confidence': 0.91, 'margin': 0.82, 'concern_probability': 0.04}
    gate_reviewers = []
    report_reviewers = []
    for idx in range(1, min_rev + 1):
        rev_cid = f'rev-cid-{idx}'
        effort_verif = 'explicit --effort high plus verified High model slug; init omits effort'
        rev_usage = {
            'input_tokens': 10000 + idx * 10,
            'output_tokens': 800 + idx * 5,
            'thinking_tokens': 700,
            'cache_read_tokens': 200 if idx == 1 else 0,
            'total_tokens': (10000 + idx * 10) + (800 + idx * 5),
        }
        ans = {
            'verdict': 'PASS',
            'confidence': 96.5,
            'findings': [],
            'resolved_questions': [],
            'residual_risk': 'Verification relies on the supplied 10-test oracle suite within text_utils.py.',
        }
        rev_inv = {
            'argv': [
                '/opt/homebrew/bin/agy',
                '--model',
                selected_agy,
                '--effort',
                'high',
                '--output-format',
                'stream-json',
                '--print',
                'Review the bounded workflow evidence packet.',
            ],
            'state': 'FINISHED',
            'timed_out': False,
            'exit_code': 0,
        }
        rev_events = [
            {
                'event': 'init',
                'conversation_id': rev_cid,
                'init': {
                    'conversation_id': rev_cid,
                    'model': selected_agy,
                    'permission_mode': 'request-review',
                    'tool_denied': False,
                },
            },
            {
                'event': 'step_update',
                'step_update': {
                    'conversation_id': rev_cid,
                    'step_type': 'agent_response',
                    'status': 'DONE',
                },
            },
            {
                'event': 'result',
                'conversation_id': rev_cid,
                'result': {
                    'conversation_id': rev_cid,
                    'status': 'SUCCESS',
                    'exit_code': 0,
                    'tool_denied': False,
                    'response': json.dumps(ans),
                    'usage': rev_usage,
                },
            },
        ]
        rev_res = {
            'conversation_id': rev_cid,
            'model': selected_agy,
            'effort': 'high',
            'effort_verification': effort_verif,
            'exit_code': 0,
            'answer': ans,
            'usage': rev_usage,
            'usage_updates_observed': 1,
        }
        (gate_dir / f'reviewer-{idx}-invocation.json').write_text(json.dumps(rev_inv, indent=2), encoding='utf-8')
        (gate_dir / f'reviewer-{idx}.stdout.jsonl').write_text(
            '\n'.join(json.dumps(ev) for ev in rev_events) + '\n',
            encoding='utf-8',
        )
        (gate_dir / f'reviewer-{idx}-result.json').write_text(json.dumps(rev_res, indent=2), encoding='utf-8')
        gate_reviewers.append({
            'model': selected_agy,
            'effort': 'high',
            'effort_verification': effort_verif,
            'conversation_id': rev_cid,
            'exit_code': 0,
            'answer': ans,
            'usage': rev_usage,
            'usage_updates_observed': 1,
        })
        report_reviewers.append({
            'conversation_id': rev_cid,
            'model': selected_agy,
            'effort': 'high',
            'effort_verification': effort_verif,
            **ans,
            'usage': rev_usage,
        })

    decision = {
        'case_id': packet['id'],
        'selected_model': selected_agy,
        'worker_provider': worker_provider,
        'policy_version': gate.POLICY_VERSION,
        'calibration': 'Unvalidated for engineering/quant evidence; ticket accuracy does not transfer.',
        'packet_sha256': gate.stable_hash(packet),
        'gate_source_sha256': 'a' * 64,
        'thresholds': {'confidence': 0.85, 'margin': 0.2, 'max_concern_exclusive': 0.15},
        'reviewers': gate_reviewers,
        'route': 'standard_review',
        'reviewers_required': min_rev,
        'reasons': [],
        'jev_assessment': jev_assessment,
        'jev_error': None,
        'verdict': 'PASS',
        'jev_usage': {'input_tokens': 1760, 'output_tokens': 80},
    }

    external_steps = []
    attempts_log = []
    for att in range(1, attempt_num + 1):
        if worker_provider == 'devin':
            external_steps.append({
                'step': f'Devin worker generation ({gate.DEVIN_MODEL})',
                'role': 'worker',
                'provider': 'devin',
                'attempt': att,
                'requested_model': gate.DEVIN_MODEL,
                'model': gate.DEVIN_MODEL,
                'effort': None,
                'context_mode': 'lean',
                'prompt_chars': 2900,
                'conversation_id': f'spiny-notebook-{att}',
                'usage': {
                    'total_prompt_tokens': 24000 + att * 100,
                    'total_completion_tokens': 850 + att * 10,
                    'total_cached_tokens': 5500,
                    'total_steps': 8,
                },
                'usage_basis': wf.devin.USAGE_BASIS,
                'usage_updates_observed': 1,
                'exit_code': 0,
                'duration_seconds': 12.0,
                'status': 'VERIFIED',
                'files_applied': ['text_utils.py'],
                'reason': None,
            })
        else:
            external_steps.append({
                'step': f'Worker implementation ({selected_agy})',
                'role': 'worker',
                'provider': 'agy',
                'attempt': att,
                'model': selected_agy,
                'effort': 'high',
                'context_mode': 'lean',
                'prompt_chars': 2900,
                'conversation_id': 'agy-worker-cid-1',
                'usage': {
                    'input_tokens': 5000,
                    'output_tokens': 600,
                    'thinking_tokens': 200,
                    'cache_read_tokens': 1000,
                    'total_tokens': 5600,
                },
                'raw_terminal_usage': {
                    'input_tokens': 5000 * att,
                    'output_tokens': 600 * att,
                    'thinking_tokens': 200 * att,
                    'cache_read_tokens': 1000 * att,
                    'total_tokens': 5600 * att,
                },
                'usage_basis': 'initial CLI conversation counter',
                'usage_updates_observed': 1,
                'exit_code': 0,
                'duration_seconds': 10.0,
                'status': 'SUCCESS',
                'reason': None,
            })
        if att < attempt_num:
            prev_att_dir = wf_dir / f'attempt-{att}'
            prev_att_dir.mkdir(parents=True, exist_ok=True)
            attempts_log.append({
                'attempt': att,
                'stage': 'mechanical_checks',
                'status': 'FAILED',
                'reasons': ['tests check failed'],
                'artifact_dir': f'/untrusted/report/path/attempt-{att}',
            })

    external_steps.append({
        'step': 'Jev đánh giá',
        'role': 'jev',
        'attempt': attempt_num,
        'model': gate.JEV_MODEL,
        'usage': {'input_tokens': 1760, 'output_tokens': 80, 'total_tokens': 1840},
        'usage_basis': 'API input/output fields; total calculated as their sum',
        'exit_code': 0,
    })
    for idx, rev in enumerate(gate_reviewers, start=1):
        external_steps.append({
            'step': f'Independent review {idx} ({selected_agy})',
            'role': 'reviewer',
            'provider': 'agy',
            'attempt': attempt_num,
            'model': selected_agy,
            'effort': 'high',
            'conversation_id': rev['conversation_id'],
            'usage': dict(rev['usage']),
            'raw_terminal_usage': dict(rev['usage']),
            'usage_basis': 'fresh reviewer conversation CLI counter',
            'usage_updates_observed': 1,
            'exit_code': 0,
            'verdict': 'PASS',
        })

    attempts_log.append({
        'attempt': attempt_num,
        'stage': 'review_gate',
        'status': 'PASS',
        'route': 'standard_review',
        'reasons': [],
        'artifact_dir': '/untrusted/report/path/attempt-final/review-gate',
    })

    report = {
        'verdict': 'PASS',
        'id': 'abba-1',
        'kind': kind,
        'task': 'Correct only the loop whitespace predicate in collapse_whitespace',
        'workspace': '/untrusted/report/path/workspace',
        'source_files': ['text_utils.py'],
        'source_hashes': dict(source_hashes),
        'source_hashes_unchanged_during_chatgpt_verification_and_review': True,
        'source_or_tests_written_by_chatgpt': 0,
        'worker_provider': worker_provider,
        'worker_model': gate.DEVIN_MODEL if worker_provider == 'devin' else selected_agy,
        'worker_effort': None if worker_provider == 'devin' else 'high',
        'reviewer_provider': 'agy',
        'reviewer_model': selected_agy,
        'reviewer_effort': 'high',
        'worker_and_reviewer_model': None if worker_provider == 'devin' else selected_agy,
        'effort': None if worker_provider == 'devin' else 'high',
        'context_mode': 'lean',
        'prompt_chars': 2900,
        'rework_loops': rework_loops,
        'max_reworks': 2,
        'unique_tests_passed': 10,
        'coordinator_test_command': 'python3 -B -m unittest discover -s tests -v',
        'test_exit_code': 0,
        'gate_exit_code': 0,
        'jev_route': 'standard_review',
        'jev_assessment': jev_assessment,
        'jev_error': None,
        'reasons': [],
        'summary': SAMPLE_HANDOFF[:120],
        'residual_risk': 'Verification relies on the supplied 10-test oracle suite within text_utils.py.',
        'reviewers': report_reviewers,
        'stronger_reviewer_required': bool(kind == 'quant' or stronger_reviewer_required),
        'same_model_is_stronger': False,
        'external_steps': external_steps,
        'usage_totals': wf.aggregate_external_usage(external_steps),
        'attempts': attempts_log,
        'proof_pack': 'TASK: abba-1\nVERDICT: PASS\n',
    }

    (attempt_dir / 'mechanical-checks.json').write_text(json.dumps(mech, indent=2), encoding='utf-8')
    (gate_dir / 'packet.json').write_text(json.dumps(packet, indent=2), encoding='utf-8')
    (gate_dir / 'decision.json').write_text(json.dumps(decision, indent=2), encoding='utf-8')
    (wf_dir / 'workflow-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (wf_dir / 'proof-pack.txt').write_text(report['proof_pack'], encoding='utf-8')
    return wf_dir, ws


def make_codex_stream(
    *,
    verdict: str = 'PASS',
    confidence: float = 95.0,
    findings: list[str] | None = None,
    residual_risk: str = 'Bounded unit test scope only; no live execution risk.',
    usage: dict | None = None,
    extra_events: list[dict] | None = None,
    raw_answer_text: str | None = None,
) -> str:
    answer_obj = {
        'verdict': verdict,
        'confidence': confidence,
        'findings': [] if findings is None else findings,
        'residual_risk': residual_risk,
    }
    text = raw_answer_text if raw_answer_text is not None else json.dumps(answer_obj)
    default_usage = {
        'input_tokens': 8500,
        'cached_input_tokens': 6400,
        'output_tokens': 320,
        'reasoning_output_tokens': 192,
    }
    events = [
        {'type': 'thread.started', 'thread_id': 'thread-ephemeral-1'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'reasoning', 'text': 'Auditing compact proof packet.'}},
        *(extra_events or []),
        {'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': text}},
        {'type': 'turn.completed', 'usage': default_usage if usage is None else usage},
    ]
    return '\n'.join(json.dumps(ev) for ev in events) + '\n'


class ChatGPTReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake_home = make_superpowers_home(self.root)
        self.fake_disabled_skills = [str((self.fake_home / '.agents/skills/unrelated-helper/SKILL.md').resolve())]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_build_packet_preserves_full_contract_diff_verification_proof_and_usage(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root, rework_loops=1)
        raw_packet = json.loads((wf_dir / 'attempt-2/review-gate/packet.json').read_text(encoding='utf-8'))
        raw_report = json.loads((wf_dir / 'workflow-result.json').read_text(encoding='utf-8'))
        raw_decision = json.loads((wf_dir / 'attempt-2/review-gate/decision.json').read_text(encoding='utf-8'))

        pkt = cr.build_packet(wf_dir, ws)
        self.assertEqual(pkt['objective'], raw_packet['objective'])
        self.assertEqual(pkt['focused_diff'], raw_packet['evidence']['focused_diff'])
        self.assertEqual(pkt['verification_output'], raw_packet['evidence']['verification_output'])
        self.assertEqual(pkt['worker']['full_worker_handoff'], raw_packet['evidence']['full_worker_handoff'])
        self.assertEqual(pkt['worker']['worker_proof'], raw_packet['evidence']['worker_proof'])
        self.assertEqual(pkt['jev']['assessment'], raw_decision['jev_assessment'])
        self.assertEqual(pkt['jev']['route'], raw_decision['route'])
        self.assertEqual(pkt['source_hashes'], raw_report['source_hashes'])
        self.assertEqual(len(pkt['reviewers']), 1)
        self.assertEqual(pkt['reviewers'][0]['verdict'], 'PASS')
        self.assertEqual(pkt['reviewers'][0]['confidence'], 96.5)
        self.assertEqual(pkt['reviewers'][0]['findings'], [])

        # Usage counts both attempt 1 and attempt 2 worker runs + Jev + reviewer; Devin cached is never added/subtracted
        provider_io = pkt['provider_io_usage']
        expected_devin_io = (24100 + 860) + (24200 + 870)
        expected_jev_io = 1760 + 80
        expected_agy_io = 10010 + 200 + 805  # input + cache_read + output (thinking 700 already in output)
        self.assertEqual(provider_io['devin_io_tokens'], expected_devin_io)
        self.assertEqual(provider_io['devin_cached_tokens_separate'], 11000)
        self.assertEqual(provider_io['jev_io_tokens'], expected_jev_io)
        self.assertEqual(provider_io['agy_io_tokens'], expected_agy_io)
        self.assertEqual(provider_io['provider_io_total_tokens'], expected_devin_io + expected_jev_io + expected_agy_io)

    def test_quant_packet_preserves_two_reviewers_and_stronger_reviewer_obligation(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root, kind='quant', worker_provider='agy')
        pkt = cr.build_packet(wf_dir, ws)
        self.assertEqual(pkt['kind'], 'quant')
        self.assertEqual(len(pkt['reviewers']), 2)
        self.assertTrue(pkt['stronger_reviewer_required'])
        self.assertFalse(pkt['same_model_is_stronger'])
        self.assertFalse(pkt['chatgpt_substitutes_for_stronger_reviewer'])

        # Tampering quant to have only 1 reviewer or clearing stronger_reviewer_required blocks
        dec_path = wf_dir / 'attempt-1/review-gate/decision.json'
        dec = json.loads(dec_path.read_text(encoding='utf-8'))
        dec['reviewers'] = dec['reviewers'][:1]
        dec_path.write_text(json.dumps(dec), encoding='utf-8')
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir, ws)

    def test_hash_scope_symlink_and_tamper_rejection(self) -> None:
        # 1. Workspace file hash mismatch
        wf_dir, ws = make_fixture_tree(self.root / 'case1')
        (ws / 'text_utils.py').write_text('def collapse_whitespace(text: str) -> str:\n    return text\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir, ws)

        # 2. Disagreement between mechanical-checks.json and workflow-result.json source_hashes
        wf_dir2, ws2 = make_fixture_tree(self.root / 'case2')
        mech_path = wf_dir2 / 'attempt-1/mechanical-checks.json'
        mech = json.loads(mech_path.read_text(encoding='utf-8'))
        mech['source_hashes']['text_utils.py'] = '0' * 64
        mech_path.write_text(json.dumps(mech), encoding='utf-8')
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir2, ws2)

        # 3. Absolute or traversal scope path rejected
        for idx, bad_scope in enumerate((['/etc/passwd'], ['../outside.py'])):
            wf_dir_b, ws_b = make_fixture_tree(self.root / f'scope-{idx}')
            rep_path = wf_dir_b / 'workflow-result.json'
            rep = json.loads(rep_path.read_text(encoding='utf-8'))
            rep['source_files'] = bad_scope
            rep_path.write_text(json.dumps(rep), encoding='utf-8')
            with self.assertRaises(ValueError):
                cr.build_packet(wf_dir_b, ws_b)

        # 4. Symlinked workspace file rejected
        wf_dir3, ws3 = make_fixture_tree(self.root / 'case3')
        outside_file = self.root / 'outside.py'
        outside_file.write_text(SAMPLE_SOURCE, encoding='utf-8')
        (ws3 / 'text_utils.py').unlink()
        (ws3 / 'text_utils.py').symlink_to(outside_file)
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir3, ws3)

        # 5. Symlinked attempt directory under workflow_dir rejected
        wf_dir4, ws4 = make_fixture_tree(self.root / 'case4')
        moved = self.root / 'moved-attempt-1'
        (wf_dir4 / 'attempt-1').rename(moved)
        (wf_dir4 / 'attempt-1').symlink_to(moved)
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir4, ws4)

        # 6. Oversized or clipped diff and non-PASS native workflow rejected
        wf_dir5, ws5 = make_fixture_tree(self.root / 'case5')
        pkt_path = wf_dir5 / 'attempt-1/review-gate/packet.json'
        pkt_obj = json.loads(pkt_path.read_text(encoding='utf-8'))
        pkt_obj['evidence']['focused_diff'] += '\n... [diff truncated to bound]'
        pkt_path.write_text(json.dumps(pkt_obj), encoding='utf-8')
        with self.assertRaises(ValueError):
            cr.build_packet(wf_dir5, ws5)

        # 7. output_dir inside workspace or already existing rejected
        wf_dir6, ws6 = make_fixture_tree(self.root / 'case6')
        with self.assertRaises(ValueError):
            cr.run_review(wf_dir6, ws6, ws6 / 'inside-ws')
        existing_out = self.root / 'already-exists'
        existing_out.mkdir()
        with self.assertRaises(ValueError):
            cr.run_review(wf_dir6, ws6, existing_out)

    def test_invalid_external_steps_usage_and_mismatching_totals_rejected(self) -> None:
        mutations = [
            ('mismatching-totals', lambda r: r['usage_totals']['jev'].update(total_tokens=999999)),
            ('bool-counter', lambda r: r['external_steps'][0]['usage'].update(total_prompt_tokens=True)),
            ('negative-counter', lambda r: r['external_steps'][0]['usage'].update(total_cached_tokens=-1)),
            ('zero-worker-prompt', lambda r: r['external_steps'][0]['usage'].update(total_prompt_tokens=0)),
            ('unknown-role', lambda r: r['external_steps'].append({'role': 'mystery', 'attempt': 1, 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}})),
            ('unknown-provider', lambda r: r['external_steps'][0].update(provider='opencode')),
        ]
        for label, mutate in mutations:
            with self.subTest(case=label):
                wf_dir, ws = make_fixture_tree(self.root / f'usage-{label}')
                rep_path = wf_dir / 'workflow-result.json'
                rep = json.loads(rep_path.read_text(encoding='utf-8'))
                mutate(rep)
                rep_path.write_text(json.dumps(rep), encoding='utf-8')
                with self.assertRaises(ValueError):
                    cr.build_packet(wf_dir, ws)

    def test_secret_blocks_before_codex_invocation(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root / 'secret-case')
        (ws / 'text_utils.py').write_text('API_TOKEN = "sk-live-secret-1234567890abcdef"\n', encoding='utf-8')
        new_hash = sha256_text((ws / 'text_utils.py').read_text(encoding='utf-8'))
        for rel in ('workflow-result.json', 'attempt-1/mechanical-checks.json', 'attempt-1/review-gate/packet.json'):
            p = wf_dir / rel
            data = json.loads(p.read_text(encoding='utf-8'))
            if 'source_hashes' in data:
                data['source_hashes']['text_utils.py'] = new_hash
            if 'evidence' in data:
                data['evidence']['source_hashes']['text_utils.py'] = new_hash
            p.write_text(json.dumps(data), encoding='utf-8')

        out_dir = self.root / 'secret-out'
        with patch.object(Path, 'home', return_value=self.fake_home), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=self.fake_disabled_skills
        ), patch.object(cr.subprocess, 'run') as mock_run:
            res = cr.run_review(wf_dir, ws, out_dir, profile='scoped')
        self.assertEqual(res['verdict'], 'BLOCKED')
        mock_run.assert_not_called()

    def test_parse_codex_rejects_tools_errors_bad_usage_and_invalid_verdicts(self) -> None:
        # Valid stream parses cleanly
        ok = cr.parse_codex(make_codex_stream(), 0)
        self.assertEqual(ok['answer']['verdict'], 'PASS')
        self.assertEqual(ok['usage']['input_tokens'], 8500)
        self.assertEqual(ok['usage']['cached_input_tokens'], 6400)
        self.assertEqual(ok['usage']['uncached_input_tokens'], 2100)
        self.assertEqual(ok['usage']['output_tokens'], 320)
        self.assertEqual(ok['usage']['reasoning_output_tokens'], 192)
        self.assertEqual(ok['usage']['total_tokens'], 8820)

        bad_cases = [
            ('nonzero-exit', make_codex_stream(), 1),
            ('command-item', make_codex_stream(extra_events=[{'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'ls'}}]), 0),
            ('mcp-item', make_codex_stream(extra_events=[{'type': 'item.completed', 'item': {'id': 'm1', 'type': 'mcp_tool_call', 'server': 'playwright'}}]), 0),
            ('web-search-item', make_codex_stream(extra_events=[{'type': 'item.completed', 'item': {'id': 'w1', 'type': 'web_search', 'query': 'python'}}]), 0),
            ('file-change-item', make_codex_stream(extra_events=[{'type': 'item.completed', 'item': {'id': 'f1', 'type': 'file_change'}}]), 0),
            ('error-event', make_codex_stream(extra_events=[{'type': 'error', 'message': 'stream error'}]), 0),
            ('failed-turn', make_codex_stream(extra_events=[{'type': 'turn.failed', 'error': {'message': 'failed'}}]), 0),
            ('duplicate-answer', make_codex_stream(extra_events=[{'type': 'item.completed', 'item': {'id': 'a2', 'type': 'agent_message', 'text': '{"verdict":"PASS","confidence":90,"findings":[],"residual_risk":"ok"}'}}]), 0),
            ('duplicate-turn-completed', make_codex_stream() + json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}}) + '\n', 0),
            ('missing-usage', '\n'.join([json.dumps({'type': 'item.completed', 'item': {'id': '1', 'type': 'agent_message', 'text': '{"verdict":"PASS","confidence":90,"findings":[],"residual_risk":"ok"}'}}), json.dumps({'type': 'turn.completed'})]), 0),
            ('bool-usage', make_codex_stream(usage={'input_tokens': True, 'cached_input_tokens': 0, 'output_tokens': 10}), 0),
            ('negative-usage', make_codex_stream(usage={'input_tokens': 100, 'cached_input_tokens': -1, 'output_tokens': 10}), 0),
            ('cached-gt-input', make_codex_stream(usage={'input_tokens': 100, 'cached_input_tokens': 101, 'output_tokens': 10}), 0),
            ('reasoning-gt-output', make_codex_stream(usage={'input_tokens': 100, 'cached_input_tokens': 20, 'output_tokens': 10, 'reasoning_output_tokens': 11}), 0),
            ('pass-with-findings', make_codex_stream(verdict='PASS', confidence=92.0, findings=['Unresolved bug']), 0),
            ('pass-low-confidence', make_codex_stream(verdict='PASS', confidence=84.9, findings=[]), 0),
            ('confidence-100', make_codex_stream(verdict='PASS', confidence=100.0, findings=[]), 0),
            ('reject-without-findings', make_codex_stream(verdict='REJECT', confidence=90.0, findings=[]), 0),
            ('empty-residual-risk', make_codex_stream(verdict='PASS', confidence=90.0, findings=[], residual_risk='   '), 0),
            ('duplicate-json-key-in-answer', make_codex_stream(raw_answer_text='{"verdict":"PASS","verdict":"PASS","confidence":90,"findings":[],"residual_risk":"ok"}'), 0),
        ]
        for label, stream, code in bad_cases:
            with self.subTest(case=label), self.assertRaises(ValueError):
                cr.parse_codex(stream, code)

    def test_baseline_and_scoped_profiles_flags_skills_mcp_and_accounting(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root / 'profiles')
        orig_wf_bytes = (wf_dir / 'workflow-result.json').read_bytes()
        orig_pack_bytes = (wf_dir / 'proof-pack.txt').read_bytes()

        calls: list[tuple[list[str], dict]] = []

        def fake_run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=make_codex_stream(), stderr='')

        with patch.object(Path, 'home', return_value=self.fake_home), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=self.fake_disabled_skills
        ), patch.object(
            cr, 'resolve_codex_executable', return_value='/opt/homebrew/bin/codex'
        ), patch.object(cr.subprocess, 'run', side_effect=fake_run):
            res_scoped = cr.run_review(wf_dir, ws, self.root / 'out-scoped', profile='scoped')
            res_base = cr.run_review(wf_dir, ws, self.root / 'out-baseline', profile='baseline')

        self.assertEqual(res_scoped['verdict'], 'PASS')
        self.assertEqual(res_base['verdict'], 'PASS')
        self.assertEqual(len(calls), 2)

        scoped_argv, scoped_kw = calls[0]
        base_argv, base_kw = calls[1]

        for argv in (scoped_argv, base_argv):
            self.assertIn('--ask-for-approval', argv)
            self.assertEqual(argv[argv.index('--ask-for-approval') + 1], 'never')
            self.assertIn('exec', argv)
            self.assertIn('--ephemeral', argv)
            self.assertIn('--skip-git-repo-check', argv)
            self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
            self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6.1-sol')
            self.assertIn('--json', argv)
            self.assertIn('--output-schema', argv)
            self.assertIn('model_reasoning_effort="max"', argv)
            self.assertIn('mcp_servers.github_mcp.enabled=false', argv)
            self.assertIn('mcp_servers.playwright.enabled=false', argv)
            self.assertEqual(argv[-1], '-')
            self.assertNotIn('--ignore-user-config', argv)
            self.assertNotIn('skills.max_context_tokens=800', argv)
            self.assertNotIn('tools.view_image=false', argv)
            self.assertNotIn('do-not-log-this-value', ' '.join(argv))

        # Scoped-only catalog flags and per-file skill selectors
        expected_skills_override = cr.format_skills_config_override(self.fake_disabled_skills)
        for flag in (
            'features.shell_tool=false',
            'features.apps=false',
            'features.multi_agent=false',
            'features.browser_use=false',
            'features.computer_use=false',
            'features.image_generation=false',
            'features.view_image=false',
            'web_search="disabled"',
            expected_skills_override,
        ):
            self.assertIn(flag, scoped_argv)
            self.assertNotIn(flag, base_argv)

        # Both prompts include full using-superpowers and verification-before-completion SKILL contents via stdin
        for prompt in (scoped_kw['input'], base_kw['input']):
            self.assertIn('# Using Superpowers', prompt)
            self.assertIn('# Verification Before Completion', prompt)
        # Scoped prompt is smaller than baseline prompt (deduplicated packet vs 4 raw objects)
        self.assertLess(len(scoped_kw['input']), len(base_kw['input']))

        # All-in accounting: Codex (8500 + 320 = 8820; cached 6400 separate) + Provider I/O (24100+860 + 1840 + 10010+200+805 = 37815)
        self.assertEqual(res_scoped['codex_usage']['input_tokens'], 8500)
        self.assertEqual(res_scoped['codex_usage']['cached_input_tokens'], 6400)
        self.assertEqual(res_scoped['codex_usage']['output_tokens'], 320)
        self.assertEqual(res_scoped['codex_usage']['total_tokens'], 8820)
        self.assertEqual(res_scoped['provider_io_usage']['provider_io_total_tokens'], 37815)
        self.assertEqual(res_scoped['all_in_total_tokens'], 8820 + 37815)

        # Artifacts written in exclusive output_dir; original workflow-result and proof-pack untouched
        for fname in ('argv.json', 'codex-invocation.json', 'prompt.txt', 'schema.json', 'packet.json', 'codex.stdout.jsonl', 'codex.stderr.txt', 'response.json', 'chatgpt-review-result.json'):
            self.assertTrue((self.root / 'out-scoped' / fname).is_file(), fname)
        self.assertEqual((wf_dir / 'workflow-result.json').read_bytes(), orig_wf_bytes)
        self.assertEqual((wf_dir / 'proof-pack.txt').read_bytes(), orig_pack_bytes)

    def test_source_mutation_timeout_and_partial_usage_fail_closed(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root / 'mut')

        def mutating_codex(argv, **kwargs):
            (ws / 'text_utils.py').write_text('# mutated during review\n', encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0, stdout=make_codex_stream(), stderr='')

        with patch.object(Path, 'home', return_value=self.fake_home), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=self.fake_disabled_skills
        ), patch.object(
            cr, 'resolve_codex_executable', return_value='/opt/homebrew/bin/codex'
        ), patch.object(cr.subprocess, 'run', side_effect=mutating_codex):
            res_mut = cr.run_review(wf_dir, ws, self.root / 'out-mut', profile='scoped')
        self.assertEqual(res_mut['verdict'], 'BLOCKED')
        self.assertFalse(res_mut['source_hashes_unchanged_during_chatgpt_review'])

        # Timeout with partial usage saves available counters and marks missing as 'unknown'
        wf_dir2, ws2 = make_fixture_tree(self.root / 'timeout')
        partial_stdout = json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 4200, 'cached_input_tokens': 3000}}) + '\n'
        timeout_exc = subprocess.TimeoutExpired(['codex'], 10, output=partial_stdout, stderr='timed out')
        with patch.object(Path, 'home', return_value=self.fake_home), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=self.fake_disabled_skills
        ), patch.object(
            cr, 'resolve_codex_executable', return_value='/opt/homebrew/bin/codex'
        ), patch.object(cr.subprocess, 'run', side_effect=timeout_exc):
            res_to = cr.run_review(wf_dir2, ws2, self.root / 'out-timeout', profile='scoped', timeout=10)
        self.assertEqual(res_to['verdict'], 'BLOCKED')
        self.assertEqual(res_to['codex_usage']['input_tokens'], 4200)
        self.assertEqual(res_to['codex_usage']['cached_input_tokens'], 3000)
        self.assertEqual(res_to['codex_usage']['output_tokens'], 'unknown')
        self.assertEqual(res_to['all_in_total_tokens'], 'unknown')

    def test_prepare_only_makes_zero_calls_and_reports_prepared_pending(self) -> None:
        wf_dir, ws = make_fixture_tree(self.root / 'prep')
        out_dir = self.root / 'out-prep'
        stdout_buf = io.StringIO()
        with patch.object(Path, 'home', return_value=self.fake_home), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=self.fake_disabled_skills
        ), patch.object(
            cr.subprocess, 'run'
        ) as mock_run, patch('sys.stdout', stdout_buf):
            rc = cr.main([
                '--workflow-dir', str(wf_dir),
                '--workspace', str(ws),
                '--output-dir', str(out_dir),
                '--profile', 'scoped',
                '--prepare-only',
            ])
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        saved = json.loads((out_dir / 'chatgpt-review-result.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'PREPARED')
        self.assertEqual(saved['verdict'], 'PENDING')
        self.assertNotEqual(saved['verdict'], 'PASS')
        self.assertIn('PREPARED/PENDING', stdout_buf.getvalue())

    def test_discover_disabled_skill_paths_filters_unrelated_retains_mandatory_and_guards_symlinks(self) -> None:
        disc_home = make_superpowers_home(self.root / 'disc')
        unrel_a = disc_home / '.agents/skills/extra-alpha/SKILL.md'
        unrel_b = disc_home / '.codex/skills/extra-beta/SKILL.md'
        unrel_c = disc_home / '.codex/plugins/cache/other/1.0/skills/extra-gamma/SKILL.md'
        kept_task = disc_home / '.agents/skills/antigravity-task-runner/SKILL.md'
        kept_quant = disc_home / '.agents/skills/antigravity-sol-quant-runner/SKILL.md'
        kept_jev = disc_home / '.codex/skills/jev/SKILL.md'
        prior_user_disabled = disc_home / 'custom/prior-disabled/SKILL.md'
        for p in (unrel_a, unrel_b, unrel_c, kept_task, kept_quant, kept_jev, prior_user_disabled):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('---\nname: test\n---\n# Skill\n', encoding='utf-8')
        # Ignore non-SKILL.md files inside skill directories
        (unrel_a.parent / 'README.md').write_text('notes\n', encoding='utf-8')

        cfg_path = disc_home / '.codex/config.toml'
        cfg_path.write_text(
            f'model = "gpt-6.1-sol"\n[[skills.config]]\npath = "{prior_user_disabled.resolve()}"\nenabled = false\n',
            encoding='utf-8',
        )

        disabled = cr.discover_disabled_skill_paths(home_dir=disc_home, config_path=cfg_path)
        disabled_set = set(disabled)

        for unrel in (unrel_a, unrel_b, unrel_c, prior_user_disabled):
            self.assertIn(str(unrel.resolve()), disabled_set)
        sp_root = disc_home / '.codex/plugins/cache/openai-curated-remote/superpowers/7.0.1/skills'
        for kept in (
            kept_task,
            kept_quant,
            kept_jev,
            sp_root / 'using-superpowers/SKILL.md',
            sp_root / 'verification-before-completion/SKILL.md',
        ):
            self.assertNotIn(str(kept.resolve()), disabled_set)
            self.assertNotIn(os.path.abspath(kept), disabled_set)

        # User config attempting to disable a mandatory skill is rejected
        bad_cfg = disc_home / '.codex/bad-config.toml'
        bad_cfg.write_text(
            f'[[skills.config]]\npath = "{kept_task.resolve()}"\nenabled = false\n',
            encoding='utf-8',
        )
        with self.assertRaises(ValueError):
            cr.discover_disabled_skill_paths(home_dir=disc_home, config_path=bad_cfg)

        # Symlink loop inside discovery tree fails closed without infinite recursion
        loop_link = disc_home / '.agents/skills/extra-alpha/subloop'
        loop_link.symlink_to(disc_home / '.agents/skills')
        with self.assertRaises(ValueError):
            cr.discover_disabled_skill_paths(home_dir=disc_home, config_path=cfg_path)


if __name__ == '__main__':
    unittest.main()
