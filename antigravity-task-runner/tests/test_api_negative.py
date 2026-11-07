import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from run_workflow import aggregate_external_usage

class TestAPINegative(unittest.TestCase):
    def test_valid_control_both_providers(self):
        external_steps = [
            {'provider': 'zai', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}},
            {'provider': 'openrouter', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['zai']['total_tokens'], 110)
        self.assertEqual(result['openrouter']['total_tokens'], 110)
        self.assertEqual(result['combined_non_cache_total_tokens'], 220)

    def test_zai_negative_cached(self):
        external_steps = [
            {'provider': 'zai', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': -1, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['zai']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_zai_negative_reasoning(self):
        external_steps = [
            {'provider': 'zai', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': 0, 'reasoning_tokens': -1, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['zai']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_openrouter_negative_cached(self):
        external_steps = [
            {'provider': 'openrouter', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': -1, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['openrouter']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_openrouter_negative_reasoning(self):
        external_steps = [
            {'provider': 'openrouter', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'cached_tokens': 0, 'reasoning_tokens': -1, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['openrouter']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_zai_string_type(self):
        external_steps = [
            {'provider': 'zai', 'role': 'worker', 'usage': {'prompt_tokens': '100', 'completion_tokens': 10, 'cached_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['zai']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

    def test_openrouter_bool_type(self):
        external_steps = [
            {'provider': 'openrouter', 'role': 'worker', 'usage': {'prompt_tokens': 100, 'completion_tokens': True, 'cached_tokens': 0, 'reasoning_tokens': 0, 'total_tokens': 110, 'total_steps': 1}}
        ]
        result = aggregate_external_usage(external_steps)
        self.assertEqual(result['openrouter']['total_tokens'], 'unknown')
        self.assertEqual(result['combined_non_cache_total_tokens'], 'unknown')

if __name__ == '__main__':
    unittest.main()