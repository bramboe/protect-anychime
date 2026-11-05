"""Independent synthetic installations; no real console certificates or audio."""
import hashlib
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock,patch
import connection
from native import NativeChime
from ringtones import RingtoneStore
from test_support import certificate,SYNTHETIC_MP3

class PortableInstallationTests(unittest.TestCase):
    def options(self,pem,host='10.23.45.1'):
        return {'controller_ip':host,'native_ip':'10.23.45.20',
            'controller_certificate':pem,'controller_fingerprint':hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()}

    def test_different_users_trust_their_own_certificate_and_not_each_others(self):
        a=self.options(certificate('console-a.invalid'))
        b=self.options(certificate('console-b.invalid'),'10.55.66.1')
        self.assertNotEqual(a['controller_fingerprint'],b['controller_fingerprint'])
        for options in [a,b]:
            with patch('connection.fetch_certificate') as fetch:
                self.assertEqual(connection.ensure_saved_certificate(options),options)
                fetch.assert_not_called()
        with self.assertRaises(ValueError):
            connection.ensure_saved_certificate({**a,'controller_certificate':b['controller_certificate']})

    def test_pin_only_upgrade_fetches_and_privately_saves_only_the_already_trusted_certificate(self):
        options=self.options(certificate())
        pem=options.pop('controller_certificate')
        with tempfile.TemporaryDirectory() as directory,patch.object(connection,'MANAGED_PATH',Path(directory)/'managed.json'):
            fetch=Mock(return_value=(pem,options['controller_fingerprint']))
            result=connection.ensure_saved_certificate(options,fetcher=fetch)
            self.assertEqual(result['controller_certificate'],pem)
            self.assertEqual(connection.read_managed()['controller_certificate'],pem)
            self.assertEqual(connection.MANAGED_PATH.stat().st_mode&0o077,0)
            fetch.assert_called_once_with(options['controller_ip'])

    def test_pin_only_upgrade_does_not_trust_a_changed_certificate(self):
        options=self.options(certificate());options.pop('controller_certificate')
        other=self.options(certificate('different-console.invalid'))
        with tempfile.TemporaryDirectory() as directory,patch.object(connection,'MANAGED_PATH',Path(directory)/'managed.json'):
            with self.assertRaises(ValueError):
                connection.ensure_saved_certificate(options,fetcher=Mock(return_value=(other['controller_certificate'],other['controller_fingerprint'])))
            self.assertFalse(connection.MANAGED_PATH.exists())

    def test_new_installation_never_fetches_or_trusts_a_certificate_without_confirmation(self):
        with patch('connection.fetch_certificate') as fetch:
            self.assertEqual(connection.ensure_saved_certificate({}),{})
            fetch.assert_not_called()

    def test_native_requires_explicit_addresses_and_installation_certificate(self):
        options=self.options(certificate())
        with tempfile.TemporaryDirectory() as directory:
            for key in ['controller_ip','native_ip']:
                invalid=dict(options);invalid.pop(key)
                with self.assertRaises(ValueError):NativeChime(invalid,directory)
            pin_only=dict(options);pin_only.pop('controller_certificate')
            device=NativeChime(pin_only,directory,'/unused/developer-certificate.pem')
            with patch.object(Path,'read_text') as read:
                with self.assertRaisesRegex(ValueError,'installation'):
                    device._client_ssl()
                read.assert_not_called()

    def test_ringtone_catalog_is_empty_until_the_console_uploads_the_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RingtoneStore(directory)
            self.assertEqual(store.audio_info(),{'status':'ok','speaker':[]})
            for track in range(1,5):
                data=SYNTHETIC_MP3+bytes([track])
                store.upload(track,'Console tone '+str(track)+'.mp3',data)
                self.assertEqual(store.get(track)['path'].read_bytes(),data)
            restored=RingtoneStore(directory)
            self.assertEqual([x['track_no'] for x in restored.audio_info()['speaker']],[1,2,3,4])
            self.assertIsNone(restored.get(5))
