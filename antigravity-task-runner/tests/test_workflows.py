#!/usr/bin/env python3
"""Unit tests for the bounded packet, audit-pack, and telemetry contracts."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TASK = ROOT / "scripts" / "build_task_packet.py"
QUANT = ROOT.parent / "antigravity-sol-quant-runner" / "scripts" / "build_quant_audit_pack.py"
TELEMETRY = ROOT / "scripts" / "record_run.py"


def execute(script: pathlib.Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script), *args], input=stdin, text=True, capture_output=True, check=False)


class WorkflowTests(unittest.TestCase):
    def test_protected_task_adds_registry_context(self) -> None:
        result = execute(TASK, "audit funding", "strategies/funding.go")
        self.assertEqual(result.returncode, 0)
        self.assertIn("FIXES_REGISTRY.md", result.stdout)
        self.assertIn("Stop conditions", result.stdout)

    def test_quant_pass_requires_proof(self) -> None:
        result = execute(QUANT, stdin=json.dumps({"task": "x", "verdict": "PASS", "confidence": 95}))
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKED", result.stdout)
        self.assertIn("proof is empty", result.stdout)

    def test_quant_rejects_invalid_confidence(self) -> None:
        result = execute(QUANT, stdin=json.dumps({"task": "x", "verdict": "REJECT", "confidence": 101}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("confidence", result.stderr)

    def test_telemetry_redacts_secrets(self) -> None:
        result = execute(
            TELEMETRY,
            "--kind", "review", "--mode", "dual", "--status", "pass",
            "--command", "curl token=secret-value -H 'Authorization: Bearer bearer-part-1 bearer-part-2'",
            "--notes", "api_key: abc; password = spaced-secret-val",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("<redacted>", result.stdout)
        self.assertNotIn("secret-value", result.stdout)
        self.assertNotIn("bearer-part-1", result.stdout)
        self.assertNotIn("bearer-part-2", result.stdout)
        self.assertNotIn("abc", result.stdout)
        self.assertNotIn("spaced-secret-val", result.stdout)


if __name__ == "__main__":
    unittest.main()
