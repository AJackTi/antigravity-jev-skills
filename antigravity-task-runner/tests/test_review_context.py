import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone


MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'chatgpt_review.py'
spec = importlib.util.spec_from_file_location('chatgpt_review', MODULE)
cr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cr)
f = cr.runner.free_api


class CoordinatorContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.ws = self.base / 'ws'
        self.ws.mkdir()
        self.path = self.base / 'context.json'
        self.packet = {
            'id': 'context-case',
            'worker': {'provider': 'openrouter', 'model': 'nvidia/nemotron-3.5-super-vl:free'},
            'source_hashes': {'sample.py': 'a' * 64},
            'reviewers': [{'model': 'gemini-4-argon-high'}],
        }
        self.context = {
            'schema_version': 1,
            'case_id': 'context-case',
            'workspace': str(self.ws),
            'worker_provider': 'openrouter',
            'worker_model': 'nvidia/nemotron-3.5-super-vl:free',
            'source_hashes': {'sample.py': 'a' * 64},
            'user_instruction': 'Allow this isolated free API sample.',
            'quota_preflight': {
                'native_command': 'agy --print /usage',
                'native_output': 'Gemini fixture remaining85percent',
                'selected_agy_model': 'gemini-4-argon-high',
                'selection_basis': 'User selected Gemini; fixture Claude0',
                'checked_at_utc': datetime.now(timezone.utc).isoformat(),
            },
        }
        self.write_context()

    def write_context(self, value=None):
        data = json.dumps(self.context if value is None else value).encode('utf-8')
        self.path.write_bytes(data)
        self.path.chmod(0o600)
        return data

    def prefix(self):
        with mock.patch.dict(os.environ, {'WORKFLOW_REVIEW_CONTEXT_FILE': str(self.path)}):
            return cr._coordinator_context_prefix(self.ws, self.packet)

    def test_missing_context_is_optional_only_for_non_api(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                cr._coordinator_context_prefix(self.ws, self.packet)
            other = dict(self.packet)
            other['worker'] = {'provider': 'agy', 'model': 'gemini-4-argon-high'}
            self.assertEqual(cr._coordinator_context_prefix(self.ws, other), '')

    def test_valid_context_has_permission_quota_and_actual_provenance(self):
        data = self.path.read_bytes()
        prefix = self.prefix()
        self.assertIn('Allow this isolated free API sample.', prefix)
        self.assertIn('Gemini fixture remaining85percent', prefix)
        self.assertIn(hashlib.sha256(data).hexdigest(), prefix)
        self.assertIn(str(self.path), prefix)
        self.assertNotIn('UNTRUSTED_WORKFLOW_EVIDENCE', prefix)

    def test_bindings_and_schema_fail_closed(self):
        cases = [
            ('schema_version', True),
            ('case_id', 'another-case'),
            ('worker_provider', 'zai'),
            ('worker_model', 'another-model'),
            ('source_hashes', {'sample.py': 'b' * 64}),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                changed = json.loads(json.dumps(self.context))
                changed[field] = value
                self.write_context(changed)
                with self.assertRaises(ValueError):
                    self.prefix()
        changed = json.loads(json.dumps(self.context))
        changed['quota_preflight']['selected_agy_model'] = 'claude-opus-5-5-high'
        self.write_context(changed)
        with self.assertRaises(ValueError):
            self.prefix()

    def test_file_location_symlink_and_mode_fail_closed(self):
        self.path = self.ws / 'inside.json'
        self.write_context()
        with self.assertRaises(ValueError):
            self.prefix()
        self.path = self.base / 'context.json'
        self.write_context()
        link = self.base / 'linked.json'
        link.symlink_to(self.path)
        self.path = link
        with self.assertRaises(ValueError):
            self.prefix()
        self.path = self.base / 'context.json'
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.prefix()

    def test_duplicate_keys_and_time_bounds_fail_closed(self):
        self.path.write_text('{"schema_version":1,"schema_version":1}', encoding='utf-8')
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.prefix()
        for offset in (-3601, 61):
            with self.subTest(offset=offset):
                changed = json.loads(json.dumps(self.context))
                changed['quota_preflight']['checked_at_utc'] = (
                    datetime.now(timezone.utc) + timedelta(seconds=offset)
                ).isoformat()
                self.write_context(changed)
                with self.assertRaises(ValueError):
                    self.prefix()

    def test_secret_scanner_rejects_context(self):
        self.write_context()
        with mock.patch.object(cr.runner.gate, 'contains_secret', return_value=True):
            with self.assertRaises(ValueError):
                self.prefix()


class FreeApiResponseTests(unittest.TestCase):
    model = 'nvidia/nemotron-3.5-super-vl:free'
    provider = 'openrouter'

    def raw(self, content):
        return {
            'id': 'fixture-response',
            'model': self.model,
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18},
        }

    def test_structured_source_with_quota_word_survives_response_validation(self):
        content = json.dumps({
            'handoff': 'Generated sample.',
            'files': {'sample.py': 'quota_preflight = "fixture"\n'},
        })
        proof, returned, usage = f.validate_chat_completion_response(
            self.provider, self.model, self.raw(content)
        )
        self.assertEqual(returned, content)
        self.assertEqual(usage['usage']['total_tokens'], 18)
        self.assertIsNotNone(proof)
        _, files = f.parse_generation(returned, ['sample.py'])
        self.assertEqual(files['sample.py'], 'quota_preflight = "fixture"\n'.encode('utf-8'))

    def test_plain_provider_error_and_native_error_are_blocked(self):
        with self.assertRaises(f.FreeApiBlocked):
            f.validate_chat_completion_response(
                self.provider, self.model, self.raw('Free usage limit reached; quota exhausted')
            )
        raw = self.raw(json.dumps({'handoff': 'Generated.', 'files': {'sample.py': 'quota = 1\n'}}))
        raw['error'] = {'message': 'quota exhausted'}
        with self.assertRaises(f.FreeApiBlocked):
            f.validate_chat_completion_response(self.provider, self.model, raw)

    def test_native_model_tool_and_finish_checks_remain(self):
        content = json.dumps({'handoff': 'Generated.', 'files': {'sample.py': 'quota = 1\n'}})
        variants = []
        wrong_model = self.raw(content)
        wrong_model['model'] = 'other/model'
        variants.append(wrong_model)
        tool_call = self.raw(content)
        tool_call['choices'][0]['message']['tool_calls'] = [{'id': 'call-1'}]
        variants.append(tool_call)
        incomplete = self.raw(content)
        incomplete['choices'][0]['finish_reason'] = 'length'
        variants.append(incomplete)
        for raw in variants:
            with self.subTest(raw=raw):
                with self.assertRaises(f.FreeApiBlocked):
                    f.validate_chat_completion_response(self.provider, self.model, raw)

    def test_duplicate_generation_and_blocked_handoff_rejected(self):
        duplicate = '{"handoff":"Generated.","handoff":"Again.","files":{"sample.py":"quota = 1"}}'
        with self.assertRaises(ValueError):
            f.parse_generation(duplicate, ['sample.py'])
        blocked = json.dumps({'handoff': 'BLOCKED: provider quota exhausted', 'files': {'sample.py': 'quota = 1\n'}})
        with self.assertRaises(ValueError):
            f.parse_generation(blocked, ['sample.py'])


if __name__ == '__main__':
    unittest.main()
