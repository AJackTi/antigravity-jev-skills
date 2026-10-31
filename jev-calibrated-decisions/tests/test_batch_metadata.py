import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "jev.py"


def load_jev():
    spec = importlib.util.spec_from_file_location("jev_batch_test_module", SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BatchMetadataTests(unittest.TestCase):
    def test_structured_rows_keep_named_state_and_audit_hashes(self):
        jev = load_jev()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.model = kwargs.get("model") or "jev-1.13.0"

            def ask(self, state, questions):
                self.state = state
                return {
                    "model": "jev-1.13.0",
                    "answers": {"q": {"type": "noul", "noul": 0.9}},
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                }

        with tempfile.TemporaryDirectory(prefix="jev-batch-test-") as directory:
            root = Path(directory)
            source = root / "input.jsonl"
            output = root / "output.jsonl"
            source.write_text('{"id":"a","message":"hello"}\n')
            args = SimpleNamespace(
                input=str(source),
                text_lines=False,
                state_key=None,
                id_key="id",
                api_key=None,
                model="jev-1.13.0",
                timeout=1,
                questions_file=None,
                questions_json='{"q":{"type":"noul","instructions":"Is `message` relevant?"}}',
                qlist=None,
                out=str(output),
                overwrite=False,
                echo=False,
                concurrency=1,
            )
            with patch.object(jev, "JevClient", FakeClient):
                self.assertEqual(jev.cmd_batch(args), 0)
            row = json.loads(output.read_text())
        self.assertEqual(row["id"], "a")
        self.assertEqual(row["model"], "jev-1.13.0")
        self.assertEqual(row["requested_model"], "jev-1.13.0")
        self.assertEqual(len(row["state_sha256"]), 64)
        self.assertEqual(len(row["question_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
