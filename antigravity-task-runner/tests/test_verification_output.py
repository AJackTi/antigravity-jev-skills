from pathlib import Path
import sys, tempfile, unittest
from test_run_workflow import wf

S3 = 'Preserved original instruction prefix: passed\nUTF-8, newline and instruction size integrity: passed\nStatic integrity checks: 2 passed'
PY = f'{sys.executable} -B'


class VerificationOutputTests(unittest.TestCase):
    def _chk(self, cmd, extra=None, exp=(False, 1, 0), inc=(), exc=()):
        fs = {'mod.py': 'HEADER = "preserved"\n', **(extra or {})}
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / 'w'
            ws.mkdir()
            b = wf.snapshot_workspace(ws)
            for k, v in fs.items():
                (ws / k).parent.mkdir(parents=True, exist_ok=True)
                (ws / k).write_text(v)
            m = wf.run_mechanical_checks(ws, b, list(fs), [cmd], attempt_dir=Path(d) / 'a')
            t = m['checks'][0]
            if exp:
                self.assertEqual((m['all_passed'], t['exit_code'], t['passed']), exp)
            for s in inc:
                self.assertIn(s, t['output'])
            for s in exc:
                self.assertNotIn(s, t['output'])
            return m, t

    def test_static_output_fails_with_diagnostic(self):
        self.assertEqual(wf.parse_test_count(S3, 0), 0)
        self._chk(f'{PY} -c "print({S3!r})"', inc=('(exit 0)', S3, 'no recognized executed-test summary found', 'unittest', 'static integrity assertions'))

    def test_real_passing_unittest_counts_two(self):
        s = 'import pathlib as p, unittest as u\nclass T(u.TestCase):\n def test_1(self): self.assertTrue(p.Path("mod.py").read_text().startswith("HEADER ="))\n def test_2(self): self.assertNotIn(b"\\r\\n", p.Path("mod.py").read_bytes())\n'
        self._chk(f'{PY} -m unittest discover -s tests -v', {'tests/test_s.py': s}, (True, 0, 2), ('Ran 2 tests',), ('Diagnostic:',))

    def test_nonzero_with_summary_rejected_without_diagnostic(self):
        s = 'Ran 2 tests in 0.001s\n\nOK'
        self.assertEqual(wf.parse_test_count(s, 1), 0)
        self._chk(f'{PY} -c "print({s!r}); assert 0, \'real failure\'"', inc=('(exit 1)', s, 'AssertionError: real failure'), exc=('Diagnostic:',))

    def test_zero_test_unittest_rejected_with_diagnostic(self):
        e = {'tests/test_e.py': 'import unittest\nclass E(unittest.TestCase): pass\n'}
        self._chk(f'{PY} -c "import unittest; unittest.main(module=None, argv=[\'unittest\', \'discover\', \'-s\', \'tests\'], exit=False)"', e, inc=('Ran 0 tests', 'no recognized executed-test summary found', 'truly no tests'))
        m, t = self._chk(f'{PY} -m unittest discover -s tests', e, None, ('Ran 0 tests',))
        self.assertEqual((m['all_passed'], t['passed'], t['exit_code'] != 0), (False, 0, True))
