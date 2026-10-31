"""Offline evaluator contract tests; no TypeSafe calls or keys."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_results.py"


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.input = Path(self.directory.name) / "results.jsonl"
        self.expected = Path(self.directory.name) / "labels.jsonl"
        self.raw_metadata = {
            "model": "jev-1.13.0",
            "requested_model": "jev-1.13.0",
            "question_sha256": "a" * 64,
            "state_sha256": "b" * 64,
        }

    def write(self, rows, path=None):
        target = path or self.input
        if path is None:
            rows = [
                ({**self.raw_metadata, **row} if isinstance(row, dict) and "answers" in row else row)
                for row in rows
            ]
        target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def call(self, *flags, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", str(self.input), *flags],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            return json.loads(result.stdout)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        return result.stderr

    def test_choice_metrics_and_fixed_universe(self):
        self.write([
            {"id": "1", "expected": "a", "predicted": "a", "confidence": .9},
            {"id": "2", "expected": "a", "predicted": "b", "confidence": .9},
            {"id": "3", "expected": "b", "predicted": "b", "confidence": .9},
            {"id": "4", "expected": "c", "predicted": "c", "confidence": .1},
        ])
        result = self.call("--kind", "choice", "--min-confidence", ".8", "--labels", "a", "b", "c")
        self.assertEqual((result["n"], result["accepted"], result["abstained"]), (4, 3, 1))
        self.assertEqual(result["accepted_denominator"], 3)
        self.assertEqual(result["accuracy_all_if_forced"], .75)
        self.assertEqual(result["accuracy_accepted"], 2 / 3)
        self.assertAlmostEqual(result["macro_f1_accepted_fixed_labels"], 4 / 9)
        self.assertEqual(result["per_class"][2]["support"], 1)
        self.assertEqual(result["per_class"][2]["f1_accepted"], 0)
        self.assertAlmostEqual(result["accuracy_accepted_wilson_95"][0], .2076596008020477)
        self.assertNotIn("PASS", json.dumps(result))

    def test_abstain_labels_override_confidence_and_all_abstained(self):
        self.write([
            {"id": "1", "expected": "a", "predicted": "insufficient_evidence", "confidence": 1},
            {"id": "2", "expected": "b", "predicted": "review", "confidence": 1},
        ])
        result = self.call(
            "--kind", "choice", "--min-confidence", "0", "--labels", "a", "b",
            "--abstain-label", "insufficient_evidence", "--abstain-label", "review",
        )
        self.assertEqual(result["accepted"], 0)
        self.assertIsNone(result["accuracy_accepted"])
        self.assertIsNone(result["accuracy_accepted_wilson_95"])
        self.assertIsNone(result["macro_f1_accepted_fixed_labels"])

    def test_noul_band_boundaries_and_calibration(self):
        self.write([
            {"id": str(i), "expected": expected, "predicted": predicted}
            for i, (expected, predicted) in enumerate(
                [(False, .199), (True, .2), (False, .799), (True, .8)]
            )
        ])
        result = self.call("--kind", "noul", "--band", ".2", ".8")
        self.assertEqual((result["accepted"], result["abstained"]), (2, 2))
        self.assertEqual(result["accuracy_accepted"], 1)
        self.assertEqual(result["confusion_accepted"], {"tn": 1, "tp": 1, "fp": 0, "fn": 0})
        self.assertEqual((result["precision"], result["recall"], result["specificity"]), (.5, .5, .5))
        self.assertEqual((result["precision_accepted"], result["recall_accepted"], result["specificity_accepted"]), (1, 1, 1))
        self.assertAlmostEqual(result["brier_all"], (.199**2 + .8**2 + .799**2 + .2**2) / 4)
        self.assertAlmostEqual(result["ece_10_bins_all"], .4995)

    def test_score_metrics_and_all_abstained(self):
        self.write([
            {"id": str(i), "expected": expected, "predicted": predicted, "confidence": confidence}
            for i, (expected, predicted, confidence) in enumerate(
                [(0, .5, .9), (3, 1.5, .9), (2, 2, .1)]
            )
        ])
        result = self.call("--kind", "score", "--min-confidence", ".8", "--levels", "4")
        self.assertAlmostEqual(result["mae_all"], 2 / 3)
        self.assertEqual(result["mae_accepted"], 1)
        self.assertEqual(result["within_one_level_accepted"], .5)
        self.assertEqual(result["per_class"][2]["coverage"], 0)

    def test_raw_join_shuffled_ids_and_two_questions(self):
        self.write([
            {"id": "2", "answers": {
                "department": {"type": "choice", "choice": "b", "confidence": .9},
                "urgency": {"type": "score", "score": 3, "confidence": .9},
            }},
            {"id": "1", "answers": {
                "department": {"type": "choice", "choice": "a", "confidence": .9},
                "urgency": {"type": "score", "score": 1, "confidence": .9},
            }},
        ])
        self.write([{"id": "1", "expected": "a"}, {"id": "2", "expected": "b"}], self.expected)
        result = self.call(
            "--kind", "choice", "--expected", str(self.expected), "--question", "department",
            "--min-confidence", ".8", "--labels", "a", "b",
        )
        self.assertEqual(result["input_format"], "raw_batch_join")
        self.assertEqual(result["accuracy_accepted"], 1)
        self.write([{"id": "1", "expected": 1}, {"id": "2", "expected": 3}], self.expected)
        result = self.call(
            "--kind", "score", "--expected", str(self.expected), "--question", "urgency",
            "--min-confidence", ".8", "--levels", "4",
        )
        self.assertEqual(result["mae_accepted"], 0)

    def test_synthetic_200_ticket_path(self):
        raw, labels = [], []
        for i in range(200):
            ident = f"ticket-{i:03}"
            team = "billing" if i % 2 == 0 else "technical"
            raw.append({"id": ident, "answers": {
                "department": {"type": "choice", "choice": team, "confidence": .95 if i < 160 else .4},
            }})
            labels.append({"id": ident, "expected": team})
        self.write(list(reversed(raw)))
        self.write(labels, self.expected)
        result = self.call(
            "--kind", "choice", "--expected", str(self.expected), "--question", "department",
            "--min-confidence", ".8", "--labels", "billing", "technical",
        )
        self.assertEqual((result["n"], result["accepted"], result["coverage"]), (200, 160, .8))
        self.assertEqual(result["accuracy_accepted"], 1)

    def test_rejects_invalid_rows_even_if_abstained(self):
        valid = {"id": "1", "expected": 1, "predicted": 1, "confidence": 0}
        for field, value in [
            ("expected", "1"), ("expected", 1.0), ("expected", True), ("expected", -1),
            ("predicted", {}), ("predicted", True), ("predicted", 3),
            ("confidence", True), ("confidence", -1), ("confidence", 2),
        ]:
            with self.subTest(field=field, value=value):
                self.write([{**valid, field: value}])
                self.call("--kind", "score", "--min-confidence", ".8", "--levels", "3", success=False)

    def test_rejects_bad_json_duplicate_keys_and_bad_ids(self):
        for content in [
            "", "\n", "[]\n", "not json", '{"id":"1","id":"2"}',
            '{"id":"1","expected":true,"predicted":NaN}',
            '{"id":"1","expected":true,"predicted":Infinity}',
            '{"id":"1","expected":true,"predicted":1e999}',
            '{"id":"1","expected":true,"predicted":.5}',
            '{"id":"1"}\n{"id":"1"}',
            '{"id":" ","expected":true,"predicted":.5}',
        ]:
            with self.subTest(content=content):
                self.input.write_text(content, encoding="utf-8")
                self.call("--kind", "noul", "--threshold", ".5", success=False)

    def test_rejects_nonfinite_parameters_and_missing_modes(self):
        self.write([{"id": "1", "expected": True, "predicted": .9}])
        bad = [
            [], ["--kind", "noul"], ["--kind", "choice"],
            ["--kind", "score", "--min-confidence", ".8"],
            ["--kind", "score", "--min-confidence", ".8", "--levels", "11"],
            ["--kind", "noul", "--threshold", ".5", "--band", ".2", ".8"],
            ["--kind", "noul", "--band", ".8", ".2"],
            ["--kind", "choice", "--min-confidence", ".8", "--labels", "a", "a"],
            ["--kind", "noul", "--threshold", ".5", "--abstain-label", "a"],
        ]
        for flags in bad:
            with self.subTest(flags=flags):
                self.call(*flags, success=False)
        for value in ("nan", "inf", "-inf", "-0.1", "1.1"):
            for kind, option in (("noul", "--threshold"), ("choice", "--min-confidence")):
                with self.subTest(kind=kind, value=value):
                    self.call("--kind", kind, f"{option}={value}", success=False)

    def test_raw_join_rejects_mismatch_errors_missing_answers_and_wrong_type(self):
        self.write([{"id": "1", "expected": "a"}], self.expected)
        valid = {"id": "1", "answers": {"department": {
            "type": "choice", "choice": "a", "confidence": .9,
        }}}
        cases = [
            [{**valid, "id": "2"}],
            [valid, {**valid, "id": "2"}],
            [valid, valid],
            [{**valid, "error": "provider detail must not be printed"}],
            [{"id": "1"}],
            [{**valid, "answers": {}}],
            [{**valid, "answers": {"department": {"choice": "a", "confidence": .9}}}],
            [{**valid, "answers": {"department": {"type": "score", "score": 1, "confidence": .9}}}],
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.write(raw)
                error = self.call(
                    "--kind", "choice", "--expected", str(self.expected), "--question", "department",
                    "--min-confidence", ".8", success=False,
                )
                self.assertNotIn("provider detail", error)

    def test_raw_join_requires_auditable_metadata(self):
        valid = {"id": "1", "answers": {"department": {
            "type": "choice", "choice": "a", "confidence": .9,
        }}}
        self.write([{"id": "1", "expected": "a"}], self.expected)
        self.input.write_text(json.dumps(valid) + "\n", encoding="utf-8")
        self.call(
            "--kind", "choice", "--expected", str(self.expected), "--question", "department",
            "--min-confidence", ".8", success=False,
        )
        for field, value in [
            ("model", ""), ("requested_model", None),
            ("question_sha256", "x" * 64), ("state_sha256", "0" * 63),
        ]:
            with self.subTest(field=field, value=value):
                self.write([{**valid, field: value}])
                self.call(
                    "--kind", "choice", "--expected", str(self.expected), "--question", "department",
                    "--min-confidence", ".8", success=False,
                )

    def test_label_and_confidence_strict(self):
        valid = {"id": "1", "expected": "a", "predicted": "a", "confidence": 0}
        for field in ("id", "expected", "predicted"):
            for value in (None, "", "  ", False, 2, [], {}):
                with self.subTest(field=field, value=value):
                    self.write([{**valid, field: value}])
                    self.call("--kind", "choice", "--min-confidence", ".8", success=False)
        self.write([{k: v for k, v in valid.items() if k != "confidence"}])
        self.call("--kind", "choice", "--min-confidence", "0", success=False)
        self.write([{**valid, "predicted": "unknown"}])
        self.call("--kind", "choice", "--min-confidence", ".8", "--labels", "a", "b", success=False)


if __name__ == "__main__":
    unittest.main()
