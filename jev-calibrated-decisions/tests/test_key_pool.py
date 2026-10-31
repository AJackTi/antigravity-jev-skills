"""Offline tests; fixtures are fake keys in temporary directories only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
POOL_PATH = ROOT / "scripts" / "jev-pool"
loader = importlib.machinery.SourceFileLoader("jev_pool", str(POOL_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
pool = importlib.util.module_from_spec(spec)
loader.exec_module(pool)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jev-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "keys"
        self.directory.mkdir(mode=0o700)
        self.environment = {
            "TYPESAFE_KEY_DIR": str(self.directory),
            "JEV_AGENT": "offline-worker",
            "PATH": os.defpath,
        }
        for n in range(1, 4):
            item = self.directory / f"key-{n:02}.key"
            item.write_text(f"fake-test-key-{n}\n")
            item.chmod(0o600)

    def select(self, **environment):
        return pool.select_key(self.directory, self.environment | environment)

    def test_explicit_slot_precedes_mode_and_environment_key(self):
        self.assertEqual(self.select(JEV_KEY_SLOT="2", JEV_KEY_MODE="invalid",
                                     TYPESAFE_API_KEY="stale-fake"), "fake-test-key-2")

    def test_stable_assignment_is_repeatable(self):
        self.assertEqual(self.select(), self.select())
        self.assertFalse((self.directory / ".rotation.json").exists())

    def test_round_robin_wraps(self):
        values = [self.select(JEV_KEY_MODE="round-robin") for _ in range(7)]
        self.assertEqual(values, ["fake-test-key-1", "fake-test-key-2",
                                 "fake-test-key-3"] * 2 + ["fake-test-key-1"])
        self.assertEqual((self.directory / ".rotation.json").stat().st_mode & 0o777, 0o600)

    def test_round_robin_multiple_processes(self):
        code = (
            "import runpy,sys; from pathlib import Path; "
            "m=runpy.run_path(sys.argv[1]); "
            "print(m['select_key'](Path(sys.argv[2]), {'JEV_KEY_MODE':'round-robin'}))"
        )
        def invoke(_):
            result = subprocess.run(
                [sys.executable, "-B", "-c", code, str(POOL_PATH), str(self.directory)],
                capture_output=True, text=True, check=True, env={"PATH": os.defpath},
                timeout=15,
            )
            return result.stdout.strip()
        with ThreadPoolExecutor(max_workers=8) as workers:
            outputs = list(workers.map(invoke, range(24)))
        self.assertEqual({v: outputs.count(v) for v in set(outputs)},
                         {f"fake-test-key-{i}": 8 for i in range(1, 4)})

    def test_invalid_slots_and_mode(self):
        for slot in ("", "0", "-1", "1.0", "abc", "04", "4", "9" * 100):
            with self.subTest(slot=slot), self.assertRaises(pool.PoolError):
                self.select(JEV_KEY_SLOT=slot)
        with self.assertRaises(pool.PoolError):
            self.select(JEV_KEY_MODE="unknown")

    def test_rejects_directory_permissions(self):
        self.directory.chmod(0o755)
        with self.assertRaises(pool.PoolError):
            self.select()

    def test_rejects_key_permissions_even_for_unselected_member(self):
        (self.directory / "key-03.key").chmod(0o644)
        with self.assertRaises(pool.PoolError):
            self.select(JEV_KEY_SLOT="1")

    def test_rejects_symlink_key(self):
        (self.directory / "link.key").symlink_to(self.directory / "key-01.key")
        with self.assertRaises((pool.PoolError, OSError)):
            self.select(JEV_KEY_SLOT="1")

    def test_rejects_symlink_directory(self):
        alias = Path(self.temporary.name) / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            pool.select_key(alias, {})

    def test_rejects_hardlink(self):
        os.link(self.directory / "key-01.key", self.directory / "copy.key")
        with self.assertRaises(pool.PoolError):
            self.select()

    def test_rejects_invalid_key_values(self):
        target = self.directory / "key-01.key"
        for value in ("", "one two", "one\ntwo", "bad\tkey", "x" * 4097, "\u00e9", "\n"):
            target.write_text(value)
            with self.subTest(value=value[:20]), self.assertRaises(pool.PoolError):
                self.select(JEV_KEY_SLOT="1")

    def test_missing_and_empty_pool(self):
        with self.assertRaises(OSError):
            pool.select_key(self.directory / "missing", {})
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir(mode=0o700)
        with self.assertRaises(pool.PoolError):
            pool.select_key(empty, {})

    def test_rejects_invalid_filename(self):
        (self.directory / "bad\nname.key").write_text("fake")
        with self.assertRaises(pool.PoolError):
            self.select()

    def test_rejects_corrupt_or_symlink_counter(self):
        counter = self.directory / ".rotation.json"
        counter.write_text('{"unexpected":123}')
        counter.chmod(0o600)
        with self.assertRaises(pool.PoolError):
            self.select(JEV_KEY_MODE="round-robin")
        counter.unlink()
        counter.symlink_to(self.directory / "key-01.key")
        with self.assertRaises(OSError):
            self.select(JEV_KEY_MODE="round-robin")

    def test_exec_has_no_key_in_argv(self):
        with patch.dict(os.environ, self.environment | {"JEV_KEY_SLOT": "2"}, clear=True):
            with patch.object(pool.os, "execve") as execute:
                self.assertEqual(pool.main(["--version"]), 0)
        program, argv, environment = execute.call_args.args
        self.assertEqual(environment["TYPESAFE_API_KEY"], "fake-test-key-2")
        self.assertNotIn("fake-test-key-2", repr(argv))
        self.assertEqual(argv, [str(ROOT / "scripts" / "jev"), "--version"])
        self.assertEqual(program, argv[0])

    def test_rejects_key_override_abbreviations(self):
        for flag in ("--api-key", "--api-key=fake", "--api=fake", "--a"):
            with self.subTest(flag=flag), patch.object(pool, "select_key") as select:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(pool.main(["yes", "question", flag]), 2)
                select.assert_not_called()

    def test_rejects_mutating_auth(self):
        for args in (["auth", "set"], ["auth", "clear"]):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(pool.main(args), 2)

    def test_rejects_endpoint_override_without_reading_keys(self):
        with patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://example.invalid"}, clear=True):
            with patch.object(pool, "select_key") as select, redirect_stderr(io.StringIO()):
                self.assertEqual(pool.main(["--version"]), 2)
                select.assert_not_called()

    def test_errors_redact_secret_and_file_content(self):
        error = io.StringIO()
        with patch.dict(os.environ, self.environment, clear=True):
            with patch.object(pool, "select_key", side_effect=OSError("fake-secret-key")):
                with redirect_stderr(error):
                    self.assertEqual(pool.main(["--version"]), 3)
        self.assertNotIn("fake-secret-key", error.getvalue())

    def test_offline_help_does_not_read_keys(self):
        with patch.object(pool, "select_key") as select, redirect_stdout(io.StringIO()):
            self.assertEqual(pool.main(["--help"]), 0)
            select.assert_not_called()

    def test_real_launcher_offline_version(self):
        result = subprocess.run(
            [str(POOL_PATH), "--version"], env=self.environment,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "jev 1.1.0")
        self.assertNotIn("fake-test-key", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
