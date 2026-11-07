from pathlib import Path
import sys
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import chatgpt_review as cr


class TestApiReviewPositive(unittest.TestCase):
    def _build_fixture(
        self,
        worker_provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int,
        reasoning_tokens: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        jev_usage = {
            "input_tokens": 400,
            "output_tokens": 100,
            "total_tokens": 500,
        }
        reviewer_usage = {
            "input_tokens": 900,
            "output_tokens": 350,
            "thinking_tokens": 120,
            "cache_read_tokens": 250,
            "total_tokens": 1250,
        }
        steps = [
            {
                "role": "worker",
                "provider": worker_provider,
                "attempt": 1,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "cached_tokens": cached_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "total_steps": 2,
                },
            },
            {
                "role": "jev",
                "provider": "jev",
                "model": cr.gate.JEV_MODEL,
                "attempt": 1,
                "usage": dict(jev_usage),
            },
            {
                "role": "reviewer",
                "provider": "agy",
                "model": "gemini-4-argon-high",
                "effort": "high",
                "attempt": 1,
                "conversation_id": "agy-reviewer-conv-1",
                "usage": dict(reviewer_usage),
            },
        ]
        report: dict[str, object] = {
            "worker_provider": worker_provider,
            "attempts": [{"stage": "review_gate", "attempt": 1}],
            "external_steps": steps,
            "usage_totals": cr.runner.aggregate_external_usage(steps),
        }
        decision: dict[str, object] = {
            "jev_usage": dict(jev_usage),
            "reviewers": [
                {
                    "conversation_id": "agy-reviewer-conv-1",
                    "usage": dict(reviewer_usage),
                }
            ],
        }
        return report, decision

    def test_zai_worker_positive_usage_aggregation(self) -> None:
        report, decision = self._build_fixture(
            worker_provider="zai",
            prompt_tokens=1200,
            completion_tokens=500,
            cached_tokens=300,
            reasoning_tokens=150,
        )
        reaggregated, provider_io = cr._validate_and_aggregate_usage(report, decision, 1)

        self.assertEqual(reaggregated, report["usage_totals"])
        self.assertEqual(provider_io["zai_prompt_tokens"], 1200)
        self.assertEqual(provider_io["zai_completion_tokens"], 500)
        self.assertEqual(provider_io["zai_io_tokens"], 1700)
        self.assertEqual(provider_io["zai_cached_tokens_separate"], 300)
        self.assertEqual(provider_io["zai_reasoning_tokens_separate"], 150)

        self.assertEqual(provider_io["openrouter_prompt_tokens"], 0)
        self.assertEqual(provider_io["openrouter_completion_tokens"], 0)
        self.assertEqual(provider_io["openrouter_io_tokens"], 0)
        self.assertEqual(provider_io["openrouter_cached_tokens_separate"], 0)
        self.assertEqual(provider_io["openrouter_reasoning_tokens_separate"], 0)

        # 1700 (zai I/O) + 500 (Jev I/O) + 1500 (AGY I/O: 900 input + 250 cache_read + 350 output) = 3700
        self.assertEqual(provider_io["provider_io_total_tokens"], 3700)

    def test_openrouter_worker_positive_usage_aggregation(self) -> None:
        report, decision = self._build_fixture(
            worker_provider="openrouter",
            prompt_tokens=1500,
            completion_tokens=600,
            cached_tokens=400,
            reasoning_tokens=200,
        )
        reaggregated, provider_io = cr._validate_and_aggregate_usage(report, decision, 1)

        self.assertEqual(reaggregated, report["usage_totals"])
        self.assertEqual(provider_io["openrouter_prompt_tokens"], 1500)
        self.assertEqual(provider_io["openrouter_completion_tokens"], 600)
        self.assertEqual(provider_io["openrouter_io_tokens"], 2100)
        self.assertEqual(provider_io["openrouter_cached_tokens_separate"], 400)
        self.assertEqual(provider_io["openrouter_reasoning_tokens_separate"], 200)

        self.assertEqual(provider_io["zai_prompt_tokens"], 0)
        self.assertEqual(provider_io["zai_completion_tokens"], 0)
        self.assertEqual(provider_io["zai_io_tokens"], 0)
        self.assertEqual(provider_io["zai_cached_tokens_separate"], 0)
        self.assertEqual(provider_io["zai_reasoning_tokens_separate"], 0)

        # 2100 (openrouter I/O) + 500 (Jev I/O) + 1500 (AGY I/O: 900 input + 250 cache_read + 350 output) = 4100
        self.assertEqual(provider_io["provider_io_total_tokens"], 4100)


if __name__ == "__main__":
    unittest.main()
