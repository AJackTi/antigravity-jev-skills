#!/usr/bin/env python3
"""Replay task and quant fixtures as a lightweight skill regression suite."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK_SCRIPT = ROOT / "scripts" / "build_task_packet.py"
QUANT_SCRIPT = ROOT.parent / "antigravity-sol-quant-runner" / "scripts" / "build_quant_audit_pack.py"


def run(command: list[str], payload: str | None = None) -> tuple[int, str]:
    result = subprocess.run(command, input=payload, text=True, capture_output=True, check=False)
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    failures: list[str] = []
    tasks = json.loads((ROOT / "fixtures" / "task_cases.json").read_text(encoding="utf-8"))
    for case in tasks:
        code, output = run([sys.executable, str(TASK_SCRIPT), case["task"], *case["files"]])
        missing = [item for item in case["expected"] if item not in output]
        if code != 0 or missing:
            failures.append(f"task:{case['name']} code={code} missing={missing}")

    quant_root = QUANT_SCRIPT.parents[1]
    quant_cases = json.loads((quant_root / "fixtures" / "quant_cases.json").read_text(encoding="utf-8"))
    for case in quant_cases:
        code, output = run([sys.executable, str(QUANT_SCRIPT)], json.dumps(case["input"]))
        if code != case["expected_status"]:
            failures.append(f"quant:{case['name']} code={code} expected={case['expected_status']} output={output[:160]}")

    if failures:
        print("REPLAY FAIL")
        print("\n".join(failures))
        return 1
    print(f"REPLAY PASS: {len(tasks)} task cases, {len(quant_cases)} quant cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
