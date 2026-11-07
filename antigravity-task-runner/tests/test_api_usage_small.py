import inspect
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / 'scripts'
if not SCRIPTS_DIR.is_dir():
    SCRIPTS_DIR = Path(__file__).resolve().parent / 'scripts'
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import free_api_worker as f

_RETURNS_STATUS_TUPLE = bool(
    re.search(r'\w+\s*,\s*\w+\s*=\s*http_transport\(', inspect.getsource(f))
)


def _wrap_http_payload(payload_obj: dict[str, object]):
    return (200, json.dumps(payload_obj).encode('utf-8'))


class FreeApiUsageSmallTests(unittest.TestCase):
    def _run_attempt_with_post_fixture(self, post_payload: dict[str, object]):
        catalog_payload = {
            'data': [
                {
                    'id': 'vendor/demo:free',
                    'pricing': {
                        'prompt': '0',
                        'completion': '0',
                        'request': '0',
                        'image': '0',
                        'web_search': '0',
                        'internal_reasoning': '0',
                    },
                }
            ]
        }

        def http_transport(*args, **kwargs):
            joined = ' '.join(str(a) for a in args) + ' ' + ' '.join(str(v) for v in kwargs.values())
            if 'GET' in joined or '/models' in joined:
                return _wrap_http_payload(catalog_payload)
            return _wrap_http_payload(post_payload)

        with tempfile.TemporaryDirectory() as tmp_root:
            root = Path(tmp_root)
            workspace = root / 'workspace'
            attempt_dir = root / 'attempt'
            workspace.mkdir(parents=True, exist_ok=True)
            attempt_dir.mkdir(parents=True, exist_ok=True)
            sample_file = workspace / 'sample.txt'
            sample_file.write_text('original\n', encoding='utf-8')

            with mock.patch.object(
                f,
                'load_api_credential',
                return_value=(
                    'harmless-synthetic-marker',
                    {'credential_source_status': 'VERIFIED_PRIVATE_FILE'},
                ),
            ):
                result = f.run_free_api_attempt(
                    provider='openrouter',
                    workspace=workspace,
                    attempt_dir=attempt_dir,
                    contract='smoke fixture task',
                    scope_files=['sample.txt'],
                    verify_cmds=['python3 -B -m unittest discover -q'],
                    timeout_seconds=5,
                    model='vendor/demo:free',
                    http_transport=http_transport,
                )
            resp_files = sorted(attempt_dir.glob('*response*.json'))
            retained_bytes = resp_files[0].read_bytes() if resp_files else b''
            workspace_unchanged = sample_file.read_text(encoding='utf-8') == 'original\n'
            return result, retained_bytes, workspace_unchanged

    def test_finish_reason_length_retains_raw_response_and_native_usage(self) -> None:
        post_fixture = {
            'id': 'fixture-id',
            'model': 'vendor/demo:free',
            'choices': [
                {
                    'index': 0,
                    'finish_reason': 'length',
                    'message': {'role': 'assistant', 'content': ''},
                }
            ],
            'usage': {
                'prompt_tokens': 100,
                'completion_tokens': 4096,
                'total_tokens': 4196,
                'prompt_tokens_details': {'cached_tokens': 25},
                'completion_tokens_details': {'reasoning_tokens': 4000},
                'cost': 0,
            },
        }
        result, retained_bytes, workspace_unchanged = self._run_attempt_with_post_fixture(post_fixture)
        self.assertTrue(workspace_unchanged)
        self.assertIs(result['blocked'], True)
        self.assertIs(result['ok'], False)
        self.assertTrue(retained_bytes)
        self.assertEqual(result['response_sha256'], f.sha256_bytes(retained_bytes))
        self.assertEqual(json.loads(retained_bytes.decode('utf-8')), post_fixture)
        self.assertEqual(
            result['usage'],
            {
                'prompt_tokens': 100,
                'completion_tokens': 4096,
                'total_tokens': 4196,
                'cached_tokens': 25,
                'reasoning_tokens': 4000,
                'total_steps': 1,
            },
        )
        self.assertEqual(result['usage_updates_observed'], 1)

    def test_missing_or_malformed_usage_preserves_empty_usage_without_synthetic_zeros(self) -> None:
        for bad_usage in (None, {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 999}):
            post_fixture = {
                'id': 'fixture-id',
                'model': 'vendor/demo:free',
                'choices': [
                    {
                        'index': 0,
                        'finish_reason': 'length',
                        'message': {'role': 'assistant', 'content': ''},
                    }
                ],
            }
            if bad_usage is not None:
                post_fixture['usage'] = bad_usage
            result, retained_bytes, workspace_unchanged = self._run_attempt_with_post_fixture(post_fixture)
            self.assertTrue(workspace_unchanged)
            self.assertIs(result['blocked'], True)
            self.assertIs(result['ok'], False)
            self.assertEqual(result['response_sha256'], f.sha256_bytes(retained_bytes))
            self.assertEqual(result['usage'], {})
            self.assertEqual(result['usage_updates_observed'], 0)

    def test_request_builder_controls(self) -> None:
        zai_req = f.build_chat_request_payload('zai', 'glm-4.5-flash', 'ping')
        self.assertEqual(zai_req['max_tokens'], 4096)
        self.assertEqual(zai_req['response_format'], {'type': 'json_object'})
        self.assertNotIn('reasoning', zai_req)

        or_req = f.build_chat_request_payload('openrouter', 'vendor/demo:free', 'ping')
        self.assertEqual(or_req['max_tokens'], 4096)
        self.assertEqual(or_req['reasoning'], {'enabled': False})
        self.assertEqual(or_req['response_format'], {'type': 'json_object'})
        self.assertEqual(or_req['provider']['allow_fallbacks'], False)


if __name__ == '__main__':
    unittest.main()
