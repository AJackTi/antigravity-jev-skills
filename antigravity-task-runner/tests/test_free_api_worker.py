import ast
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import importlib
free_api_worker = importlib.import_module('free_api_worker')

class TestFreeApiWorker(unittest.TestCase):

    def test_resolve_zai_model_default(self):
        os.environ.pop('ZAI_MODEL', None)
        result = free_api_worker.resolve_zai_model()
        self.assertEqual(result, 'glm-4.5-flash')

    def test_resolve_openrouter_model_valid(self):
        os.environ.pop('OPENROUTER_MODEL', None)
        full_id, vendor, slug = free_api_worker.resolve_openrouter_model('openai/gpt-4o-mini:free')
        self.assertTrue(full_id.endswith(':free'))
        self.assertEqual(vendor, 'openai')
        self.assertEqual(slug, 'gpt-4o-mini:free')

    def test_is_zero_price_value_true(self):
        self.assertTrue(free_api_worker._is_zero_price_value(0))
        self.assertTrue(free_api_worker._is_zero_price_value(0.0))
        self.assertTrue(free_api_worker._is_zero_price_value('0'))
        self.assertTrue(free_api_worker._is_zero_price_value('0.0'))

    def test_is_zero_price_value_false(self):
        self.assertFalse(free_api_worker._is_zero_price_value(1))
        self.assertFalse(free_api_worker._is_zero_price_value(0.01))
        self.assertFalse(free_api_worker._is_zero_price_value('1'))
        self.assertFalse(free_api_worker._is_zero_price_value(None))
        self.assertFalse(free_api_worker._is_zero_price_value(True))

    def test_validate_openrouter_catalog_model_good(self):
        payload = {
            'data': [{
                'id': 'openai/gpt-4o-mini:free',
                'pricing': {'prompt': 0, 'completion': 0, 'request': 0}
            }]
        }
        result = free_api_worker.validate_openrouter_catalog_model(payload, 'openai/gpt-4o-mini:free')
        self.assertEqual(result['id'], 'openai/gpt-4o-mini:free')
        self.assertTrue(result['verified_zero_price'])

    def test_validate_openrouter_catalog_model_missing(self):
        payload = {'data': [{'id': 'other/model', 'pricing': {'prompt': 0, 'completion': 0, 'request': 0}}]}
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_openrouter_catalog_model(payload, 'openai/gpt-4o-mini:free')

    def test_validate_openrouter_catalog_model_duplicate(self):
        payload = {
            'data': [
                {'id': 'openai/gpt-4o-mini', 'pricing': {'prompt': 0, 'completion': 0, 'request': 0}},
                {'id': 'openai/gpt-4o-mini', 'pricing': {'prompt': 0, 'completion': 0, 'request': 0}}
            ]
        }
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_openrouter_catalog_model(payload, 'openai/gpt-4o-mini:free')

    def test_validate_openrouter_catalog_model_nonzero_price(self):
        payload = {
            'data': [{'id': 'openai/gpt-4o-mini', 'pricing': {'prompt': 0.01, 'completion': 0, 'request': 0}}]
        }
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_openrouter_catalog_model(payload, 'openai/gpt-4o-mini:free')

    def test_validate_fixed_endpoint_bad_url(self):
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_fixed_endpoint('openrouter', 'https://evil.com/api')

    def test_validate_fixed_endpoint_http(self):
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_fixed_endpoint('openrouter', 'http://official.com')

    def test_build_chat_request_unsupported_provider(self):
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.build_chat_request_payload('unknown', 'model', 'prompt')

    def test_build_chat_request_zai_max_tokens(self):
        payload = free_api_worker.build_chat_request_payload('zai', 'glm-4.5-flash', 'prompt')
        self.assertEqual(payload['model'], 'glm-4.5-flash')
        self.assertEqual(payload['max_tokens'], 4096)

    def test_build_chat_request_openrouter_max_tokens_and_constraints(self):
        payload = free_api_worker.build_chat_request_payload('openrouter', 'vendor/demo:free', 'prompt')
        self.assertEqual(payload['model'], 'vendor/demo:free')
        self.assertEqual(payload['max_tokens'], 4096)
        self.assertEqual(payload['max_price'], dict(free_api_worker.ZERO_MAX_PRICE))
        self.assertEqual(
            payload['provider'],
            {'allow_fallbacks': False, 'require_parameters': True, 'max_price': {'prompt': 0, 'completion': 0, 'request': 0}},
        )

    def test_runtime_header_and_redaction_call_sites_offline(self):
        worker_path = Path(free_api_worker.__file__)
        source_text = worker_path.read_text(encoding='utf-8')
        tree = ast.parse(source_text, filename=str(worker_path))

        blocks = []
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if not isinstance(body, list):
                continue
            for idx in range(len(body) - 3):
                s0, s1, s2, s3 = body[idx], body[idx + 1], body[idx + 2], body[idx + 3]
                if (
                    isinstance(s0, ast.Assign)
                    and len(s0.targets) == 1
                    and isinstance(s0.targets[0], ast.Name)
                    and s0.targets[0].id in ('cat_header_value', 'req_header_value')
                ):
                    blocks.append((s0.targets[0].id, [s0, s1, s2, s3]))

        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            sorted([name for name, _ in blocks]),
            sorted(['cat_header_value', 'req_header_value']),
        )

        marker = 'harmless-synthetic-marker'
        expected_header = f'Bearer {marker}'
        calls = []

        def fake_transport(url, *, method, headers, body, timeout_seconds, max_bytes, redaction_value=None):
            calls.append(
                {
                    'url': url,
                    'method': method,
                    'headers': dict(headers),
                    'body': body,
                    'timeout_seconds': timeout_seconds,
                    'max_bytes': max_bytes,
                    'redaction_value': redaction_value,
                }
            )
            return 200, b'{}'

        for _, stmts in sorted(blocks, key=lambda item: item[0]):
            mod = ast.Module(body=stmts, type_ignores=[])
            ast.fix_missing_locations(mod)
            env = dict(
                OPENROUTER_MODELS_ENDPOINT=free_api_worker.OPENROUTER_MODELS_ENDPOINT,
                MAX_CATALOG_BYTES=free_api_worker.MAX_CATALOG_BYTES,
                MAX_HTTP_RESPONSE_BYTES=free_api_worker.MAX_HTTP_RESPONSE_BYTES,
                endpoint=free_api_worker.ZAI_CHAT_ENDPOINT,
                req_body=b'{}',
                timeout_seconds=5,
                transport=fake_transport,
            )
            env.update(api_key=marker)
            exec(compile(mod, filename=str(worker_path), mode='exec'), env)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['url'], free_api_worker.OPENROUTER_MODELS_ENDPOINT)
        self.assertEqual(calls[0]['method'], 'GET')
        self.assertEqual(calls[0]['headers']['Authorization'], expected_header)
        self.assertEqual(calls[0]['redaction_value'], marker)

        self.assertEqual(calls[1]['url'], free_api_worker.ZAI_CHAT_ENDPOINT)
        self.assertEqual(calls[1]['method'], 'POST')
        self.assertEqual(calls[1]['headers']['Authorization'], expected_header)
        self.assertEqual(calls[1]['redaction_value'], marker)

    def test_default_http_transport_headers_redaction_and_no_redirect_offline(self):
        marker = 'harmless-synthetic-marker'
        header_value = f'Bearer {marker}'
        headers = dict(Accept='application/json', Authorization=header_value)
        captured_requests = []

        class _FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _n):
                return b'{"ok":true}'

        class _FakeOpener:
            def open(self, req, timeout=None):
                captured_requests.append((req, timeout))
                return _FakeResponse()

        with mock.patch('urllib.request.build_opener', return_value=_FakeOpener()) as patched_builder:
            status, data = free_api_worker._default_http_transport(
                free_api_worker.OPENROUTER_MODELS_ENDPOINT,
                method='GET',
                headers=headers,
                body=None,
                timeout_seconds=7,
                max_bytes=1024,
                redaction_value=marker,
            )
            self.assertEqual(status, 200)
            self.assertEqual(data, b'{"ok":true}')
            self.assertEqual(len(captured_requests), 1)
            req_obj, timeout_val = captured_requests[0]
            self.assertEqual(timeout_val, 7)
            self.assertEqual(req_obj.get_header('Authorization'), header_value)
            handler_types = [type(h) for h in patched_builder.call_args.args]
            self.assertIn(free_api_worker._NoRedirectHandler, handler_types)

        class _HttpErrOpener:
            def open(self, req, timeout=None):
                err_body = f'{{"error":{{"message":"denied for {marker}"}}}}'.encode('utf-8')
                raise urllib.error.HTTPError(
                    req.full_url,
                    401,
                    'Unauthorized',
                    hdrs=None,
                    fp=io.BytesIO(err_body),
                )

        with mock.patch('urllib.request.build_opener', return_value=_HttpErrOpener()):
            with self.assertRaises(free_api_worker.FreeApiBlocked) as ctx:
                free_api_worker._default_http_transport(
                    free_api_worker.OPENROUTER_MODELS_ENDPOINT,
                    method='GET',
                    headers=headers,
                    body=None,
                    timeout_seconds=7,
                    max_bytes=1024,
                    redaction_value=marker,
                )
            self.assertNotIn(marker, str(ctx.exception))
            self.assertEqual(ctx.exception.http_status, 401)

        class _UrlErrOpener:
            def open(self, req, timeout=None):
                raise urllib.error.URLError(f'transport failure involving {marker}')

        with mock.patch('urllib.request.build_opener', return_value=_UrlErrOpener()):
            with self.assertRaises(free_api_worker.FreeApiBlocked) as ctx:
                free_api_worker._default_http_transport(
                    free_api_worker.OPENROUTER_MODELS_ENDPOINT,
                    method='GET',
                    headers=headers,
                    body=None,
                    timeout_seconds=7,
                    max_bytes=1024,
                    redaction_value=marker,
                )
            self.assertNotIn(marker, str(ctx.exception))

        class _RedirectOpener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 302, 'Found', hdrs=None, fp=None)

        with mock.patch('urllib.request.build_opener', return_value=_RedirectOpener()):
            with self.assertRaises(free_api_worker.FreeApiBlocked) as ctx:
                free_api_worker._default_http_transport(
                    free_api_worker.OPENROUTER_MODELS_ENDPOINT,
                    method='GET',
                    headers=headers,
                    body=None,
                    timeout_seconds=7,
                    max_bytes=1024,
                    redaction_value=marker,
                )
            self.assertEqual(ctx.exception.http_status, 302)

    def test_source_passes_unchanged_contains_secret_scanner(self):
        worker_source = Path(free_api_worker.__file__).read_text(encoding='utf-8')
        test_source = Path(__file__).read_text(encoding='utf-8')
        self.assertFalse(free_api_worker.gate.contains_secret(worker_source))
        self.assertFalse(free_api_worker.gate.contains_secret(test_source))

if __name__ == '__main__':
    unittest.main()
