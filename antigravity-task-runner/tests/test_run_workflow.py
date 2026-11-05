"""Behavioral positive and negative tests for run_workflow.py."""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_workflow.py'
spec = importlib.util.spec_from_file_location('run_workflow', SCRIPT)
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)


def make_worker_stream(
    *,
    model: str = wf.GEMINI_MODEL,
    effort: str | None = 'high',
    permission_mode: str | None = 'request-review',
    status: str = 'SUCCESS',
    response: str = 'Understanding: ok\nPlan: 1\nFiles: mod.py\nChange: impl\nVerification: ok\nResidual risk: low',
    cid: str = 'worker-cid-1',
    usage: dict | None = None,
    denied_step: bool = False,
) -> str:
    init_obj = {'model': model}
    if permission_mode is not None:
        init_obj['permission_mode'] = permission_mode
    if effort is not None:
        init_obj['effort'] = effort
    events = [{'event': 'init', 'init': init_obj}]
    if denied_step:
        events.append({'event': 'step_update', 'step_update': {'step_type': 'tool', 'status': 'ERROR', 'error': 'permission check failed for read_file'}})
    events.append(
        {
            'event': 'result',
            'result': {
                'status': status,
                'response': response,
                'conversation_id': cid,
                'usage': usage or {'input_tokens': 1000, 'output_tokens': 200, 'thinking_tokens': 80, 'cache_read_tokens': 500, 'total_tokens': 1200},
            },
        }
    )
    return '\n'.join(json.dumps(e) for e in events)


def make_jev_assessment() -> dict:
    return {'label': 'adequate', 'confidence': 0.92, 'margin': 0.88, 'concern_probability': 0.01}


def write_valid_module_and_tests(workspace: Path) -> None:
    (workspace / 'mod.py').write_text('def parse_token(token: str) -> int:\n    token = token.strip()\n    return int(token)\n', encoding='utf-8')
    tests_dir = workspace / 'tests'
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / 'test_mod.py').write_text(
        'import unittest\nfrom mod import parse_token\n\nclass ModTests(unittest.TestCase):\n'
        '    def test_valid(self):\n        self.assertEqual(parse_token(" 42 "), 42)\n'
        '    def test_zero(self):\n        self.assertEqual(parse_token("0"), 0)\n',
        encoding='utf-8',
    )


class RunWorkflowTests(unittest.TestCase):
    def test_successful_single_pass_and_settings_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            out = root / 'out'

            def fake_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                settings = wf.build_worker_settings(workspace, scope_files, verify_cmds)
                self.assertEqual(settings['toolPermission'], 'request-review')
                self.assertFalse(settings['allowNonWorkspaceAccess'])
                self.assertIn('write_file(mod.py)', settings['permissions']['allow'])
                self.assertIn('command(python3 -B -m unittest discover -s tests -v)', settings['permissions']['allow'])
                write_valid_module_and_tests(workspace)
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'worker-cid-1',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': 'Implemented mod.py and tests.',
                    'duration_seconds': 1.2,
                    'usage': {'input_tokens': 1200, 'output_tokens': 300, 'thinking_tokens': 100, 'cache_read_tokens': 400, 'total_tokens': 1500},
                    'raw_terminal_usage': {'input_tokens': 1200, 'output_tokens': 300, 'thinking_tokens': 100, 'cache_read_tokens': 400, 'total_tokens': 1500},
                    'usage_basis': 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            fake_gate = {
                'route': 'standard_review',
                'verdict': 'PASS',
                'reasons': [],
                'jev_assessment': make_jev_assessment(),
                'jev_usage': {'input_tokens': 250, 'output_tokens': 40, 'total_tokens': 290},
                'reviewers': [
                    {
                        'model': wf.GEMINI_MODEL,
                        'effort': 'high',
                        'effort_verification': 'init effort field',
                        'conversation_id': 'fresh-reviewer-1',
                        'exit_code': 0,
                        'usage': {'input_tokens': 800, 'output_tokens': 150, 'thinking_tokens': 60, 'cache_read_tokens': 0, 'total_tokens': 950},
                        'usage_updates_observed': 1,
                        'answer': {'verdict': 'PASS', 'confidence': 94, 'findings': [], 'resolved_questions': [], 'residual_risk': 'Bounded unit test scope'},
                    }
                ],
            }

            with patch.object(wf, 'run_worker_attempt', side_effect=fake_worker), patch.object(wf.gate, 'run', return_value=fake_gate):
                res = wf.run_workflow(
                    task_id='t-pass',
                    objective='Implement token parser',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=['python3 -B -m unittest discover -s tests -v'],
                )

            self.assertEqual(res['verdict'], 'PASS')
            self.assertEqual(res['rework_loops'], 0)
            self.assertEqual(res['unique_tests_passed'], 2)
            self.assertTrue(res['source_hashes_unchanged_during_chatgpt_verification_and_review'])
            self.assertEqual(res['usage_totals']['gemini']['total_tokens'], 2450)
            self.assertEqual(res['usage_totals']['gemini']['cache_read_tokens'], 400)
            self.assertEqual(res['usage_totals']['jev']['total_tokens'], 290)
            self.assertTrue((out / 'workflow-result.json').is_file())
            self.assertTrue((out / 'proof-pack.txt').is_file())

    def test_mechanical_failure_triggers_rework_and_cumulative_usage_differencing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            out = root / 'out'
            calls = []

            def fake_subproc(argv, **kwargs):
                calls.append(argv)
                if len(calls) == 1:
                    (ws / 'mod.py').write_text('def parse_token(token: str) -> int:\n    return 0\n', encoding='utf-8')
                    (ws / 'tests').mkdir(exist_ok=True)
                    (ws / 'tests/test_mod.py').write_text(
                        'import unittest\nfrom mod import parse_token\nclass T(unittest.TestCase):\n'
                        '    def test_fail(self):\n        self.assertEqual(parse_token("5"), 5)\n',
                        encoding='utf-8',
                    )
                    stream = make_worker_stream(
                        cid='same-worker-cid',
                        usage={'input_tokens': 1000, 'output_tokens': 200, 'thinking_tokens': 50, 'cache_read_tokens': 300, 'total_tokens': 1200},
                    )
                else:
                    self.assertIn('--conversation', argv)
                    self.assertNotIn('--resume', argv)
                    self.assertEqual(argv[argv.index('--conversation') + 1], 'same-worker-cid')
                    write_valid_module_and_tests(ws)
                    stream = make_worker_stream(
                        cid='same-worker-cid',
                        usage={'input_tokens': 1600, 'output_tokens': 350, 'thinking_tokens': 90, 'cache_read_tokens': 1100, 'total_tokens': 1950},
                    )
                return subprocess.CompletedProcess(argv, 0, stdout=stream, stderr='')

            fake_gate = {
                'route': 'standard_review',
                'verdict': 'PASS',
                'reasons': [],
                'jev_assessment': make_jev_assessment(),
                'jev_usage': {'input_tokens': 200, 'output_tokens': 30, 'total_tokens': 230},
                'reviewers': [
                    {
                        'model': wf.GEMINI_MODEL,
                        'effort': 'high',
                        'effort_verification': 'init effort field',
                        'conversation_id': 'fresh-rev-2',
                        'exit_code': 0,
                        'usage': {'input_tokens': 500, 'output_tokens': 100, 'thinking_tokens': 20, 'cache_read_tokens': 0, 'total_tokens': 600},
                        'answer': {'verdict': 'PASS', 'confidence': 92, 'findings': [], 'resolved_questions': [], 'residual_risk': 'low'},
                    }
                ],
            }

            real_run = subprocess.run
            with patch.object(wf.gate, 'resolve_executable', return_value='/usr/bin/agy'), patch.object(
                wf.subprocess, 'run', side_effect=lambda argv, **kw: fake_subproc(argv, **kw) if argv[0] == '/usr/bin/agy' else real_run(argv, **kw)
            ), patch.object(
                wf.gate, 'run', return_value=fake_gate
            ) as gate_mock:
                res = wf.run_workflow(
                    task_id='t-rework',
                    objective='Fix parser test failure',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'],
                )

            self.assertEqual(res['verdict'], 'PASS')
            self.assertEqual(res['rework_loops'], 1)
            gate_mock.assert_called_once()
            worker_steps = [s for s in res['external_steps'] if s['role'] == 'worker']
            self.assertEqual(len(worker_steps), 2)
            self.assertEqual(worker_steps[0]['usage']['total_tokens'], 1200)
            self.assertEqual(worker_steps[1]['usage']['input_tokens'], 600)
            self.assertEqual(worker_steps[1]['usage']['output_tokens'], 150)
            self.assertEqual(worker_steps[1]['usage']['cache_read_tokens'], 800)
            self.assertEqual(worker_steps[1]['usage']['total_tokens'], 750)
            self.assertEqual(worker_steps[1]['raw_terminal_usage']['total_tokens'], 1950)
            # Total Gemini usage must be 1200 + 750 (rework delta) + 600 (reviewer) = 2550 (no double counting!)
            self.assertEqual(res['usage_totals']['gemini']['total_tokens'], 2550)
            self.assertEqual(res['usage_totals']['gemini']['cache_read_tokens'], 1100)

    def test_reviewer_reject_stops_at_max_two_reworks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            out = root / 'out'
            worker_invocations = 0

            def fake_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                nonlocal worker_invocations
                worker_invocations += 1
                self.assertLessEqual(wf.estimate_tokens(prompt), 400)
                write_valid_module_and_tests(workspace)
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'w-cid',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': f'Attempt {worker_invocations}',
                    'duration_seconds': 1.0,
                    'usage': {'input_tokens': 500, 'output_tokens': 100, 'thinking_tokens': 20, 'cache_read_tokens': 0, 'total_tokens': 600},
                    'raw_terminal_usage': {'input_tokens': 500 * worker_invocations, 'output_tokens': 100 * worker_invocations, 'thinking_tokens': 20, 'cache_read_tokens': 0, 'total_tokens': 600 * worker_invocations},
                    'usage_basis': 'difference of terminal usage counters in the same resumed worker conversation' if worker_invocations > 1 else 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            reject_gate = {
                'route': 'escalated_review',
                'verdict': 'REJECT',
                'reasons': ['missing negative boundary test'],
                'jev_assessment': make_jev_assessment(),
                'jev_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120},
                'reviewers': [
                    {
                        'model': wf.GEMINI_MODEL,
                        'effort': 'high',
                        'effort_verification': 'init effort field',
                        'conversation_id': 'rev-cid',
                        'exit_code': 0,
                        'usage': {'input_tokens': 300, 'output_tokens': 60, 'thinking_tokens': 10, 'cache_read_tokens': 0, 'total_tokens': 360},
                        'answer': {'verdict': 'REJECT', 'confidence': 90, 'findings': ['Missing negative boundary test for 65536'], 'resolved_questions': [], 'residual_risk': 'Defect remains'},
                    }
                ],
            }

            with patch.object(wf, 'run_worker_attempt', side_effect=fake_worker), patch.object(wf.gate, 'run', return_value=reject_gate):
                res = wf.run_workflow(
                    task_id='t-reject-cap',
                    objective='Implement parser',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'],
                )

            self.assertEqual(res['verdict'], 'REJECT')
            self.assertEqual(res['rework_loops'], 2)
            self.assertEqual(worker_invocations, 3)

    def test_worker_model_effort_denial_empty_and_timeout_fail_closed(self) -> None:
        cases = [
            ('wrong-model', make_worker_stream(model='gemini-flash')),
            ('wrong-effort', make_worker_stream(effort='low')),
            ('missing-permission-mode', make_worker_stream(permission_mode=None)),
            ('empty-response-after-exit-0', make_worker_stream(response='   ')),
            ('tool-denial-step', make_worker_stream(denied_step=True)),
            ('worker-self-blocked', make_worker_stream(response='BLOCKED: missing verification dependency')),
        ]
        for label, stream in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / 'ws'
                ws.mkdir()
                out = Path(tmp) / 'out'
                completed = subprocess.CompletedProcess(['agy'], 0, stdout=stream, stderr='')
                with patch.object(wf.gate, 'resolve_executable', return_value='/usr/bin/agy'), patch.object(
                    wf.subprocess, 'run', return_value=completed
                ), patch.object(wf.gate, 'run') as gate_mock:
                    res = wf.run_workflow(
                        task_id=label,
                        objective='Test fail closed',
                        workspace=ws,
                        output_dir=out,
                        scope_files=['mod.py'],
                    )
                self.assertEqual(res['verdict'], 'BLOCKED')
                self.assertEqual(res['rework_loops'], 0)
                gate_mock.assert_not_called()

        # Timeout case
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            out = Path(tmp) / 'out'
            timeout_exc = subprocess.TimeoutExpired(['agy'], 10, output=b'', stderr=b'Authorization: Bearer secret-tok-12345')
            with patch.object(wf.gate, 'resolve_executable', return_value='/usr/bin/agy'), patch.object(
                wf.subprocess, 'run', side_effect=timeout_exc
            ):
                res = wf.run_workflow(task_id='timeout', objective='Test timeout', workspace=ws, output_dir=out, scope_files=['mod.py'])
            self.assertEqual(res['verdict'], 'BLOCKED')
            self.assertTrue((out / 'attempt-1/worker-timeout.json').is_file())
            self.assertNotIn('secret-tok-12345', (out / 'attempt-1/worker.stderr.txt').read_text(encoding='utf-8'))

    def test_selected_claude_worker_args_success_and_model_mismatch(self) -> None:
        claude = 'claude-opus-5-5-high'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AGY_MODEL': claude}):
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            ok = subprocess.CompletedProcess(['agy'], 0, stdout=make_worker_stream(model=claude), stderr='')
            with patch.object(wf.gate, 'resolve_executable', return_value='/usr/bin/agy'), patch.object(wf.subprocess, 'run', return_value=ok) as run:
                res = wf.run_worker_attempt(ws, Path(tmp) / 'a1', 'prompt', ['mod.py'], ['python3 -B -m unittest'], 60)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--model') + 1], claude)
            self.assertEqual(argv[argv.index('--effort') + 1], 'high')
            self.assertEqual(json.loads((Path(tmp) / 'a1/worker-settings.json').read_text())['toolPermission'], 'request-review')
            self.assertTrue(res['ok'])
            self.assertEqual(res['model'], claude)
            # A Gemini init under a Claude selection is a mismatch, never relabelled
            wrong = subprocess.CompletedProcess(['agy'], 0, stdout=make_worker_stream(model=wf.GEMINI_MODEL), stderr='')
            with patch.object(wf.gate, 'resolve_executable', return_value='/usr/bin/agy'), patch.object(
                wf.subprocess, 'run', return_value=wrong
            ), patch.object(wf.gate, 'run') as gate_mock:
                flow = wf.run_workflow(task_id='mismatch', objective='Test mismatch', workspace=ws, output_dir=Path(tmp) / 'out', scope_files=['mod.py'])
            self.assertEqual(flow['verdict'], 'BLOCKED')
            gate_mock.assert_not_called()
            self.assertIn('unexpected worker model', flow['external_steps'][0]['reason'])
            self.assertEqual(flow['worker_and_reviewer_model'], claude)
            self.assertEqual(flow['external_steps'][0]['model'], wf.GEMINI_MODEL)
            self.assertNotIn('Gemini', flow['external_steps'][0]['step'])
            self.assertIn(claude, flow['proof_pack'])

    def test_unsupported_agy_model_blocks_before_external_calls(self) -> None:
        for bad in ('gpt-6.1-sol', 'claude-opus-5-5', 'gemini-4-argon'):
            with self.subTest(model=bad), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AGY_MODEL': bad}), patch.object(
                wf.gate, 'resolve_executable', return_value='/usr/bin/agy'
            ), patch.object(wf.subprocess, 'run') as run, patch.object(wf.gate, 'run') as gate_mock, patch.object(wf.gate, 'call_jev') as jev:
                ws = Path(tmp) / 'ws'
                ws.mkdir()
                res = wf.run_workflow(task_id='bad-model', objective='Test', workspace=ws, output_dir=Path(tmp) / 'out', scope_files=['mod.py'])
                self.assertEqual(res['verdict'], 'BLOCKED')
                self.assertIsNone(res['worker_and_reviewer_model'])
                self.assertTrue(any('unsupported AGY_MODEL' in r for r in res['reasons']))
                with self.assertRaises(ValueError):
                    wf.run_worker_attempt(ws, Path(tmp) / 'a1', 'p', ['mod.py'], ['true'], 60)
                run.assert_not_called()
                gate_mock.assert_not_called()
                jev.assert_not_called()

    def test_out_of_scope_and_protected_paths_fail_closed(self) -> None:
        # Protected path without authorization blocks preflight
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            out = Path(tmp) / 'out-protected'
            with patch.object(wf, 'run_worker_attempt') as worker_mock:
                res = wf.run_workflow(
                    task_id='prot',
                    objective='Edit live config',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['configs/live/prod.yaml'],
                )
            self.assertEqual(res['verdict'], 'BLOCKED')
            worker_mock.assert_not_called()

        # Worker writing out-of-scope file blocks immediately without calling gate
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            out = Path(tmp) / 'out-scope'

            def rogue_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                write_valid_module_and_tests(workspace)
                (workspace / 'unauthorised_helper.py').write_text('x = 1\n', encoding='utf-8')
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'w-1',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': 'Done',
                    'duration_seconds': 0.5,
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'thinking_tokens': 0, 'cache_read_tokens': 0, 'total_tokens': 120},
                    'raw_terminal_usage': {'input_tokens': 100, 'output_tokens': 20, 'thinking_tokens': 0, 'cache_read_tokens': 0, 'total_tokens': 120},
                    'usage_basis': 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            with patch.object(wf, 'run_worker_attempt', side_effect=rogue_worker), patch.object(wf.gate, 'run') as gate_mock:
                res = wf.run_workflow(
                    task_id='scope-violation',
                    objective='Test scope enforcement',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'],
                )
            self.assertEqual(res['verdict'], 'BLOCKED')
            self.assertEqual(res['rework_loops'], 0)
            gate_mock.assert_not_called()

    def test_quant_two_reviewers_and_stronger_reviewer_obligations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            # Missing quant contract blocks preflight
            res_missing = wf.run_workflow(
                task_id='q-missing',
                objective='Audit funding',
                workspace=ws,
                output_dir=Path(tmp) / 'out-1',
                kind='quant',
            )
            self.assertEqual(res_missing['verdict'], 'BLOCKED')

            # Requiring a stronger reviewer cannot be satisfied by the same Gemini 4 Argon High model
            # OR by an unverified caller-supplied model name string
            qc = {k: 'Explicit synthetic contract' for k in ('data_provenance', 'friction', 'causality', 'parity')}
            for supplied_model in (wf.GEMINI_MODEL, 'gpt-6.1-sol', 'claude-4-opus', None):
                res_stronger = wf.run_workflow(
                    task_id='q-stronger-blocked',
                    objective='Audit funding',
                    workspace=ws,
                    output_dir=Path(tmp) / f'out-stronger-{supplied_model}',
                    kind='quant',
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    quant_contract=qc,
                    require_stronger_reviewer=True,
                    stronger_reviewer_model=supplied_model,
                )
                self.assertEqual(res_stronger['verdict'], 'BLOCKED')
                self.assertFalse(res_stronger['same_model_is_stronger'])

            # Quant workflow enforces min_reviewers >= 2 even if caller passes min_reviewers=1
            captured_packets = []

            def fake_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                write_valid_module_and_tests(workspace)
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'quant-worker',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': 'Quant seam implemented.',
                    'duration_seconds': 1.0,
                    'usage': {'input_tokens': 400, 'output_tokens': 80, 'thinking_tokens': 20, 'cache_read_tokens': 0, 'total_tokens': 480},
                    'raw_terminal_usage': {'input_tokens': 400, 'output_tokens': 80, 'thinking_tokens': 20, 'cache_read_tokens': 0, 'total_tokens': 480},
                    'usage_basis': 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            def fake_gate_run(packet, gate_dir, slot=1, triage_only=False):
                captured_packets.append(packet)
                return {
                    'route': 'standard_review',
                    'verdict': 'PASS',
                    'reasons': [],
                    'jev_assessment': make_jev_assessment(),
                    'jev_usage': {'input_tokens': 150, 'output_tokens': 25, 'total_tokens': 175},
                    'reviewers': [
                        {
                            'model': wf.GEMINI_MODEL,
                            'effort': 'high',
                            'effort_verification': 'init effort field',
                            'conversation_id': f'quant-rev-{i}',
                            'exit_code': 0,
                            'usage': {'input_tokens': 300, 'output_tokens': 50, 'thinking_tokens': 10, 'cache_read_tokens': 0, 'total_tokens': 350},
                            'answer': {'verdict': 'PASS', 'confidence': 91, 'findings': [], 'resolved_questions': [], 'residual_risk': 'Synthetic fixture only'},
                        }
                        for i in (1, 2)
                    ],
                }

            with patch.object(wf, 'run_worker_attempt', side_effect=fake_worker), patch.object(wf.gate, 'run', side_effect=fake_gate_run):
                res_quant = wf.run_workflow(
                    task_id='q-pass',
                    objective='Audit funding parity',
                    workspace=ws,
                    output_dir=Path(tmp) / 'out-3',
                    kind='quant',
                    min_reviewers=1,
                    quant_contract=qc,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'],
                )
            self.assertEqual(res_quant['verdict'], 'PASS')
            self.assertEqual(captured_packets[0]['min_reviewers'], 2)
            self.assertEqual(len(res_quant['reviewers']), 2)
            self.assertTrue(res_quant['stronger_reviewer_required'])
            self.assertFalse(res_quant['same_model_is_stronger'])

    def test_long_packet_dotted_paths_and_late_constraint_preserved_for_worker_and_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            padding = '\n'.join(
                f'REQUIREMENT_{i:02d}: Validate deterministic port range invariant #{i:02d} across boundary inputs without silent coercion.'
                for i in range(1, 28)
            )
            late_marker = 'LATE_ACCEPTANCE_CONSTRAINT_REJECT_RESERVED_PORT_49151_EXACTLY'
            long_packet = (
                'TASK: Implement strict dotted-module parser pkg.v1/port.spec.py and tests/test_port_spec.py.\n\n'
                'WRITE SCOPE: only pkg.v1/port.spec.py and tests/test_port_spec.py. '
                f'The only authorized shell command is exactly: {sys.executable} -B -m unittest discover -s tests -p "test_*.py" -v\n\n'
                f'CONTRACT DETAILS:\n{padding}\n'
                f'FINAL BOUNDARY RULE: {late_marker}.\n'
            )
            self.assertGreater(len(long_packet), 2303)

            captured_worker_prompts: list[str] = []
            captured_reviewer_payloads: list[dict] = []

            def fake_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                captured_worker_prompts.append(prompt)
                self.assertEqual(scope_files, ['pkg.v1/port.spec.py', 'tests/test_port_spec.py'])
                pkg_dir = workspace / 'pkg.v1'
                pkg_dir.mkdir(parents=True, exist_ok=True)
                (pkg_dir / 'port.spec.py').write_text('RESERVED = 49151\n', encoding='utf-8')
                t_dir = workspace / 'tests'
                t_dir.mkdir(parents=True, exist_ok=True)
                (t_dir / 'test_port_spec.py').write_text(
                    'import unittest\nclass DottedTests(unittest.TestCase):\n'
                    '    def test_reserved(self):\n        self.assertEqual(49151, 49151)\n',
                    encoding='utf-8',
                )
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'worker-long-1',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': 'Implemented dotted module and tests.',
                    'duration_seconds': 1.1,
                    'usage': {'input_tokens': 900, 'output_tokens': 180, 'thinking_tokens': 40, 'cache_read_tokens': 0, 'total_tokens': 1080},
                    'raw_terminal_usage': {'input_tokens': 900, 'output_tokens': 180, 'thinking_tokens': 40, 'cache_read_tokens': 0, 'total_tokens': 1080},
                    'usage_basis': 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            def fake_gate_run(packet, gate_dir, slot=1, triage_only=False):
                self.assertEqual(wf.gate.check_packet(packet), [])
                rev_payload = wf.gate.build_reviewer_payload(packet, make_jev_assessment(), {'route': 'standard_review', 'reasons': []})
                captured_reviewer_payloads.append(rev_payload)
                return {
                    'route': 'standard_review',
                    'verdict': 'PASS',
                    'reasons': [],
                    'jev_assessment': make_jev_assessment(),
                    'jev_usage': {'input_tokens': 200, 'output_tokens': 30, 'total_tokens': 230},
                    'reviewers': [
                        {
                            'model': wf.GEMINI_MODEL,
                            'effort': 'high',
                            'effort_verification': 'init effort field',
                            'conversation_id': 'rev-long-1',
                            'exit_code': 0,
                            'usage': {'input_tokens': 700, 'output_tokens': 120, 'thinking_tokens': 30, 'cache_read_tokens': 0, 'total_tokens': 820},
                            'answer': {'verdict': 'PASS', 'confidence': 95, 'findings': [], 'resolved_questions': [], 'residual_risk': 'low'},
                        }
                    ],
                }

            with patch.object(wf, 'run_worker_attempt', side_effect=fake_worker), patch.object(wf.gate, 'run', side_effect=fake_gate_run):
                res = wf.run_workflow(
                    task_id='t-long-dotted',
                    objective='Short CLI summary objective',
                    packet_text=long_packet,
                    workspace=ws,
                    output_dir=root / 'out-long',
                )

            self.assertEqual(res['verdict'], 'PASS')
            self.assertEqual(res['source_files'], ['pkg.v1/port.spec.py', 'tests/test_port_spec.py'])
            # Worker prompt preserves the full >2303 char contract and late constraint plus literal-command rule
            self.assertEqual(len(captured_worker_prompts), 1)
            self.assertIn(long_packet.strip(), captured_worker_prompts[0])
            self.assertIn(late_marker, captured_worker_prompts[0])
            self.assertIn('literally and separately', captured_worker_prompts[0])
            # Reviewer payload receives the complete contract once (including late_marker)
            self.assertEqual(len(captured_reviewer_payloads), 1)
            rev_json = json.dumps(captured_reviewer_payloads[0])
            self.assertEqual(rev_json.count(late_marker), 1)
            self.assertIn(long_packet.strip(), captured_reviewer_payloads[0]['objective'])
            # Compact coordinator-parsed worker proof, full handoff and source workspace reach the reviewer
            ev = captured_reviewer_payloads[0]['evidence']
            self.assertEqual(ev['full_worker_handoff'], 'Implemented dotted module and tests.')
            self.assertIn('excerpt', ev['summary_label'])
            self.assertEqual(ev['source_workspace'], str(ws.resolve()))
            proof = ev['worker_proof']
            self.assertEqual((proof['actual_init_model'], proof['permission_mode'], proof['status'], proof['exit_code'], proof['tool_denied'], proof['conversation_id']),
                             (wf.GEMINI_MODEL, 'request-review', 'SUCCESS', 0, False, 'worker-long-1'))
            self.assertNotIn('raw_terminal_usage', rev_json)
            self.assertNotIn('worker', captured_reviewer_payloads[0])

            # Empty/missing write scope blocks rather than broadening to whole-workspace writes
            with self.assertRaises(ValueError):
                wf.build_worker_settings(ws, [], ['python3 -B -m unittest'])
            res_no_scope = wf.run_workflow(
                task_id='t-no-scope',
                objective='Task without scope',
                workspace=ws,
                output_dir=root / 'out-no-scope',
                scope_files=[],
            )
            self.assertEqual(res_no_scope['verdict'], 'BLOCKED')

            # Oversized packet beyond MAX_CONTRACT_CHARS blocks rather than truncating silently
            oversized = long_packet + ('X' * (wf.MAX_CONTRACT_CHARS + 100))
            res_over = wf.run_workflow(
                task_id='t-oversized',
                objective='Oversized packet',
                packet_text=oversized,
                workspace=ws,
                output_dir=root / 'out-oversized',
                scope_files=['pkg.v1/port.spec.py'],
            )
            self.assertEqual(res_over['verdict'], 'BLOCKED')

    def test_oversized_focused_diff_blocks_before_jev_or_reviewer_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            out = root / 'out-oversized-diff'

            def oversized_worker(workspace, attempt_dir, prompt, scope_files, verify_cmds, timeout_seconds, resume_conversation_id=None, prev_raw_by_cid=None):
                lines = ['def parse_token(token: str) -> int:\n', '    token = token.strip()\n']
                for idx in range(350):
                    lines.append(f'    invariant_rule_{idx:03d} = {idx} + len(token)\n')
                lines.append('    return int(token)\n')
                (workspace / 'mod.py').write_text(''.join(lines), encoding='utf-8')
                tests_dir = workspace / 'tests'
                tests_dir.mkdir(exist_ok=True)
                (tests_dir / 'test_mod.py').write_text(
                    'import unittest\nfrom mod import parse_token\n\nclass ModTests(unittest.TestCase):\n'
                    '    def test_valid(self):\n        self.assertEqual(parse_token(" 42 "), 42)\n'
                    '    def test_zero(self):\n        self.assertEqual(parse_token("0"), 0)\n',
                    encoding='utf-8',
                )
                return {
                    'ok': True,
                    'blocked': False,
                    'reason': None,
                    'model': wf.GEMINI_MODEL,
                    'effort': 'high',
                    'effort_verification': 'init effort field',
                    'permission_mode': 'request-review',
                    'conversation_id': 'worker-oversized-1',
                    'exit_code': 0,
                    'status': 'SUCCESS',
                    'tool_denied': False,
                    'response': 'Implemented large change and ran unit tests.',
                    'duration_seconds': 1.0,
                    'usage': {'input_tokens': 800, 'output_tokens': 250, 'thinking_tokens': 40, 'cache_read_tokens': 0, 'total_tokens': 1050},
                    'raw_terminal_usage': {'input_tokens': 800, 'output_tokens': 250, 'thinking_tokens': 40, 'cache_read_tokens': 0, 'total_tokens': 1050},
                    'usage_basis': 'initial CLI conversation counter',
                    'usage_updates_observed': 1,
                }

            with patch.object(wf, 'run_worker_attempt', side_effect=oversized_worker) as worker_mock, patch.object(
                wf.gate, 'run'
            ) as gate_run_mock, patch.object(wf.gate, 'call_jev') as jev_mock, patch.object(
                wf.gate, 'run_reviewer'
            ) as reviewer_mock:
                res = wf.run_workflow(
                    task_id='t-oversized-diff',
                    objective='Implement parser seam',
                    workspace=ws,
                    output_dir=out,
                    scope_files=['mod.py', 'tests/test_mod.py'],
                    verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'],
                )

            worker_mock.assert_called_once()
            gate_run_mock.assert_not_called()
            jev_mock.assert_not_called()
            reviewer_mock.assert_not_called()
            self.assertEqual(res['verdict'], 'BLOCKED')
            self.assertEqual(res['jev_route'], 'blocked')
            self.assertIsNone(res['jev_assessment'])
            self.assertEqual(res['reviewers'], [])
            self.assertEqual(res['rework_loops'], 0)
            self.assertTrue(any('budget' in r and 'split seam' in r for r in res['reasons']))
            # Raw local proof is retained without silent truncation
            self.assertEqual(res['test_exit_code'], 0)
            self.assertEqual(res['unique_tests_passed'], 2)
            self.assertEqual(res['source_files'], ['mod.py', 'tests/test_mod.py'])
            self.assertIn('mod.py', res['source_hashes'])
            mech_saved = json.loads((out / 'attempt-1/mechanical-checks.json').read_text(encoding='utf-8'))
            self.assertTrue(mech_saved['oversized_diff'])
            self.assertGreater(len(mech_saved['focused_diff']), wf.MAX_DIFF_CHARS)
            self.assertNotIn('[diff truncated', mech_saved['focused_diff'])
            self.assertIn('invariant_rule_349', mech_saved['focused_diff'])

    def test_verify_timeout_default_propagation_validation_and_timeout_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / 'ws'
            ws.mkdir()
            write_valid_module_and_tests(ws)
            verify_cmd = f'{sys.executable} -B -m unittest discover -s tests -v'

            for idx, invalid_val in enumerate((0, 1801, True, False)):
                with patch.object(wf, 'run_worker_attempt') as worker_mock:
                    res_bad = wf.run_workflow(
                        task_id=f't-bad-{idx}', objective='Reject bad verify_timeout', workspace=ws,
                        output_dir=root / f'out-bad-{idx}', scope_files=['mod.py'], verify_timeout=invalid_val,  # type: ignore[arg-type]
                    )
                worker_mock.assert_not_called()
                self.assertEqual(res_bad['verdict'], 'BLOCKED')
                with self.assertRaises(ValueError):
                    wf.run_mechanical_checks(ws, {}, ['mod.py'], [verify_cmd], verify_timeout=invalid_val)  # type: ignore[arg-type]

            observed_timeouts: list[int | None] = []
            real_run = subprocess.run

            def spy_run(argv, **kwargs):
                if argv and argv[0] != 'git':
                    observed_timeouts.append(kwargs.get('timeout'))
                return real_run(argv, **kwargs)

            with patch.object(wf.subprocess, 'run', side_effect=spy_run):
                mech_def = wf.run_mechanical_checks(ws, {}, ['mod.py', 'tests/test_mod.py'], [verify_cmd])
                mech_max = wf.run_mechanical_checks(ws, {}, ['mod.py', 'tests/test_mod.py'], [verify_cmd], verify_timeout=1800)
            self.assertTrue(mech_def['all_passed'] and mech_max['all_passed'])
            self.assertEqual(observed_timeouts, [120, 1800])

            with patch.object(wf.subprocess, 'run', side_effect=subprocess.TimeoutExpired([sys.executable], 7)):
                mech_to = wf.run_mechanical_checks(ws, {}, ['mod.py', 'tests/test_mod.py'], [verify_cmd], verify_timeout=7)
            tests_entry = next(c for c in mech_to['checks'] if c['kind'] == 'tests')
            self.assertFalse(mech_to['all_passed'])
            self.assertEqual((tests_entry['exit_code'], tests_entry['passed']), (2, 0))
            self.assertIn('timed out after 7 seconds', tests_entry['output'])


DEVIN_EXE = '/opt/fake/devin'
CALC_OK = {
    'calc.py': 'def add(a, b):\n    return a + b\n',
    'tests/test_calc.py': 'import unittest\nfrom calc import add\n\n\nclass CalcTests(unittest.TestCase):\n'
                          '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n',
}


def devin_atif(response: str, sid: str) -> dict:
    tool_defs = [
        {
            'type': 'function',
            'function': {
                'name': name,
                'description': f'{name} tool',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            },
        }
        for name in wf.devin.LEAN_REMAINING_TOOLS
    ]
    return {
        'schema_version': 'ATIF-v1.7', 'session_id': sid,
        'agent': {
            'name': 'devin', 'model_name': 'SWE-1.6 Slow',
            'tool_definitions': tool_defs,
            'extra': {'backend': 'Windsurf', 'permission_mode': 'Normal'},
        },
        'steps': [{'source': 'user', 'message': 'p'},
                  {'source': 'agent', 'model_name': 'swe-1-6-slow', 'message': response, 'tool_calls': [], 'extra': {'generation_model': 'swe-1-6-slow'}}],
        'final_metrics': {'total_prompt_tokens': 20000, 'total_completion_tokens': 400, 'total_cached_tokens': 8000, 'total_steps': 2},
    }


def reviewer_gate(captured: list, verdict: str = 'PASS'):
    def fake_gate_run(packet, gate_dir, slot=1, triage_only=False):
        captured.append(packet)
        return {
            'route': 'standard_review', 'verdict': verdict, 'reasons': [], 'jev_assessment': make_jev_assessment(),
            'jev_problems': wf.gate.check_packet(packet),
            'jev_usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110},
            'reviewers': [{'model': wf.gate.selected_model(), 'effort': 'high', 'effort_verification': 'init effort field',
                           'conversation_id': f'rev-{len(captured)}', 'exit_code': 0,
                           'usage': {'input_tokens': 500, 'output_tokens': 50, 'thinking_tokens': 0, 'cache_read_tokens': 0, 'total_tokens': 550},
                           'answer': {'verdict': verdict, 'confidence': 90, 'findings': [] if verdict == 'PASS' else ['defect'],
                                      'resolved_questions': [], 'residual_risk': 'synthetic'}}],
        }
    return fake_gate_run


class DevinWorkflowTests(unittest.TestCase):
    def run_devin(self, root: Path, responses: list[str], *, kind='task', **kw):
        ws = root / 'ws'
        ws.mkdir(exist_ok=True)
        calls: list = []
        captured: list = []
        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            if argv[0] != DEVIN_EXE:
                return real_run(argv, **kwargs)
            calls.append((argv, kwargs))
            response = responses[min(len(calls), len(responses)) - 1]
            Path(argv[argv.index('--export') + 1]).write_text(json.dumps(devin_atif(response, f'devin-s{len(calls)}')), encoding='utf-8')
            return subprocess.CompletedProcess(argv, 0, stdout=response + '\n', stderr='')

        qc = {k: 'Explicit synthetic contract' for k in ('data_provenance', 'friction', 'causality', 'parity')} if kind == 'quant' else None
        with patch.dict(os.environ, {'WORKFLOW_WORKER': 'devin', 'AGY_MODEL': 'claude-opus-5-5-high'}), patch.object(
            wf.devin, 'resolve_devin_executable', return_value=DEVIN_EXE
        ), patch.object(wf.subprocess, 'run', side_effect=fake_run), patch.object(wf.gate, 'run', side_effect=reviewer_gate(captured, **kw)), patch.object(
            wf, 'run_worker_attempt'
        ) as agy_worker:
            res = wf.run_workflow(task_id='devin-1', objective='Implement add ORIGINAL_CONTRACT_MARKER', workspace=ws, output_dir=root / 'out',
                                  kind=kind, quant_contract=qc, scope_files=['calc.py', 'tests/test_calc.py'],
                                  verify_cmds=[f'{sys.executable} -B -m unittest discover -s tests -v'])
        agy_worker.assert_not_called()
        return res, calls, captured

    def test_devin_worker_pass_keeps_provider_separation_and_agy_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res, calls, captured = self.run_devin(Path(tmp), [json.dumps({'handoff': 'Added add() and test.', 'files': CALC_OK})])
        self.assertEqual(res['verdict'], 'PASS', res['reasons'])
        self.assertEqual(len(calls), 1)
        packet = captured[0]
        with patch.dict(os.environ, {'AGY_MODEL': 'claude-opus-5-5-high'}):
            self.assertEqual(wf.gate.check_packet(packet), [])
        self.assertEqual(packet['worker']['provider'], 'devin')
        self.assertNotIn('effort', packet['worker'])
        self.assertNotIn('status', packet['worker'])
        proof = packet['evidence']['worker_proof']
        self.assertNotIn('actual_init_model', proof)
        self.assertEqual((proof['actual_generation_model'], proof['permission_mode'], proof['tool_calls']), ('swe-1-6-slow', 'Normal', 0))
        self.assertEqual(packet['evidence']['full_worker_handoff'], 'Added add() and test.')
        self.assertEqual((res['worker_provider'], res['worker_model'], res['worker_effort']), ('devin', 'swe-1-6-slow', None))
        self.assertEqual((res['reviewer_model'], res['reviewer_effort']), ('claude-opus-5-5-high', 'high'))
        self.assertIsNone(res['worker_and_reviewer_model'])
        self.assertIsNone(res['effort'])
        self.assertEqual(res['usage_totals']['gemini']['total_tokens'], 550)  # agy reviewer only, Devin never pooled
        self.assertEqual(res['usage_totals']['devin']['total_prompt_tokens'], 20000)
        self.assertEqual(res['usage_totals']['devin']['sessions_with_verified_usage'], 1)
        self.assertIn('worker=devin:swe-1-6-slow', res['proof_pack'])
        self.assertEqual(len(res['reviewers']), 1)

    def test_devin_rework_uses_fresh_session_with_original_contract_and_feedback(self) -> None:
        bad = dict(CALC_OK, **{'calc.py': 'def add(a, b):\n    return 0\n'})
        with tempfile.TemporaryDirectory() as tmp:
            res, calls, _ = self.run_devin(Path(tmp), [json.dumps({'handoff': 'v1', 'files': bad}), json.dumps({'handoff': 'v2', 'files': CALC_OK})])
        self.assertEqual(res['verdict'], 'PASS', res['reasons'])
        self.assertEqual(res['rework_loops'], 1)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][1]['cwd'], calls[1][1]['cwd'])
        second_prompt = calls[1][0][-1]
        self.assertIn('ORIGINAL_CONTRACT_MARKER', second_prompt)
        self.assertIn('FEEDBACK FROM PREVIOUS ATTEMPT', second_prompt)
        self.assertIn('return 0', second_prompt)  # current scoped source, not a resumed conversation
        self.assertFalse(any('--conversation' in a for a in calls[1][0]))
        self.assertEqual(res['usage_totals']['devin']['sessions_with_verified_usage'], 2)
        self.assertEqual(res['usage_totals']['devin']['total_completion_tokens'], 800)

    def test_devin_quant_keeps_two_reviewers_and_invalid_output_blocks_before_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res, _, captured = self.run_devin(Path(tmp), [json.dumps({'handoff': 'q', 'files': CALC_OK})], kind='quant')
        self.assertEqual(captured[0]['min_reviewers'], 2)
        self.assertTrue(res['stronger_reviewer_required'])
        with tempfile.TemporaryDirectory() as tmp:
            res, _, captured = self.run_devin(Path(tmp), [json.dumps({'handoff': 'x', 'files': {'evil.py': 'x\n'}})])
            self.assertFalse((Path(tmp) / 'ws/evil.py').exists())
        self.assertEqual(res['verdict'], 'BLOCKED')
        self.assertEqual(captured, [])

    def test_unknown_worker_or_bad_agy_model_blocks_before_external_calls(self) -> None:
        for env in ({'WORKFLOW_WORKER': 'opencode'}, {'WORKFLOW_WORKER': 'Devin'}, {'WORKFLOW_WORKER': 'devin', 'AGY_MODEL': 'swe-1-6-slow'}):
            with self.subTest(env=env), tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), patch.object(
                wf.subprocess, 'run'
            ) as run, patch.object(wf.devin, 'run_devin_attempt') as dev, patch.object(wf, 'run_worker_attempt') as agy, patch.object(wf.gate, 'run') as g:
                ws = Path(tmp) / 'ws'
                ws.mkdir()
                res = wf.run_workflow(task_id='b', objective='T', workspace=ws, output_dir=Path(tmp) / 'out', scope_files=['calc.py'])
                self.assertEqual(res['verdict'], 'BLOCKED')
                for m in (run, dev, agy, g):
                    m.assert_not_called()

    def test_agy_print_timeout_with_success_status_blocks(self) -> None:
        events = [{'event': 'init', 'init': {'model': wf.GEMINI_MODEL, 'permission_mode': 'request-review', 'effort': 'high'}},
                  {'event': 'result', 'result': {'status': 'SUCCESS', 'finished_reason': 'print_timeout', 'response': 'partial', 'conversation_id': 'c'}}]
        done = subprocess.CompletedProcess(['agy'], 0, stdout='\n'.join(json.dumps(e) for e in events), stderr='')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'WORKFLOW_WORKER': '', 'AGY_MODEL': ''}), patch.object(
            wf.gate, 'resolve_executable', return_value='/usr/bin/agy'
        ), patch.object(wf.subprocess, 'run', return_value=done), patch.object(wf.gate, 'run') as g:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            res = wf.run_workflow(task_id='pt', objective='T', workspace=ws, output_dir=Path(tmp) / 'out', scope_files=['mod.py'])
        self.assertEqual(res['verdict'], 'BLOCKED')
        self.assertIn('timeout', res['reasons'][0])
        self.assertEqual(res['worker_provider'], 'agy')
        g.assert_not_called()


class RtkAndContextWorkflowTests(unittest.TestCase):
    RAW_PYTEST = (
        '============================= test session starts ==============================\n'
        'collected 3 items\n\n'
        'tests/test_mod.py ...                                                    [100%]\n\n'
        '============================== 3 passed in 0.04s ===============================\n'
    )

    def test_rtk_filter_compresses_matching_pytest_and_writes_artifacts(self) -> None:
        done = subprocess.CompletedProcess(['/usr/local/bin/rtk', 'pipe', '-f', 'pytest'], 0, stdout='Pytest: 3 passed in 0.04s\n', stderr='')
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            wf, 'resolve_rtk_executable', return_value='/usr/local/bin/rtk'
        ), patch.object(wf.subprocess, 'run', return_value=done) as run_mock:
            attempt_dir = Path(tmp) / 'attempt-1'
            meta = wf.filter_verification_output(self.RAW_PYTEST, 0, 3, attempt_dir=attempt_dir)
            self.assertTrue(meta['rtk_applied'])
            self.assertEqual((meta['raw_exit_code'], meta['raw_passed'], meta['filtered_passed']), (0, 3, 3))
            self.assertEqual(meta['raw_output'], self.RAW_PYTEST)
            self.assertEqual(meta['filtered_output'], 'Pytest: 3 passed in 0.04s')
            self.assertGreater(meta['saved_chars'], 0)
            self.assertNotEqual(meta['raw_sha256'], meta['filtered_sha256'])
            self.assertEqual(Path(meta['raw_artifact']).read_text(encoding='utf-8'), self.RAW_PYTEST)
            self.assertEqual(Path(meta['filtered_artifact']).read_text(encoding='utf-8'), 'Pytest: 3 passed in 0.04s')
            run_mock.assert_called_once()

    def test_rtk_filter_bypasses_unsupported_or_failing_and_falls_back_raw(self) -> None:
        unittest_out = '....\n----------------------------------------------------------------------\nRan 4 tests in 0.01s\n\nOK\n'
        with patch.object(wf, 'resolve_rtk_executable') as loc_mock, patch.object(wf.subprocess, 'run') as run_mock:
            u_meta = wf.filter_verification_output(unittest_out, 0, 4)
            f_meta = wf.filter_verification_output(self.RAW_PYTEST, 1, 3)
        self.assertFalse(u_meta['rtk_applied'] or f_meta['rtk_applied'])
        self.assertIsNone(u_meta['filter'])
        self.assertEqual(u_meta['filtered_output'], unittest_out)
        self.assertEqual(f_meta['filtered_output'], self.RAW_PYTEST)
        loc_mock.assert_not_called()
        run_mock.assert_not_called()

        with patch.object(wf, 'resolve_rtk_executable', return_value=None), patch.object(wf.subprocess, 'run') as run_mock:
            no_exe = wf.filter_verification_output(self.RAW_PYTEST, 0, 3)
        self.assertFalse(no_exe['rtk_applied'])
        self.assertEqual(no_exe['filtered_output'], self.RAW_PYTEST)
        run_mock.assert_not_called()

        cases = [
            subprocess.CompletedProcess(['rtk'], 1, stdout='Pytest: 3 passed in 0.04s\n', stderr='err'),
            subprocess.TimeoutExpired(['rtk'], 5),
            subprocess.CompletedProcess(['rtk'], 0, stdout='   \n', stderr=''),
            subprocess.CompletedProcess(['rtk'], 0, stdout='Pytest: No tests collected\n', stderr=''),
            subprocess.CompletedProcess(['rtk'], 0, stdout='Pytest: 2 passed in 0.04s\n', stderr=''),
        ]
        for effect in cases:
            with self.subTest(effect=effect), patch.object(wf, 'resolve_rtk_executable', return_value='/usr/local/bin/rtk'), patch.object(
                wf.subprocess, 'run', side_effect=[effect]
            ):
                meta = wf.filter_verification_output(self.RAW_PYTEST, 0, 3)
                self.assertFalse(meta['rtk_applied'])
                self.assertEqual((meta['raw_exit_code'], meta['raw_passed']), (0, 3))
                self.assertEqual(meta['raw_output'], self.RAW_PYTEST)
                self.assertEqual(meta['filtered_output'], self.RAW_PYTEST)

    def test_invalid_workflow_context_blocks_before_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'WORKFLOW_CONTEXT': 'invalid'}), patch.object(
            wf.subprocess, 'run'
        ) as run, patch.object(wf.devin, 'run_devin_attempt') as dev, patch.object(wf, 'run_worker_attempt') as agy, patch.object(wf.gate, 'run') as g:
            ws = Path(tmp) / 'ws'
            ws.mkdir()
            res = wf.run_workflow(task_id='ctx-bad', objective='T', workspace=ws, output_dir=Path(tmp) / 'out', scope_files=['calc.py'])
            self.assertEqual(res['verdict'], 'BLOCKED')
            self.assertTrue(any('WORKFLOW_CONTEXT' in r for r in res['reasons']))
            for m in (run, dev, agy, g):
                m.assert_not_called()


if __name__ == '__main__':
    unittest.main()
