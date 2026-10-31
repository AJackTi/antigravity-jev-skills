#!/usr/bin/env python3
"""Render and validate a compact quant audit pack from JSON."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

FIELDS = (
    ("TASK/HYPOTHESIS", "task"),
    ("MATH DIFF", "math_diff"),
    ("DATA", "data"),
    ("FRICTION", "friction"),
    ("RESULTS", "results"),
    ("LEAKAGE TEST", "leakage_test"),
    ("PARITY", "parity"),
    ("PROOF", "proof"),
    ("VERDICT", "verdict"),
    ("CONFIDENCE", "confidence"),
    ("RESIDUAL RISK", "residual_risk"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path, nargs="?", help="JSON input; defaults to stdin")
    parser.add_argument("--output", type=pathlib.Path, help="write pack here instead of stdout")
    parser.add_argument("--max-chars", type=int, default=6000)
    return parser.parse_args()


def read_input(path: pathlib.Path | None) -> dict[str, object]:
    raw = path.read_text(encoding="utf-8") if path else sys.stdin.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def render(value: dict[str, object], max_chars: int) -> tuple[str, list[str]]:
    warnings: list[str] = []
    verdict = str(value.get("verdict", "BLOCKED")).upper()
    if verdict not in {"PASS", "REJECT", "BLOCKED"}:
        raise ValueError("verdict must be PASS, REJECT, or BLOCKED")
    proof = str(value.get("proof", "")).strip()
    if verdict == "PASS" and not proof:
        verdict = "BLOCKED"
        warnings.append("PASS downgraded to BLOCKED because proof is empty")
    confidence = value.get("confidence", "unknown")
    try:
        if confidence != "unknown" and not 0 <= float(confidence) <= 100:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be unknown or a number from 0 to 100") from exc

    lines = ["### TARGETED QUANT AUDIT PACK", ""]
    for label, key in FIELDS:
        item = verdict if key == "verdict" else value.get(key, "unknown")
        text = str(item).strip().replace("\x00", "")
        lines.append(f"{label}: {text or 'unknown'}")
    if warnings:
        lines.append("WARNINGS: " + "; ".join(warnings))
    pack = "\n".join(lines) + "\n"
    if len(pack) > max_chars:
        raise ValueError(f"audit pack is {len(pack)} chars; limit is {max_chars}")
    return pack, warnings


def main() -> int:
    args = parse_args()
    try:
        pack, warnings = render(read_input(args.input), args.max_chars)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(pack, encoding="utf-8")
        print(f"Generated {args.output}")
    else:
        sys.stdout.write(pack)
    return 0 if not warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())
