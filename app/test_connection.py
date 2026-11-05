from test_support import certificate
import hashlib
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import configuration
import connection
import server
from runtime import NativeWorker


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(connection, 'MANAGED_PATH', Path(self.directory.name)/'managed.json')
        self.path_patch.start()
        self.pem = certificate()
        self.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.pem)).hexdigest()

    def tearDown(self):
        self.path_patch.stop()
        self.directory.cleanup()

    def manager(self, now=100):
        fetcher = Mock(return_value=(self.pem, self.fingerprint))
        return connection.CertificateManager(fetcher=fetcher, clock=lambda: now), fetcher

    def test_existing_settings_moved_privately_before_supervisor_cleanup(self):
        raw = {'audio_url': 'https://example.test/chime.wav', 'native_ip': '10.23.45.20',
               'controller_ip': '10.23.45.1', 'controller_fingerprint': self.fingerprint,
               'native_enabled': False, 'unifi_compatibility_profile': '1.3.7',
               'playback_enabled': False, 'volume_limit': 4}
        request = Mock(return_value={'options': raw})
        clean = configuration.migrate(raw, requester=request, data_dir=self.directory.name)
        self.assertEqual(clean, {})
        self.assertEqual(connection.read_managed()['volume_limit'],4)
        self.assertEqual(connection.read_managed()['audio_url'], raw['audio_url'])
        self.assertEqual(connection.read_managed()['controller_fingerprint'], self.fingerprint)
        self.assertEqual(connection.MANAGED_PATH.stat().st_mode & 0o077, 0)
        self.assertEqual(request.call_args.args, ('POST', {'options': clean}))

    def test_migration_uses_resolved_sound_secret_without_logging_or_posting_it(self):
        raw = {'audio_url': '!secret sound'}
        request = Mock(return_value={'options': raw})
        clean = configuration.migrate(raw, requester=request, data_dir=self.directory.name,
                                      effective={'audio_url': 'https://example.test/private.wav'})
        self.assertEqual(connection.read_managed()['audio_url'], 'https://example.test/private.wav')
        self.assertNotIn('audio_url', clean)

    def test_gateway_and_own_address_are_automatic_and_no_playback_is_enabled(self):
        route = Mock()
        route.getsockname.return_value = ('10.23.45.20', 3456)
        route.__enter__ = Mock(return_value=route)
        route.__exit__ = Mock(return_value=False)
        with patch('connection.default_gateway', return_value='10.23.45.1'), \
                patch('connection.socket.socket', return_value=route):
            options = connection.runtime_options(configuration.normalize({}))
        self.assertTrue(options['native_enabled'])
        self.assertNotIn('playback_enabled',options)
        self.assertEqual(options['native_ip'], '10.23.45.20')
        self.assertEqual(options['controller_ip'], '10.23.45.1')
        self.assertNotIn('success_url',options)
        route.send.assert_not_called()
        route.sendto.assert_not_called()

    def test_default_gateway_decoded_from_linux_route_table(self):
        path = Path(self.directory.name)/'route'
        path.write_text('Iface Destination Gateway Flags\neth0 00000000 012D170A 0003\n')
        self.assertEqual(connection.default_gateway(str(path)), '10.23.45.1')

    def test_offline_address_falls_back_to_preserved_address(self):
        with patch('connection.socket.socket', side_effect=OSError('offline')):
            self.assertEqual(connection.local_address('10.23.45.1', '10.23.45.20'), '10.23.45.20')

    def test_native_worker_starts_without_a_separate_enable_toggle(self):
        worker = NativeWorker()
        with patch('runtime.threading.Thread') as thread:
            worker.start({},str(Path(self.directory.name)/'native'))
            thread.return_value.start.assert_called_once()

    def test_matching_certificate_is_observation_only(self):
        manager, fetch = self.manager()
        result = manager.inspect('10.23.45.1', {'controller_ip': '10.23.45.1',
                                                'controller_fingerprint': self.fingerprint})
        self.assertTrue(result['matches_trusted'])
        self.assertIsNone(result['confirmation_token'])
        self.assertFalse(connection.MANAGED_PATH.exists())
        fetch.assert_called_once_with('10.23.45.1')

    def test_changed_certificate_requires_one_use_confirmation_before_saving(self):
        manager, _ = self.manager()
        result = manager.inspect('10.23.45.1', {'controller_ip': '10.23.45.1',
                                                'controller_fingerprint': '00'*32})
        self.assertFalse(connection.MANAGED_PATH.exists())
        self.assertFalse(result['matches_trusted'])
        saved = manager.confirm(result['confirmation_token'])
        self.assertTrue(saved['restart_required'])
        self.assertFalse(saved['audio_played'])
        self.assertEqual(connection.read_managed()['controller_certificate'], self.pem)
        self.assertEqual(connection.read_managed()['controller_fingerprint'], self.fingerprint)
        with self.assertRaises(ValueError):
            manager.confirm(result['confirmation_token'])

    def test_certificate_confirmation_expiry_and_invalid_token_do_not_save(self):
        manager, _ = self.manager()
        result = manager.inspect('10.23.45.1', {})
        with self.assertRaises(ValueError):
            manager.confirm('wrong')
        manager.clock = lambda: 221
        with self.assertRaises(ValueError):
            manager.confirm(result['confirmation_token'])
        self.assertFalse(connection.MANAGED_PATH.exists())

    def test_public_host_and_non_lan_certificate_targets_are_rejected_before_fetch(self):
        manager, fetch = self.manager()
        for target in ['8.8.8.8', '127.0.0.1', '169.254.1.1', 'https://10.23.45.1', '10.23.45.1:443']:
            with self.assertRaises(ValueError):
                manager.inspect(target, {})
        fetch.assert_not_called()

    def test_settings_schema_only_contains_daily_playback_controls(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root/'config.json').read_text())
        self.assertEqual(config['schema'], {'protect_username':'str','protect_password':'password','auto_adopt_chimes':'bool'})

    def test_imported_certificate_keeps_native_pin_check_in_force(self):
        from native import NativeChime
        options = {'controller_ip': '10.23.45.1', 'native_ip': '10.23.45.20',
                   'controller_fingerprint': self.fingerprint, 'controller_certificate': self.pem}
        device = NativeChime(options, self.directory.name, '/missing/bundled-cert.pem')
        with patch('native.ssl.SSLContext') as context:
            device._client_ssl()
            context.return_value.load_verify_locations.assert_called_once_with(cadata=self.pem)
        device.controller_fingerprint = '00' * 32
        with self.assertRaisesRegex(ValueError, 'Pinned controller certificate'):
            device._client_ssl()

    def test_empty_legacy_sound_keeps_builtin_url_dynamic(self):
        raw = {'audio_url': ''}
        configuration.migrate(raw, requester=Mock(return_value={'options': raw}),
                              data_dir=self.directory.name,
                              effective={'audio_url': 'http://10.23.45.20:48199/media/default-chime.wav'})
        self.assertEqual(connection.read_managed()['audio_url'], '')
        with patch('connection.local_address', return_value='10.23.45.21'), \
                patch('connection.default_gateway', return_value='10.23.45.1'):
            options = connection.runtime_options({})
        self.assertNotIn('success_url',options)
        self.assertEqual(options['audio_url'],'')

    def test_certificate_route_is_ingress_only_and_refresh_does_not_confirm(self):
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ('172.30.32.2', 12345)
        handler.path = '/api/connection/certificate'
        body = json.dumps({'controller_ip': '10.23.45.1'}).encode()
        handler.headers = {'X-Prototype-Test': 'silent', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.respond = Mock()
        with patch('server.load_options', return_value={}), patch('server.CERTIFICATES') as manager:
            manager.inspect.return_value = {'matches_trusted': True, 'audio_played': False}
            handler.do_POST()
            manager.inspect.assert_called_once_with('10.23.45.1', {})
            manager.confirm.assert_not_called()
            self.assertEqual(handler.respond.call_args.args[0], 200)
            handler.client_address = ('10.23.45.50', 12345)
            handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0], 403)

    def test_only_builtin_audio_is_public_and_other_routes_require_ingress(self):
        handler = server.Handler.__new__(server.Handler)
        handler.client_address = ('10.23.45.50', 12345)
        handler.respond = Mock()
        for path, expected in [('/media/success.mp3', 403),('/api/connection', 403),
                               ('/api/status', 403),('/assets/chime.png', 403),
                               ('/media/../connection.py', 403)]:
            handler.path = path
            handler.do_GET()
            self.assertEqual(handler.respond.call_args.args[0], expected)

    def test_certificate_changes_reject_loopback_and_forged_forwarded_headers(self):
        for peer in ['127.0.0.1','::1','10.23.45.50']:
            for route in ['/api/connection/certificate','/api/connection/trust']:
                with self.subTest(peer=peer,route=route):
                    handler=server.Handler.__new__(server.Handler)
                    handler.client_address=(peer,1234);handler.path=route
                    handler.headers={'X-Prototype-Test':'silent','Content-Length':'2',
                        'X-Forwarded-For':'172.30.32.2'}
                    handler.rfile=io.BytesIO(b'{}');handler.respond=Mock()
                    with patch('server.CERTIFICATES') as manager,patch('server.load_options') as options:
                        handler.do_POST()
                        self.assertEqual(handler.respond.call_args.args[0],403)
                        manager.inspect.assert_not_called();manager.confirm.assert_not_called()
                        options.assert_not_called()
                    self.assertEqual(handler.rfile.tell(),0)



if __name__ == '__main__':
    unittest.main()
