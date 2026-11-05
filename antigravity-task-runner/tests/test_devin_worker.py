"""Offline fail-closed tests for the tool-free Devin worker adapter (no real CLI or model calls)."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/devin_worker.py'
spec = importlib.util.spec_from_file_location('devin_worker_under_test', SCRIPT)
dw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dw)

FAKE_EXE = '/opt/fake/devin'
SCOPE = ['calc.py', 'tests/test_calc.py']
GOOD_FILES = {'calc.py': 'def add(a, b):\n    return a + b\n', 'tests/test_calc.py': 'import unittest\n'}
def make_tool_def(name: str, **fn_over) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': f'{name} tool',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
            **fn_over,
        },
    }


LEAN_TOOL_DEFS = [make_tool_def(name) for name in dw.LEAN_REMAINING_TOOLS]
NATIVE_TOOL_DEFS_PATH = Path(__file__).resolve().parent / "fixtures" / "devin_tool_defs.json"


def make_response(files=None, handoff='Understanding: add. Change: calc.py. Tests: added. Residual risk: low.') -> str:
    return json.dumps({'handoff': handoff, 'files': GOOD_FILES if files is None else files})


def make_atif(response: str, **over) -> dict:
    atif = {
        'schema_version': 'ATIF-v1.7',
        'session_id': 'devin-session-1',
        'agent': {
            'name': 'devin',
            'model_name': 'SWE-1.6 Slow',
            'tool_definitions': [dict(item) for item in LEAN_TOOL_DEFS],
            'extra': {'backend': 'Windsurf', 'permission_mode': 'Normal'},
        },
        'steps': [
            {'step_id': 1, 'source': 'system', 'message': 'harness'},
            {'step_id': 2, 'source': 'user', 'message': 'prompt'},
            {'step_id': 3, 'source': 'agent', 'model_name': 'swe-1-6-slow', 'message': response, 'tool_calls': [],
             'extra': {'generation_model': 'swe-1-6-slow'}},
        ],
        'final_metrics': {'total_prompt_tokens': 21000, 'total_completion_tokens': 300, 'total_cached_tokens': 9000, 'total_steps': 3},
    }
    atif.update(over)
    return atif


def fake_cli(response=None, *, atif=None, export_text=None, stdout=None, stderr='', code=0, write_export=True, calls=None, mutate_config=None):
    response = make_response() if response is None else response

    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if mutate_config is not None:
            mutate_config(Path(argv[argv.index('--config') + 1]))
        export = Path(argv[argv.index('--export') + 1])
        if write_export:
            export.write_text(export_text if export_text is not None else json.dumps(atif or make_atif(response)), encoding='utf-8')
        return subprocess.CompletedProcess(argv, code, stdout=(response + '\n') if stdout is None else stdout, stderr=stderr)
    return run


class DevinAdapterTests(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop('WORKFLOW_CONTEXT', None)
        os.environ.pop('WORKFLOW_WORKER', None)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / 'ws'
        self.ws.mkdir()
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        self.out = root / 'out'

    def tearDown(self):
        self.tmp.cleanup()
        self._env_patch.stop()

    def attempt(self, run_side_effect, name='a1', scope=None):
        with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(dw.subprocess, 'run', side_effect=run_side_effect) as run:
            res = dw.run_devin_attempt(workspace=self.ws, attempt_dir=self.out / name, contract='TASK: implement add',
                                       scope_files=scope or SCOPE, verify_cmds=['python3 -B -m unittest'], timeout_seconds=60)
        return res, run

    def ws_state(self):
        return {p.relative_to(self.ws).as_posix(): p.read_bytes() for p in sorted(self.ws.rglob('*')) if p.is_file()}

    def test_worker_selector_rejects_unknown_before_calls(self):
        self.assertEqual(dw.resolve_worker(''), 'agy')
        self.assertEqual(dw.resolve_worker('devin'), 'devin')
        with patch.dict(os.environ, {'WORKFLOW_WORKER': ''}):
            self.assertEqual(dw.resolve_worker(), 'agy')
        for bad in ('Devin', ' devin', 'opencode', 'swe-1-6-slow'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dw.resolve_worker(bad)

    def test_config_has_deny_rules_and_exit2_hook_without_credentials(self):
        cfg = dw.build_devin_config()
        self.assertEqual(dw.validate_devin_config(cfg), [])
        self.assertEqual(cfg['hooks']['PreToolUse'], [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'exit 2'}]}])
        self.assertFalse(cfg['subagents_enabled'])
        self.assertTrue(all(v is False for v in cfg['read_config_from'].values()))
        self.assertTrue({'read', 'write', 'edit', 'exec', 'grep', 'apply_patch', 'run_subagent', 'webfetch', 'Read(/**)', 'Write(/**)', 'mcp__*'} <= set(cfg['permissions']['deny']))
        self.assertNotIn('allow', cfg['permissions'])
        for mutate in (lambda c: c.pop('hooks'), lambda c: c['hooks']['PreToolUse'][0].update(matcher='exec'),
                       lambda c: c['permissions']['deny'].remove('exec'), lambda c: c['permissions'].update(allow=['read']),
                       lambda c: c.update(subagents_enabled=True), lambda c: c['read_config_from'].update(claude=True),
                       lambda c: c.update({('api_' + 'key'): 'x'})):
            c = dw.build_devin_config(); mutate(c)
            self.assertTrue(dw.validate_devin_config(c))

    def test_success_applies_only_after_proof_with_isolated_cwd_and_safe_argv(self):
        calls = []
        res, _ = self.attempt(fake_cli(calls=calls))
        self.assertTrue(res['ok'], res['reason'])
        argv, kwargs = calls[0]
        self.assertEqual(argv[argv.index('--model') + 1], 'swe-1-6-slow')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'auto')
        self.assertFalse(any('danger' in a or 'accept-edits' in a or 'trust' in a for a in argv[:-1]))
        cwd = Path(kwargs['cwd'])
        self.assertNotEqual(cwd, self.ws)
        self.assertFalse(str(cwd).startswith(str(self.ws.resolve())))
        cfg = json.loads(Path(argv[argv.index('--config') + 1]).read_text())
        self.assertEqual(dw.validate_devin_config(cfg), [])
        self.assertEqual(self.ws_state()['calc.py'].decode(), GOOD_FILES['calc.py'])
        self.assertEqual((res['model'], res['permission_mode'], res['tool_calls'], res['effort']), ('swe-1-6-slow', 'Normal', 0, None))
        self.assertNotIn('status', res)
        self.assertEqual(res['usage']['total_prompt_tokens'], 21000)
        self.assertEqual(res['files_written'], SCOPE)

    def test_proof_failures_block_without_any_write(self):
        resp = make_response()
        agent = lambda **o: {'step_id': 3, 'source': 'agent', 'model_name': 'swe-1-6-slow', 'message': resp, 'tool_calls': [], 'extra': {'generation_model': 'swe-1-6-slow'}, **o}
        two_steps = lambda second: [{'step_id': 2, 'source': 'user', 'message': 'p'}, agent(message='thinking'), second]
        cases = {
            'malformed-export': dict(export_text='{not json'),
            'duplicate-key-export': dict(export_text='{"schema_version":"ATIF-v1.7","schema_version":"ATIF-v1.7"}'),
            'missing-export': dict(write_export=False),
            'wrong-schema': dict(atif=make_atif(resp, schema_version='ATIF-v2')),
            'missing-session': dict(atif=make_atif(resp, session_id='')),
            'wrong-agent-model-name': dict(atif=make_atif(resp, agent={'name': 'devin', 'model_name': 'Claude Opus', 'extra': {'permission_mode': 'Normal'}})),
            'wrong-permission-mode': dict(atif=make_atif(resp, agent={'name': 'devin', 'model_name': 'SWE-1.6 Slow', 'extra': {'permission_mode': 'Dangerous'}})),
            'wrong-model-later-step': dict(atif=make_atif(resp, steps=two_steps(agent(model_name='swe-2-high')))),
            'wrong-generation-model': dict(atif=make_atif(resp, steps=[agent(extra={'generation_model': 'opus-5-5'})])),
            'tool-call': dict(atif=make_atif(resp, steps=[agent(tool_calls=[{'function_name': 'read'}])])),
            'missing-tool-calls': dict(atif=make_atif(resp, steps=[{k: v for k, v in agent().items() if k != 'tool_calls'}])),
            'observation': dict(atif=make_atif(resp, steps=[agent(observation={'results': [{'content': 'denied'}]})])),
            'post-generation-error-step': dict(atif=make_atif(resp, steps=[agent(), {'source': 'system', 'message': 'Upgrade to Pro'}])),
            'missing-metrics': dict(atif=make_atif(resp, final_metrics=None)),
            'nonzero-exit': dict(code=1),
            'quota-stderr': dict(stderr='Error: quota exceeded, Upgrade to Pro'),
            'auth-stderr': dict(stderr='not authenticated: please log in'),
            'stdout-mismatch': dict(stdout=make_response(handoff='different') + '\n'),
            'empty-stdout': dict(stdout=''),
        }
        before = self.ws_state()
        for idx, (label, kwargs) in enumerate(cases.items()):
            with self.subTest(case=label):
                res, _ = self.attempt(fake_cli(**kwargs), name=f'p{idx}')
                self.assertTrue(res['blocked'])
                self.assertFalse(res['ok'])
                self.assertEqual(res['files_written'], [])
                self.assertEqual(self.ws_state(), before)

    def test_timeout_blocks_and_redacts(self):
        exc = subprocess.TimeoutExpired([FAKE_EXE], 1, output=b'', stderr=b'Authorization: Bearer unique-devin-secret')
        res, _ = self.attempt(exc)
        self.assertTrue(res['blocked'])
        self.assertIn('timed out', res['reason'])
        self.assertNotIn('unique-devin-secret', (self.out / 'a1/devin.stderr.txt').read_text())

    def test_invalid_generation_json_or_paths_never_partially_write(self):
        before = self.ws_state()
        bad = {
            'duplicate-keys': '{"handoff":"h","files":{"calc.py":"a","calc.py":"b"}}',
            'fenced-prose-before': 'Here is the JSON:\n```json\n' + make_response() + '\n```',
            'fenced-prose-after': '```json\n' + make_response() + '\n```\nTrailing explanation.',
            'two-fenced-blocks': '```json\n' + make_response() + '\n```\n```json\n' + make_response() + '\n```',
            'wrong-fence-lang': '```python\n' + make_response() + '\n```',
            'fenced-duplicate-keys': '```json\n{"handoff":"h","files":{"calc.py":"a","calc.py":"b"}}\n```',
            'extra-key': json.dumps({'handoff': 'h', 'files': GOOD_FILES, 'commands': ['rm -rf /']}),
            'unknown-path': make_response({'calc.py': 'x\n', 'other.py': 'y\n'}),
            'dot-alias': make_response({'./calc.py': 'x\n'}),
            'traversal': make_response({'../calc.py': 'x\n'}),
            'absolute': make_response({str(self.ws / 'calc.py'): 'x\n'}),
            'case-alias': make_response({'CALC.py': 'x\n'}),
            'non-string': make_response({'calc.py': 1}),
            'oversize': make_response({'calc.py': 'x' * (dw.MAX_FILE_BYTES + 1)}),
            'secret-output': make_response({'calc.py': 'ok = 1\n', 'tests/test_calc.py': 'KEY = "sk-abcdef1234567890abcdef"\n'}),
            'empty-files': make_response({}),
            'self-blocked': make_response(handoff='BLOCKED: unsafe'),
            'long-handoff': make_response(handoff='x' * (dw.MAX_HANDOFF_CHARS + 1)),
        }
        for idx, (label, response) in enumerate(bad.items()):
            with self.subTest(case=label):
                try:
                    res, _ = self.attempt(fake_cli(response), name=f'g{idx}')
                    self.assertTrue(res['blocked'])
                    self.assertEqual(self.ws_state(), before)
                finally:
                    for p in list(self.ws.rglob('*')):
                        if p.is_file() and p.relative_to(self.ws).as_posix() not in before:
                            p.unlink()
                    for rel, data in before.items():
                        (self.ws / rel).write_bytes(data)

    def test_unsafe_sources_and_scope_block_before_external_call(self):
        outside = Path(self.tmp.name) / 'outside.py'
        outside.write_text('x = 1\n')
        cases = []
        link = self.ws / 'link.py'; link.symlink_to(outside); cases.append(['link.py'])
        (self.ws / 'realdir').mkdir(); (self.ws / 'dirlink').symlink_to(self.ws / 'realdir'); cases.append(['dirlink/x.py'])
        (self.ws / 'adir').mkdir(); cases.append(['adir'])
        (self.ws / 'big.py').write_bytes(b'x' * (dw.MAX_FILE_BYTES + 1)); cases.append(['big.py'])
        (self.ws / 'cred.py').write_text('password = "unique-source-secret"\n'); cases.append(['cred.py'])
        (self.ws / 'bin.py').write_bytes(b'\xff\xfe'); cases.append(['bin.py'])
        cases += [['calc.py', './calc.py'], ['calc.py', 'CALC.py'], ['../x.py'], ['/etc/passwd'], ['a//b.py'], []]
        for idx, scope in enumerate(cases):
            with self.subTest(scope=scope):
                with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(dw.subprocess, 'run') as run:
                    res = dw.run_devin_attempt(workspace=self.ws, attempt_dir=self.out / f's{idx}', contract='TASK: x',
                                               scope_files=scope, verify_cmds=[], timeout_seconds=5)
                self.assertTrue(res['blocked'])
                run.assert_not_called()
        self.assertEqual(outside.read_text(), 'x = 1\n')

    def test_precondition_conflict_and_rollback_on_partial_failure(self):
        sources = dw.scan_sources(self.ws, SCOPE)
        pre = {s['path']: s['sha256'] for s in sources}
        data = {k: v.encode() for k, v in GOOD_FILES.items()}
        (self.ws / 'calc.py').write_text('changed concurrently\n')
        before = self.ws_state()
        with self.assertRaises(dw.DevinBlocked):
            dw.apply_files(self.ws, data, pre)
        self.assertEqual(self.ws_state(), before)
        self.assertFalse((self.ws / 'tests').exists())
        pre = {s['path']: s['sha256'] for s in dw.scan_sources(self.ws, SCOPE)}
        real_replace = os.replace
        count = {'n': 0}

        def flaky(src, dst):
            count['n'] += 1
            if count['n'] == 2:
                raise OSError('disk full')
            return real_replace(src, dst)
        with patch.object(dw.os, 'replace', side_effect=flaky), self.assertRaises(OSError):
            dw.apply_files(self.ws, data, pre)
        self.assertEqual(self.ws_state(), before)
        self.assertFalse((self.ws / 'tests').exists())
        self.assertEqual([p.name for p in self.ws.iterdir() if p.name.startswith('.devin-')], [])

    def test_prompt_bounded_untrusted_and_complete(self):
        sources = dw.scan_sources(self.ws, SCOPE)
        full_p = dw.build_prompt('TASK: full contract LATE_MARKER', SCOPE, sources, ['python3 -B -m unittest'],
                                 feedback='tests failed', open_questions=['Q1?'], context_mode='full')
        compact_p = dw.build_prompt('TASK: full contract LATE_MARKER', SCOPE, sources, ['python3 -B -m unittest'],
                                    feedback='tests failed', open_questions=['Q1?'], context_mode='compact')
        lean_p = dw.build_prompt('TASK: full contract LATE_MARKER', SCOPE, sources, ['python3 -B -m unittest'],
                                 feedback='tests failed', open_questions=['Q1?'], context_mode='lean')
        self.assertEqual(lean_p, compact_p)
        for prompt in (full_p, compact_p, lean_p):
            self.assertIn('LATE_MARKER', prompt)
            self.assertIn('UNTRUSTED DATA', prompt)
            self.assertIn('return 0', prompt)
            self.assertIn(sources[0]['sha256'], prompt)
            self.assertIn('tests failed', prompt)
            self.assertIn('Q1?', prompt)
        self.assertLess(len(compact_p), len(full_p))
        with self.assertRaises(dw.DevinBlocked):
            dw.build_prompt('X' * dw.MAX_PROMPT_CHARS, SCOPE, sources, [])
        self.assertLessEqual(len(dw.build_feedback(['d' * 5000])), dw.MAX_FEEDBACK_CHARS)

    def test_single_fenced_json_wrapper_and_context_mode_validation(self):
        self.assertEqual(dw.resolve_context_mode(''), 'lean')
        self.assertEqual(dw.resolve_context_mode('lean'), 'lean')
        self.assertEqual(dw.resolve_context_mode('compact'), 'compact')
        self.assertEqual(dw.resolve_context_mode('full'), 'full')
        for bad_mode in ('FULL', ' compact', 'summary', 'invalid'):
            with self.subTest(mode=bad_mode), self.assertRaises(dw.DevinBlocked):
                dw.resolve_context_mode(bad_mode)
            with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(dw.subprocess, 'run') as run:
                res = dw.run_devin_attempt(workspace=self.ws, attempt_dir=self.out / f'cm-{bad_mode.strip()}',
                                           contract='TASK: add', scope_files=SCOPE, verify_cmds=[], timeout_seconds=10,
                                           context_mode=bad_mode)
            self.assertTrue(res['blocked'])
            run.assert_not_called()
        for idx, wrapped in enumerate(('```json\n' + make_response() + '\n```', '```json\r\n' + make_response() + '\r\n```')):
            (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
            res, _ = self.attempt(fake_cli(wrapped), name=f'wrap-{idx}')
            self.assertTrue(res['ok'], res['reason'])
            self.assertEqual(res['context_mode'], 'lean')
            self.assertIsInstance(res['prompt_chars'], int)
            self.assertEqual(self.ws_state()['calc.py'].decode(), GOOD_FILES['calc.py'])

    def test_lean_config_and_validation_vs_legacy_modes(self):
        lean_cfg = dw.build_devin_config()
        self.assertEqual(lean_cfg, dw.build_devin_config('lean'))
        self.assertEqual(dw.validate_devin_config(lean_cfg, context_mode='lean'), [])
        self.assertEqual(lean_cfg['disabled_tools'], list(dw.LEAN_DISABLED_TOOLS))
        self.assertIn('web_search', lean_cfg['disabled_tools'])
        self.assertEqual(len(lean_cfg['disabled_tools']), 26)
        for legacy_mode in ('compact', 'full'):
            legacy_cfg = dw.build_devin_config(legacy_mode)
            self.assertNotIn('disabled_tools', legacy_cfg)
            self.assertEqual(dw.validate_devin_config(legacy_cfg, context_mode=legacy_mode), [])
            self.assertTrue(dw.validate_devin_config(legacy_cfg, context_mode='lean'))
            self.assertTrue(dw.validate_devin_config(lean_cfg, context_mode=legacy_mode))
        for mutate in (
            lambda c: c.pop('disabled_tools'),
            lambda c: c['disabled_tools'].remove('web_search'),
            lambda c: c['disabled_tools'].__setitem__(0, 'altered_tool'),
            lambda c: c['disabled_tools'].append('extra_tool'),
            lambda c: c['disabled_tools'].append('web_search'),
            lambda c: c.update(disabled_tools='not-a-list'),
            lambda c: c['permissions'].update(allow=['skill']),
        ):
            cfg = dw.build_devin_config('lean')
            mutate(cfg)
            self.assertTrue(dw.validate_devin_config(cfg, context_mode='lean'))

    def test_lean_atif_schema_guard_metrics_and_legacy_compatibility(self):
        resp = make_response()
        sys_msg = 'prefix <available_skills>' + ('s' * 120) + '</available_skills> suffix'
        lean_atif = make_atif(
            resp,
            steps=[
                {'step_id': 1, 'source': 'system', 'message': sys_msg},
                {'step_id': 2, 'source': 'user', 'message': 'prompt'},
                {'step_id': 3, 'source': 'agent', 'model_name': 'swe-1-6-slow', 'message': resp, 'tool_calls': [],
                 'extra': {'generation_model': 'swe-1-6-slow'}},
            ],
        )
        res, _ = self.attempt(fake_cli(atif=lean_atif), name='lean-ok')
        self.assertTrue(res['ok'], res['reason'])
        self.assertEqual(res['context_mode'], 'lean')
        self.assertEqual(res['tool_definition_count'], 5)
        self.assertEqual(res['tool_definition_names'], list(dw.LEAN_REMAINING_TOOLS))
        self.assertEqual(res['tool_schema_chars'], len(json.dumps(LEAN_TOOL_DEFS, ensure_ascii=False)))
        self.assertEqual(res['system_chars'], len(sys_msg))
        self.assertEqual(res['available_skills_chars'], len('<available_skills>' + ('s' * 120) + '</available_skills>'))
        self.assertEqual(set(res['usage']), set(dw.FINAL_METRIC_KEYS))

        # Subset of the 5 mandatory tools is accepted in lean mode
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        subset_atif = make_atif(resp, agent={**lean_atif['agent'], 'tool_definitions': [make_tool_def('skill'), make_tool_def('mcp_call_tool')]})
        sub_res, _ = self.attempt(fake_cli(atif=subset_atif), name='lean-subset')
        self.assertTrue(sub_res['ok'], sub_res['reason'])
        self.assertEqual(sub_res['tool_definition_count'], 2)

        # Real NATIVE-TOOL-DEFS.json passes both standalone validate_atif and run_devin_attempt on an ordinary ATIF fixture
        native_defs = json.loads(NATIVE_TOOL_DEFS_PATH.read_text(encoding='utf-8'))
        native_atif = make_atif(resp, agent={**lean_atif['agent'], 'tool_definitions': native_defs})
        nat_proof, nat_final = dw.validate_atif(json.dumps(native_atif), context_mode='lean')
        self.assertEqual(nat_final, resp)
        self.assertEqual(nat_proof['tool_definition_count'], 5)
        self.assertEqual(nat_proof['tool_definition_names'], list(dw.LEAN_REMAINING_TOOLS))
        self.assertEqual(nat_proof['tool_schema_chars'], len(json.dumps(native_defs, ensure_ascii=False)))
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        nat_res, _ = self.attempt(fake_cli(atif=native_atif), name='lean-native-defs')
        self.assertTrue(nat_res['ok'], nat_res['reason'])
        self.assertEqual(nat_res['tool_definition_count'], 5)
        self.assertEqual(nat_res['tool_definition_names'], list(dw.LEAN_REMAINING_TOOLS))
        self.assertEqual(nat_res['tool_schema_chars'], len(json.dumps(native_defs, ensure_ascii=False)))

        # Explicit mode propagation to disk config and legacy ATIF support when lean not requested
        legacy_agent = {'name': 'devin', 'model_name': 'SWE-1.6 Slow', 'extra': {'backend': 'Windsurf', 'permission_mode': 'Normal'}}
        legacy_atif = make_atif(resp, agent=legacy_agent)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WORKFLOW_CONTEXT', None)
            proof, final = dw.validate_atif(json.dumps(legacy_atif))
        self.assertEqual(final, resp)
        self.assertEqual(proof['tool_definition_count'], 0)
        for mode in ('compact', 'full'):
            (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
            calls = []
            with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(
                dw.subprocess, 'run', side_effect=fake_cli(atif=legacy_atif, calls=calls)
            ):
                m_res = dw.run_devin_attempt(
                    workspace=self.ws, attempt_dir=self.out / f'mode-{mode}', contract='TASK: implement add',
                    scope_files=SCOPE, verify_cmds=['python3 -B -m unittest'], timeout_seconds=60, context_mode=mode,
                )
            self.assertTrue(m_res['ok'], m_res['reason'])
            self.assertEqual(m_res['context_mode'], mode)
            argv, _ = calls[0]
            disk_cfg = json.loads(Path(argv[argv.index('--config') + 1]).read_text())
            self.assertNotIn('disabled_tools', disk_cfg)
            self.assertEqual(dw.validate_devin_config(disk_cfg, context_mode=mode), [])

        # Lean fail-closed cases: missing/null/empty/flat/malformed/duplicate/extra tool definitions block before file apply,
        # even when WORKFLOW_CONTEXT env is unset and steps lack a system step
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        (self.ws / 'tests/test_calc.py').unlink(missing_ok=True)
        before = self.ws_state()
        no_sys_steps = [
            {'step_id': 1, 'source': 'user', 'message': 'prompt'},
            {'step_id': 2, 'source': 'agent', 'model_name': 'swe-1-6-slow', 'message': resp, 'tool_calls': [],
             'extra': {'generation_model': 'swe-1-6-slow'}},
        ]
        bad_tool_defs = [
            legacy_agent,
            {**legacy_agent, 'tool_definitions': None},
            {**legacy_agent, 'tool_definitions': []},
            {**legacy_agent, 'tool_definitions': 'not-a-list'},
            {**legacy_agent, 'tool_definitions': [{'name': name} for name in dw.LEAN_REMAINING_TOOLS]},
            {**legacy_agent, 'tool_definitions': [{'type': 'function', 'name': 'skill', 'function': make_tool_def('skill')['function']}]},
            {**legacy_agent, 'tool_definitions': [{'type': 'tool', 'function': make_tool_def('skill')['function']}]},
            {**legacy_agent, 'tool_definitions': [{'type': 'function'}]},
            {**legacy_agent, 'tool_definitions': [{'type': 'function', 'function': 'not-an-object'}]},
            {**legacy_agent, 'tool_definitions': [{'type': 'function', 'function': {'parameters': {'type': 'object'}}}]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('  ')]},
            {**legacy_agent, 'tool_definitions': [make_tool_def(' skill ')]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', parameters=None)]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', parameters='not-an-object')]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', parameters={})]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', parameters={'type': 'array'})]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', parameters={'type': 'object', 'properties': 'bad'})]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill', description=123)]},
            {**legacy_agent, 'tool_definitions': [*LEAN_TOOL_DEFS, make_tool_def('exec')]},
            {**legacy_agent, 'tool_definitions': [*LEAN_TOOL_DEFS, make_tool_def('web_search')]},
            {**legacy_agent, 'tool_definitions': [make_tool_def('skill'), make_tool_def('skill')]},
            {**legacy_agent, 'tool_definitions': ['skill']},
            {**legacy_agent, 'tool_definitions': [{}]},
        ]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WORKFLOW_CONTEXT', None)
            for idx, bad_agent in enumerate(bad_tool_defs):
                with self.subTest(bad_idx=idx):
                    bad_atif = make_atif(resp, agent=bad_agent, steps=no_sys_steps)
                    with self.assertRaises(dw.DevinBlocked):
                        dw.validate_atif(json.dumps(bad_atif), context_mode='lean')
                    b_res, _ = self.attempt(fake_cli(atif=bad_atif), name=f'lean-bad-{idx}')
                    self.assertTrue(b_res['blocked'])
                    self.assertFalse(b_res['ok'])
                    self.assertEqual(b_res['files_written'], [])
                    self.assertEqual(self.ws_state(), before)
    def test_known_bracketed_paste_prefix_is_the_only_tolerated_stdout_difference(self):
        resp = make_response()
        raw = '\x1b[?2004l' + resp + '\n'
        res, _ = self.attempt(fake_cli(stdout=raw))
        self.assertTrue(res['ok'], res['reason'])
        self.assertEqual(res['stdout_normalization'], dw.STDOUT_NORMALIZATION)
        self.assertEqual(res['raw_stdout_sha256'], dw.sha256_bytes(raw.encode()))
        self.assertEqual((self.out / 'a1/devin.stdout.txt').read_text(), raw)  # raw stdout preserved
        plain, _ = self.attempt(fake_cli(), name='plain')
        self.assertTrue(plain['ok'])
        self.assertIsNone(plain['stdout_normalization'])
        before = self.ws_state()
        for idx, bad in enumerate(('\x1b[?2004h' + resp, '\x1b[0m' + resp, '\x1b[?2004l\x1b[?2004l' + resp, resp + '\x1b[?2004l',
                                   '\x1b[?2004l ' + resp, 'Here: ' + resp, resp + '\nDone', resp + '\n\n', ' ' + resp, '\x1b[?2004l')):
            with self.subTest(stdout=repr(bad)):
                res, _ = self.attempt(fake_cli(stdout=bad), name=f'n{idx}')
                self.assertTrue(res['blocked'])
                self.assertEqual(self.ws_state(), before)

    def test_staged_precondition_conflict_keeps_concurrent_bytes(self):
        pre = {s['path']: s['sha256'] for s in dw.scan_sources(self.ws, SCOPE)}
        original_calc = (self.ws / 'calc.py').read_bytes()
        real_mkstemp = dw.tempfile.mkstemp
        calls = {'n': 0}

        def staging_then_concurrent_write(*a, **kw):
            result = real_mkstemp(*a, **kw)
            calls['n'] += 1
            if calls['n'] == 2:  # both files staged; another writer creates the second target
                (self.ws / 'tests/test_calc.py').write_bytes(b'concurrent\n')
            return result
        with patch.object(dw.tempfile, 'mkstemp', side_effect=staging_then_concurrent_write), self.assertRaises(dw.DevinBlocked) as ctx:
            dw.apply_files(self.ws, {k: v.encode() for k, v in GOOD_FILES.items()}, pre)
        self.assertIn('after staging', str(ctx.exception))
        self.assertEqual((self.ws / 'tests/test_calc.py').read_bytes(), b'concurrent\n')
        self.assertEqual((self.ws / 'calc.py').read_bytes(), original_calc)
        self.assertEqual([p.name for p in self.ws.rglob('.devin-*')], [])

    def test_failed_restoration_reports_partial_mutation_and_preserves_recovery(self):
        original_calc = (self.ws / 'calc.py').read_bytes()
        real_replace = os.replace
        count = {'n': 0}

        def failing(src, dst):
            count['n'] += 1
            if count['n'] >= 2:  # second apply replace AND the rollback replace fail
                raise OSError('disk full')
            return real_replace(src, dst)
        with patch.object(dw.os, 'replace', side_effect=failing):
            res, _ = self.attempt(fake_cli())
        self.assertTrue(res['blocked'])
        self.assertTrue(res['partial_mutation'])
        self.assertIn('PARTIAL MUTATION', res['reason'])
        self.assertNotIn('restored and verified', res['reason'])
        rec = self.out / 'a1/recovery'
        self.assertEqual((rec / 'calc.py.orig').read_bytes(), original_calc)
        self.assertIn('calc.py', json.loads((rec / 'manifest.json').read_text()))
        self.assertEqual(self.ws_state()['calc.py'].decode(), GOOD_FILES['calc.py'])  # truthfully surfaced, not hidden

    def test_postrun_config_deny_mutation_blocks_without_workspace_writes(self):
        before = self.ws_state()
        mutated_raw: list[bytes] = []

        def drop_exec_deny(config_path: Path):
            cfg = json.loads(config_path.read_text(encoding='utf-8'))
            cfg['permissions']['deny'].remove('exec')
            raw = (json.dumps(cfg, indent=2) + '\n').encode('utf-8')
            mutated_raw.append(raw)
            config_path.write_bytes(raw)

        res, _ = self.attempt(fake_cli(mutate_config=drop_exec_deny), name='postrun-deny-mutated')
        self.assertTrue(res['blocked'])
        self.assertFalse(res['ok'])
        self.assertEqual(res['files_written'], [])
        self.assertEqual(self.ws_state(), before)
        self.assertFalse(res['protected_config_equal'])
        self.assertEqual(res['config_proof_status'], 'BLOCKED')
        self.assertEqual(res['config_proof']['status'], 'BLOCKED')
        self.assertFalse(res['config_proof']['protected_fields_equal'])
        attempt_dir = self.out / 'postrun-deny-mutated'
        self.assertEqual((attempt_dir / 'devin-session/devin-config.json').read_bytes(), mutated_raw[0])
        self.assertEqual((attempt_dir / 'devin-config.postrun.json').read_bytes(), mutated_raw[0])
        self.assertEqual(res['postrun_config_sha256'], dw.sha256_bytes(mutated_raw[0]))

    def test_positive_postrun_native_metadata_and_no_metadata_across_modes(self):
        synthetic_org_id = 'org_0123456789abcdef0123456789abcdef'
        synthetic_org_uuid = '01234567-89ab-cdef-0123-456789abcdef'
        self.assertEqual(len(synthetic_org_id), 36)
        self.assertEqual(len(synthetic_org_uuid), 36)

        independent_base = {
            'agent': {'model': 'swe-1-6-slow'},
            'subagents_enabled': False,
            'auto_update': False,
            'permission_mode': 'normal',
            'read_config_from': {
                'agents_standard': False, 'cursor': False, 'windsurf': False, 'claude': False,
                'copilot': False, 'opencode': False, 'zed': False, 'cloud': False,
            },
            'permissions': {
                'deny': [
                    'ask_user_question', 'browser_preview', 'close_browser_preview', 'edit', 'exec',
                    'find_file_by_name', 'get_output', 'grep', 'kill_shell', 'mcp_call_tool',
                    'mcp_list_servers', 'mcp_list_tools', 'mcp_read_resource', 'notebook_edit',
                    'notebook_read', 'read', 'read_subagent', 'request_scope', 'run_subagent',
                    'skill', 'todo_write', 'webfetch', 'write', 'apply_patch', 'write_to_process',
                    'glob', 'fetch', 'Read(/**)', 'Write(/**)', 'mcp__*',
                ],
            },
            'hooks': {'PreToolUse': [{'matcher': '', 'hooks': [{'type': 'command', 'command': 'exit 2'}]}]},
        }
        independent_lean_disabled = [
            'apply_patch', 'ask_user_question', 'browser_preview', 'close_browser_preview', 'edit', 'exec',
            'find_file_by_name', 'get_output', 'grep', 'kill_shell', 'mcp_call_tool', 'mcp_list_servers',
            'mcp_list_tools', 'mcp_read_resource', 'notebook_edit', 'notebook_read', 'read', 'read_subagent',
            'request_scope', 'run_subagent', 'skill', 'todo_write', 'web_search', 'webfetch', 'write',
            'write_to_process',
        ]
        native_metadata = {
            'version': 1,
            'devin': {'org_id': synthetic_org_id},
            'shell': {'setup_complete': True},
            'theme_mode': 'dark',
        }

        for mode in ('lean', 'compact', 'full'):
            expected_pre = json.loads(json.dumps(independent_base))
            if mode == 'lean':
                expected_pre['disabled_tools'] = list(independent_lean_disabled)
            expected_pre_bytes = (json.dumps(expected_pre, indent=2) + '\n').encode('utf-8')
            expected_pre_sha = dw.sha256_bytes(expected_pre_bytes)

            # 1) No post-run metadata added
            (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
            (self.ws / 'tests/test_calc.py').unlink(missing_ok=True)
            with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(
                dw.subprocess, 'run', side_effect=fake_cli()
            ):
                no_meta_dir = self.out / f'pos-nometa-{mode}'
                res_no_meta = dw.run_devin_attempt(
                    workspace=self.ws, attempt_dir=no_meta_dir, contract='TASK: implement add',
                    scope_files=SCOPE, verify_cmds=['python3 -B -m unittest'], timeout_seconds=60, context_mode=mode,
                )
            self.assertTrue(res_no_meta['ok'], res_no_meta['reason'])
            self.assertFalse(res_no_meta['blocked'])
            self.assertEqual(res_no_meta['files_written'], SCOPE)
            self.assertEqual(res_no_meta['prelaunch_config_sha256'], expected_pre_sha)
            self.assertEqual(res_no_meta['postrun_config_sha256'], expected_pre_sha)
            self.assertTrue(res_no_meta['protected_config_equal'])
            self.assertEqual(res_no_meta['postrun_metadata_keys'], [])
            self.assertEqual(res_no_meta['config_proof_status'], 'VERIFIED')
            self.assertEqual(res_no_meta['config_proof']['status'], 'VERIFIED')
            self.assertEqual(res_no_meta['config_proof']['metadata_status'], 'NONE')
            self.assertEqual(res_no_meta['config_proof']['metadata_keys'], [])
            self.assertTrue(res_no_meta['config_proof']['protected_fields_equal'])
            self.assertEqual((no_meta_dir / 'devin-config.prelaunch.json').read_bytes(), expected_pre_bytes)
            self.assertEqual((no_meta_dir / 'devin-config.postrun.json').read_bytes(), expected_pre_bytes)

            # 2) Full observed native post-run metadata persisted by CLI
            (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
            (self.ws / 'tests/test_calc.py').unlink(missing_ok=True)
            expected_post = {**expected_pre, **native_metadata}
            expected_post_bytes = (json.dumps(expected_post, indent=2) + '\n').encode('utf-8')
            expected_post_sha = dw.sha256_bytes(expected_post_bytes)
            self.assertEqual(
                dw.validate_postrun_devin_config(expected_post, context_mode=mode, prelaunch_config=expected_pre), []
            )

            def persist_native_metadata(config_path: Path, payload: bytes = expected_post_bytes):
                config_path.write_bytes(payload)

            with patch.object(dw, 'resolve_devin_executable', return_value=FAKE_EXE), patch.object(
                dw.subprocess, 'run', side_effect=fake_cli(mutate_config=persist_native_metadata)
            ):
                meta_dir = self.out / f'pos-meta-{mode}'
                res_meta = dw.run_devin_attempt(
                    workspace=self.ws, attempt_dir=meta_dir, contract='TASK: implement add',
                    scope_files=SCOPE, verify_cmds=['python3 -B -m unittest'], timeout_seconds=60, context_mode=mode,
                )
            self.assertTrue(res_meta['ok'], res_meta['reason'])
            self.assertFalse(res_meta['blocked'])
            self.assertEqual(res_meta['files_written'], SCOPE)
            self.assertEqual(self.ws_state()['calc.py'].decode('utf-8'), GOOD_FILES['calc.py'])
            self.assertEqual(res_meta['prelaunch_config_sha256'], expected_pre_sha)
            self.assertEqual(res_meta['postrun_config_sha256'], expected_post_sha)
            self.assertNotEqual(res_meta['prelaunch_config_sha256'], res_meta['postrun_config_sha256'])
            self.assertTrue(res_meta['protected_config_equal'])
            self.assertEqual(res_meta['postrun_metadata_keys'], ['devin', 'shell', 'theme_mode', 'version'])
            self.assertEqual(res_meta['config_proof_status'], 'VERIFIED')
            proof_cfg = res_meta['config_proof']
            self.assertEqual(proof_cfg['status'], 'VERIFIED')
            self.assertEqual(proof_cfg['metadata_status'], 'VERIFIED')
            self.assertEqual(proof_cfg['metadata_keys'], ['devin', 'shell', 'theme_mode', 'version'])
            self.assertTrue(proof_cfg['protected_fields_equal'])
            self.assertEqual(proof_cfg['prelaunch_sha256'], expected_pre_sha)
            self.assertEqual(proof_cfg['postrun_sha256'], expected_post_sha)
            self.assertEqual((meta_dir / 'devin-config.prelaunch.json').read_bytes(), expected_pre_bytes)
            self.assertEqual((meta_dir / 'devin-config.postrun.json').read_bytes(), expected_post_bytes)
            self.assertEqual((meta_dir / 'devin-session/devin-config.json').read_bytes(), expected_post_bytes)
            # Never leak org_id in reviewer-facing proof or result record
            self.assertNotIn(synthetic_org_id, json.dumps(res_meta))
            self.assertNotIn(synthetic_org_id, (meta_dir / 'devin-result.json').read_text(encoding='utf-8'))

            # Verify gate compatibility with new optional proof fields
            packet = {
                'schema_version': 1,
                'id': f'gate-compat-{mode}',
                'kind': 'task',
                'objective': 'TASK: implement add',
                'worker': {
                    'provider': 'devin',
                    'model': res_meta['model'],
                    'exit_code': res_meta['exit_code'],
                    'proof_status': res_meta['proof_status'],
                    'response': res_meta['response'],
                    'tool_denied': False,
                    'tool_calls': res_meta['tool_calls'],
                },
                'checks': [
                    {'kind': 'tests', 'command': 'python3 -B -m unittest', 'exit_code': 0, 'passed': 1, 'output': 'Ran 1 test\nOK'},
                    {'kind': 'scope', 'command': 'scope', 'exit_code': 0, 'output': 'ok'},
                    {'kind': 'whitespace', 'command': 'git diff --check', 'exit_code': 0, 'output': 'ok'},
                ],
                'evidence': {
                    'focused_diff': '--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-    return 0\n+    return a + b',
                    'verification_output': 'Ran 1 test\nOK',
                    'summary': res_meta['response'],
                    'full_worker_handoff': res_meta['response'],
                    'worker_proof': {
                        'provider': 'devin',
                        'actual_generation_model': res_meta['model'],
                        'agent_model_name': res_meta['agent_model_name'],
                        'permission_mode': res_meta['permission_mode'],
                        'schema_version': res_meta['schema_version'],
                        'exit_code': res_meta['exit_code'],
                        'tool_calls': res_meta['tool_calls'],
                        'tool_denied': False,
                        'stdout_matches_export': res_meta['stdout_matches_export'],
                        'stdout_normalization': res_meta['stdout_normalization'],
                        'session_id': res_meta['session_id'],
                        'config_proof': res_meta['config_proof'],
                    },
                },
                'open_questions': [],
            }
            with patch.dict(os.environ, {'AGY_MODEL': dw.gate.GEMINI_MODEL}):
                self.assertEqual(dw.gate.check_packet(packet), [])

            # 3) Partial metadata subsets and dashed UUID org_id also validate in post-run
            for subset in (
                {'version': 1},
                {'devin': {'org_id': synthetic_org_uuid}},
                {'shell': {'setup_complete': True}, 'theme_mode': 'dark'},
            ):
                self.assertEqual(
                    dw.validate_postrun_devin_config({**expected_pre, **subset}, context_mode=mode, prelaunch_config=expected_pre),
                    [],
                )

    def test_prelaunch_metadata_rejected_before_cli_invocation(self):
        synthetic_org_id = 'org_0123456789abcdef0123456789abcdef'
        metadata_variants = [
            {'version': 1},
            {'devin': {'org_id': synthetic_org_id}},
            {'shell': {'setup_complete': True}},
            {'theme_mode': 'dark'},
            {
                'version': 1,
                'devin': {'org_id': synthetic_org_id},
                'shell': {'setup_complete': True},
                'theme_mode': 'dark',
            },
        ]
        before = self.ws_state()
        for mode in ('lean', 'compact', 'full'):
            base_cfg = dw.build_devin_config(mode)
            for idx, extra_meta in enumerate(metadata_variants):
                pre_with_meta = {**base_cfg, **extra_meta}
                problems = dw.validate_devin_config(pre_with_meta, context_mode=mode)
                self.assertTrue(problems, f'expected prelaunch rejection for {mode} + {extra_meta}')
                self.assertIn('Devin config contains unexpected top-level keys', problems)
                with patch.object(dw, 'build_devin_config', return_value=pre_with_meta), patch.object(
                    dw, 'resolve_devin_executable', return_value=FAKE_EXE
                ), patch.object(dw.subprocess, 'run') as run_mock:
                    res = dw.run_devin_attempt(
                        workspace=self.ws, attempt_dir=self.out / f'pre-meta-{mode}-{idx}',
                        contract='TASK: implement add', scope_files=SCOPE,
                        verify_cmds=['python3 -B -m unittest'], timeout_seconds=30, context_mode=mode,
                    )
                self.assertTrue(res['blocked'])
                self.assertFalse(res['ok'])
                self.assertEqual(res['files_written'], [])
                self.assertEqual(self.ws_state(), before)
                run_mock.assert_not_called()

    def test_negative_postrun_metadata_protected_fields_and_config_file_mutations(self):
        synthetic_org_id = 'org_0123456789abcdef0123456789abcdef'
        valid_meta = {
            'version': 1,
            'devin': {'org_id': synthetic_org_id},
            'shell': {'setup_complete': True},
            'theme_mode': 'dark',
        }
        before = self.ws_state()
        expected_pre = dw.build_devin_config('lean')
        expected_pre_bytes = (json.dumps(expected_pre, indent=2) + '\n').encode('utf-8')

        dict_mutations: dict[str, object] = {
            # Malformed version (including bool-as-int)
            'version-true-bool-as-int': lambda c: c.update(version=True),
            'version-false-bool-as-int': lambda c: c.update(version=False),
            'version-zero': lambda c: c.update(version=0),
            'version-two': lambda c: c.update(version=2),
            'version-float': lambda c: c.update(version=1.0),
            'version-str': lambda c: c.update(version='1'),
            'version-none': lambda c: c.update(version=None),
            # Malformed devin / org_id
            'devin-empty-dict': lambda c: c.update(devin={}),
            'devin-none': lambda c: c.update(devin=None),
            'devin-list': lambda c: c.update(devin=[synthetic_org_id]),
            'devin-extra-nested-key': lambda c: c.update(devin={'org_id': synthetic_org_id, 'extra': 1}),
            'org-id-empty': lambda c: c.update(devin={'org_id': ''}),
            'org-id-len-35': lambda c: c.update(devin={'org_id': 'a' * 35}),
            'org-id-len-37': lambda c: c.update(devin={'org_id': 'a' * 37}),
            'org-id-newline': lambda c: c.update(devin={'org_id': ('a' * 35) + '\n'}),
            'org-id-space': lambda c: c.update(devin={'org_id': ('a' * 35) + ' '}),
            'org-id-dot': lambda c: c.update(devin={'org_id': ('a' * 35) + '.'}),
            'org-id-slash': lambda c: c.update(devin={'org_id': ('a' * 35) + '/'}),
            'org-id-non-ascii': lambda c: c.update(devin={'org_id': 'ä' + ('a' * 35)}),
            'org-id-int': lambda c: c.update(devin={'org_id': 123456}),
            'org-id-secret-pattern': lambda c: c.update(devin={'org_id': 'sk-' + ('a' * 33)}),
            # Malformed shell / setup_complete (including bool-as-int)
            'shell-empty-dict': lambda c: c.update(shell={}),
            'shell-none': lambda c: c.update(shell=None),
            'shell-bool': lambda c: c.update(shell=True),
            'shell-setup-false': lambda c: c.update(shell={'setup_complete': False}),
            'shell-setup-int-one': lambda c: c.update(shell={'setup_complete': 1}),
            'shell-setup-int-zero': lambda c: c.update(shell={'setup_complete': 0}),
            'shell-setup-str': lambda c: c.update(shell={'setup_complete': 'true'}),
            'shell-extra-nested-key': lambda c: c.update(shell={'setup_complete': True, 'extra': True}),
            # Malformed theme_mode
            'theme-light': lambda c: c.update(theme_mode='light'),
            'theme-uppercase': lambda c: c.update(theme_mode='DARK'),
            'theme-empty': lambda c: c.update(theme_mode=''),
            'theme-none': lambda c: c.update(theme_mode=None),
            'theme-bool': lambda c: c.update(theme_mode=True),
            'theme-int': lambda c: c.update(theme_mode=1),
            # Unknown top-level and credential/auth/endpoint keys
            'unknown-top-level': lambda c: c.update( valid_meta, extra_top='value'),
            'top-level-org-id': lambda c: c.update(org_id=synthetic_org_id),
            'forbidden-api-key': lambda c: c.update({('api_' + 'key'): 'x'}),
            'forbidden-auth-key': lambda c: c.update(auth_provider='cloud'),
            'forbidden-endpoint-key': lambda c: c.update(endpoint='https://api.example.com'),
            'forbidden-nested-token-key': lambda c: c.update(devin={'org_id': synthetic_org_id, 'token': 'x'}),
            # Protected-field mutations (even when valid post-run metadata is present)
            'protected-model-changed': lambda c: (c.update(valid_meta), c['agent'].update(model='claude-opus-4')),
            'protected-agent-extra': lambda c: (c.update(valid_meta), c['agent'].update(extra=1)),
            'protected-subagents-true': lambda c: (c.update(valid_meta), c.update(subagents_enabled=True)),
            'protected-subagents-zero-int': lambda c: (c.update(valid_meta), c.update(subagents_enabled=0)),
            'protected-auto-update-true': lambda c: (c.update(valid_meta), c.update(auto_update=True)),
            'protected-auto-update-zero-int': lambda c: (c.update(valid_meta), c.update(auto_update=0)),
            'protected-permission-mode': lambda c: (c.update(valid_meta), c.update(permission_mode='auto')),
            'protected-rcf-true': lambda c: (c.update(valid_meta), c['read_config_from'].update(claude=True)),
            'protected-rcf-zero-int': lambda c: (c.update(valid_meta), c['read_config_from'].update(claude=0)),
            'protected-rcf-removed-key': lambda c: (c.update(valid_meta), c['read_config_from'].pop('cloud')),
            'protected-deny-removed': lambda c: (c.update(valid_meta), c['permissions']['deny'].remove('exec')),
            'protected-deny-reordered': lambda c: (c.update(valid_meta), c['permissions'].update(deny=list(reversed(c['permissions']['deny'])))),
            'protected-deny-extra-item': lambda c: (c.update(valid_meta), c['permissions']['deny'].append('custom_tool')),
            'protected-permissions-allow': lambda c: (c.update(valid_meta), c['permissions'].update(allow=['read'])),
            'protected-hooks-removed': lambda c: (c.update(valid_meta), c.pop('hooks')),
            'protected-hook-matcher-changed': lambda c: (c.update(valid_meta), c['hooks']['PreToolUse'][0].update(matcher='exec')),
            'protected-hook-cmd-changed': lambda c: (c.update(valid_meta), c['hooks']['PreToolUse'][0]['hooks'][0].update(command='exit 0')),
            'protected-hook-extra-key': lambda c: (c.update(valid_meta), c['hooks']['PreToolUse'][0].update(extra=1)),
            'protected-disabled-removed': lambda c: (c.update(valid_meta), c.pop('disabled_tools')),
            'protected-disabled-item-removed': lambda c: (c.update(valid_meta), c['disabled_tools'].remove('web_search')),
            'protected-disabled-reordered': lambda c: (c.update(valid_meta), c.update(disabled_tools=list(reversed(c['disabled_tools'])))),
        }

        for idx, (label, mutate_fn) in enumerate(dict_mutations.items()):
            with self.subTest(mutation=label):
                sample = dw.build_devin_config('lean')
                mutate_fn(sample)
                self.assertTrue(
                    dw.validate_postrun_devin_config(sample, context_mode='lean', prelaunch_config=expected_pre),
                    f'expected postrun validator rejection for {label}',
                )
                saved_bytes = (json.dumps(sample, indent=2) + '\n').encode('utf-8')
                attempt_name = f'neg-dict-{idx}'
                res, _ = self.attempt(
                    fake_cli(mutate_config=lambda p, b=saved_bytes: p.write_bytes(b)),
                    name=attempt_name,
                )
                self.assertTrue(res['blocked'], f'expected BLOCKED for {label}')
                self.assertFalse(res['ok'])
                self.assertEqual(res['files_written'], [])
                self.assertEqual(self.ws_state(), before)
                self.assertEqual(res['config_proof_status'], 'BLOCKED')
                self.assertEqual(res['config_proof']['status'], 'BLOCKED')
                self.assertNotIn(synthetic_org_id, json.dumps(res))
                attempt_dir = self.out / attempt_name
                self.assertEqual((attempt_dir / 'devin-config.prelaunch.json').read_bytes(), expected_pre_bytes)
                self.assertEqual((attempt_dir / 'devin-session/devin-config.json').read_bytes(), saved_bytes)
                self.assertEqual((attempt_dir / 'devin-config.postrun.json').read_bytes(), saved_bytes)
                self.assertEqual(res['postrun_config_sha256'], dw.sha256_bytes(saved_bytes))

        # Raw config-file mutations on disk after launch (missing, symlink, directory, hardlink, oversize, unreadable, invalid UTF-8, malformed/duplicate-key/non-object JSON)
        outside_cfg = Path(self.tmp.name) / 'outside-config.json'
        outside_cfg.write_bytes(expected_pre_bytes)

        raw_file_cases: list[tuple[str, object, bytes | None]] = [
            ('missing', lambda p: p.unlink(), None),
            ('symlink', lambda p: (p.unlink(), p.symlink_to(outside_cfg)), None),
            ('directory', lambda p: (p.unlink(), p.mkdir()), None),
            ('hardlink', lambda p: os.link(p, p.parent / 'hardlink-config.json'), None),
            ('oversize', lambda p: p.write_bytes(b' ' * (dw.MAX_CONFIG_BYTES + 1)), None),
            ('unreadable', lambda p: p.chmod(0o000), None),
            ('invalid-utf8', lambda p: p.write_bytes(b'{\xff\xfe}'), b'{\xff\xfe}'),
            ('malformed-json', lambda p: p.write_bytes(b'{not valid json\n'), b'{not valid json\n'),
            ('duplicate-key-json', lambda p: p.write_bytes(b'{"version":1,"version":1}\n'), b'{"version":1,"version":1}\n'),
            ('non-object-json', lambda p: p.write_bytes(b'[1, 2, 3]\n'), b'[1, 2, 3]\n'),
        ]

        for idx, (label, file_mutate, expected_raw_post) in enumerate(raw_file_cases):
            with self.subTest(file_case=label):
                attempt_name = f'neg-file-{idx}'
                attempt_dir = self.out / attempt_name
                cfg_on_disk = attempt_dir / 'devin-session/devin-config.json'
                try:
                    res, _ = self.attempt(fake_cli(mutate_config=file_mutate), name=attempt_name)
                    self.assertTrue(res['blocked'], f'expected BLOCKED for {label}')
                    self.assertFalse(res['ok'])
                    self.assertEqual(res['files_written'], [])
                    self.assertEqual(self.ws_state(), before)
                    self.assertEqual(res['config_proof_status'], 'BLOCKED')
                    self.assertEqual(res['config_proof']['status'], 'BLOCKED')
                    self.assertEqual((attempt_dir / 'devin-config.prelaunch.json').read_bytes(), expected_pre_bytes)
                    if label == 'missing':
                        self.assertFalse(os.path.lexists(cfg_on_disk))
                    elif label == 'symlink':
                        self.assertTrue(os.path.islink(cfg_on_disk))
                    elif label == 'directory':
                        self.assertTrue(cfg_on_disk.is_dir())
                    elif label == 'oversize':
                        self.assertEqual(len(cfg_on_disk.read_bytes()), dw.MAX_CONFIG_BYTES + 1)
                    elif expected_raw_post is not None:
                        self.assertEqual(cfg_on_disk.read_bytes(), expected_raw_post)
                        self.assertEqual((attempt_dir / 'devin-config.postrun.json').read_bytes(), expected_raw_post)
                        self.assertEqual(res['postrun_config_sha256'], dw.sha256_bytes(expected_raw_post))
                finally:
                    if os.path.lexists(cfg_on_disk) and not os.path.islink(cfg_on_disk) and cfg_on_disk.is_file():
                        try:
                            cfg_on_disk.chmod(0o600)
                        except OSError:
                            pass

    def test_postrun_prelaunch_config_evidence_tampering_blocks_without_workspace_writes(self):
        before = self.ws_state()
        synthetic_org_id = 'org_0123456789abcdef0123456789abcdef'
        expected_pre = dw.build_devin_config('lean')
        expected_pre_bytes = (json.dumps(expected_pre, indent=2) + '\n').encode('utf-8')
        valid_post = {
            **expected_pre,
            'version': 1,
            'devin': {'org_id': synthetic_org_id},
            'shell': {'setup_complete': True},
            'theme_mode': 'dark',
        }
        valid_post_bytes = (json.dumps(valid_post, indent=2) + '\n').encode('utf-8')
        outside_pre = Path(self.tmp.name) / 'outside-prelaunch.json'
        outside_pre.write_bytes(expected_pre_bytes)
        tampered_pre = json.loads(expected_pre_bytes.decode('utf-8'))
        tampered_pre['permissions']['deny'].remove('exec')
        tampered_pre_bytes = (json.dumps(tampered_pre, indent=2) + '\n').encode('utf-8')

        evidence_cases: list[tuple[str, object]] = [
            ('modified', lambda pre: (pre.chmod(0o600), pre.write_bytes(tampered_pre_bytes))),
            ('removed', lambda pre: pre.unlink()),
            ('replaced-symlink', lambda pre: (pre.unlink(), pre.symlink_to(outside_pre))),
            ('replaced-hardlink', lambda pre: os.link(pre, pre.parent / 'prelaunch-alias.json')),
            ('replaced-directory', lambda pre: (pre.unlink(), pre.mkdir())),
        ]

        for idx, (label, tamper_fn) in enumerate(evidence_cases):
            with self.subTest(evidence_case=label):
                attempt_name = f'neg-prelaunch-evidence-{idx}'
                attempt_dir = self.out / attempt_name

                def mutate_both(config_path: Path, fn=tamper_fn):
                    config_path.write_bytes(valid_post_bytes)
                    fn(config_path.parent.parent / dw.PRELAUNCH_CONFIG_FILENAME)

                res, _ = self.attempt(fake_cli(mutate_config=mutate_both), name=attempt_name)
                self.assertTrue(res['blocked'], f'expected BLOCKED for prelaunch evidence {label}')
                self.assertFalse(res['ok'])
                self.assertIn('Devin prelaunch config evidence', res['reason'])
                self.assertEqual(res['files_written'], [])
                self.assertEqual(self.ws_state(), before)
                self.assertFalse(res['protected_config_equal'])
                self.assertEqual(res['config_proof_status'], 'BLOCKED')
                self.assertEqual(res['config_proof']['status'], 'BLOCKED')
                self.assertFalse(res['config_proof']['protected_fields_equal'])
                self.assertEqual((attempt_dir / dw.POSTRUN_CONFIG_FILENAME).read_bytes(), valid_post_bytes)
                self.assertEqual(res['postrun_config_sha256'], dw.sha256_bytes(valid_post_bytes))


if __name__ == '__main__':
    unittest.main()
