import inspect
import json
from pathlib import Path
import sys
import unittest

SCRIPTS_DIR = (Path(__file__).resolve().parent / '../scripts').resolve()
if not SCRIPTS_DIR.is_dir():
    SCRIPTS_DIR = (Path(__file__).resolve().parent / 'scripts').resolve()
sys.path.insert(0, str(SCRIPTS_DIR))
import free_api_worker as f


def _pick_model(resolver, preferred: str) -> str:
    candidates = [preferred]
    for val in vars(f).values():
        if isinstance(val, str):
            candidates.append(val)
        elif isinstance(val, (set, frozenset, list, tuple, dict)):
            candidates.extend(x for x in val if isinstance(x, str))
    for candidate in candidates:
        try:
            resolver(candidate)
            return candidate
        except Exception:
            continue
    return preferred


class OpenRouterJsonMessagesTest(unittest.TestCase):
    def test_openrouter_payload_messages_and_invariants(self) -> None:
        prompt = 'DECLARED PATHS:\n- app.py\nKeep exact prompt.'
        or_model = _pick_model(f.resolve_openrouter_model, 'cohere/north-mini-code:free')
        payload = f.build_chat_request_payload('openrouter', or_model, prompt)

        messages = payload['messages']
        self.assertIsInstance(messages, list)
        self.assertEqual(messages[-1], {'role': 'user', 'content': prompt})
        self.assertEqual(messages[0]['role'], 'system')
        self.assertTrue(messages[0]['content'].strip())

        assistant_messages = [m for m in messages if m['role'] == 'assistant']
        self.assertEqual(len(assistant_messages), 1)
        example_obj = json.loads(assistant_messages[0]['content'])
        self.assertIsInstance(example_obj, dict)
        self.assertEqual(set(example_obj.keys()), {'handoff', 'files'})
        self.assertIsInstance(example_obj['handoff'], str)
        self.assertIsInstance(example_obj['files'], dict)
        self.assertTrue(example_obj['files'])
        self.assertTrue(
            all(isinstance(k, str) and isinstance(v, str) for k, v in example_obj['files'].items())
        )
        self.assertTrue(any('\n' in v and '"' in v for v in example_obj['files'].values()))
        self.assertEqual(json.loads(json.dumps(example_obj)), example_obj)

        self.assertEqual(payload['model'], f.resolve_openrouter_model(or_model)[0])
        self.assertEqual(payload['temperature'], 0)
        self.assertEqual(payload['max_tokens'], 4096)
        self.assertIs(payload['stream'], False)
        self.assertEqual(payload['reasoning'], {'enabled': False})
        self.assertEqual(payload['max_price'], dict(f.ZERO_MAX_PRICE))
        self.assertEqual(
            payload['provider'],
            {'allow_fallbacks': False, 'require_parameters': True, 'max_price': {'prompt': 0, 'completion': 0, 'request': 0}},
        )
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertNotIn('json_schema', payload)
        self.assertNotIn('tools', payload)

    def test_zai_messages_unchanged(self) -> None:
        prompt = 'ZAI prompt unchanged'
        zai_model = _pick_model(f.resolve_zai_model, 'glm-4.5-flash')
        payload = f.build_chat_request_payload('zai', zai_model, prompt)
        self.assertEqual(payload['messages'], [{'role': 'user', 'content': prompt}])
        self.assertEqual(payload['response_format'], {'type': 'json_object'})

    def test_parse_generation_rejects_trailing_closing_brace(self) -> None:
        valid_raw = json.dumps({'handoff': 'ok', 'files': {'a.py': 'VALUE = "1"\n'}})
        malformed_raw = valid_raw + '}'
        params = [
            p
            for p in inspect.signature(f.parse_generation).parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        extra_args = (['a.py'],) if len(params) > 1 else ()
        with self.assertRaises((f.FreeApiBlocked, ValueError)):
            f.parse_generation(malformed_raw, *extra_args)


if __name__ == '__main__':
    unittest.main()
