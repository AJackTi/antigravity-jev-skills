import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import free_api_worker as f


class TestOpenRouterRequiredJson(unittest.TestCase):
    def test_openrouter_gemma_request_enforces_required_json_and_free_controls(self) -> None:
        prompt = 'Return strict JSON handoff and files.'
        req = f.build_chat_request_payload(
            'openrouter', 'google/gemma-4-31b-it:free', prompt
        )
        self.assertEqual(req['model'], 'google/gemma-4-31b-it:free')
        self.assertEqual(req['response_format'], {'type': 'json_object'})
        self.assertEqual(req['max_tokens'], 4096)
        self.assertEqual(req['temperature'], 0)
        self.assertIs(req['stream'], False)
        self.assertEqual(req['reasoning'], {'enabled': False})
        self.assertIs(req['reasoning']['enabled'], False)
        self.assertEqual(
            req['max_price'],
            {'prompt': 0, 'completion': 0, 'request': 0},
        )
        self.assertEqual(
            req['provider'],
            {
                'allow_fallbacks': False,
                'require_parameters': True,
                'max_price': {'prompt': 0, 'completion': 0, 'request': 0},
            },
        )
        self.assertIs(req['provider']['allow_fallbacks'], False)
        self.assertIs(req['provider']['require_parameters'], True)
        self.assertEqual(req['messages'][-1], {'role': 'user', 'content': prompt})
        self.assertNotIn('tools', req)
        self.assertNotIn('tool_choice', req)
        self.assertNotIn('json_schema', req)
        self.assertNotIn('json_schema', req['response_format'])

    def test_zai_request_controls_unchanged(self) -> None:
        prompt = 'Return strict JSON.'
        req = f.build_chat_request_payload('zai', 'glm-4.5-flash', prompt)
        self.assertEqual(req['model'], 'glm-4.5-flash')
        self.assertEqual(req['messages'], [{'role': 'user', 'content': prompt}])
        self.assertEqual(req['temperature'], 0)
        self.assertEqual(req['max_tokens'], 4096)
        self.assertIs(req['stream'], False)
        self.assertEqual(req['thinking'], {'type': 'disabled'})
        self.assertEqual(req['response_format'], {'type': 'json_object'})
        self.assertNotIn('provider', req)

    def test_parse_generation_accepts_valid_json_and_rejects_trailing_brace(self) -> None:
        self.assertTrue(issubclass(f.FreeApiBlocked, ValueError))
        self.assertTrue(issubclass(f.devin.DevinBlocked, ValueError))
        scope_files = ['example.txt']
        fixture = json.dumps({'handoff': 'ok', 'files': {'example.txt': 'value'}})
        parsed = f.parse_generation(fixture, scope_files)
        self.assertIsNotNone(parsed)
        with self.assertRaises((f.FreeApiBlocked, f.devin.DevinBlocked)):
            f.parse_generation(fixture + '}', scope_files)


if __name__ == '__main__':
    unittest.main()
