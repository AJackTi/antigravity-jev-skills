"""Focused offline tests for the bounded OpenCode external-worker adapter and workflow integration."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / 'scripts'

_opc_spec = importlib.util.spec_from_file_location('opencode_worker_under_test', SCRIPTS_DIR / 'opencode_worker.py')
opc = importlib.util.module_from_spec(_opc_spec)
_opc_spec.loader.exec_module(opc)

_wf_spec = importlib.util.spec_from_file_location('run_workflow_for_opencode_test', SCRIPTS_DIR / 'run_workflow.py')
wf = importlib.util.module_from_spec(_wf_spec)
_wf_spec.loader.exec_module(wf)

_cr_spec = importlib.util.spec_from_file_location('chatgpt_review_for_opencode_test', SCRIPTS_DIR / 'chatgpt_review.py')
cr = importlib.util.module_from_spec(_cr_spec)
_cr_spec.loader.exec_module(cr)

FAKE_OPENCODE = '/opt/fake/opencode'
FAKE_AGY = '/opt/fake/agy'
FAKE_JEV = '/opt/fake/jev-pool'
FAKE_CODEX = '/opt/fake/codex'
TEST_MODEL = 'opencode/glm-5-free'
SCOPE = ['calc.py', 'tests/test_calc.py']
GOOD_FILES = {
    'calc.py': 'def add(a, b):\n    return a + b\n',
    'tests/test_calc.py': (
        'import unittest\nfrom calc import add\n\n'
        'class CalcTests(unittest.TestCase):\n'
        '    def test_add(self):\n'
        '        self.assertEqual(add(2, 3), 5)\n'
    ),
}


def make_response(
    files: dict[str, str] | None = None,
    handoff: str = 'Understanding: implement add. Plan: 1. update calc.py 2. add test. Files: calc.py, tests/test_calc.py. Change: implemented add. Verification: unittest. Residual risk: bounded scope only.',
) -> str:
    return json.dumps({'handoff': handoff, 'files': GOOD_FILES if files is None else files})


def make_tokens(
    *,
    input_tok: int = 1200,
    output_tok: int = 180,
    reasoning_tok: int = 40,
    cache_read: int = 300,
    cache_write: int = 20,
    total_override: object = None,
    include_total: bool = True,
) -> dict[str, object]:
    tok: dict[str, object] = {
        'input': input_tok,
        'output': output_tok,
        'reasoning': reasoning_tok,
        'cache': {'read': cache_read, 'write': cache_write},
    }
    if include_total:
        tok['total'] = (
            (input_tok + output_tok + reasoning_tok + cache_read + cache_write)
            if total_override is None
            else total_override
        )
    return tok


def make_run_stdout(
    response_text: str,
    *,
    session_id: str = 'ses_opencode_1',
    reason: str = 'stop',
    tokens: dict[str, object] | None = None,
    extra_event_fields: dict[str, object] | None = None,
    extra_part_fields: dict[str, object] | None = None,
) -> str:
    tok = make_tokens() if tokens is None else tokens
    ev_extra = extra_event_fields or {}
    part_extra = extra_part_fields or {}
    events = [
        {
            'type': 'step_start',
            'timestamp': 1700000000001,
            'sessionID': session_id,
            **ev_extra,
            'part': {
                'id': 'prt_1',
                'sessionID': session_id,
                'messageID': 'msg_assistant_1',
                'type': 'step-start',
                **part_extra,
            },
        },
        {
            'type': 'text',
            'timestamp': 1700000000002,
            'sessionID': session_id,
            **ev_extra,
            'part': {
                'id': 'prt_2',
                'sessionID': session_id,
                'messageID': 'msg_assistant_1',
                'type': 'text',
                'text': response_text,
                **part_extra,
            },
        },
        {
            'type': 'step_finish',
            'timestamp': 1700000000003,
            'sessionID': session_id,
            **ev_extra,
            'part': {
                'id': 'prt_3',
                'sessionID': session_id,
                'messageID': 'msg_assistant_1',
                'type': 'step-finish',
                'reason': reason,
                'tokens': tok,
                **part_extra,
            },
        },
    ]
    return '\n'.join(json.dumps(e) for e in events) + '\n'


def make_export_json(
    response_text: str,
    *,
    session_id: str = 'ses_opencode_1',
    provider_id: str = 'opencode',
    model_id: str = 'glm-5-free',
    title: str = 'opencode-worker-attempt-1',
    finish: str = 'stop',
    tokens: dict[str, object] | None = None,
    info_overrides: dict[str, object] | None = None,
    assistant_info_overrides: dict[str, object] | None = None,
    assistant_parts_overrides: list[dict[str, object]] | None = None,
) -> str:
    tok = make_tokens() if tokens is None else tokens
    s_info: dict[str, object] = {
        'id': session_id,
        'title': title,
        'time': {'created': 1700000000000, 'updated': 1700000000010},
    }
    if info_overrides:
        s_info.update(info_overrides)
    a_info: dict[str, object] = {
        'id': 'msg_assistant_1',
        'sessionID': session_id,
        'role': 'assistant',
        'providerID': provider_id,
        'modelID': model_id,
        'finish': finish,
        'time': {'created': 1700000000001, 'completed': 1700000000009},
        'tokens': tok,
    }
    if assistant_info_overrides:
        a_info.update(assistant_info_overrides)
    a_parts = (
        [
            {'id': 'prt_1', 'sessionID': session_id, 'messageID': 'msg_assistant_1', 'type': 'step-start'},
            {'id': 'prt_2', 'sessionID': session_id, 'messageID': 'msg_assistant_1', 'type': 'text', 'text': response_text},
            {
                'id': 'prt_3',
                'sessionID': session_id,
                'messageID': 'msg_assistant_1',
                'type': 'step-finish',
                'reason': finish,
                'tokens': tok,
            },
        ]
        if assistant_parts_overrides is None
        else assistant_parts_overrides
    )
    export_obj = {
        'info': s_info,
        'messages': [
            {
                'info': {'id': 'msg_user_1', 'sessionID': session_id, 'role': 'user'},
                'parts': [{'id': 'prt_u1', 'sessionID': session_id, 'messageID': 'msg_user_1', 'type': 'text', 'text': 'prompt'}],
            },
            {
                'info': a_info,
                'parts': a_parts,
            },
        ],
    }
    return json.dumps(export_obj)


def fake_opencode_cli(
    response: str | None = None,
    *,
    run_stdout: str | None = None,
    run_stderr: str = '',
    run_code: int = 0,
    export_stdout: str | None = None,
    export_stderr: str = '',
    export_code: int = 0,
    native_log_text: str | None = None,
    mutate_config=None,
    calls: list | None = None,
):
    resp = make_response() if response is None else response
    eff_run_stdout = make_run_stdout(resp) if run_stdout is None else run_stdout
    eff_export_stdout = make_export_json(resp) if export_stdout is None else export_stdout

    def _run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), kwargs))
        env = kwargs.get('env') or {}
        if 'run' in argv:
            if mutate_config is not None:
                mutate_config(Path(env['OPENCODE_CONFIG']))
            if native_log_text is not None:
                log_dir = Path(env['XDG_DATA_HOME']) / 'opencode/log'
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / 'session.log').write_text(native_log_text, encoding='utf-8')
            return subprocess.CompletedProcess(argv, run_code, stdout=eff_run_stdout, stderr=run_stderr)
        if 'export' in argv:
            return subprocess.CompletedProcess(argv, export_code, stdout=eff_export_stdout, stderr=export_stderr)
        raise AssertionError(f'Unexpected subprocess call: {argv}')

    return _run


class OpenCodeWorkerAdapterTests(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for k in list(os.environ):
            if k.startswith('OPENCODE_') or k in ('WORKFLOW_WORKER', 'WORKFLOW_CONTEXT', 'AGY_MODEL'):
                os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / 'ws'
        self.ws.mkdir()
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        self.out = root / 'out'

    def tearDown(self):
        self.tmp.cleanup()
        self._env_patch.stop()

    def ws_state(self) -> dict[str, bytes]:
        return {p.relative_to(self.ws).as_posix(): p.read_bytes() for p in sorted(self.ws.rglob('*')) if p.is_file()}

    def run_attempt(self, side_effect, *, name: str = 'a1', model: object = TEST_MODEL, scope=None, **extra):
        with patch.object(opc, 'resolve_opencode_executable', return_value=FAKE_OPENCODE), patch.object(
            opc.subprocess, 'run', side_effect=side_effect
        ) as run_mock:
            res = opc.run_opencode_attempt(
                workspace=self.ws,
                attempt_dir=self.out / name,
                contract='TASK: implement add(a, b)',
                scope_files=SCOPE if scope is None else scope,
                verify_cmds=['python3 -B -m unittest discover -s tests'],
                timeout_seconds=60,
                model=model,
                **extra,
            )
        return res, run_mock

    def test_reuses_devin_helpers_without_copying(self):
        self.assertIs(opc.scan_sources, opc.devin.scan_sources)
        self.assertIs(opc.build_prompt, opc.devin.build_prompt)
        self.assertIs(opc.parse_generation, opc.devin.parse_generation)
        self.assertIs(opc.apply_files, opc.devin.apply_files)
        self.assertEqual(opc.USAGE_BASIS, opc.OPENCODE_USAGE_BASIS)

    def test_worker_selector_and_explicit_model_and_bin_resolution(self):
        self.assertEqual(opc.resolve_worker(None), 'agy')
        self.assertEqual(opc.resolve_worker(''), 'agy')
        self.assertEqual(opc.resolve_worker('devin'), 'devin')
        self.assertEqual(opc.resolve_worker('opencode'), 'opencode')
        for bad_worker in ('OpenCode', ' opencode', 'pi', 'codex'):
            with self.subTest(bad_worker=bad_worker), self.assertRaises(ValueError):
                opc.resolve_worker(bad_worker)

        self.assertEqual(
            opc.resolve_opencode_model('opencode/glm-5-free'),
            ('opencode/glm-5-free', 'opencode', 'glm-5-free'),
        )
        for bad_model in (None, '', 'glm-5-free', 'opencode/', '/glm-5-free', 'opencode//glm', 'opencode/..', ' opencode/glm', 'opencode/glm:'):
            with self.subTest(bad_model=bad_model), self.assertRaises(opc.OpenCodeBlocked):
                opc.resolve_opencode_model(bad_model)

        fake_bin = Path(self.tmp.name) / 'custom-opencode'
        fake_bin.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        fake_bin.chmod(0o755)
        self.assertEqual(opc.resolve_opencode_executable(str(fake_bin)), str(fake_bin.resolve()))
        with self.assertRaises(FileNotFoundError):
            opc.resolve_opencode_executable(str(Path(self.tmp.name) / 'missing-opencode'))
        with self.assertRaises(FileNotFoundError):
            opc.resolve_opencode_executable(' spaced/bin ')

    def test_config_isolation_and_run_argv_include_deterministic_title_and_disable_auxiliary_agents(self):
        cfg = opc.build_opencode_config(TEST_MODEL)
        self.assertEqual(opc.validate_opencode_config(cfg, expected_model=TEST_MODEL), [])
        self.assertEqual(cfg['model'], TEST_MODEL)
        self.assertEqual(cfg['small_model'], TEST_MODEL)
        self.assertFalse(cfg['autoupdate'])
        self.assertEqual(cfg['share'], 'disabled')
        self.assertFalse(cfg['snapshot'])
        self.assertEqual(cfg['plugin'], [])
        self.assertEqual(cfg['mcp'], {})
        self.assertEqual(cfg['instructions'], [])
        self.assertEqual(
            cfg['agent'],
            {'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'disable': True}},
        )
        self.assertEqual(list(cfg['permission'].keys()), list(opc.KNOWN_OPENCODE_PERMISSIONS))
        self.assertEqual(list(cfg['permission'].keys())[-1], '*')
        self.assertTrue(all(v == 'deny' for v in cfg['permission'].values()))

        for mut in (
            lambda c: c.update(small_model='opencode/other'),
            lambda c: c['agent']['title'].update(disable=False),
            lambda c: c['agent'].pop('summary'),
            lambda c: c['permission'].update(bash='allow'),
            lambda c: c.update(autoupdate=True),
            lambda c: c.update(api_key='sk-abcdef1234567890abcdef'),
        ):
            bad_cfg = json.loads(json.dumps(cfg))
            mut(bad_cfg)
            self.assertTrue(opc.validate_opencode_config(bad_cfg, expected_model=TEST_MODEL))

        argv = opc.build_run_argv(FAKE_OPENCODE, TEST_MODEL, 'PROMPT_BODY', title='opencode-worker-attempt-1')
        self.assertEqual(
            argv,
            [FAKE_OPENCODE, '--pure', 'run', '--format', 'json', '--model', TEST_MODEL, '--title', 'opencode-worker-attempt-1', 'PROMPT_BODY'],
        )
        for bad_title in ('', '  ', ' lead', 'trail ', 'bad\x00title'):
            with self.assertRaises(opc.OpenCodeBlocked):
                opc.build_run_argv(FAKE_OPENCODE, TEST_MODEL, 'PROMPT_BODY', title=bad_title)

        attempt_dir = self.out / 'iso-check'
        xdg_root = attempt_dir / 'opencode-xdg'
        cfg_path = xdg_root / 'config/opencode/opencode.json'
        env, summary = opc.build_isolated_env(
            {
                'HOME': str(Path.home()),
                'PATH': '/usr/bin:/bin',
                'OPENCODE_SERVER_URL': 'http://evil.local',
                'OPENCODE_CONFIG': '/tmp/ambient.json',
                'OPENAI_BASE_URL': 'http://evil.openai',
            },
            attempt_dir=attempt_dir,
            xdg_root=xdg_root,
            config_path=cfg_path,
            config_obj=cfg,
        )
        self.assertEqual(env['HOME'], str(Path.home()))
        self.assertFalse(summary['home_repurposed'])
        self.assertNotIn('OPENCODE_SERVER_URL', env)
        self.assertNotIn('OPENAI_BASE_URL', env)
        self.assertEqual(env['OPENCODE_DISABLE_DEFAULT_PLUGINS'], 'true')
        self.assertEqual(env['OPENCODE_DISABLE_EXTERNAL_SKILLS'], 'true')
        self.assertEqual(env['OPENCODE_DISABLE_PROJECT_CONFIG'], 'true')
        self.assertEqual(env['OPENCODE_DISABLE_AUTOCOMPACT'], 'true')
        self.assertEqual(env['OPENCODE_DISABLE_PRUNE'], 'true')
        self.assertEqual(env['OPENCODE_DISABLE_TERMINAL_TITLE'], 'true')

        with self.assertRaises(opc.OpenCodeBlocked):
            opc.build_isolated_env(
                {'HOME': str(attempt_dir)},
                attempt_dir=attempt_dir,
                xdg_root=xdg_root,
                config_path=cfg_path,
                config_obj=cfg,
            )

    def test_positive_attempt_validates_stream_export_and_applies_files(self):
        calls = []
        res, _ = self.run_attempt(fake_opencode_cli(calls=calls), name='pos-1')
        self.assertTrue(res['ok'], res['reason'])
        self.assertFalse(res['blocked'])
        self.assertEqual(len(calls), 2)
        run_argv, _ = calls[0]
        export_argv, _ = calls[1]
        self.assertIn('--title', run_argv)
        self.assertEqual(run_argv[run_argv.index('--title') + 1], 'opencode-worker-attempt-1')
        self.assertEqual(export_argv, [FAKE_OPENCODE, '--pure', 'export', 'ses_opencode_1'])
        self.assertEqual(res['model'], TEST_MODEL)
        self.assertEqual(res['provider_id'], 'opencode')
        self.assertEqual(res['model_id'], 'glm-5-free')
        self.assertIsNone(res['effort'])
        self.assertIsNone(res['permission_mode'])
        self.assertNotIn('status', res)
        self.assertEqual(res['proof_status'], 'VERIFIED')
        self.assertEqual(res['config_proof_status'], 'VERIFIED')
        self.assertTrue(res['stream_matches_export'])
        self.assertEqual(
            res['usage'],
            {
                'input_tokens': 1200,
                'output_tokens': 180,
                'reasoning_tokens': 40,
                'cache_read_tokens': 300,
                'cache_write_tokens': 20,
                'total_tokens': 1740,
                'total_steps': 1,
            },
        )
        self.assertEqual(self.ws_state()['calc.py'].decode('utf-8'), GOOD_FILES['calc.py'])

        # Optional native tokens.total may be omitted; adapter still sums all five disjoint categories to 1740
        no_total_tok = make_tokens(include_total=False)
        res_notot, _ = self.run_attempt(
            fake_opencode_cli(
                run_stdout=make_run_stdout(make_response(), tokens=no_total_tok),
                export_stdout=make_export_json(make_response(), tokens=no_total_tok),
            ),
            name='pos-no-optional-total',
        )
        self.assertTrue(res_notot['ok'], res_notot['reason'])
        self.assertEqual(res_notot['usage']['total_tokens'], 1740)

    def test_auxiliary_title_or_small_model_generation_blocks_preserves_raw_proof_and_keeps_usage_unknown(self):
        before = self.ws_state()
        native_log_with_title = (
            'INFO 2026-11-02T20:00:00 +1ms service=llm providerID=opencode modelID=glm-5-free sessionID=ses_opencode_1 small=false agent=build mode=primary stream\n'
            'INFO 2026-11-02T20:00:01 +2ms service=llm providerID=opencode modelID=glm-5-free sessionID=ses_opencode_1 small=true agent=title mode=title stream\n'
        )
        res_log, _ = self.run_attempt(
            fake_opencode_cli(native_log_text=native_log_with_title),
            name='aux-native-log',
        )
        self.assertTrue(res_log['blocked'])
        self.assertFalse(res_log['ok'])
        self.assertIn('auxiliary title/summary/small-model', res_log['reason'])
        self.assertEqual(res_log['usage'], opc.UNKNOWN_USAGE)
        self.assertEqual(res_log['files_written'], [])
        self.assertEqual(self.ws_state(), before)
        attempt_dir = self.out / 'aux-native-log'
        self.assertTrue((attempt_dir / 'opencode.stdout.jsonl').is_file())
        self.assertTrue((attempt_dir / 'opencode-export.json').is_file())
        self.assertTrue((attempt_dir / 'opencode-native.log').is_file())
        self.assertIn('agent=title', (attempt_dir / 'opencode-native.log').read_text(encoding='utf-8'))

        # Also block if export or run events directly carry agent=title / small=true / mismatched title
        for idx, kwargs in enumerate((
            {'run_stdout': make_run_stdout(make_response(), extra_event_fields={'agent': 'title'})},
            {'run_stdout': make_run_stdout(make_response(), extra_part_fields={'small': True})},
            {'export_stdout': make_export_json(make_response(), assistant_info_overrides={'agent': 'title'})},
            {'export_stdout': make_export_json(make_response(), assistant_info_overrides={'small': True})},
            {'export_stdout': make_export_json(make_response(), title='Auto Generated Random Title')},
        )):
            with self.subTest(aux_case=idx):
                r, _ = self.run_attempt(fake_opencode_cli(**kwargs), name=f'aux-ev-{idx}')
                self.assertTrue(r['blocked'])
                self.assertEqual(r['usage'], opc.UNKNOWN_USAGE)
                self.assertEqual(self.ws_state(), before)

    def test_negative_proof_quota_mismatch_tool_and_config_failures_block_before_apply(self):
        before = self.ws_state()
        resp = make_response()
        bad_cases = {
            'free-limit-stderr': {'run_code': 1, 'run_stderr': 'Error: Free usage limit reached'},
            'wrong-export-provider': {'export_stdout': make_export_json(resp, provider_id='other')},
            'wrong-export-model': {'export_stdout': make_export_json(resp, model_id='deepseek-v4.1-flash-free')},
            'nonstop-finish-stream': {'run_stdout': make_run_stdout(resp, reason='length')},
            'nonstop-finish-export': {'export_stdout': make_export_json(resp, finish='tool-calls')},
            'tool-part-in-export': {
                'export_stdout': make_export_json(
                    resp,
                    assistant_parts_overrides=[
                        {'id': 'p1', 'sessionID': 'ses_opencode_1', 'type': 'tool', 'tool': 'bash'}
                    ],
                )
            },
            'stream-export-text-mismatch': {'export_stdout': make_export_json(make_response(handoff='Different handoff'))},
            'token-disagreement': {'export_stdout': make_export_json(resp, tokens=make_tokens(input_tok=999))},
            'bad-optional-total-stream': {
                'run_stdout': make_run_stdout(resp, tokens=make_tokens(total_override=1380)),
                'export_stdout': make_export_json(resp, tokens=make_tokens(total_override=1380)),
            },
            'bad-optional-total-export': {
                'export_stdout': make_export_json(resp, tokens=make_tokens(total_override=1741)),
            },
            'bool-optional-total': {
                'run_stdout': make_run_stdout(resp, tokens=make_tokens(total_override=True)),
            },
            'missing-cache-key': {'run_stdout': make_run_stdout(resp, tokens={'input': 10, 'output': 5, 'reasoning': 0})},
            'zero-input-tokens': {
                'run_stdout': make_run_stdout(resp, tokens=make_tokens(input_tok=0)),
                'export_stdout': make_export_json(resp, tokens=make_tokens(input_tok=0)),
            },
            'postrun-config-widened': {
                'mutate_config': lambda p: p.write_text(
                    json.dumps({** json.loads(p.read_text()), 'autoupdate': True}, indent=2) + '\n',
                    encoding='utf-8',
                )
            },
            'out-of-scope-generation': {'response': make_response({'calc.py': 'x = 1\n', 'evil.py': 'y = 2\n'})},
            'secret-in-generation': {'response': make_response({'calc.py': 'API_KEY = "sk-abcdef1234567890abcdef"\n', 'tests/test_calc.py': 'x = 1\n'})},
        }
        for idx, (label, kw) in enumerate(bad_cases.items()):
            with self.subTest(case=label):
                res, _ = self.run_attempt(fake_opencode_cli(**kw), name=f'neg-{idx}')
                self.assertTrue(res['blocked'], f'expected BLOCKED for {label}')
                self.assertFalse(res['ok'])
                self.assertEqual(res['files_written'], [])
                self.assertEqual(self.ws_state(), before)

    def test_workflow_and_chatgpt_review_end_to_end_and_reviewer_quota_block(self):
        resp = make_response()
        jev_raw = {
            'model': 'jev-1.13.0',
            'answers': {
                'evidence_state': {
                    'type': 'choice',
                    'choice': 'adequate',
                    'confidence': 0.92,
                    'probabilities': {'adequate': 0.94, 'incomplete': 0.03, 'conflicting': 0.01, 'unclear': 0.02},
                },
                'unresolved_concern': {'type': 'noul', 'noul': 0.04},
            },
            'usage': {'input_tokens': 400, 'output_tokens': 25, 'total_tokens': 425},
        }
        rev_ans = {
            'verdict': 'PASS',
            'confidence': 93.0,
            'findings': [],
            'resolved_questions': [],
            'residual_risk': 'Scoped arithmetic addition only.',
        }
        rev_stdout = '\n'.join([
            json.dumps({
                'event': 'init',
                'conversation_id': 'rev-agy-1',
                'init': {
                    'model': 'gemini-4-argon-high',
                    'permission_mode': 'request-review',
                    'effort': 'high',
                    'conversation_id': 'rev-agy-1',
                },
            }),
            json.dumps({
                'event': 'result',
                'conversation_id': 'rev-agy-1',
                'result': {
                    'status': 'SUCCESS',
                    'conversation_id': 'rev-agy-1',
                    'response': json.dumps(rev_ans),
                    'usage': {
                        'input_tokens': 900,
                        'output_tokens': 110,
                        'thinking_tokens': 30,
                        'cache_read_tokens': 200,
                        'total_tokens': 1010,
                    },
                },
            }),
        ]) + '\n'

        def workflow_subproc(argv, **kwargs):
            cmd0 = str(argv[0])
            if cmd0 == FAKE_OPENCODE:
                return fake_opencode_cli(resp)(argv, **kwargs)
            if cmd0 == FAKE_JEV:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(jev_raw), stderr='')
            if cmd0 == FAKE_AGY:
                return subprocess.CompletedProcess(argv, 0, stdout=rev_stdout, stderr='')
            if cmd0 == 'python3' and 'unittest' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout='Ran 1 test in 0.01s\n\nOK\n', stderr='')
            if 'git' in cmd0 or (len(argv) > 1 and argv[1] == 'diff'):
                return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
            raise AssertionError(f'Unexpected command: {argv}')

        wf_out = Path(self.tmp.name) / 'wf-pass'
        with patch.dict(
            os.environ,
            {'WORKFLOW_WORKER': 'opencode', 'OPENCODE_MODEL': TEST_MODEL, 'AGY_MODEL': 'gemini-4-argon-high'},
        ), patch.object(wf.opencode, 'resolve_opencode_executable', return_value=FAKE_OPENCODE), patch.object(
            wf.gate, 'resolve_executable', side_effect=lambda name: FAKE_JEV if name == 'jev-pool' else FAKE_AGY
        ), patch.object(wf, 'resolve_rtk_executable', return_value=None), patch.object(
            wf.subprocess, 'run', side_effect=workflow_subproc
        ), patch.object(wf.opencode.subprocess, 'run', side_effect=workflow_subproc), patch.object(
            wf.gate.subprocess, 'run', side_effect=workflow_subproc
        ):
            report = wf.run_workflow(
                task_id='opc-task-1',
                objective='Implement add(a, b)',
                workspace=self.ws,
                output_dir=wf_out,
                scope_files=SCOPE,
                verify_cmds=['python3 -B -m unittest discover -s tests'],
            )

        self.assertEqual(report['verdict'], 'PASS', report.get('reasons'))
        self.assertEqual(report['worker_provider'], 'opencode')
        self.assertEqual(report['worker_model'], TEST_MODEL)
        self.assertIsNone(report['worker_effort'])
        self.assertIsNone(report['worker_and_reviewer_model'])
        self.assertEqual(report['reviewer_model'], 'gemini-4-argon-high')
        opc_totals = report['usage_totals']['opencode']
        self.assertEqual(opc_totals['sessions_with_verified_usage'], 1)
        self.assertEqual(opc_totals['input_tokens'], 1200)
        self.assertEqual(opc_totals['output_tokens'], 180)
        self.assertEqual(opc_totals['reasoning_tokens'], 40)
        self.assertEqual(opc_totals['cache_read_tokens'], 300)
        self.assertEqual(opc_totals['cache_write_tokens'], 20)
        self.assertEqual(opc_totals['total_tokens'], 1740)
        self.assertEqual(report['usage_totals']['gemini']['total_tokens'], 1010)
        self.assertEqual(report['usage_totals']['devin']['sessions_with_verified_usage'], 0)

        # Runner aggregation rejects a step whose total_tokens omits reasoning/cache_read/cache_write (e.g. 1380 != 1740)
        bad_step_agg = wf.aggregate_external_usage([
            {
                'provider': 'opencode',
                'role': 'worker',
                'usage': {
                    'input_tokens': 1200,
                    'output_tokens': 180,
                    'reasoning_tokens': 40,
                    'cache_read_tokens': 300,
                    'cache_write_tokens': 20,
                    'total_tokens': 1380,
                    'total_steps': 1,
                },
            }
        ])
        self.assertEqual(bad_step_agg['opencode']['sessions_with_verified_usage'], 0)
        self.assertEqual(bad_step_agg['opencode']['total_tokens'], 'unknown')

        # ChatGPT review helper validates OpenCode raw proof and computes all-in tokens across all five disjoint categories
        codex_stdout = '\n'.join([
            json.dumps({'type': 'thread.started', 'thread_id': 'th_opc_1'}),
            json.dumps({'type': 'turn.started'}),
            json.dumps({
                'type': 'item.completed',
                'item': {
                    'id': 'msg_1',
                    'type': 'agent_message',
                    'text': json.dumps({
                        'verdict': 'PASS',
                        'confidence': 94.0,
                        'findings': [],
                        'residual_risk': 'Bounded scope verified.',
                    }),
                },
            }),
            json.dumps({
                'type': 'turn.completed',
                'usage': {
                    'input_tokens': 2000,
                    'cached_input_tokens': 500,
                    'output_tokens': 100,
                    'reasoning_output_tokens': 20,
                    'total_tokens': 2100,
                },
            }),
        ]) + '\n'
        gpt_out = Path(self.tmp.name) / 'wf-pass-chatgpt-review'
        with patch.dict(os.environ, {'AGY_MODEL': 'gemini-4-argon-high'}), patch.object(
            cr, 'load_superpowers_skills', return_value={'using-superpowers': 'US', 'verification-before-completion': 'VBC'}
        ), patch.object(cr, 'discover_mcp_server_names', return_value=[]), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=[]
        ), patch.object(cr, 'resolve_codex_executable', return_value=FAKE_CODEX), patch.object(
            cr.subprocess,
            'run',
            return_value=subprocess.CompletedProcess([FAKE_CODEX], 0, stdout=codex_stdout, stderr=''),
        ):
            gpt_res = cr.run_review(workflow_dir=wf_out, workspace=self.ws, output_dir=gpt_out, profile='scoped')

        self.assertEqual(gpt_res['verdict'], 'PASS', gpt_res.get('reasons'))
        prov_io = gpt_res['provider_io_usage']
        self.assertEqual(prov_io['opencode_input_tokens'], 1200)
        self.assertEqual(prov_io['opencode_output_tokens'], 180)
        self.assertEqual(prov_io['opencode_reasoning_tokens'], 40)
        self.assertEqual(prov_io['opencode_cache_read_tokens'], 300)
        self.assertEqual(prov_io['opencode_cache_write_tokens'], 20)
        self.assertEqual(prov_io['opencode_io_tokens'], 1740)
        self.assertEqual(prov_io['opencode_cache_read_tokens_separate'], 300)
        self.assertEqual(prov_io['opencode_cache_write_tokens_separate'], 20)
        self.assertEqual(prov_io['opencode_reasoning_tokens_separate'], 40)
        self.assertEqual(prov_io['jev_io_tokens'], 425)
        self.assertEqual(prov_io['agy_io_tokens'], 1210)  # 900 + 200 cache_read + 110 output
        self.assertEqual(prov_io['provider_io_total_tokens'], 1740 + 425 + 1210)
        self.assertEqual(gpt_res['all_in_total_tokens'], 2100 + 1740 + 425 + 1210)

        # ChatGPT review fails closed if workflow-result external_steps or usage_totals under-reports OpenCode total_tokens
        wf_res_path = wf_out / 'workflow-result.json'
        orig_wf_res = wf_res_path.read_text(encoding='utf-8')
        tampered = json.loads(orig_wf_res)
        tampered['external_steps'][0]['usage']['total_tokens'] = 1380
        tampered['usage_totals']['opencode']['total_tokens'] = 1380
        wf_res_path.write_text(json.dumps(tampered, indent=2), encoding='utf-8')
        try:
            with patch.dict(os.environ, {'AGY_MODEL': 'gemini-4-argon-high'}):
                with self.assertRaises(ValueError):
                    cr.build_packet(wf_out, self.ws)
        finally:
            wf_res_path.write_text(orig_wf_res, encoding='utf-8')

        # If native OpenCode logs later reveal a separate small=true agent=title call omitted from step counters,
        # ChatGPT review refuses to claim complete all-in usage and blocks with 'unknown' counters.
        late_log_dir = wf_out / 'attempt-1/opencode-xdg/data/opencode/log'
        late_log_dir.mkdir(parents=True, exist_ok=True)
        (late_log_dir / 'session.log').write_text(
            'INFO service=llm providerID=opencode modelID=glm-5-free small=true agent=title stream\n',
            encoding='utf-8',
        )
        gpt_out_aux = Path(self.tmp.name) / 'wf-pass-chatgpt-review-aux-blocked'
        with patch.dict(os.environ, {'AGY_MODEL': 'gemini-4-argon-high'}), patch.object(
            cr, 'load_superpowers_skills', return_value={'using-superpowers': 'US', 'verification-before-completion': 'VBC'}
        ), patch.object(cr, 'discover_mcp_server_names', return_value=[]), patch.object(
            cr, 'discover_disabled_skill_paths', return_value=[]
        ):
            gpt_aux_res = cr.run_review(workflow_dir=wf_out, workspace=self.ws, output_dir=gpt_out_aux, profile='scoped')
        self.assertEqual(gpt_aux_res['verdict'], 'BLOCKED')
        self.assertEqual(gpt_aux_res['all_in_total_tokens'], 'unknown')
        self.assertEqual(gpt_aux_res['provider_io_usage']['provider_io_total_tokens'], 'unknown')

        # Missing/exhausted AGY reviewer quota still blocks final PASS even when OpenCode worker succeeds
        (self.ws / 'calc.py').write_text('def add(a, b):\n    return 0\n', encoding='utf-8')
        (self.ws / 'tests/test_calc.py').unlink(missing_ok=True)
        wf_rev_blocked = Path(self.tmp.name) / 'wf-rev-blocked'

        def rev_quota_exhausted(argv, **kwargs):
            if str(argv[0]) == FAKE_AGY:
                return subprocess.CompletedProcess(argv, 1, stdout='', stderr='Error: quota exceeded')
            return workflow_subproc(argv, **kwargs)

        with patch.dict(
            os.environ,
            {'WORKFLOW_WORKER': 'opencode', 'OPENCODE_MODEL': TEST_MODEL, 'AGY_MODEL': 'gemini-4-argon-high'},
        ), patch.object(wf.opencode, 'resolve_opencode_executable', return_value=FAKE_OPENCODE), patch.object(
            wf.gate, 'resolve_executable', side_effect=lambda name: FAKE_JEV if name == 'jev-pool' else FAKE_AGY
        ), patch.object(wf, 'resolve_rtk_executable', return_value=None), patch.object(
            wf.subprocess, 'run', side_effect=rev_quota_exhausted
        ), patch.object(wf.opencode.subprocess, 'run', side_effect=rev_quota_exhausted), patch.object(
            wf.gate.subprocess, 'run', side_effect=rev_quota_exhausted
        ):
            blocked_rep = wf.run_workflow(
                task_id='opc-task-blocked',
                objective='Implement add(a, b)',
                workspace=self.ws,
                output_dir=wf_rev_blocked,
                scope_files=SCOPE,
                verify_cmds=['python3 -B -m unittest discover -s tests'],
            )
        self.assertEqual(blocked_rep['verdict'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
