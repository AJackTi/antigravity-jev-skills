import unittest
import sys
from pathlib import Path

scripts_dir = Path(__file__).parent.parent / 'scripts'
if scripts_dir.exists():
    sys.path.insert(0, str(scripts_dir))

from run_workflow import aggregate_external_usage


class TestAPIAccounting(unittest.TestCase):
    def test_zai_and_openrouter_separate_buckets(self):
        steps = [
            {'provider': 'zai', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                                          'total_tokens': 150, 'cached_tokens': 20,
                                          'reasoning_tokens': 10, 'total_steps': 1}},
            {'provider': 'openrouter', 'usage': {'prompt_tokens': 200, 'completion_tokens': 75,
                                                 'total_tokens': 275, 'cached_tokens': 30,
                                                 'reasoning_tokens': 15, 'total_steps': 2}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['zai']['prompt_tokens'], 100)
        self.assertEqual(result['zai']['completion_tokens'], 50)
        self.assertEqual(result['zai']['total_tokens'], 150)
        self.assertEqual(result['zai']['cached_tokens'], 20)
        self.assertEqual(result['zai']['reasoning_tokens'], 10)
        self.assertEqual(result['zai']['total_steps'], 1)
        self.assertEqual(result['openrouter']['prompt_tokens'], 200)
        self.assertEqual(result['openrouter']['completion_tokens'], 75)
        self.assertEqual(result['openrouter']['total_tokens'], 275)
        self.assertEqual(result['openrouter']['cached_tokens'], 30)
        self.assertEqual(result['openrouter']['reasoning_tokens'], 15)
        self.assertEqual(result['openrouter']['total_steps'], 2)

    def test_no_agy_leakage_for_api_providers(self):
        steps = [
            {'provider': 'zai', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                                          'total_tokens': 150, 'cached_tokens': 20,
                                          'reasoning_tokens': 10, 'total_steps': 1}},
            {'provider': 'openrouter', 'usage': {'prompt_tokens': 200, 'completion_tokens': 75,
                                                 'total_tokens': 275, 'cached_tokens': 30,
                                                 'reasoning_tokens': 15, 'total_steps': 2}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['gemini']['input_tokens'], 0)
        self.assertEqual(result['gemini']['output_tokens'], 0)
        self.assertEqual(result['gemini']['total_tokens'], 0)

    def test_cached_and_reasoning_counted_once(self):
        steps = [
            {'provider': 'zai', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                                          'total_tokens': 150, 'cached_tokens': 20,
                                          'reasoning_tokens': 10, 'total_steps': 1}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['zai']['total_tokens'], 150)
        self.assertEqual(result['zai']['prompt_tokens'], 100)
        self.assertEqual(result['zai']['completion_tokens'], 50)
        self.assertEqual(result['zai']['cached_tokens'], 20)
        self.assertEqual(result['zai']['reasoning_tokens'], 10)

    def test_invalid_usage_yields_unknown(self):
        steps = [
            {'provider': 'zai', 'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                                          'total_tokens': 200, 'cached_tokens': 20,
                                          'reasoning_tokens': 10, 'total_steps': 1}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['zai']['prompt_tokens'], 'unknown')
        self.assertEqual(result['zai']['completion_tokens'], 'unknown')
        self.assertEqual(result['zai']['total_tokens'], 'unknown')
        self.assertEqual(result['zai']['cached_tokens'], 'unknown')
        self.assertEqual(result['zai']['reasoning_tokens'], 'unknown')
        self.assertEqual(result['zai']['total_steps'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_absent_usage_yields_unknown(self):
        steps = [{'provider': 'zai', 'usage': {}}]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['zai']['prompt_tokens'], 'unknown')
        self.assertEqual(result['zai']['completion_tokens'], 'unknown')
        self.assertEqual(result['zai']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_wrong_native_type_rejects(self):
        steps = [
            {'provider': 'zai', 'usage': {'prompt_tokens': '100', 'completion_tokens': 50,
                                          'total_tokens': 150, 'cached_tokens': 20,
                                          'reasoning_tokens': 10, 'total_steps': 1}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['zai']['prompt_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_legacy_noapi_unaffected(self):
        steps = [
            {'provider': 'unknown', 'usage': {'input_tokens': 100, 'output_tokens': 50,
                                            'total_tokens': 150, 'thinking_tokens': 5,
                                            'cache_read_tokens': 10}},
            {'model': 'jev-model', 'role': 'jev', 'usage': {'input_tokens': 200,
                                                           'output_tokens': 75,
                                                           'total_tokens': 275}},
        ]
        result = aggregate_external_usage(steps)
        self.assertEqual(result['gemini']['input_tokens'], 100)
        self.assertEqual(result['gemini']['output_tokens'], 50)
        self.assertEqual(result['gemini']['total_tokens'], 150)
        self.assertEqual(result['gemini']['thinking_tokens'], 5)
        self.assertEqual(result['gemini']['cache_read_tokens'], 10)
        self.assertEqual(result['jev']['input_tokens'], 200)
        self.assertEqual(result['jev']['output_tokens'], 75)
        self.assertEqual(result['jev']['total_tokens'], 275)
        self.assertEqual(result['zai']['sessions_with_verified_usage'], 0)
        self.assertEqual(result['openrouter']['sessions_with_verified_usage'], 0)


if __name__ == '__main__':
    unittest.main()
