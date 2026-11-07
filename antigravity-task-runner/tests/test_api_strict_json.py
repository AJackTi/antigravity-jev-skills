import sys
import unittest
from pathlib import Path

for _candidate in (
    (Path(__file__).resolve().parent / '../scripts').resolve(),
    (Path(__file__).resolve().parent / 'scripts').resolve(),
):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import free_api_worker as f


class TestApiStrictJson(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = ['app.py']
        self.expected_Exception = (f.FreeApiBlocked, f.devin.DevinBlocked)

    def test_valid_raw_json_accepted(self) -> None:
        raw = '{"handoff": "applied minimal fix", "files": {"app.py": "pass\\n"}}'
        handoff, files = f.parse_generation(raw, self.scope)
        self.assertEqual(handoff, 'applied minimal fix')
        self.assertEqual(files, {'app.py': b'pass\n'})

    def test_valid_whitespace_wrapped_json_accepted(self) -> None:
        raw = '  \n\t {"handoff": "applied minimal fix", "files": {"app.py": "pass\\n"}} \r\n '
        handoff, files = f.parse_generation(raw, self.scope)
        self.assertEqual(handoff, 'applied minimal fix')
        self.assertEqual(files, {'app.py': b'pass\n'})

    def test_fenced_json_rejected_by_api_parser(self) -> None:
        fenced = '```json\n{"handoff": "ok", "files": {"app.py": "pass\\n"}}\n```'
        with self.assertRaises(self.expected_Exception):
            f.parse_generation(fenced, self.scope)

    def test_devin_parser_still_accepts_single_fence(self) -> None:
        fenced = '```json\n{"handoff": "ok", "files": {"app.py": "pass\\n"}}\n```'
        handoff, files = f.devin.parse_generation(fenced, self.scope)
        self.assertEqual(handoff, 'ok')
        self.assertEqual(files, {'app.py': b'pass\n'})

    def test_prose_and_trailing_data_rejected(self) -> None:
        cases = [
            'Here is the output:\n{"handoff": "ok", "files": {"app.py": "pass\\n"}}',
            '{"handoff": "ok", "files": {"app.py": "pass\\n"}}\nDone.',
            '{"handoff": "ok", "files": {"app.py": "pass\\n"}} {"extra": true}',
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(self.expected_Exception):
                    f.parse_generation(payload, self.scope)

    def test_duplicate_keys_rejected(self) -> None:
        cases = [
            '{"handoff": "ok", "handoff": "dup", "files": {"app.py": "pass\\n"}}',
            '{"handoff": "ok", "files": {"app.py": "pass\\n", "app.py": "pass\\n"}}',
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(self.expected_Exception):
                    f.parse_generation(payload, self.scope)

    def test_nonobject_and_invalid_schema_rejected(self) -> None:
        cases = [
            '[{"handoff": "ok", "files": {"app.py": "pass\\n"}}]',
            '"raw string"',
            '123',
            '{"handoff": "ok"}',
            '{"files": {"app.py": "pass\\n"}}',
            '{"handoff": "ok", "files": {"app.py": "pass\\n"}, "extra": 1}',
            '{"handoff": "   ", "files": {"app.py": "pass\\n"}}',
            '{"handoff": "ok", "files": {}}',
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(self.expected_Exception):
                    f.parse_generation(payload, self.scope)

    def test_nonstring_file_content_rejected(self) -> None:
        cases = [
            '{"handoff": "ok", "files": {"app.py": 123}}',
            '{"handoff": "ok", "files": {"app.py": ["pass\\n"]}}',
            '{"handoff": "ok", "files": {"app.py": null}}',
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(self.expected_Exception):
                    f.parse_generation(payload, self.scope)

    def test_undeclared_path_rejected(self) -> None:
        payload = '{"handoff": "ok", "files": {"other.py": "pass\\n"}}'
        with self.assertRaises(self.expected_Exception):
            f.parse_generation(payload, self.scope)


if __name__ == '__main__':
    unittest.main()
