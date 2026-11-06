"""One-step Protect connection from the web interface. No network, HA or audio."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import connection
from management import ManagementError
from runtime import NativeWorker
import setup_flow

HOST = '10.23.45.1'


class FakeCertificates:
    def __init__(self, trusted=False):
        self.trusted = trusted
        self.confirmed = []

    def inspect(self, controller, options):
        same = self.trusted and controller == options.get('controller_ip')
        return {'controller_ip': controller, 'fingerprint': 'f' * 64,
                'matches_trusted': same, 'confirmation_token': None if same else 'token'}

    def confirm(self, token):
        self.confirmed.append(token)
        with connection.LOCK:
            managed = connection.read_managed()
            managed.update(controller_ip=HOST, controller_fingerprint='f' * 64, controller_certificate='pem')
            connection.save_managed(managed)


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.managed = Path(self.temp.name) / 'managed-settings.json'
        self.patch = patch.object(connection, 'MANAGED_PATH', self.managed)
        self.patch.start()
        self.certificates = FakeCertificates()
        self.management = Mock()
        self.management.verify_account.return_value = {'console_name': 'Dream Machine Pro',
            'console_model': 'UDM Pro', 'console_version': '6.1.2'}

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def connect(self, payload, options=None):
        return setup_flow.connect(payload, {'controller_ip': HOST} if options is None else options,
                                  certificates=self.certificates, management=self.management)

    def test_connect_with_account_confirms_console_and_saves_account_privately(self):
        result = self.connect({'controller_ip': HOST, 'username': ' local-user ',
                               'password': 'private-test-password', 'auto_adopt': True})
        self.assertEqual(self.certificates.confirmed, ['token'])
        self.management.verify_account.assert_called_once_with(HOST, 'local-user', 'private-test-password',
                                                               refresh_certificate=True)
        saved = connection.read_managed()
        self.assertEqual(saved['protect_username'], 'local-user')
        self.assertEqual(saved['protect_password'], 'private-test-password')
        self.assertIs(saved['auto_adopt_chimes'], True)
        self.assertEqual(saved['console_name'], 'Dream Machine Pro')
        self.assertEqual(saved['controller_fingerprint'], 'f' * 64)
        self.assertEqual(self.managed.stat().st_mode & 0o077, 0)
        self.assertNotIn('private-test-password', json.dumps(result))
        self.assertTrue(result['account_configured'] and result['auto_adopt'])
        self.assertFalse(result['audio_played'])
        public = setup_flow.public_setup(saved)
        self.assertNotIn('private-test-password', json.dumps(public))
        self.assertEqual(public['username'], 'local-user')

    def test_failed_sign_in_saves_nothing_and_does_not_trust_the_console(self):
        self.management.verify_account.side_effect = ManagementError('refused', kind='authentication')
        with self.assertRaises(ManagementError):
            self.connect({'controller_ip': HOST, 'username': 'local-user', 'password': 'wrong'})
        self.assertEqual(self.certificates.confirmed, [])
        self.assertFalse(self.managed.exists())

    def test_missing_fields_are_refused_before_any_sign_in(self):
        for payload in ({'controller_ip': '', 'username': 'u', 'password': 'p'},
                        {'controller_ip': HOST, 'username': '', 'password': 'p'},
                        {'controller_ip': HOST, 'username': 'u', 'password': ''},
                        {'controller_ip': '8.8.8.8', 'username': 'u', 'password': 'p'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.connect(payload, {})
        self.management.verify_account.assert_not_called()
        self.assertFalse(self.managed.exists())

    def test_without_account_confirms_console_and_removes_a_saved_account(self):
        connection.save_managed({'protect_username': 'old', 'protect_password': 'old-password',
                                 'auto_adopt_chimes': True, 'console_name': 'Old'})
        result = self.connect({'controller_ip': HOST, 'use_account': False})
        self.management.verify_account.assert_not_called()
        saved = connection.read_managed()
        for key in setup_flow.ACCOUNT_KEYS + setup_flow.CONSOLE_KEYS:
            self.assertNotIn(key, saved)
        self.assertEqual(saved['controller_fingerprint'], 'f' * 64)
        self.assertFalse(result['account_configured'] or result['auto_adopt'])

    def test_blank_password_keeps_the_saved_one_for_the_same_console_and_user(self):
        self.certificates.trusted = True
        options = {'controller_ip': HOST, 'protect_username': 'local-user', 'protect_password': 'kept'}
        result = self.connect({'controller_ip': HOST, 'username': 'local-user', 'password': '',
                               'auto_adopt': False}, options)
        self.management.verify_account.assert_called_once_with(HOST, 'local-user', 'kept', refresh_certificate=False)
        self.assertEqual(self.certificates.confirmed, [])
        self.assertEqual(connection.read_managed()['protect_password'], 'kept')
        self.assertIs(connection.read_managed()['auto_adopt_chimes'], False)
        self.assertFalse(result['console_changed'])
        # A different user or a new console always needs the password again.
        with self.assertRaises(ValueError):
            self.connect({'controller_ip': HOST, 'username': 'other-user', 'password': ''}, options)

    def test_home_assistant_account_is_imported_once_and_cleared(self):
        stored = {'protect_username': 'ha-user', 'protect_password': 'ha-password',
                  'auto_adopt_chimes': True, 'unrelated': 1}
        calls = []
        def request(method, body=None):
            calls.append(method)
            if method == 'POST':
                stored.clear(); stored.update(body['options']); return {}
            return {'options': dict(stored)}
        raw = dict(stored)
        self.assertTrue(setup_flow.import_home_assistant_account(raw, request))
        saved = connection.read_managed()
        self.assertEqual((saved['protect_username'], saved['protect_password']), ('ha-user', 'ha-password'))
        self.assertIs(saved['auto_adopt_chimes'], True)
        self.assertEqual(stored, {'unrelated': 1})
        self.assertEqual(calls, ['GET', 'POST'])

    def test_unclearable_home_assistant_values_never_overwrite_a_newer_app_account(self):
        raw = {'protect_username': 'ha-user', 'protect_password': 'ha-password'}
        failing = Mock(side_effect=OSError('supervisor unavailable'))
        self.assertTrue(setup_flow.import_home_assistant_account(raw, failing))
        managed = connection.read_managed()
        managed.update(protect_username='app-user', protect_password='app-password')
        connection.save_managed(managed)
        self.assertFalse(setup_flow.import_home_assistant_account(raw, failing))
        self.assertEqual(connection.read_managed()['protect_username'], 'app-user')

    def test_home_assistant_options_never_supply_the_account_directly(self):
        options = setup_flow.without_account({'protect_username': 'u', 'protect_password': 'p',
                                              'auto_adopt_chimes': True, 'audio_url': ''})
        self.assertEqual(options, {'audio_url': ''})


class BridgeResumeTests(unittest.TestCase):
    def test_waiting_bridge_starts_after_setup_without_app_restart(self):
        worker = NativeWorker()
        finished = threading.Thread(target=lambda: None); finished.start(); finished.join()
        worker.thread = finished; worker.waiting_for_setup = True; worker._data_dir = '/tmp/native'
        worker.error = 'Connect to your Protect console at the top of this page.'
        worker.stop_event.set()
        with patch.object(worker, 'start') as start:
            self.assertTrue(worker.resume_after_setup({'controller_ip': HOST}))
        start.assert_called_once_with({'controller_ip': HOST}, '/tmp/native')
        self.assertIsNone(worker.error); self.assertFalse(worker.stop_event.is_set())

    def test_running_bridge_is_not_restarted(self):
        worker = NativeWorker()
        worker.thread = Mock(); worker.thread.is_alive.return_value = True
        worker.waiting_for_setup = False
        with patch.object(worker, 'start') as start:
            self.assertFalse(worker.resume_after_setup({}))
        start.assert_not_called()

    def test_apply_reports_restart_only_for_a_running_bridge_on_a_new_console(self):
        worker = Mock()
        worker.resume_after_setup.return_value = False
        worker.thread.is_alive.return_value = True
        self.assertEqual(setup_flow.apply(worker, {}, {'console_changed': True, 'auto_adopt': True}), 'restart_required')
        worker.set_auto_adopt.assert_called_with(True)
        self.assertEqual(setup_flow.apply(worker, {}, {'console_changed': False}), 'running')
        worker.set_auto_adopt.assert_called_with(False)
        worker.resume_after_setup.return_value = True
        self.assertEqual(setup_flow.apply(worker, {}, {'console_changed': True}), 'started')


class ConnectRouteTests(unittest.TestCase):
    def post(self, body, peer='172.30.32.2', **patches):
        import io
        import server
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/connection/connect'; handler.client_address = (peer, 1)
        raw = json.dumps(body).encode()
        handler.headers = {'X-AnyChime-Request': '1', 'Content-Length': str(len(raw))}
        handler.rfile = io.BytesIO(raw); handler.respond = Mock()
        with patch.object(server, 'load_options', return_value={'controller_ip': HOST}), \
                patch.object(server.setup_flow, 'connect', **patches) as connect, \
                patch.object(server.setup_flow, 'apply', return_value='started'), \
                patch('builtins.print') as log:
            handler.do_POST()
        return handler.respond.call_args.args, connect, ' '.join(str(c.args[0]) for c in log.call_args_list)

    def test_only_home_assistant_ingress_can_connect(self):
        (status, _), connect, _ = self.post({'controller_ip': HOST}, peer='127.0.0.1')
        self.assertEqual(status, 403); connect.assert_not_called()

    def test_success_reports_bridge_state_and_logs_no_credentials(self):
        body = {'controller_ip': HOST, 'username': 'local-user', 'password': 'private-test-password'}
        (status, result), _, log = self.post(body, return_value={'account_configured': True, 'controller_ip': HOST})
        self.assertEqual(status, 200); self.assertEqual(result['bridge'], 'started')
        self.assertNotIn('private-test-password', log); self.assertNotIn('local-user', log)

    def test_refused_sign_in_explains_local_account_without_leaking_details(self):
        body = {'controller_ip': HOST, 'username': 'local-user', 'password': 'private-test-password'}
        (status, result), _, log = self.post(body, side_effect=ManagementError('private upstream text', kind='authentication', http_status=401))
        self.assertEqual(status, 400); self.assertIn('local UniFi account', result['error'])
        self.assertNotIn('private', result['error'] + log); self.assertIn('HTTP 401', log)


if __name__ == '__main__':
    unittest.main()
