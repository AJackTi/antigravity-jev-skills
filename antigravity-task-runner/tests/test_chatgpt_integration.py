"""Focused offline integration tests for scripts/run_workflow.py --chatgpt-review (mocked pipeline + helper)."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
WF_SCRIPT = SCRIPT_DIR / 'run_workflow.py'
wf_spec = importlib.util.spec_from_file_location('run_workflow_integration_tests', WF_SCRIPT)
wf = importlib.util.module_from_spec(wf_spec)
wf_spec.loader.exec_module(wf)


def make_pipeline_report(
    workspace: Path,
    output_dir: Path,
    *,
    verdict: str = 'PASS',
    reasons: list[str] | None = None,
) -> tuple[dict[str, object], bytes, bytes]:
    source_hashes = {'text_utils.py': 'a' * 64}
    report: dict[str, object] = {
        'verdict': verdict,
        'id': 'int-1',
        'kind': 'task',
        'task': 'Fix whitespace predicate',
        'workspace': str(workspace),
        'source_files': ['text_utils.py'],
        'source_hashes': dict(source_hashes),
        'source_hashes_unchanged_during_chatgpt_verification_and_review': True,
        'source_or_tests_written_by_chatgpt': 0,
        'worker_provider': 'devin',
        'worker_model': 'swe-1-6-slow',
        'worker_effort': None,
        'reviewer_provider': 'agy',
        'reviewer_model': wf.GEMINI_MODEL,
        'reviewer_effort': 'high',
        'context_mode': 'lean',
        'prompt_chars': 2800,
        'context_optimization': {'context_mode': 'lean'},
        'rework_loops': 0,
        'max_reworks': 0,
        'unique_tests_passed': 10 if verdict == 'PASS' else 0,
        'coordinator_test_command': 'python3 -B -m unittest discover -s tests -v',
        'test_exit_code': 0 if verdict == 'PASS' else 1,
        'gate_exit_code': {'PASS': 0, 'REJECT': 1, 'BLOCKED': 2}.get(verdict, 2),
        'jev_route': 'standard_review' if verdict != 'BLOCKED' else 'blocked',
        'jev_assessment': {'label': 'adequate', 'confidence': 0.92, 'margin': 0.84, 'concern_probability': 0.03},
        'jev_error': None,
        'reasons': [] if reasons is None else list(reasons),
        'summary': 'Updated whitespace predicate.',
        'residual_risk': 'Bounded unit test scope only.',
        'reviewers': [],
        'stronger_reviewer_required': False,
        'same_model_is_stronger': False,
        'external_steps': [],
        'usage_totals': {
            'devin': {
                'total_prompt_tokens': 24100,
                'total_completion_tokens': 860,
                'total_cached_tokens': 5500,
                'total_steps': 8,
            },
            'jev': {'input_tokens': 1760, 'output_tokens': 80, 'total_tokens': 1840},
            'gemini': {
                'input_tokens': 10010,
                'output_tokens': 805,
                'thinking_tokens': 700,
                'cache_read_tokens': 200,
                'total_tokens': 10815,
            },
        },
        'attempts': [{'attempt': 1, 'stage': 'review_gate', 'status': verdict, 'reasons': []}],
        'proof_pack': f'TASK: int-1\nVERDICT: {verdict}\n',
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    wf_path = output_dir / 'workflow-result.json'
    pack_path = output_dir / 'proof-pack.txt'
    wf_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    pack_path.write_text(str(report['proof_pack']), encoding='utf-8')
    return report, wf_path.read_bytes(), pack_path.read_bytes()


def make_helper_result(
    *,
    verdict: str = 'PASS',
    status: str = 'COMPLETED',
    profile: str = 'scoped',
    source_hashes: dict[str, str] | None = None,
    hashes_unchanged: bool = True,
    codex_usage: dict[str, object] | None = None,
    provider_io_usage: dict[str, object] | None = None,
    all_in_total_tokens: int | str = 58510,
    findings: list[str] | None = None,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    hashes = {'text_utils.py': 'a' * 64} if source_hashes is None else dict(source_hashes)
    c_usage = (
        {
            'input_tokens': 20211,
            'cached_input_tokens': 15232,
            'cache_write_input_tokens': 2048,
            'uncached_input_tokens': 4979,
            'output_tokens': 320,
            'reasoning_output_tokens': 192,
            'total_tokens': 20531,
        }
        if codex_usage is None
        else codex_usage
    )
    p_usage = (
        {
            'devin_prompt_tokens': 24100,
            'devin_completion_tokens': 860,
            'devin_io_tokens': 24960,
            'devin_cached_tokens_separate': 5500,
            'jev_input_tokens': 1760,
            'jev_output_tokens': 80,
            'jev_io_tokens': 1840,
            'agy_input_tokens': 10010,
            'agy_cache_read_tokens': 200,
            'agy_output_tokens': 805,
            'agy_thinking_tokens_subset_of_output': 700,
            'agy_io_tokens': 11015,
            'provider_io_total_tokens': 37979,
        }
        if provider_io_usage is None
        else provider_io_usage
    )
    return {
        'status': status,
        'verdict': verdict,
        'confidence': 95.0 if verdict in ('PASS', 'REJECT') else None,
        'profile': profile,
        'prepare_only': False,
        'id': 'int-1',
        'kind': 'task',
        'configured_model': 'gpt-6.1-sol',
        'configured_reasoning_effort': 'max',
        'source_files': ['text_utils.py'],
        'source_hashes': hashes,
        'source_hashes_unchanged_during_chatgpt_review': hashes_unchanged,
        'codex_usage': c_usage,
        'provider_io_usage': p_usage,
        'all_in_total_tokens': all_in_total_tokens,
        'findings': [] if findings is None else list(findings),
        'reasons': [] if reasons is None else list(reasons),
        'residual_risk': 'Bounded scope only; verified in ephemeral read-only mode.',
    }


class ChatGPTIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / 'text_utils.py').write_text('def f(x):\n    return x\n', encoding='utf-8')

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explicit_off_compatibility_invokes_zero_chatgpt_review(self) -> None:
        out_in_ws = self.ws / 'artifacts-in-ws'

        def fake_pipeline(**kwargs):
            rep, _, _ = make_pipeline_report(kwargs['workspace'], kwargs['output_dir'], verdict='PASS')
            return rep

        fake_cr = MagicMock()
        stdout_buf = io.StringIO()
        with patch.object(wf, 'run_workflow', side_effect=fake_pipeline) as mock_wf, patch.object(
            wf, 'load_chatgpt_review', return_value=fake_cr
        ), patch('sys.stdout', stdout_buf):
            rc = wf.main([
                '--id', 'int-1',
                '--objective', 'Fix whitespace predicate',
                '--workspace', str(self.ws),
                '--output-dir', str(out_in_ws),
                '--chatgpt-review', 'off',
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(mock_wf.call_count, 1)
        fake_cr.run_review.assert_not_called()
        self.assertFalse((out_in_ws / 'workflow-final-result.json').exists())
        printed = json.loads(stdout_buf.getvalue())
        self.assertEqual(printed['verdict'], 'PASS')
        self.assertNotIn('chatgpt_review_mode', printed)

    def test_preflight_rejects_output_dir_inside_workspace_before_worker_call(self) -> None:
        bad_dirs = [
            self.ws,
            self.ws / 'nested-output',
            self.root,  # parent of workspace
        ]
        for idx, bad_out in enumerate(bad_dirs):
            with self.subTest(case=idx):
                fake_cr = MagicMock()
                stderr_buf = io.StringIO()
                with patch.object(wf, 'run_workflow') as mock_wf, patch.object(
                    wf, 'load_chatgpt_review', return_value=fake_cr
                ), patch('sys.stderr', stderr_buf):
                    rc = wf.main([
                        '--id', 'int-1',
                        '--objective', 'Fix whitespace predicate',
                        '--workspace', str(self.ws),
                        '--output-dir', str(bad_out),
                    ])
                self.assertEqual(rc, 2)
                mock_wf.assert_not_called()
                fake_cr.run_review.assert_not_called()

    def test_pipeline_pass_invokes_chatgpt_review_once_preserves_originals_and_writes_final_result(self) -> None:
        out_dir = self.root / 'wf-out'
        saved_bytes: dict[str, bytes] = {}

        def fake_pipeline(**kwargs):
            rep, wf_b, pack_b = make_pipeline_report(kwargs['workspace'], kwargs['output_dir'], verdict='PASS')
            saved_bytes['wf'] = wf_b
            saved_bytes['pack'] = pack_b
            return rep

        fake_cr = MagicMock()
        fake_cr.run_review.return_value = make_helper_result(verdict='PASS', profile='scoped')

        stdout_buf = io.StringIO()
        with patch.object(wf, 'run_workflow', side_effect=fake_pipeline) as mock_wf, patch.object(
            wf, 'load_chatgpt_review', return_value=fake_cr
        ), patch('sys.stdout', stdout_buf):
            rc = wf.main([
                '--id', 'int-1',
                '--objective', 'Fix whitespace predicate',
                '--workspace', str(self.ws),
                '--output-dir', str(out_dir),
                '--print-pack',
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(mock_wf.call_count, 1)
        self.assertEqual(fake_cr.run_review.call_count, 1)
        call_kw = fake_cr.run_review.call_args.kwargs
        self.assertEqual(Path(call_kw['workflow_dir']).resolve(), out_dir.resolve())
        self.assertEqual(Path(call_kw['workspace']).resolve(), self.ws.resolve())
        self.assertEqual(call_kw['profile'], 'scoped')
        child_out = Path(call_kw['output_dir']).resolve()
        self.assertEqual(child_out, out_dir.resolve().parent / 'wf-out-chatgpt-review')
        self.assertNotIn(self.ws.resolve(), child_out.parents)
        self.assertNotIn(out_dir.resolve(), child_out.parents)

        # Original workflow-result.json and proof-pack.txt remain byte-for-byte untouched
        self.assertEqual((out_dir / 'workflow-result.json').read_bytes(), saved_bytes['wf'])
        self.assertEqual((out_dir / 'proof-pack.txt').read_bytes(), saved_bytes['pack'])

        final_saved = json.loads((out_dir / 'workflow-final-result.json').read_text(encoding='utf-8'))
        self.assertEqual(final_saved['verdict'], 'PASS')
        self.assertEqual(final_saved['pipeline_verdict'], 'PASS')
        self.assertEqual(final_saved['chatgpt_review_verdict'], 'PASS')
        self.assertEqual(final_saved['all_in_total_tokens'], 58510)
        self.assertEqual(final_saved['codex_usage']['total_tokens'], 20531)
        self.assertEqual(final_saved['provider_io_usage']['provider_io_total_tokens'], 37979)
        self.assertIn('VERDICT: PASS (pipeline=PASS, chatgpt_review=PASS, mode=scoped)', stdout_buf.getvalue())
        self.assertIn('ALL_IN_TOTAL_TOKENS: 58510', stdout_buf.getvalue())

    def test_non_pass_pipeline_skips_chatgpt_review_and_propagates_verdict(self) -> None:
        for pipe_verdict, expected_rc in (('REJECT', 1), ('BLOCKED', 2)):
            with self.subTest(verdict=pipe_verdict):
                out_dir = self.root / f'wf-{pipe_verdict.lower()}'

                def fake_pipeline(**kwargs):
                    rep, _, _ = make_pipeline_report(
                        kwargs['workspace'],
                        kwargs['output_dir'],
                        verdict=pipe_verdict,
                        reasons=[f'pipeline {pipe_verdict}'],
                    )
                    return rep

                fake_cr = MagicMock()
                stdout_buf = io.StringIO()
                with patch.object(wf, 'run_workflow', side_effect=fake_pipeline) as mock_wf, patch.object(
                    wf, 'load_chatgpt_review', return_value=fake_cr
                ), patch('sys.stdout', stdout_buf):
                    rc = wf.main([
                        '--id', 'int-1',
                        '--objective', 'Fix whitespace predicate',
                        '--workspace', str(self.ws),
                        '--output-dir', str(out_dir),
                        '--chatgpt-review', 'scoped',
                    ])
                self.assertEqual(rc, expected_rc)
                self.assertEqual(mock_wf.call_count, 1)
                fake_cr.run_review.assert_not_called()
                final_saved = json.loads((out_dir / 'workflow-final-result.json').read_text(encoding='utf-8'))
                self.assertEqual(final_saved['verdict'], pipe_verdict)
                self.assertFalse(final_saved['chatgpt_review_invoked'])
                self.assertIsNone(final_saved['chatgpt_review_verdict'])

    def test_chatgpt_reject_exception_or_missing_usage_cannot_become_pass(self) -> None:
        # 1. ChatGPT REJECT -> final verdict REJECT (exit 1), no extra worker execution, never prints pipeline PASS as final
        out_rej = self.root / 'wf-gpt-reject'

        def fake_pipeline_rej(**kwargs):
            rep, _, _ = make_pipeline_report(kwargs['workspace'], kwargs['output_dir'], verdict='PASS')
            return rep

        fake_cr_rej = MagicMock()
        fake_cr_rej.run_review.return_value = make_helper_result(
            verdict='REJECT',
            findings=['Missing edge-case check for U+202F'],
        )
        stdout_rej = io.StringIO()
        with patch.object(wf, 'run_workflow', side_effect=fake_pipeline_rej) as mock_wf, patch.object(
            wf, 'load_chatgpt_review', return_value=fake_cr_rej
        ), patch('sys.stdout', stdout_rej):
            rc_rej = wf.main([
                '--id', 'int-1',
                '--objective', 'Fix whitespace predicate',
                '--workspace', str(self.ws),
                '--output-dir', str(out_rej),
                '--chatgpt-review', 'scoped',
                '--print-pack',
            ])
        self.assertEqual(rc_rej, 1)
        self.assertEqual(mock_wf.call_count, 1)
        self.assertEqual(fake_cr_rej.run_review.call_count, 1)
        self.assertIn('VERDICT: REJECT (pipeline=PASS, chatgpt_review=REJECT, mode=scoped)', stdout_rej.getvalue())
        self.assertNotIn('VERDICT: PASS\n', stdout_rej.getvalue())

        # 2. Helper exception or missing usage -> BLOCKED (exit 2), never PASS
        failure_cases = [
            ('helper-exception', MagicMock(side_effect=RuntimeError('codex failure'))),
            ('missing-usage', MagicMock(return_value=make_helper_result(verdict='PASS', all_in_total_tokens='unknown'))),
            ('mutated-hashes', MagicMock(return_value=make_helper_result(verdict='PASS', hashes_unchanged=False))),
        ]
        for label, run_review_mock in failure_cases:
            with self.subTest(case=label):
                out_err = self.root / f'wf-{label}'

                def fake_pipeline_err(**kwargs):
                    rep, _, _ = make_pipeline_report(kwargs['workspace'], kwargs['output_dir'], verdict='PASS')
                    return rep

                fake_cr_err = MagicMock()
                fake_cr_err.run_review = run_review_mock
                stdout_err = io.StringIO()
                with patch.object(wf, 'run_workflow', side_effect=fake_pipeline_err) as mock_wf_err, patch.object(
                    wf, 'load_chatgpt_review', return_value=fake_cr_err
                ), patch('sys.stdout', stdout_err):
                    rc_err = wf.main([
                        '--id', 'int-1',
                        '--objective', 'Fix whitespace predicate',
                        '--workspace', str(self.ws),
                        '--output-dir', str(out_err),
                        '--chatgpt-review', 'scoped',
                        '--print-pack',
                    ])
                self.assertEqual(rc_err, 2)
                self.assertEqual(mock_wf_err.call_count, 1)
                final_err = json.loads((out_err / 'workflow-final-result.json').read_text(encoding='utf-8'))
                self.assertEqual(final_err['verdict'], 'BLOCKED')
                self.assertEqual(final_err['all_in_total_tokens'], 'unknown')
                self.assertIn('VERDICT: BLOCKED', stdout_err.getvalue())
                self.assertNotIn('VERDICT: PASS\n', stdout_err.getvalue())


if __name__ == '__main__':
    unittest.main()
