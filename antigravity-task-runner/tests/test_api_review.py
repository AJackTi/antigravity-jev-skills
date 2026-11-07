from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import chatgpt_review as cr


def _make_step(provider: str, **usage_overrides: object) -> dict:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "total_steps": 1,
    }
    usage.update(usage_overrides)
    return {"role": "worker", "provider": provider, "attempt": 1, "usage": usage}


class TestApiReviewUsageNegative(unittest.TestCase):
    def test_missing_usage_raises_for_each_api(self) -> None:
        for provider in ("zai", "openrouter"):
            report = {
                "worker_provider": provider,
                "external_steps": [{"role": "worker", "provider": provider, "attempt": 1, "usage": None}],
            }
            with self.assertRaises(ValueError) as ctx:
                cr._validate_and_aggregate_usage(report, {}, 1)
            self.assertIn("missing usage", str(ctx.exception))

    def test_bool_prompt_tokens_raises_non_negative_ints(self) -> None:
        for provider in ("zai", "openrouter"):
            report = {
                "worker_provider": provider,
                "external_steps": [_make_step(provider, prompt_tokens=True)],
            }
            with self.assertRaises(ValueError) as ctx:
                cr._validate_and_aggregate_usage(report, {}, 1)
            self.assertIn("non-negative ints", str(ctx.exception))

    def test_worker_provider_mismatch_raises(self) -> None:
        for provider, other in (("zai", "openrouter"), ("openrouter", "zai")):
            report = {
                "worker_provider": provider,
                "external_steps": [_make_step(other)],
            }
            with self.assertRaises(ValueError) as ctx:
                cr._validate_and_aggregate_usage(report, {}, 1)
            self.assertIn("mismatches", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
