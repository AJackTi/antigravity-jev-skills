import unittest
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))
import run_workflow


class TestApiDispatch(unittest.TestCase):
    """Test free API dispatch in run_workflow for zai and openrouter providers."""

    @patch.dict(os.environ, {'AGY_MODEL': 'gemini-4-argon-high', 'OPENROUTER_MODEL': 'vendor/demo:free', 'ZAI_MODEL': 'glm-4.5-flash'}, clear=False)
    @patch('run_workflow.devin.run_devin_attempt')
    @patch('run_workflow.run_worker_attempt')
    @patch('run_workflow.free_api.run_free_api_attempt')
    def test_zai_dispatch_blocked(self, mock_free_api, mock_agy, mock_devin):
        """Test zai provider dispatch with blocked fixture."""
        mock_free_api.return_value = {
            'ok': False,
            'blocked': True,
            'reason': 'fixture unavailable',
            'provider': 'zai',
            'model': 'glm-4.5-flash',
            'context_mode': 'lean',
            'request_id': 'fixture-id',
            'usage': {},
            'usage_updates_observed': 0,
            'exit_code': 1,
            'duration_seconds': 5.0,
            'files_written': [],
            'worker_proof': {'proof': 'https://example.com/proof'}
        }

        with tempfile.TemporaryDirectory() as temp_base:
            workspace = Path(temp_base) / 'workspace'
            output_dir = Path(temp_base) / 'output'
            workspace.mkdir()
            (workspace / 'example.py').write_text('print("test")')

            result = run_workflow.run_workflow(
                task_id='test-task',
                objective='Test objective',
                workspace=workspace,
                output_dir=output_dir,
                scope_files=['example.py'],
                verify_cmds=['python3 -B -m unittest discover -v'],
                max_reworks=0,
                worker_provider='zai'
            )

        mock_free_api.assert_called_once()
        call_kwargs = mock_free_api.call_args[1]
        self.assertEqual(call_kwargs['provider'], 'zai')
        self.assertEqual(call_kwargs['model'], 'glm-4.5-flash')
        mock_agy.assert_not_called()
        mock_devin.assert_not_called()
        self.assertEqual(result['worker_provider'], 'zai')
        self.assertEqual(result['verdict'], 'BLOCKED')

    @patch.dict(os.environ, {'AGY_MODEL': 'gemini-4-argon-high', 'OPENROUTER_MODEL': 'vendor/demo:free'}, clear=False)
    @patch('run_workflow.devin.run_devin_attempt')
    @patch('run_workflow.run_worker_attempt')
    @patch('run_workflow.free_api.run_free_api_attempt')
    def test_openrouter_dispatch_blocked(self, mock_free_api, mock_agy, mock_devin):
        """Test openrouter provider dispatch with blocked fixture."""
        mock_free_api.return_value = {
            'ok': False,
            'blocked': True,
            'reason': 'fixture unavailable',
            'provider': 'openrouter',
            'model': 'vendor/demo:free',
            'context_mode': 'lean',
            'request_id': 'fixture-id',
            'usage': {},
            'usage_updates_observed': 0,
            'exit_code': 1,
            'duration_seconds': 5.0,
            'files_written': [],
            'worker_proof': {'proof': 'https://example.com/proof'}
        }

        with tempfile.TemporaryDirectory() as temp_base:
            workspace = Path(temp_base) / 'workspace'
            output_dir = Path(temp_base) / 'output'
            workspace.mkdir()
            (workspace / 'example.py').write_text('print("test")')

            result = run_workflow.run_workflow(
                task_id='test-task',
                objective='Test objective',
                workspace=workspace,
                output_dir=output_dir,
                scope_files=['example.py'],
                verify_cmds=['python3 -B -m unittest discover -v'],
                max_reworks=0,
                worker_provider='openrouter'
            )

        mock_free_api.assert_called_once()
        call_kwargs = mock_free_api.call_args[1]
        self.assertEqual(call_kwargs['provider'], 'openrouter')
        self.assertEqual(call_kwargs['model'], 'vendor/demo:free')
        mock_agy.assert_not_called()
        mock_devin.assert_not_called()
        self.assertEqual(result['worker_provider'], 'openrouter')
        self.assertEqual(result['verdict'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
