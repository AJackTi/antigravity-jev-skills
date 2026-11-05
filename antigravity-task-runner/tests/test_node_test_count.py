from __future__ import annotations
from pathlib import Path
import sys, tempfile, unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_run_workflow import wf

S26 = (
    'ℹ tests 26\nℹ suites 0\nℹ pass 26\nℹ fail 0\n'
    'ℹ cancelled 0\nℹ skipped 0\nℹ todo 0\nℹ duration_ms 231.693458'
)


def spec(t: object, p: object, f: object = 0, extra: str = '') -> str:
    return f'ℹ tests {t}\nℹ pass {p}\nℹ fail {f}\n{extra}'


POS = (
    (S26, 26),
    ('✔ suite 1 (1.2ms)\n✔ suite 2 (0.8ms)\n' + S26 + '\n', 26),
    ('TAP version 13\n# Subtest: m\nok 1 - m\n1..1\n# tests 4\n# suites 1\n# pass 4\n# fail 0\n# cancelled 0\n# skipped 0\n# todo 0\n# duration_ms 18.42\n', 4),
    (spec(10, 7, 0, 'ℹ suites 2\nℹ cancelled 0\nℹ skipped 2\nℹ todo 1\nℹ duration_ms 12.5'), 7),
    ('TAP version 13\nok 1 - a\nok 2 - b # SKIP\nnot ok 3 - c # TODO\n1..3\n# tests 3\n# pass 1\n# fail 0\n# cancelled 0\n# skipped 1\n# todo 1\n', 1),
    ('\x1b[32m✔\x1b[39m c1\n' + S26.replace('ℹ', '\x1b[34mℹ\x1b[39m') + '\x1b[0m\n', 26),
    ('boot\n✔ parses "Ran 99 tests" and "10 passed" (1ms)\nℹ note\n' + S26, 26),
    ('Ran 12 tests in 0.03s\n\nOK\n', 12),
    ('=== 8 passed in 0.12s ===\n', 8),
    ('2 skipped, 5 passed in 0.10s\n', 5),
    ('--- PASS: TestA (0.00s)\n--- PASS: TestB (0.00s)\nPASS\n', 2),
    ('OK\n', 1),
)

NEG = (
    spec(2, 0, 0, 'ℹ skipped 2\n'), spec(5, 4, 1, 'ℹ cancelled 0\n'), spec(5, 4, 0, 'ℹ cancelled 1\n'),
    '# tests 3\n# pass 2\n# fail 1\n# cancelled 0\n', '✖ broken (1.0ms)\n' + S26,
    'TAP version 13\nnot ok 1 - broken\n# tests 1\n# pass 1\n# fail 0\n',
    'ℹ suites 0\nℹ pass 3\nℹ fail 0\n', 'ℹ tests 3\nℹ suites 0\nℹ fail 0\n', 'ℹ tests 3\nℹ suites 0\nℹ pass 3\n',
    '✔ test 1 (0.5ms)\n✔ test 2 (0.4ms)\n', 'TAP version 13\nok 1 - only point\n1..1\n',
    spec(0, 0), spec(-2, 2), spec(2, -1), spec(2, 2, 0, 'ℹ skipped -1\n'),
    spec('two', 2), spec(2, '2.0'), spec(2, 2, 0, 'ℹ duration_ms -1.0\n'), spec(2, 2, 0, 'ℹ duration_ms fast\n'),
    spec(5, 3, 0, 'ℹ skipped 1\nℹ todo 0\n'), spec(3, 4),
    'ℹ tests: 2\nℹ pass: 2\nℹ fail: 0\n', 'ℹ tests 2 ℹ pass 2 ℹ fail 0\n',
    S26 + '\nℹ pass 26', S26 + '\n\n' + S26, 'ℹ tests 2\n# pass 2\nℹ fail 0\n',
    '✔ case 1 (0.3ms)\n# tests 1\n# pass 1\n# fail 0\n', 'TAP version 13\nok 1 - case 1\n' + S26,
    '✖ case 1\n' + spec(1, 0, 1, 'OK\n'), '✖ case 1\nOK\n',
    'not ok 1 - c\nRan 4 tests in 0.01s\n\nOK\n', 'not ok 1 - c\n4 passed in 0.02s\n',
    'status is OK for startup', 'INFO: 5 passed validation rules during boot',
    '✔ test name saying 3 passed and OK without summary', 'ℹ informational line without counters',
    'Ran 12 tests in 0.03s\n\nFAILED (failures=1)\n', '1 failed, 5 passed in 0.10s\n',
)


class NodeTestCountTests(unittest.TestCase):
    def test_positive_and_legacy_counts(self) -> None:
        for out, exp in POS:
            with self.subTest(out=out):
                self.assertEqual(wf.parse_test_count(out, 0), exp)

    def test_rejected_counts(self) -> None:
        for code in (1, -1, 2, True, False, None):  # type: ignore[list-item]
            with self.subTest(code=code):
                self.assertEqual(wf.parse_test_count(S26, code), 0)  # type: ignore[arg-type]
        for out in NEG:
            with self.subTest(out=out):
                self.assertEqual(wf.parse_test_count(out, 0), 0)

    def test_mechanical_checks_node_fixture_and_independent_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / 'm.js').write_text('export const v = 1;\n', encoding='utf-8')
            base = wf.snapshot_workspace(ws)
            (ws / 'm.js').write_text('export const v = 2;\n', encoding='utf-8')
            cmd = f'python3 -c "print({S26!r})"'
            ok = wf.run_mechanical_checks(ws, base, ['m.js'], [cmd])
            self.assertTrue(ok['all_passed'])
            self.assertEqual(ok['checks'][0]['passed'], 26)
            (ws / 'r.js').write_text('export const bad = 1;\n', encoding='utf-8')
            sc = wf.run_mechanical_checks(ws, base, ['m.js'], [cmd])
            self.assertFalse(sc['all_passed'])
            self.assertEqual(sc['checks'][0]['passed'], 26)
            self.assertNotEqual(sc['checks'][1]['exit_code'], 0)
            (ws / 'r.js').unlink()
            (ws / 'm.js').write_text('export const v = 2;   \n', encoding='utf-8')
            ws_res = wf.run_mechanical_checks(ws, base, ['m.js'], [cmd])
            self.assertFalse(ws_res['all_passed'])
            self.assertEqual(ws_res['checks'][0]['passed'], 26)
            self.assertNotEqual(ws_res['checks'][2]['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
