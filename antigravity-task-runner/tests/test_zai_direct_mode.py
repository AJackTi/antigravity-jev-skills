import json
from pathlib import Path
import sys
import unittest

SCRIPTS_DIR = (Path(__file__).resolve().parent / '../scripts').resolve()
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import free_api_worker


class TestZaiDirectMode(unittest.TestCase):
    def test_zai_payload_disables_thinking_and_preserves_json_constraints(self) -> None:
        payload = free_api_worker.build_chat_request_payload(
            'zai', 'glm-4.5-flash', 'return json'
        )
        roundtrip = json.loads(json.dumps(payload))
        self.assertEqual(roundtrip['model'], 'glm-4.5-flash')
        self.assertEqual(
            roundtrip['messages'], [{'role': 'user', 'content': 'return json'}]
        )
        self.assertEqual(roundtrip['temperature'], 0)
        self.assertEqual(roundtrip['max_tokens'], 4096)
        self.assertIs(roundtrip['stream'], False)
        self.assertEqual(roundtrip['thinking'], {'type': 'disabled'})
        self.assertEqual(roundtrip['response_format'], {'type': 'json_object'})
        self.assertNotIn('tools', roundtrip)

    def test_openrouter_payload_keeps_reasoning_disabled_without_zai_thinking(self) -> None:
        payload = free_api_worker.build_chat_request_payload(
            'openrouter', 'z-ai/glm-4.5-air:free', 'return json'
        )
        roundtrip = json.loads(json.dumps(payload))
        self.assertEqual(roundtrip['reasoning'], {'enabled': False})
        self.assertNotIn('thinking', roundtrip)
        self.assertNotIn('tools', roundtrip)
        self.assertEqual(roundtrip['max_tokens'], 4096)
        self.assertIs(roundtrip['stream'], False)
        self.assertIs(roundtrip['provider']['allow_fallbacks'], False)


if __name__ == '__main__':
    unittest.main()
