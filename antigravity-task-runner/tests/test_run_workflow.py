"""Behavioral positive and negative tests for run_workflow.py."""
from __future__ import annotations

import importlib.util
import json
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
    permission_mode: str = 'request-review',
    status: str = 'SUCCESS',
    response: str = 'Understanding: ok\nPlan: 1\nFiles: mod.py\nChange: impl\nVerification: ok\nResidual risk: low',
    cid: str = 'worker-cid-1',
    usage: dict | None = None,
    denied_step: bool = False,
) -> str:
    init_obj = {'model': model, 'permission_mode': permission_mode}
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
                    self.assertIn('--resume', argv)
                    self.assertIn('same-worker-cid', argv)
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


if __name__ == '__main__':
    unittest.main()
