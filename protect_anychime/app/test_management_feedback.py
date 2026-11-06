"""Explicit management failures have useful, private diagnostics; no real audio."""
import io
import json
import unittest
from unittest.mock import Mock, patch
import server
from management import ManagementError

class ManagementFeedbackTests(unittest.TestCase):
    def request(self,error):
        h=server.Handler.__new__(server.Handler);h.path='/api/chimes/test';h.client_address=('172.30.32.2',1)
        raw=json.dumps({'key':'primary','confirmed':True,'volume':10}).encode()
        h.headers={'X-AnyChime-Request':'1','Content-Length':str(len(raw))};h.rfile=io.BytesIO(raw);h.respond=Mock()
        with patch.object(server,'load_options',return_value={'controller_ip':'10.0.0.1'}),patch.object(server.MANAGEMENT,'test_virtual_chime',side_effect=error) as play,patch('builtins.print') as log:
            h.do_POST()
        play.assert_called_once()
        return h.respond.call_args.args,log.call_args.args[0]
    def test_upstream_error_reports_status_without_blaming_credentials_or_retrying(self):
        (status,body),log=self.request(ManagementError('private secret',kind='http_error',http_status=500))
        self.assertEqual(status,400);self.assertEqual(body['http_status'],500)
        self.assertIn('HTTP 500',body['error']);self.assertIn('not retried automatically',body['error'])
        self.assertIn('test chime (http_error, HTTP 500)',log)
        self.assertNotIn('private secret',log+body['error'])
    def test_rate_limit_has_wait_guidance(self):
        (_,body),log=self.request(ManagementError('private secret',kind='rate_limit',http_status=429))
        self.assertIn('Wait a minute',body['error']);self.assertIn('HTTP 429',log)
    def test_access_failure_points_to_local_account(self):
        (_,body),log=self.request(ManagementError('private secret',kind='authentication',http_status=403))
        self.assertIn('local account permissions',body['error']);self.assertIn('HTTP 403',log)
