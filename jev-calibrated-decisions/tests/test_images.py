"""Offline image/Box transport tests; fake keys and deterministic PNG fixtures."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zlib

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('jev_image_tests',ROOT/'scripts/jev.py')
jev=importlib.util.module_from_spec(spec);spec.loader.exec_module(jev)


def png(width=280,height=280,include_target=True):
    def chunk(name,data):return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)
    pixels=[]
    for y in range(height):
        row=b''.join(b'\xff\x00\x00' if include_target and width//4<=x<3*width//4 and height//4<=y<3*height//4 else b'\xff\xff\xff' for x in range(width))
        pixels.append(b'\x00'+row)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(pixels)))+chunk(b'IEND',b'')


def asset():return {'type':'image','format':'png','data_base64':base64.b64encode(png()).decode()}
def result(model='jev-1.14.0'):
    return {'model':model,'answers':{'present':{'type':'noul','noul':0.99},'target':{'type':'box','box':{'left':0.25,'right':0.75,'top':0.25,'bottom':0.75},'confidence':0.9}},'usage':{'input_tokens':200,'output_tokens':20}}


class FakeHTTP(io.BytesIO):
    def __init__(self,value):super().__init__(json.dumps(value).encode());self.headers={}


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.client=jev.JevClient(api_key='fake-image-test-key',model='jev-1.14.0',retries=0,record=False)
        self.questions={'present':jev.noul('Is a red rectangle visible in `screenshot`?'),'target':jev.box('The red rectangle in `screenshot`')}
        self.assets={'test-image-reference':asset()};self.state={'screenshot':'test-image-reference'}
    def test_header_and_assets_reach_actual_http_request(self):
        seen=[]
        def send(request,**kwargs):seen.append(request);return FakeHTTP(result())
        with patch.object(self.client._opener,'open',side_effect=send):
            response=self.client.ask(self.state,self.questions,assets=self.assets,enable_images=True)
        request=seen[0];headers={k.lower():v for k,v in request.header_items()}
        self.assertEqual(headers['typesafe-danger-zone-images'],'true')
        self.assertNotIn('typesafe-danger-zone-long-context',headers)
        self.assertEqual(json.loads(request.data)['assets'],self.assets)
        self.assertEqual(response['answers']['target']['box']['left'],0.25)
    def test_plain_text_keeps_original_body_and_no_feature_headers(self):
        with patch.object(self.client._opener,'open',return_value=FakeHTTP({'model':'jev-1.13.0','answers':{},'usage':{}})) as send:
            self.client.ask({'message':'sample'},{'q':jev.noul('Is `message` a word?')})
        request=send.call_args.args[0]
        self.assertNotIn('assets',json.loads(request.data))
        self.assertFalse(any('danger-zone' in k.lower() for k,v in request.header_items()))
    def test_image_opt_in_required_before_http(self):
        with patch.object(self.client._opener,'open') as send:
            with self.assertRaises(jev.UsageError):self.client.ask(self.state,self.questions,assets=self.assets)
            send.assert_not_called()
    def test_box_requires_image_and_supported_model(self):
        with patch.object(self.client._opener,'open') as send:
            for kwargs in ({'enable_images':True},{'assets':self.assets,'enable_images':True,'model':'jev-1.13.0'}):
                with self.assertRaises(jev.UsageError):self.client.ask(self.state,self.questions,**kwargs)
            send.assert_not_called()
    def test_long_context_opt_in_is_separate(self):
        with patch.object(self.client._opener,'open',return_value=FakeHTTP({'model':'jev-1.14.0','answers':{},'usage':{}})) as send:
            self.client.ask({'text':'sample'},{'q':jev.noul('Is `text` short?')},enable_long_context=True)
        headers={k.lower():v for k,v in send.call_args.args[0].header_items()}
        self.assertEqual(headers['typesafe-danger-zone-long-context'],'true');self.assertNotIn('typesafe-danger-zone-images',headers)
    def test_invalid_assets_fail_before_http(self):
        for value in ('not-base64','data:image/png;base64,AAAA',base64.b64encode(b'not-a-png').decode()):
            assets={'test-image-reference':{'format':'png','data_base64':value}}
            with self.subTest(value=value[:12]),patch.object(self.client._opener,'open') as send:
                with self.assertRaises(jev.UsageError):self.client.ask(self.state,self.questions,assets=assets,enable_images=True)
                send.assert_not_called()
    def test_reference_must_be_in_value_not_an_object_key(self):
        with self.assertRaises(jev.UsageError):jev.validate_assets(self.assets,{'test-image-reference':'plain text'},{'q':jev.noul('Something visible?')})
        jev.validate_assets(self.assets,{}, {'q':jev.choice('Identify image',{'red':{'example':'test-image-reference'},'blue':'blue'})})
    def test_multipart_and_wrong_format_are_explicitly_rejected(self):
        for value in ({'part':'photo'}, {'type':'video','format':'png','data_base64':'AAAA'},{'format':'gif','data_base64':'AAAA'}):
            with self.assertRaises(jev.UsageError):jev.validate_assets({'test-image-reference':value},self.state,self.questions)
    def test_box_schema_and_numeric_validation(self):
        for name,value in [('left',True),('top',float('nan')),('right',2),('right',0.1)]:
            response=result();response['answers']['target']['box'][name]=value
            with self.assertRaises(jev.JevError):jev.validate_box_answers(response,self.questions)
        with self.assertRaises(jev.JevError):jev.validate_box_answers(result('jev-latest'),self.questions)
        with self.assertRaises(jev.UsageError):jev.validate_questions({'q':{'type':'box','instructions':'target','criteria':None}})
    def test_request_file_keeps_assets_and_flag(self):
        body={'model':'jev-1.14.0','state':self.state,'assets':self.assets,'questions':self.questions}
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'request.json';file.write_text(json.dumps(body))
            args=jev.build_parser().parse_args(['ask','-f',str(file),'--enable-images','--json'])
            with patch.object(jev,'JevClient',return_value=self.client),patch.object(self.client._opener,'open',return_value=FakeHTTP(result())) as send,redirect_stdout(io.StringIO()):
                self.assertEqual(jev.cmd_ask_entry(args),0)
            self.assertEqual(json.loads(send.call_args.args[0].data)['assets'],self.assets)
    def test_box_flag_and_asset_file_parse(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'assets.json';file.write_text(json.dumps(self.assets))
            args=jev.build_parser().parse_args(['ask','--field','screenshot=test-image-reference','--assets-file',str(file),'--box','target','red rectangle in `screenshot`','--noul','present','red rectangle visible in `screenshot`?','--enable-images','--model','jev-1.14.0'])
            self.assertEqual(jev.questions_from_args(args)['target']['type'],'box')
            self.assertEqual(jev.request_options(args)['assets'],self.assets)
            with self.assertRaises(jev.UsageError):jev.request_options(args,{'assets':self.assets})
    def test_body_cannot_enable_features_or_override_authorization(self):
        args=jev.build_parser().parse_args(['ask'])
        with self.assertRaises(jev.UsageError):jev.request_options(args,{'headers':{'TypeSafe-Danger-Zone-Images':'true'}})
        with patch.object(self.client._opener,'open') as send:
            with self.assertRaises(jev.UsageError):self.client._request('GET','/v1/models',extra_headers={'Authorization':'fake'})
            send.assert_not_called()
    def test_api_redirect_is_not_followed_and_key_echo_is_redacted(self):
        handler=jev._NoCredentialRedirect()
        self.assertIsNone(handler.redirect_request(None,None,302,'redirect',{},'https://other.invalid'))
        error=urllib.error.HTTPError('https://api.typesafe.ai/v1/systemone',401,'rejected',{},io.BytesIO(b'fake-image-test-key'))
        with patch.object(self.client._opener,'open',side_effect=error):
            with self.assertRaises(jev.AuthError) as raised:self.client.models()
        self.assertNotIn('fake-image-test-key',str(raised.exception))
    def test_symlink_launcher_resolves_bundle_after_move(self):
        with tempfile.TemporaryDirectory() as temp:
            link=Path(temp)/'jev';link.symlink_to(ROOT/'scripts/jev')
            response=subprocess.run([str(link),'--version'],capture_output=True,text=True,timeout=10)
            self.assertEqual(response.returncode,0,response.stderr);self.assertIn('jev 1.1.0',response.stdout)


if __name__=='__main__':unittest.main(verbosity=2)
