#!/usr/bin/env python3
"""Record redacted run telemetry as JSONL or stdout."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import sys

_GATE_PATH = pathlib.Path(__file__).resolve().parent / "jev_review_gate.py"
_spec = importlib.util.spec_from_file_location("jev_review_gate", _GATE_PATH)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("task", "quant", "review"))
    parser.add_argument("--mode", required=True)
    parser.add_argument("--status", required=True, choices=("pass", "reject", "blocked", "error"))
    parser.add_argument("--model", default="local-codex")
    parser.add_argument("--duration-ms", type=int, default=0)
    parser.add_argument("--input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--review-loops", type=int, default=0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--command", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--output", type=pathlib.Path, help="append JSONL here; otherwise stdout")
    return parser.parse_args()


def redact(text: str) -> str:
    return _gate.redact(text)


def main() -> int:
    args = parse_args()
    values = vars(args).copy()
    output = values.pop("output")
    values["timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat()
    values["model"] = redact(str(values["model"]))
    values["command"] = redact(str(values["command"]))
    values["notes"] = redact(str(values["notes"]))
    line = json.dumps(values, sort_keys=True, ensure_ascii=True)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(f"Recorded {output}")
    else:
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
