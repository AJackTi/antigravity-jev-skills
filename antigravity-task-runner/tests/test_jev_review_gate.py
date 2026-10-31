"""Offline routing/negative tests. These are not Jev model-accuracy claims."""
import importlib.util
import json
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


if __name__=='__main__':unittest.main()
