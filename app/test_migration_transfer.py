import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from migration import export_state,import_state,inspect_archive


class TransferTests(unittest.TestCase):
    def test_private_identity_and_tracks_survive_rename_verbatim(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            root=Path(old);(root/'native').mkdir()
            identity={'mac':'025253000001','device_id':'existing-id','password':'private-test','adopted':True}
            (root/'native/identity.json').write_text(json.dumps(identity))
            (root/'native/device-key.pem').write_bytes(b'private-test-key')
            (root/'native/device-cert.pem').write_bytes(b'public-test-cert')
            (root/'ringtones/primary').mkdir(parents=True)
            (root/'ringtones/primary/custom.mp3').write_bytes(b'test-audio-bytes')
            (root/'network/node').mkdir(parents=True)
            (root/'network/node/process.json').write_text('{"pid":123}')
            raw=export_state(root,{'controller_ip':'10.23.45.1','controller_fingerprint':'ab'*32},['media_player.room'])
            files=inspect_archive(raw)
            self.assertNotIn('network/node/process.json',files)
            result=import_state(raw,new)
            self.assertFalse(result['audio_played'])
            self.assertTrue(result['restart_required'])
            self.assertEqual((Path(new)/'native/device-key.pem').read_bytes(),b'private-test-key')
            self.assertEqual(json.loads((Path(new)/'native/identity.json').read_text()),identity)
            self.assertEqual(json.loads((Path(new)/'speakers.json').read_text())['entities'],['media_player.room'])
            self.assertEqual((Path(new)/'native/device-key.pem').stat().st_mode&0o077,0)

    def test_import_does_not_replace_existing_or_partial_identity(self):
        with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
            raw=export_state(old,{},[])
            (Path(new)/'native').mkdir()
            existing=Path(new)/'native/device-key.pem';existing.write_bytes(b'keep-me')
            with self.assertRaises(ValueError):import_state(raw,new)
            self.assertEqual(existing.read_bytes(),b'keep-me')

    def test_malformed_archive_and_path_traversal_are_rejected(self):
        with self.assertRaises(ValueError):inspect_archive(b'not-a-zip')
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('../outside',b'no')
        with self.assertRaises(ValueError):inspect_archive(output.getvalue())

    def test_changed_file_cannot_bypass_manifest_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            raw=export_state(directory,{},[])
            output=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output,'w') as target:
                for name in source.namelist():
                    target.writestr(name,b'{"entities":[]}' if name=='managed-settings.json' else source.read(name))
            with self.assertRaises(ValueError):inspect_archive(output.getvalue())

    def test_symlinked_state_cannot_be_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'native').mkdir()
            (root/'private.txt').write_text('private')
            (root/'native/device-key.pem').symlink_to(root/'private.txt')
            with self.assertRaises(ValueError):export_state(root,{},[])

if __name__=='__main__':unittest.main()

class TransferApiTests(unittest.TestCase):
    def handler(self, path, body=b''):
        import server
        from unittest.mock import Mock
        handler=server.Handler.__new__(server.Handler)
        handler.path=path
        handler.client_address=('172.30.32.2',123)
        handler.headers={'X-Prototype-Test':'silent','Content-Length':str(len(body))}
        handler.rfile=io.BytesIO(body)
        handler.respond=Mock()
        return handler
    def test_export_route_pauses_and_returns_archive_without_audio(self):
        import server
        from unittest.mock import Mock,patch
        with tempfile.TemporaryDirectory() as directory,patch.object(server,'DATA_ROOT',Path(directory)), \
                patch.object(server,'load_options',return_value={'speaker_entities':[]}), \
                patch.object(server,'NATIVE') as native,patch.object(server,'ONBOARDING') as onboarding:
            native.thread.is_alive.return_value=False
            handler=self.handler('/api/migration/export');handler.do_POST()
            native.stop.assert_called_once();onboarding.stop.assert_called_once()
            self.assertEqual(handler.respond.call_args.args[0],200)
            self.assertEqual(handler.respond.call_args.args[2],'application/zip')
            self.assertIn('chimes.json',inspect_archive(handler.respond.call_args.args[1]))
    def test_import_route_refuses_non_ingress_clients(self):
        handler=self.handler('/api/migration/import',b'bad')
        handler.client_address=('10.23.45.55',12)
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0],403)

    def test_private_identity_transfer_refuses_direct_loopback_clients(self):
        handler=self.handler('/api/migration/export')
        handler.client_address=('127.0.0.1',123)
        handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0],403)
