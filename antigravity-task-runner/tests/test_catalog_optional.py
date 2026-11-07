import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'scripts'))
import free_api_worker


class TestCatalogOptionalRequestPrice(unittest.TestCase):
    def test_omitted_request_accepted(self) -> None:
        payload = {'data': [{'id': 'vendor/demo:free', 'pricing': {'prompt': '0', 'completion': '0'}}]}
        res = free_api_worker.validate_openrouter_catalog_model(payload, 'vendor/demo:free')
        self.assertEqual(res['pricing'], {'prompt': '0', 'completion': '0', 'request': '0'})
        self.assertNotIn('request', res['raw_pricing'])
        self.assertEqual(res['raw_pricing'], {'prompt': '0', 'completion': '0'})
        self.assertEqual(res['request_price_assurance'], 'server_zero_cap_required')

    def test_positive_request_rejected(self) -> None:
        payload = {'data': [{'id': 'vendor/demo:free', 'pricing': {'prompt': '0', 'completion': '0', 'request': '0.01'}}]}
        with self.assertRaises(free_api_worker.FreeApiBlocked):
            free_api_worker.validate_openrouter_catalog_model(payload, 'vendor/demo:free')

    def test_explicit_request_zero_accepted(self) -> None:
        payload = {'data': [{'id': 'vendor/demo:free', 'pricing': {'prompt': '0', 'completion': '0', 'request': '0'}}]}
        res = free_api_worker.validate_openrouter_catalog_model(payload, 'vendor/demo:free')
        self.assertEqual(res['pricing'], {'prompt': '0', 'completion': '0', 'request': '0'})
        self.assertEqual(res['raw_pricing'], {'prompt': '0', 'completion': '0', 'request': '0'})
        self.assertEqual(res['request_price_assurance'], 'catalog_zero')


if __name__ == '__main__':
    unittest.main()
