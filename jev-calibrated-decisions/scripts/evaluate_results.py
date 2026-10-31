#!/usr/bin/env python3
"""Evaluate labeled Jev results offline; no network calls or model-quality verdicts."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path


class EvaluationError(ValueError):
    """Invalid input; messages deliberately omit record content and identifiers."""




def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationError(message)


def number(value: object, name: str, low: float, high: float) -> float:
    require(type(value) in (int, float), f"{name} must be a JSON number, not a boolean")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise EvaluationError(f"{name} must be finite") from None
    require(math.isfinite(result) and low <= result <= high,
            f"{name} must be finite and in [{low},{high}]")
    return result


def label(value: object, name: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{name} must be a nonempty string")
    return value


def unique_object(pairs: list[tuple]) -> dict:
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON object key")
        obj[key] = value
    return obj


def invalid_constant(_: str) -> None:
    raise EvaluationError("non-finite JSON constant")


def read_rows(path: str, role: str) -> dict[str, dict]:
    """Read JSONL with unique, nonempty string IDs and no duplicate JSON keys."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise EvaluationError(f"cannot read {role} as UTF-8") from None
    result = {}
    for line_no, raw in enumerate(source.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            require(isinstance(row, dict), "row must be a JSON object")
            ident = label(row.get("id"), "id")
            require(ident not in result, "duplicate id")
        except (ValueError, RecursionError):
            # JSON errors can contain input snippets; never print record content.
            raise EvaluationError(f"{role} line {line_no}: invalid JSON or record") from None
        result[ident] = row
    require(bool(result), f"{role} has no records")
    return result


def load_data(args: argparse.Namespace) -> list[dict]:
    inputs = read_rows(args.input, "input")
    if args.expected is None:
        return list(inputs.values())
    expected = read_rows(args.expected, "expected")
    require(set(inputs) == set(expected), "input and expected ID sets must match exactly")
    data = []
    for ident, row in inputs.items():
        require("error" not in row, "raw batch contains an API error; evaluation refused")
        for metadata in ("model", "requested_model", "question_sha256", "state_sha256"):
            require(metadata in row, f"raw batch row is missing {metadata}")
        require(label(row["model"], "raw model"), "raw model is required")
        require(label(row["requested_model"], "raw requested_model"), "raw requested_model is required")
        for metadata in ("question_sha256", "state_sha256"):
            require(isinstance(row[metadata], str) and re.fullmatch(r"[0-9a-f]{64}", row[metadata]) is not None,
                    f"raw {metadata} must be a lowercase SHA-256 hex digest")
        answers = row.get("answers")
        require(isinstance(answers, dict), "raw batch row is missing answers")
        answer = answers.get(args.question)
        require(isinstance(answer, dict), "raw batch row is missing the selected question")
        require(answer.get("type") == args.kind, "raw answer type does not match --kind")
        require(args.kind in answer, "raw answer is missing its predicted value")
        require("expected" in expected[ident], "expected row is missing expected")
        normalized = {
            "id": ident,
            "expected": expected[ident]["expected"],
            "predicted": answer[args.kind],
        }
        if "confidence" in answer:
            normalized["confidence"] = answer["confidence"]
        data.append(normalized)
    return data


def validate_options(args: argparse.Namespace) -> None:
    require((args.expected is None) == (args.question is None),
            "--expected and --question must be used together")
    if args.question is not None:
        label(args.question, "--question")
    if args.kind == "noul":
        require((args.threshold is None) != (args.band is None),
                "noul requires exactly one of --threshold or --band")
        require(args.min_confidence is None, "--min-confidence is only for choice/score")
        if args.threshold is not None:
            number(args.threshold, "--threshold", 0, 1)
        else:
            low, high = (number(value, "--band", 0, 1) for value in args.band)
            require(low < high, "--band requires LOW < HIGH")
    else:
        require(args.threshold is None and args.band is None,
                "--threshold/--band are only for noul")
        require(args.min_confidence is not None,
                "choice/score require explicit --min-confidence")
        number(args.min_confidence, "--min-confidence", 0, 1)
    if args.kind == "score":
        require(args.levels is not None and 2 <= args.levels <= 10,
                "score requires --levels in 2..10")
    else:
        require(args.levels is None, "--levels is only for score")
    require(args.kind == "choice" or not (args.labels or args.abstain_label),
            "--labels/--abstain-label are only for choice")
    for values, name in ((args.labels or [], "--labels"),
                         (args.abstain_label, "--abstain-label")):
        for value in values:
            label(value, name)
        require(len(set(values)) == len(values), f"{name} must not contain duplicates")
    require(not set(args.labels or ()) & set(args.abstain_label),
            "--labels and --abstain-label must be disjoint")


def validate_data(data: list[dict], args: argparse.Namespace) -> list:
    """Validate every row before applying any confidence gate."""
    for index, row in enumerate(data, 1):
        require("error" not in row, f"record {index}: API errors cannot be evaluated as predictions")
        require("expected" in row and "predicted" in row,
                f"record {index}: expected and predicted are required")
        truth, predicted = row["expected"], row["predicted"]
        if args.kind == "noul":
            require(type(truth) is bool,
                    f"record {index}: noul expected must be a JSON boolean")
            number(predicted, f"record {index}: predicted", 0, 1)
        elif args.kind == "choice":
            label(truth, f"record {index}: expected")
            label(predicted, f"record {index}: predicted")
            if args.labels:
                require(truth in args.labels,
                        f"record {index}: expected outside --labels")
                require(predicted in set(args.labels) | set(args.abstain_label),
                        f"record {index}: predicted outside vocabulary")
        else:
            require(type(truth) is int,
                    f"record {index}: score expected must be a JSON integer level")
            number(truth, f"record {index}: expected", 0, args.levels - 1)
            number(predicted, f"record {index}: predicted", 0, args.levels - 1)
        # Choice and score responses always need confidence, even at threshold 0.
        if args.kind != "noul" or "confidence" in row:
            number(row.get("confidence"), f"record {index}: confidence", 0, 1)
    if args.kind == "noul":
        return [False, True]
    if args.kind == "score":
        return list(range(args.levels))
    # Freeze this universe before filtering; wholly abstained classes remain visible.
    return args.labels or sorted({row["expected"] for row in data} | {
        row["predicted"] for row in data if row["predicted"] not in args.abstain_label
    })


def mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


def wilson(correct: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = correct / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def confusion(pairs: list[tuple]) -> dict:
    return {
        "tp": sum(y is True and p is True for y, p in pairs),
        "tn": sum(y is False and p is False for y, p in pairs),
        "fp": sum(y is False and p is True for y, p in pairs),
        "fn": sum(y is True and p is False for y, p in pairs),
    }


def rates(matrix: dict) -> dict:
    """Classification rates with null for an undefined denominator."""
    return {
        "precision": matrix["tp"] / (matrix["tp"] + matrix["fp"]) if matrix["tp"] + matrix["fp"] else None,
        "recall": matrix["tp"] / (matrix["tp"] + matrix["fn"]) if matrix["tp"] + matrix["fn"] else None,
        "specificity": matrix["tn"] / (matrix["tn"] + matrix["fp"]) if matrix["tn"] + matrix["fp"] else None,
    }


def evaluate(data: list[dict], args: argparse.Namespace) -> dict:
    labels = validate_data(data, args)
    accepted, all_pairs = [], []
    # A band has no one forced cutoff; 0.5 is exposed explicitly for the forced metric.
    forced_threshold = args.threshold if args.threshold is not None else 0.5
    for row in data:
        truth, predicted = row["expected"], row["predicted"]
        if args.kind == "noul":
            all_pairs.append((truth, predicted >= forced_threshold))
            if args.band is not None:
                low, high = args.band
                accept = predicted < low or predicted >= high
                decision = predicted >= high
            else:
                accept, decision = True, predicted >= args.threshold
        else:
            all_pairs.append((truth, predicted))
            accept = row["confidence"] >= args.min_confidence
            accept = accept and not (args.kind == "choice" and predicted in args.abstain_label)
            decision = predicted
        if accept:
            accepted.append((truth, decision))
    n, count = len(data), len(accepted)
    result = {
        "schema_version": 1,
        "kind": args.kind,
        "input_format": "raw_batch_join" if args.expected else "flat",
        "question": args.question,
        "n": n,
        "accepted": count,
        "abstained": n - count,
        "accepted_denominator": count,
        "coverage": count / n,
        "policy": {
            "threshold": args.threshold,
            "band": args.band,
            "min_confidence": args.min_confidence,
            "abstain_labels": args.abstain_label,
            "levels": args.levels,
            "label_universe": labels,
        },
    }
    per_class = []
    if args.kind == "score":
        all_errors = [abs(y - p) for y, p in all_pairs]
        accepted_errors = [abs(y - p) for y, p in accepted]
        result.update({
            "mae_all": mean(all_errors),
            "mae_accepted": mean(accepted_errors),
            "within_one_level_all": mean([e <= 1 for e in all_errors]),
            "within_one_level_accepted": mean([e <= 1 for e in accepted_errors]),
        })
    else:
        correct = sum(y == p for y, p in accepted)
        result.update({
            "correct_all_if_forced": sum(y == p for y, p in all_pairs),
            "accuracy_all_if_forced": mean([y == p for y, p in all_pairs]),
            "correct_accepted": correct,
            "errors_accepted": count - correct,
            "accuracy_accepted": correct / count if count else None,
            "accuracy_accepted_wilson_95": wilson(correct, count),
            "interval_assumption": "IID accepted labeled cases; not a model-quality guarantee",
        })
    f1_values = []
    for value in labels:
        all_members = [(y, p) for y, p in all_pairs if y == value]
        members = [(y, p) for y, p in accepted if y == value]
        support, accepted_support = len(all_members), len(members)
        entry = {
            "label": value,
            "support": support,
            "accepted_support": accepted_support,
            "abstained": support - accepted_support,
            "coverage": accepted_support / support if support else None,
        }
        if args.kind == "score":
            entry.update({
                "mae_all": mean([abs(y - p) for y, p in all_members]),
                "mae_accepted": mean([abs(y - p) for y, p in members]),
            })
        else:
            tp = sum(y == value and p == value for y, p in members)
            fp = sum(y != value and p == value for y, p in accepted)
            fn = sum(y == value and p != value for y, p in accepted)
            f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0
            f1_values.append(f1)
            entry.update({
                "correct_accepted": tp,
                "errors_accepted": fn,
                "errors_all_if_forced": sum(y != p for y, p in all_members),
                "false_positives_accepted": fp,
                "f1_accepted": f1 if count else None,
            })
        per_class.append(entry)
    result["per_class"] = per_class
    if args.kind != "score":
        result["macro_f1_accepted_fixed_labels"] = mean(f1_values) if count else None
        result["f1_zero_division"] = "0 for a class with no accepted support/predictions; null when no cases accepted"
    if args.kind == "noul":
        bins = [[] for _ in range(10)]
        for row in data:
            p = row["predicted"]
            bins[min(9, int(p * 10))].append((p, int(row["expected"])))
        all_matrix = confusion(all_pairs)
        accepted_matrix = confusion(accepted)
        all_rates = rates(all_matrix)
        accepted_rates = rates(accepted_matrix)
        result.update({
            "forced_threshold": forced_threshold,
            "confusion_all_if_forced": all_matrix,
            "confusion_accepted": accepted_matrix,
            "precision": all_rates["precision"],
            "recall": all_rates["recall"],
            "specificity": all_rates["specificity"],
            "precision_all_if_forced": all_rates["precision"],
            "recall_all_if_forced": all_rates["recall"],
            "specificity_all_if_forced": all_rates["specificity"],
            "precision_accepted": accepted_rates["precision"],
            "recall_accepted": accepted_rates["recall"],
            "specificity_accepted": accepted_rates["specificity"],
            "brier_all": mean([(row["predicted"] - int(row["expected"])) ** 2 for row in data]),
            "ece_10_bins_all": sum(
                len(bucket) / n * abs(mean([p for p, _ in bucket]) - mean([y for _, y in bucket]))
                for bucket in bins if bucket
            ),
        })
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--kind", choices=("noul", "choice", "score"), required=True)
    result.add_argument("--input", required=True,
                        help="flat result JSONL, or raw batch output with --expected/--question")
    result.add_argument("--expected", help="separate JSONL labels {id, expected}; exact ID match required")
    result.add_argument("--question", help="question ID to extract from raw batch answers")
    result.add_argument("--threshold", type=float, help="explicit noul yes threshold in [0,1]")
    result.add_argument("--band", type=float, nargs=2, metavar=("LOW", "HIGH"),
                        help="noul: p<LOW no, p>=HIGH yes, otherwise abstain")
    result.add_argument("--min-confidence", type=float,
                        help="required for choice/score, including an explicit 0")
    result.add_argument("--levels", type=int, help="score rubric size (2..10), required for range validation")
    result.add_argument("--labels", nargs="+",
                        help="fixed choice vocabulary; abstain labels may additionally appear as predictions")
    result.add_argument("--abstain-label", action="append", default=[],
                        help="choice label that always abstains; repeatable")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_options(args)
        output = evaluate(load_data(args), args)
    except EvaluationError as exc:
        print(f"evaluate_results: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, allow_nan=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
