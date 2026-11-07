import inspect
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import free_api_worker as f


def _get_coordinator_helper():
    if hasattr(f, 'run_free_api_with_fallback'):
        return f.run_free_api_with_fallback
    for val in vars(f).values():
        if callable(val) and not isinstance(val, type) and val is not getattr(f, 'run_free_api_attempt', None):
            params = inspect.signature(val).parameters
            if 'openrouter_model' in params and 'ledger' in params:
                return val
    raise AssertionError('Coordinator entry helper not found')


def _clean_avail_record(**overrides):
    rec = {
        'ok': False,
        'blocked': True,
        'tool_denied': False,
        'partial_mutation': False,
        'files_written': [],
        'error_category': 'availability',
        'reason': 'rate limited',
    }
    rec.update(overrides)
    return rec


def _verified_success_record(**overrides):
    rec = {
        'ok': True,
        'blocked': False,
        'tool_denied': False,
        'partial_mutation': False,
        'files_written': ['a.txt'],
        'proof_status': 'VERIFIED',
        'error_category': None,
        'reason': None,
    }
    rec.update(overrides)
    return rec


class FreeApiFallbackSmallTests(unittest.TestCase):
    def test_predicate_clean_and_malformed(self):
        base = _clean_avail_record()
        self.assertTrue(f.is_availability_fallback_eligible(base))
        for non_dict in (None, [], 'str', 123):
            with self.subTest(non_dict=non_dict):
                self.assertFalse(f.is_availability_fallback_eligible(non_dict))
        for key in list(base.keys()):
            with self.subTest(missing=key):
                cand = dict(base)
                cand.pop(key)
                self.assertFalse(f.is_availability_fallback_eligible(cand))
        bad_cases = [
            ('ok', True), ('ok', 0), ('ok', None),
            ('blocked', False), ('blocked', 1), ('blocked', None),
            ('tool_denied', True), ('tool_denied', 0), ('tool_denied', None),
            ('partial_mutation', True), ('partial_mutation', 0), ('partial_mutation', None),
            ('files_written', ['a.txt']), ('files_written', ()), ('files_written', None), ('files_written', ''),
            ('error_category', 'terminal_proof'), ('error_category', 'scope'), ('error_category', None),
        ]
        for key, val in bad_cases:
            with self.subTest(key=key, val=val):
                self.assertFalse(f.is_availability_fallback_eligible(_clean_avail_record(**{key: val})))

    def test_terminal_proof_blocks_switch_and_reused_helper(self):
        helper = _get_coordinator_helper()
        with tempfile.TemporaryDirectory() as tmp:
            ws = pathlib.Path(tmp)
            (ws / 'a.txt').write_text('v1\n', encoding='utf-8')
            ledger = f.CoordinatorRetryLedger(ws, ['a.txt'])
            term_rec = _clean_avail_record(error_category='terminal_proof', reason='unit test failed')
            ledger.record_attempt('zai', term_rec)
            self.assertIs(ledger.attempts[-1]['terminal'], True)
            ok_switch, reason = ledger.can_switch_provider(_clean_avail_record(), 'openrouter')
            self.assertFalse(ok_switch)
            self.assertTrue(reason)
            with mock.patch.object(f, 'run_free_api_attempt') as m_run:
                res = helper(
                    workspace=ws,
                    output_dir=ws / 'out',
                    contract={},
                    scope_files=['a.txt'],
                    verify_cmds=[],
                    openrouter_model='openrouter/free',
                    ledger=ledger,
                )
                m_run.assert_not_called()
            self.assertIs(res['ok'], False)
            self.assertIs(res['blocked'], True)
            self.assertIsNone(res['selected_provider'])
            self.assertIsNone(res['worker_result'])
            self.assertIs(res['fallback_used'], False)
            self.assertEqual(res['ledger']['executions_used'], 1)
            self.assertTrue(res['reason'])

    def test_record_flag_clean_legacy_exhaustion_and_baseline(self):
        helper = _get_coordinator_helper()
        with tempfile.TemporaryDirectory() as tmp:
            ws = pathlib.Path(tmp)
            target = ws / 'a.txt'
            target.write_text('baseline\n', encoding='utf-8')
            ledger = f.CoordinatorRetryLedger(ws, ['a.txt'])
            ledger.record_attempt('zai', _clean_avail_record())
            self.assertIs(ledger.attempts[-1]['terminal'], False)
            self.assertEqual(ledger.can_switch_provider(_clean_avail_record(), 'openrouter'), (True, None))
            ledger.record_attempt('openrouter', _verified_success_record())
            self.assertIs(ledger.attempts[-1]['terminal'], False)

            for bad_succ in (
                _verified_success_record(proof_status='FAILED'),
                _verified_success_record(ok=1),
                _verified_success_record(blocked=0),
                _verified_success_record(files_written=None),
            ):
                l2 = f.CoordinatorRetryLedger(ws, ['a.txt'])
                l2.record_attempt('zai', bad_succ)
                self.assertIs(l2.attempts[-1]['terminal'], True)

            legacy_ledger = f.CoordinatorRetryLedger(ws, ['a.txt'])
            legacy_ledger.attempts.append({'attempt_number': 1, 'provider': 'zai', 'ok': False, 'error_category': 'availability'})
            self.assertFalse(legacy_ledger.can_switch_provider(_clean_avail_record(), 'openrouter')[0])
            with mock.patch.object(f, 'run_free_api_attempt') as m_legacy:
                res_legacy = helper(
                    workspace=ws,
                    output_dir=ws / 'out',
                    contract={},
                    scope_files=['a.txt'],
                    verify_cmds=[],
                    openrouter_model='openrouter/free',
                    ledger=legacy_ledger,
                )
                m_legacy.assert_not_called()
            self.assertIs(res_legacy['blocked'], True)

            target.write_text('mutated\n', encoding='utf-8')
            ledger.restore_baseline()
            self.assertEqual(target.read_text(encoding='utf-8'), 'baseline\n')

            ledger.record_attempt('openrouter', _clean_avail_record())
            self.assertEqual(ledger.remaining_executions, 0)
            self.assertFalse(ledger.can_switch_provider(_clean_avail_record(), 'openrouter')[0])
            with self.assertRaises(f.FreeApiBlocked):
                ledger.record_attempt('openrouter', _clean_avail_record())


if __name__ == '__main__':
    unittest.main()
