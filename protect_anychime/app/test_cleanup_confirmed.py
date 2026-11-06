"""Finishing Protect cleanup without an account, after the user removed the chime in Protect.

No network, Protect or audio: only the local registry and identity files.
"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from registry import ChimeRegistry
from runtime import NativeWorker
import server

A = 'media_player.a'
ROWS = [{'entity_id': A, 'name': 'Kitchen', 'available': True, 'supported': True,
         'integration': 'sonos', 'ha_device_id': '1' * 32, 'group': [A], 'features': 1049092}]


def post(body, peer='172.30.32.2'):
    handler = server.Handler.__new__(server.Handler)
    handler.path = '/api/chimes/cleanup-confirmed'; handler.client_address = (peer, 1)
    raw = json.dumps(body).encode()
    handler.headers = {'X-AnyChime-Request': '1', 'Content-Length': str(len(raw))}
    handler.rfile = io.BytesIO(raw); handler.respond = Mock()
    with patch('builtins.print') as log:
        handler.do_POST()
    return handler.respond.call_args.args, ' '.join(str(c.args[0]) for c in log.call_args_list)


class CleanupConfirmedRouteTests(unittest.TestCase):
    def registry(self, **entry):
        registry = Mock()
        registry.snapshot.return_value = [{'key': 'primary', 'active': False, **entry}]
        return registry

    def test_pending_removal_is_finished_locally_without_a_protect_request(self):
        registry = self.registry(removal_requested=True, removal_pending=True)
        with patch.object(server.NATIVE, 'registry', registry), \
                patch.object(server.NATIVE, 'remove_from_protect') as finish, \
                patch.object(server.MANAGEMENT, 'remove_virtual_chime') as protect:
            (status, body), log = post({'key': 'primary', 'confirmed': True})
        self.assertEqual(status, 200); self.assertFalse(body['audio_played'])
        finish.assert_called_once_with('primary'); protect.assert_not_called()
        self.assertIn('No Protect request was sent', log)

    def test_only_chimes_waiting_for_cleanup_can_be_confirmed(self):
        for entry in ({'active': True}, {'removal_pending': True}, {'removal_requested': True}):
            with self.subTest(entry=entry), patch.object(server.NATIVE, 'registry', self.registry(**entry)), \
                    patch.object(server.NATIVE, 'remove_from_protect') as finish:
                (status, body), _ = post({'key': 'primary', 'confirmed': True})
                self.assertEqual(status, 400); finish.assert_not_called()

    def test_requires_confirmation_valid_key_and_ingress(self):
        registry = self.registry(removal_requested=True, removal_pending=True)
        with patch.object(server.NATIVE, 'registry', registry), \
                patch.object(server.NATIVE, 'remove_from_protect') as finish:
            self.assertEqual(post({'key': 'primary'})[0][0], 400)
            self.assertEqual(post({'key': '../x', 'confirmed': True})[0][0], 400)
            self.assertEqual(post({'key': 'primary', 'confirmed': True}, peer='127.0.0.1')[0][0], 403)
        finish.assert_not_called()


class CleanupConfirmedRegistryTests(unittest.TestCase):
    def test_confirmed_cleanup_lets_the_bridge_erase_the_saved_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = ChimeRegistry(directory)
            registry.transfer(A, None, ROWS)
            entry = next(e for e in registry.snapshot() if e['active'])
            identity = registry.identity_dir(entry); identity.mkdir(parents=True, exist_ok=True)
            (identity / 'identity.json').write_text('{}')
            registry.request_removal(entry['key'], controller='10.0.0.1')
            worker = NativeWorker(); worker.registry = registry
            worker.remove_from_protect(entry['key'])
            removed = next(e for e in registry.snapshot() if e['key'] == entry['key'])
            self.assertEqual(removed['removal_source'], 'protect')
            self.assertNotIn('removal_pending', removed)
            registry.purge_removed(entry['key'])
            self.assertFalse(identity.exists())
            self.assertFalse(any(e['key'] == entry['key'] and e.get('entity_id') for e in registry.snapshot()))


if __name__ == '__main__':
    unittest.main()
