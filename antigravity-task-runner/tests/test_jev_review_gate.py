"""Offline routing/negative tests. These are not Jev model-accuracy claims."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/jev_review_gate.py'
spec = importlib.util.spec_from_file_location('review_gate', SCRIPT)
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)


def packet(kind='task'):
    value = {'id':'offline-case','schema_version':1,'kind':kind,'objective':'Fix synthetic parser',
             'worker':{'exit_code':0,'status':'SUCCESS','model':gate.GEMINI_MODEL,'effort':'high','response':'Changed parser; tests ran.','tool_denied':False},
             'checks':[{'kind':name,'command':'check '+name,'exit_code':0,'output':'Observed check output','passed':4} for name in ('tests','scope','whitespace')],
             'evidence':{'focused_diff':'- buggy\n+ fixed','verification_output':'Ran 4 tests; OK; exit 0','summary':'The documented parser cases are tested.'},
             'open_questions':[]}
    if kind=='quant':value['quant_contract']={k:'Explicit synthetic fixture contract' for k in ('data_provenance','friction','causality','parity')}
    return value


def response():
    return {'model':gate.JEV_MODEL,'answers':{
        'evidence_state':{'type':'choice','choice':'adequate','probabilities':{'adequate':0.94,'incomplete':0.02,'conflicting':0.02,'unclear':0.02},'confidence':0.92},
        'unresolved_concern':{'type':'noul','noul':0.01}}}


class RoutingTests(unittest.TestCase):
    def test_adjacent_bundle_and_installed_skill_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'portable bundle';home=Path(tmp)/'isolated-home'
            script=root/'antigravity-task-runner/scripts/jev_review_gate.py';script.parent.mkdir(parents=True)
            pool=root/'jev-calibrated-decisions/scripts/jev-pool';pool.parent.mkdir(parents=True);pool.write_text('#!/bin/sh\nexit 0\n');pool.chmod(0o755)
            installed=home/'.agents/skills/jev/scripts/jev-pool';installed.parent.mkdir(parents=True);installed.write_text('#!/bin/sh\nexit 0\n');installed.chmod(0o755)
            with patch.object(gate,'__file__',str(script)),patch.object(gate.Path,'home',return_value=home),patch.object(gate.shutil,'which',return_value=None):
                self.assertEqual(gate.resolve_executable('jev-pool'),str(pool.resolve()))
                pool.unlink()
                self.assertEqual(gate.resolve_executable('jev-pool'),str(installed.resolve()))
    def test_path_and_home_fallback_and_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);onpath=home/'custom-tools/agy';onpath.parent.mkdir();onpath.write_text('binary-placeholder');onpath.chmod(0o755)
            fallback=home/'.local/bin/agy';fallback.parent.mkdir(parents=True);fallback.write_text('placeholder');fallback.chmod(0o755)
            with patch.object(gate.Path,'home',return_value=home),patch.object(gate.shutil,'which',return_value=str(onpath)):
                self.assertEqual(gate.resolve_executable('agy'),str(onpath.resolve()))
                onpath.chmod(0o644)
                self.assertEqual(gate.resolve_executable('agy'),str(fallback.resolve()))
                fallback.unlink()
                with self.assertRaises(FileNotFoundError):gate.resolve_executable('agy')
    def test_missing_binary_records_unavailable_evidence(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',side_effect=FileNotFoundError()):
            root=Path(tmp)
            with self.assertRaises(FileNotFoundError):gate.call_jev(packet(),root,1)
            self.assertEqual(json.loads((root/'jev-invocation.json').read_text())['state'],'UNAVAILABLE')
            with self.assertRaises(FileNotFoundError):gate.run_reviewer(packet(),None,{},root,1)
            self.assertEqual(json.loads((root/'reviewer-1-invocation.json').read_text())['state'],'UNAVAILABLE')
    def test_complete_still_requires_independent_review(self):
        result=gate.decide(packet(),gate.parse_assessment(response()))
        self.assertEqual(result['route'],'standard_review');self.assertEqual(result['reviewers_required'],1)
    def test_quant_never_loses_two_reviewers(self):
        self.assertEqual(gate.decide(packet('quant'),gate.parse_assessment(response()))['reviewers_required'],2)
    def test_stricter_review_count_is_preserved(self):
        p=packet();p['min_reviewers']=2
        self.assertEqual(gate.decide(p,gate.parse_assessment(response()))['reviewers_required'],2)
        p=packet('quant');p['min_reviewers']=1
        self.assertEqual(gate.decide(p,gate.parse_assessment(response()))['reviewers_required'],2)
        p['min_reviewers']=True
        self.assertEqual(gate.decide(p,None)['route'],'blocked')
    def test_confidence_and_concern_boundaries(self):
        a=gate.parse_assessment(response());a['confidence']=0.85
        self.assertEqual(gate.decide(packet(),a)['route'],'standard_review')
        a['confidence']=0.84999;self.assertEqual(gate.decide(packet(),a)['route'],'escalated_review')
        a['confidence']=0.92;a['concern_probability']=0.15
        self.assertEqual(gate.decide(packet(),a)['route'],'escalated_review')
    def test_close_or_nonadequate_or_open_question_escalates(self):
        a=gate.parse_assessment(response());a['margin']=0.199
        self.assertEqual(gate.decide(packet(),a)['route'],'escalated_review')
        a['margin']=0.9;a['label']='unclear'
        self.assertEqual(gate.decide(packet(),a)['route'],'escalated_review')
        p=packet();p['open_questions']=['Unspecified behavior?']
        self.assertEqual(gate.decide(p,gate.parse_assessment(response()))['route'],'escalated_review')
    def test_failed_missing_empty_bool_checks_block(self):
        for mutate in (lambda p:p['checks'][0].update(exit_code=1),lambda p:p['checks'][0].update(passed=0),lambda p:p['checks'][0].update(exit_code=False),lambda p:p['worker'].update(response=''),lambda p:p['checks'].pop()):
            p=packet();mutate(p)
            self.assertEqual(gate.decide(p,gate.parse_assessment(response()))['route'],'blocked')
    def test_missing_quant_contract_blocks(self):
        p=packet('quant');p['quant_contract'].pop('friction')
        self.assertEqual(gate.decide(p,None)['route'],'blocked')
        for value in ('unknown','N/A','tbd'):
            p=packet('quant');p['quant_contract']['friction']=value
            self.assertEqual(gate.decide(p,None)['route'],'blocked')
    def test_credentials_not_sent_to_models(self):
        p=packet();p['evidence']['summary']='authorization: fake-sensitive-value'
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev') as call:
            result=gate.run(p,Path(tmp)/'run');self.assertEqual(result['verdict'],'BLOCKED');call.assert_not_called()
            self.assertNotIn('fake-sensitive-value',(Path(tmp)/'run/decision.json').read_text())
    def test_json_escaped_and_multiline_credentials_are_not_persisted(self):
        variants=[{'api_key':'unique-sensitive-marker'}, {'password':'unique-sensitive-marker'},
                  'Authorization: Bearer unique-sensitive-marker',
                  'password=\\"unique-sensitive-marker\\"',
                  '{"password": "unique-sensitive-marker"}',
                  'secret:\n unique-sensitive-marker']
        for item in variants:
            p=packet();p['evidence']['extra']=item
            with self.subTest(item=type(item).__name__),tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev') as call:
                result=gate.run(p,Path(tmp)/'run');self.assertEqual(result['verdict'],'BLOCKED');call.assert_not_called()
                self.assertNotIn('unique-sensitive-marker',json.dumps(gate.sanitize(item)))
                self.assertNotIn('unique-sensitive-marker',(Path(tmp)/'run/decision.json').read_text())
        p=packet();p['id']='password=unique-sensitive-marker'
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn('unique-sensitive-marker',json.dumps(gate.run(p,Path(tmp)/'run')))
    def test_invalid_jev_schema_and_model_rejected(self):
        for mutate in (lambda r:r.update(model='jev-latest'),lambda r:r['answers']['evidence_state'].update(confidence=True),lambda r:r['answers'].update(evidence_state=[]),lambda r:r['answers']['evidence_state']['probabilities'].update(adequate=0.2),lambda r:r['answers']['unresolved_concern'].update(noul=float('nan'))):
            r=response();mutate(r)
            with self.assertRaises(ValueError):gate.parse_assessment(r)
    def test_strict_json_rejects_duplicate_and_nonfinite(self):
        for text in ('{"a":1,"a":2}','{"p":NaN}'):
            with self.assertRaises(ValueError):gate.strict_json(text)
    def test_jev_failure_escalates_but_cannot_pass(self):
        p=packet()
        reviewer={'conversation_id':'fresh','answer':{'verdict':'PASS'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev',side_effect=ValueError('unavailable')),patch.object(gate,'run_reviewer',return_value=reviewer) as review:
            result=gate.run(p,Path(tmp)/'run');self.assertEqual(result['route'],'escalated_review');self.assertEqual(result['verdict'],'BLOCKED');review.assert_called_once()
    def test_triage_only_never_returns_final_pass(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev',return_value=gate.parse_assessment(response())),patch.object(gate,'run_reviewer') as review:
            result=gate.run(packet(),Path(tmp)/'run',triage_only=True)
            self.assertEqual(result['verdict'],'PENDING_REVIEW');review.assert_not_called()
    def test_cli_pending_is_distinct_from_pass_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'packet.json';source.write_text(json.dumps(packet()))
            argv=['gate',str(source),'--output-dir',str(Path(tmp)/'run'),'--triage-only']
            with patch.object(sys,'argv',argv),patch.object(gate,'call_jev',return_value=gate.parse_assessment(response())),patch('builtins.print'):
                self.assertEqual(gate.main(),3)
            argv[3]=str(Path(tmp)/'unavailable')
            with patch.object(sys,'argv',argv),patch.object(gate,'call_jev',side_effect=ValueError()),patch('builtins.print'):
                self.assertEqual(gate.main(),2)
    def test_timeouts_leave_redacted_partial_artifacts(self):
        failure=subprocess.TimeoutExpired(['dummy'],1,output=b'partial output',stderr=b'Authorization: Bearer unique-sensitive-marker')
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',side_effect=failure):
            path=Path(tmp)
            with self.assertRaises(ValueError):gate.call_jev(packet(),path,1)
            self.assertEqual(json.loads((path/'jev-invocation.json').read_text())['state'],'TIMEOUT')
            self.assertNotIn('unique-sensitive-marker',(path/'jev.stderr.txt').read_text())
            with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},path,1)
            self.assertTrue((path/'reviewer-1-timeout.json').exists())
            self.assertNotIn('unique-sensitive-marker',(path/'reviewer-1.stderr.txt').read_text())
    def test_null_event_payload_fails_closed(self):
        raw=subprocess.CompletedProcess([],0,stdout='{"event":"init","init":null}',stderr='')
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=raw):
            with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1)
    def test_explicit_wrong_effort_and_blank_context_rejected(self):
        answer={'verdict':'PASS','confidence':95,'findings':[],'resolved_questions':[],'residual_risk':'Synthetic sample only'}
        for effort,cid in [('low','fresh'),('high','   ')]:
            events=[{'event':'init','init':{'model':gate.GEMINI_MODEL,'effort':effort,'permission_mode':'request-review'}}, {'event':'result','result':{'status':'SUCCESS','response':json.dumps(answer),'conversation_id':cid}}]
            raw=subprocess.CompletedProcess([],0,stdout='\n'.join(map(json.dumps,events)),stderr='')
            with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=raw):
                with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1)
    def test_reused_quant_context_blocks(self):
        reviewer={'conversation_id':'same-context','answer':{'verdict':'PASS'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev',return_value=gate.parse_assessment(response())),patch.object(gate,'run_reviewer',return_value=reviewer):
            self.assertEqual(gate.run(packet('quant'),Path(tmp)/'run')['verdict'],'BLOCKED')
    def test_reviewer_model_and_open_question_evidence(self):
        p=packet();p['open_questions']=['What is the intended subtype behavior?']
        answer={'verdict':'PASS','confidence':95,'findings':[],'resolved_questions':[],'residual_risk':'sample only'}
        def output(model):
            return subprocess.CompletedProcess([],0,stdout='\n'.join(json.dumps(x) for x in [
                {'event':'init','init':{'model':model,'permission_mode':'request-review'}},
                {'event':'result','result':{'status':'SUCCESS','response':json.dumps(answer),'conversation_id':'fresh'}}]),stderr='')
        for model in ('wrong-model',gate.GEMINI_MODEL):
            with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=output(model)):
                with self.assertRaises(ValueError):gate.run_reviewer(p,None,{},Path(tmp),1)
        answer['resolved_questions']=[{'question':p['open_questions'][0],'resolution':'The declared contract permits int subclasses; no exact-type normalization is required.'}]
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=output(gate.GEMINI_MODEL)):
            self.assertEqual(gate.run_reviewer(p,None,{},Path(tmp),1)['answer']['verdict'],'PASS')
    def test_selected_claude_reviewer_args_and_cross_model_mismatch(self):
        claude='claude-opus-5-5-high';answer={'verdict':'PASS','confidence':95,'findings':[],'resolved_questions':[],'residual_risk':'sample only'}
        def output(model):
            return subprocess.CompletedProcess([],0,stdout='\n'.join(json.dumps(x) for x in [
                {'event':'init','init':{'model':model,'permission_mode':'request-review'}},
                {'event':'result','result':{'status':'SUCCESS','response':json.dumps(answer),'conversation_id':'fresh'}}]),stderr='')
        with patch.dict(os.environ,{'AGY_MODEL':claude}):
            with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=output(claude)) as run:
                info=gate.run_reviewer(packet(),None,{},Path(tmp),1)
                argv=run.call_args.args[0]
                self.assertEqual(argv[argv.index('--model')+1],claude);self.assertEqual(argv[argv.index('--effort')+1],'high')
                self.assertEqual(json.loads((Path(tmp)/'reviewer-1-settings.json').read_text())['toolPermission'],'request-review')
                self.assertEqual(info['model'],claude)
            with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=output(gate.GEMINI_MODEL)):
                with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1)
            self.assertTrue(any('wrong model' in x for x in gate.check_packet(packet())))
            p=packet();p['worker']['model']=claude;self.assertEqual(gate.check_packet(p),[])
        with patch.dict(os.environ,{'AGY_MODEL':''}):  # empty selector -> backwards-compatible default
            self.assertTrue(any('wrong model' in x for x in gate.check_packet(p)))
    def test_unsupported_agy_model_blocks_before_any_external_call(self):
        for bad in ('gpt-6.1-sol','claude-opus-5-5','Gemini-4-Argon-High',' claude-opus-5-5-high'):
            with self.subTest(model=bad),patch.dict(os.environ,{'AGY_MODEL':bad}),tempfile.TemporaryDirectory() as tmp,\
                 patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run') as run,patch.object(gate,'call_jev') as jev:
                result=gate.run(packet(),Path(tmp)/'run')
                self.assertEqual(result['verdict'],'BLOCKED');self.assertIsNone(result['selected_model'])
                self.assertTrue(any('unsupported AGY_MODEL' in r for r in result['reasons']))
                with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1)
                run.assert_not_called();jev.assert_not_called()
        claude='claude-opus-5-5-high'  # explicit helper model cannot bypass the AGY_MODEL selection
        for env,explicit in (('gpt-6.1-sol',gate.GEMINI_MODEL),('gpt-6.1-sol',claude),(gate.GEMINI_MODEL,'gpt-6.1-sol'),('','gpt-6.1-sol'),(gate.GEMINI_MODEL,claude),(claude,gate.GEMINI_MODEL),(claude,'')):
            with self.subTest(env=env,explicit=explicit),patch.dict(os.environ,{'AGY_MODEL':env}),tempfile.TemporaryDirectory() as tmp,\
                 patch.object(gate,'resolve_executable') as resolve,patch.object(gate.subprocess,'run') as run:
                with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1,model=explicit)
                with self.assertRaises(ValueError):gate.check_packet(packet(),explicit)
                self.assertFalse((Path(tmp)/'reviewer-1-settings.json').exists())
                resolve.assert_not_called();run.assert_not_called()
    def test_worker_proof_and_full_handoff_are_verified_and_forwarded(self):
        def with_proof(**over):
            p=packet();resp=p['worker']['response']
            proof={'actual_init_model':gate.GEMINI_MODEL,'permission_mode':'request-review','status':'SUCCESS','exit_code':0,'tool_denied':False,'conversation_id':'w-1'}
            proof.update(over);p['evidence'].update(worker_proof=proof,full_worker_handoff=resp,source_workspace='/src/ws');return p
        p=with_proof();self.assertEqual(gate.check_packet(p),[])
        ev=gate.build_reviewer_payload(p,None,{})['evidence']
        self.assertEqual((ev['worker_proof']['conversation_id'],ev['full_worker_handoff'],ev['source_workspace']),('w-1',p['worker']['response'],'/src/ws'))
        for over in ({'permission_mode':None},{'actual_init_model':'claude-opus-5-5-high'},{'tool_denied':None},{'exit_code':1}):
            with self.subTest(over=over):self.assertTrue(any('worker_proof' in x for x in gate.check_packet(with_proof(**over))))
        p=with_proof();p['evidence']['full_worker_handoff']='excerpt only'
        self.assertTrue(any('differs' in x for x in gate.check_packet(p)))
        p=with_proof();long='x'*(gate.MAX_WORKER_HANDOFF_CHARS+1);p['worker']['response']=long;p['evidence']['full_worker_handoff']=long
        with tempfile.TemporaryDirectory() as tmp,patch.object(gate,'call_jev') as jev,patch.object(gate,'run_reviewer') as rev:
            self.assertEqual(gate.run(p,Path(tmp)/'run')['verdict'],'BLOCKED');jev.assert_not_called();rev.assert_not_called()
    def test_public_code_names_and_annotations_do_not_trigger_credential_block(self):
        p=packet()
        p['evidence']['focused_diff']=(
            '+def _parse_token(token: str, password: Optional[str] = None, secret: SecretConfig = DEFAULT_CFG, api_key: str | None = None) -> int:\n'
            '+    token = token.strip()\n'
            '+    password = user_password\n'
            '+    secret = get_secret()\n'
            '+    if not token:\n'
            '+        raise ValueError(f"empty token: {token!r}")\n'
            '+    if not password:\n'
            '+        return _ verify(token=raw_token, password=user_password, api_key=config.api_key, secret=load_secret())\n'
            '+    return int(token)\n'
        )
        p['evidence']['summary']='Reject whitespace in _parse_token(token: str); verified by unit tests.'
        self.assertEqual(gate.check_packet(p),[])
        self.assertFalse(gate.contains_secret(p))
        self.assertNotIn('<redacted',gate.redact(p['evidence']['focused_diff']))
        for case_id,secret_sample,forbidden in (
            ('unquoted-colon-token','token: demo_unquoted_value','demo_unquoted_value'),
            ('prose-colon-token','synthetic token: demo_unquoted_value','demo_unquoted_value'),
            ('unquoted-eq-password','password = demo_password_value','demo_password_value'),
            ('prose-eq-password','synthetic password = demo_password_value','demo_password_value'),
            ('ambiguous-prose-colon-token','Reject whitespace inside a numeric token: verified by unit tests.','verified by unit tests'),
            ('curl-eq-token','curl token=my-secret-value','my-secret-value'),
            ('json-quoted-token','{"token": "my-secret-value"}','my-secret-value'),
            ('bearer-colon-token','token: Bearer my-secret-value','my-secret-value'),
            ('multi-part-bearer','Authorization: Bearer   my-secret-value   second-secret-part','second-secret-part'),
            ('standalone-bearer','Bearer my-secret-value','my-secret-value'),
            ('hyphen-password','password = my-secret-value','my-secret-value'),
            ('colon-api-key','api_key: my-secret-value','my-secret-value'),
            ('kwarg-quoted-password','connect(password="my-secret-value")','my-secret-value'),
            ('kwarg-sk-key',"connect(api_key='sk-abcdef1234567890abcdef')",'sk-abcdef1234567890abcdef'),
        ):
            with self.subTest(case_id=case_id):
                self.assertTrue(gate.contains_secret(secret_sample))
                redacted=gate.redact(secret_sample)
                self.assertIn('<redacted>',redacted)
                self.assertNotIn(forbidden,redacted)
                self.assertNotIn('my-secret-value',redacted)
                bad_packet=packet()
                bad_packet['evidence']['summary']=secret_sample
                self.assertIn('possible credential in packet; redact before delegation',gate.check_packet(bad_packet))
    def test_kotlin_recovery_public_syntax_and_interpolated_urls_vs_secrets(self):
        def colon(lhs,name,rhs):return f'{lhs}{name}:{rhs}'
        def eq(lhs,name,rhs):return f'{lhs}{name}={rhs}'
        marker='synthetic-secret-marker'
        ordinary_lines=[
            colon('data class RecoveryPayload(val ','token',' String, var ') + colon('','refresh_token',' String? = null)'),
            colon('private val ','secret',' String'),
            colon('lateinit var ','password',' String'),
            colon('constructor(','api_key',' String = "")'),
            colon('fun parseRecovery(','token',' String, ') + colon('','access_token',' String? = null): RecoveryPayload'),
            'fun verify(\n' + colon('    ','token',' String,\n') + colon('    ','password',' String? = null\n)'),
            eq('private val FRAGMENT_REGEX = Regex("^#?','token','([A-Za-z0-9_-]+)$")'),
            eq('private val PARAM_REGEX = Regex("(?:^|[#&])','token','([A-Za-z0-9_-]{16,128})(?:&|$)")'),
            eq('override fun toString(): String = "RecoveryPayload(','token','[REDACTED])"'),
            eq('override fun toString(): String = "RecoveryPayload(','token','REDACTED, userId=$userId)"'),
            eq('val url1 = "https://example.invalid/recover#','token','$rawToken"'),
            eq('val url2 = "https://example.invalid/recover?flow=android#','token','${session.rawToken}"'),
            eq('val url3 = "myapp://recover?state=$state&','token','$rawToken&mode=reset"'),
            eq('val url4 = "${baseUrl}/recover#','token','${rawToken}"'),
            eq('val url5 = "#','token','$rawToken"'),
        ]
        diff_text='\n'.join(
            f'+{line}' if '\n' not in line else '\n'.join(f'+{part}' for part in line.splitlines())
            for line in ordinary_lines
        )
        for text in (*ordinary_lines, '\n'.join(ordinary_lines), diff_text):
            self.assertFalse(gate.contains_secret(text),text)
            self.assertEqual(gate.redact(text),text)
        p=packet()
        p['evidence']['focused_diff']=diff_text
        p['evidence']['summary']=colon('Allow Kotlin (val ','token',' String) and interpolated recovery URL fixtures.')
        self.assertEqual(gate.check_packet(p),[])
        negatives=[
            colon('val ','token',f' String = "{marker}"'),
            colon('+    var ','password',f' String? = "{marker}"'),
            colon('data class Auth(val ','api_key',f' String = "{marker}")'),
            colon('fun auth(','secret',f' String = "{marker}")'),
            colon('val ','token',f' String =\n    "{marker}"'),
            colon('val ','token',f' String = "$prefix-{marker}"'),
            eq('val url = "https://example.invalid/recover#','token',f'${{rawToken}}{marker}"'),
            eq('+    val url = "https://example.invalid/recover#','token',f'$rawToken-{marker}"'),
            eq('val url = "https://example.invalid/recover#','token',f'{marker}$rawToken"'),
            eq('val url = "https://example.invalid/recover#','token',f'${{rawToken}}_{marker}"'),
            eq('val url = "https://example.invalid/recover#','token',f'{marker}"'),
            eq('val url = "https://example.invalid/recover?','api_key',f'{marker}"'),
            eq('// ','token',marker),
            colon('/* ','password',f' {marker} */'),
            colon('{"','refresh_token',f'": "{marker}"}}'),
            eq('','token',f'\\"{marker}\\"'),
            colon('','secret',f'\n {marker}'),
            eq('Regex("^#?','token',f'([A-Za-z0-9_-]+){marker}$")'),
            eq('override fun toString(): String = "RecoveryPayload(','token',f'REDACTED-{marker})"'),
            eq('override fun toString(): String = "RecoveryPayload(','token',f'${{{marker}}})"'),
        ]
        for bad in negatives:
            with self.subTest(bad=bad):
                self.assertTrue(gate.contains_secret(bad))
                redacted=gate.redact(bad)
                self.assertIn('<redacted>',redacted)
                self.assertNotIn(marker,redacted)
                bad_packet=packet()
                bad_packet['evidence']['focused_diff']=bad
                self.assertIn('possible credential in packet; redact before delegation',gate.check_packet(bad_packet))
        test_src=Path(__file__).read_text().split('def test_kotlin_recovery_public_syntax_and_interpolated_urls_vs_secrets')[1]
        self.assertIsNone(gate.SECRET_ASSIGNMENT.search(test_src))
        self.assertIsNone(gate.KEY_VALUE.search(test_src))
    def test_allowed_kotlin_and_url_interpolation_do_not_mask_adjacent_credentials(self):
        def colon(lhs,name,rhs):return f'{lhs}{name}:{rhs}'
        def eq(lhs,name,rhs):return f'{lhs}{name}={rhs}'
        marker='synthetic-adjacent-secret'
        kt_ok=colon('val ','token',' String')
        url_ok=eq('val u = "https://example.invalid/recover#','token','$rawToken"')
        cred_kt=colon('val ','password',f' String = "{marker}"')
        cred_url=eq('val leak = "https://example.invalid/recover#','api_key',f'{marker}"')
        same_url_after=eq('val u = "https://example.invalid/recover#','token','$rawToken&')+eq('','api_key',f'{marker}"')
        same_url_before=eq('val u = "https://example.invalid/recover?','api_key',f'{marker}&')+eq('','token','$rawToken"')
        for sample in (
            f'{kt_ok}\n{cred_kt}',
            f'{cred_kt}\n{kt_ok}',
            f'{url_ok}\n{cred_url}',
            f'{cred_url}\n{url_ok}',
            same_url_after,
            same_url_before,
        ):
            with self.subTest(sample=sample):
                self.assertTrue(gate.contains_secret(sample))
                redacted=gate.redact(sample)
                self.assertIn('<redacted>',redacted)
                self.assertNotIn(marker,redacted)
                bad_packet=packet()
                bad_packet['evidence']['focused_diff']=sample
                self.assertIn('possible credential in packet; redact before delegation',gate.check_packet(bad_packet))
    def test_jev_state_triage_omits_raw_diff_but_links_hashes_and_reviewer_keeps_diff(self):
        p=packet('quant')
        p['evidence']['focused_diff']='- old_line\n+ new_line_only_for_reviewer\n'
        p['evidence']['source_hashes']={'port_spec.py':'abc123hash'}
        jev_state=gate.build_jev_state(p)
        self.assertEqual(jev_state['evidence']['diff_sha256'],gate.stable_hash(p['evidence']['focused_diff']))
        self.assertEqual(jev_state['evidence']['packet_sha256'],gate.stable_hash(p))
        self.assertEqual(jev_state['evidence']['source_hashes'],{'port_spec.py':'abc123hash'})
        self.assertNotIn('focused_diff',jev_state['evidence'])
        self.assertNotIn('new_line_only_for_reviewer',json.dumps(jev_state))
        rev_payload=gate.build_reviewer_payload(p,gate.parse_assessment(response()),{'route':'standard_review','reasons':[]})
        self.assertIn('new_line_only_for_reviewer',rev_payload['evidence']['focused_diff'])
        self.assertNotIn('worker',rev_payload)
        self.assertTrue(all('output' not in c for c in rev_payload['checks']))
    def test_kotlin_compose_on_value_change_implicit_it_vs_secrets(self):
        def colon(lhs,name,rhs):return f'{lhs}{name}:{rhs}'
        def eq(lhs,name,rhs):return f'{lhs}{name}={rhs}'
        marker='synthetic-callback-secret'
        positives=[
            eq('onValueChange = { ','token',' it }'),
            eq('    onValueChange = { ','password',' it },'),
            eq('OutlinedTextField(value = raw, onValueChange = { ','secret',' it })'),
            eq('OutlinedTextField(onValueChange={','api_key','it})'),
        ]
        diff_text='\n'.join(f'+{line}' for line in positives)
        for text in (*positives,'\n'.join(positives),diff_text):
            self.assertFalse(gate.contains_secret(text),text)
            self.assertEqual(gate.redact(text),text)
        p=packet()
        p['evidence']['focused_diff']=diff_text
        p['evidence']['summary']=eq('Allow Compose (onValueChange = { ','token',' it }) callback.')
        self.assertEqual(gate.check_packet(p),[])
        ok_cb=positives[0]
        after_samples=[
            f'{ok_cb}\n'+eq('','password',f' "{marker}"'),
            f'+{ok_cb}\n'+colon('+val ','secret',f' String = "{marker}"'),
            eq('OutlinedTextField(onValueChange = { ','token',' it }, ')+eq('','api_key',f' "{marker}")'),
        ]
        negatives=[
            eq('onValueChange = { ','token',f' "{marker}" }}'),
            eq('+    onValueChange = { ','password',f' {marker} }}'),
            eq('onValueChange = { ','token',f' "$it-{marker}" }}'),
            eq('onValueChange = { ','token',f' "${{it}}{marker}" }}'),
            eq('onValueChange = { ','token',f' it + "{marker}" }}'),
            eq('onValueChange = { ','token',f' it + {marker} }}'),
            eq('onValueChange = { v -> ','token',' v }'),
            eq('onValueChange = { it -> ','token',' it }'),
            eq('onValueChange = { ','token',f' it; log("{marker}") }}'),
            eq('onValueChange = { log(it); ','token',' it }'),
            eq('onValueChange = { ','token',' it.trim() }'),
            eq('onValueChange = {\n    ','token',' it\n}'),
            eq('onValueChange = { ','token','\n    it }'),
            eq('// onValueChange = { ','token',f' {marker} }}'),
            colon('{"','token',f'": "{marker}"}}'),
            colon('{"onValueChange": {"','token','": "it"}}'),
            eq('','token',' it'),
            eq('    ','password',' it'),
            eq('onClick = { ','token',' it }'),
            eq('onValueChange = { state.','token',' it }'),
            *after_samples,
        ]
        for bad in negatives:
            with self.subTest(bad=bad):
                self.assertTrue(gate.contains_secret(bad),bad)
                redacted=gate.redact(bad)
                self.assertIn('<redacted>',redacted)
                self.assertNotIn(marker,redacted)
                bad_packet=packet()
                bad_packet['evidence']['focused_diff']=bad
                self.assertIn('possible credential in packet; redact before delegation',gate.check_packet(bad_packet))
        self.assertTrue(gate.redact(after_samples[0]).startswith(ok_cb+'\n<redacted>'))
    def test_devin_worker_proof_validated_separately_from_agy(self):
        def devin_packet(kind='task'):
            p=packet(kind);resp='Devin handoff'
            p['worker']={'provider':'devin','model':gate.DEVIN_MODEL,'exit_code':0,'proof_status':'VERIFIED','response':resp,'tool_denied':False,'tool_calls':0}
            p['evidence'].update(full_worker_handoff=resp,worker_proof={'provider':'devin','actual_generation_model':gate.DEVIN_MODEL,'agent_model_name':gate.DEVIN_MODEL_NAME,
                'permission_mode':'Normal','schema_version':gate.DEVIN_ATIF_SCHEMA,'exit_code':0,'tool_calls':0,'tool_denied':False,'stdout_matches_export':True,'session_id':'s-1'})
            return p
        self.assertEqual(gate.check_packet(devin_packet()),[])
        p=devin_packet();p['evidence']['worker_proof']['stdout_normalization']=gate.DEVIN_STDOUT_NORMALIZATION;self.assertEqual(gate.check_packet(p),[])
        p['evidence']['worker_proof']['stdout_normalization']='stripped arbitrary ANSI';self.assertTrue(gate.check_packet(p))
        self.assertEqual(gate.decide(devin_packet(),gate.parse_assessment(response()))['reviewers_required'],1)
        self.assertEqual(gate.decide(devin_packet('quant'),gate.parse_assessment(response()))['reviewers_required'],2)
        for mutate in (lambda p:p['worker'].update(effort='high'),lambda p:p['worker'].update(status='SUCCESS'),
                       lambda p:p['worker'].update(permission_mode='request-review'),lambda p:p['worker'].update(model=gate.GEMINI_MODEL),
                       lambda p:p['worker'].update(tool_calls=1),lambda p:p['worker'].update(proof_status='UNVERIFIED'),
                       lambda p:p['evidence'].pop('worker_proof'),lambda p:p['evidence']['worker_proof'].update(actual_init_model=gate.DEVIN_MODEL),
                       lambda p:p['evidence']['worker_proof'].update(stdout_matches_export=False),lambda p:p['evidence']['worker_proof'].update(permission_mode='request-review'),
                       lambda p:p['evidence']['worker_proof'].update(actual_generation_model='swe-2-high'),lambda p:p['worker'].update(provider='opencode')):
            p=devin_packet();mutate(p)
            with self.subTest(p=p['worker']):self.assertTrue(gate.check_packet(p))
        p=packet();p['worker']['model']=gate.DEVIN_MODEL  # Devin model never passes the agy path
        self.assertTrue(any('wrong model' in x for x in gate.check_packet(p)))
    def test_reviewer_print_timeout_with_success_status_rejected(self):
        answer={'verdict':'PASS','confidence':95,'findings':[],'resolved_questions':[],'residual_risk':'sample'}
        events=[{'event':'init','init':{'model':gate.GEMINI_MODEL,'permission_mode':'request-review'}},{'event':'result','result':{'status':'SUCCESS','finished_reason':'print_timeout','response':json.dumps(answer),'conversation_id':'fresh'}}]
        raw=subprocess.CompletedProcess([],0,stdout='\n'.join(map(json.dumps,events)),stderr='')
        with patch.dict(os.environ,{'AGY_MODEL':''}),tempfile.TemporaryDirectory() as tmp,patch.object(gate,'resolve_executable',return_value='offline-cli'),patch.object(gate.subprocess,'run',return_value=raw):
            with self.assertRaises(ValueError):gate.run_reviewer(packet(),None,{},Path(tmp),1)


if __name__=='__main__':unittest.main()
