import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,AsyncMock,patch
from registry import ChimeRegistry
from native import NativeChime
from runtime import NativeWorker
from network import NetworkManager,NetworkError
import server

class IdentityCleanupTests(unittest.TestCase):
    def make_registry(self,root):
        registry=ChimeRegistry(root);entries=registry.sync(['media_player.a','media_player.b'])
        for entry in entries:
            directory=registry.identity_dir(entry);directory.mkdir(parents=True)
            for name in ['identity.json','device-key.pem','device-cert.pem']:(directory/name).write_text('old-private-material')
            tracks=registry.audio_dir(entry);tracks.mkdir(parents=True);(tracks/'custom.mp3').write_bytes(b'old sound')
        return registry,entries

    def test_confirmed_removal_erases_all_owned_identity_files_and_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            registry,entries=self.make_registry(Path(directory));worker=NativeWorker();worker.registry=registry
            other_before=(registry.identity_dir(entries[1])/'identity.json').read_bytes()
            registry.deactivate_from_protect('primary');worker.complete_removal('primary')
            self.assertFalse(registry.identity_dir(entries[0]).exists());self.assertFalse(registry.audio_dir(entries[0]).exists())
            self.assertEqual(registry.snapshot()[0],{'key':'primary','entity_id':None,'name':'Protect AnyChime','active':False})
            self.assertEqual((registry.identity_dir(entries[1])/'identity.json').read_bytes(),other_before)
            self.assertEqual(json.loads((registry.directory/'speakers.json').read_text())['entities'],['media_player.b'])
            worker.complete_removal('primary') # Idempotent after completion.
            registry.sync(['media_player.a','media_player.b'])
            self.assertFalse((registry.directory/'native/identity.json').exists())
            self.assertEqual(registry.selected_entities(),['media_player.a','media_player.b'])

    def test_readding_creates_new_mac_uuid_and_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);registry=ChimeRegistry(root);registry.sync(['media_player.a'])
            options={'native_ip':'10.23.45.20','controller_ip':'10.23.45.1','controller_fingerprint':'00'*32}
            cert=Path(__file__).with_name('controller-cert.pem')
            first=NativeChime(options,root/'native',cert);first.initialize_identity()
            old_mac=first.state['mac'];old_id=first.state['device_id'];old_key=(root/'native/device-key.pem').read_bytes()
            registry.deactivate_from_protect('primary');registry.purge_removed('primary');registry.sync(['media_player.a'])
            second=NativeChime(options,root/'native',cert);second.initialize_identity()
            self.assertNotEqual(second.state['mac'],old_mac);self.assertNotEqual(second.state['device_id'],old_id)
            self.assertNotEqual((root/'native/device-key.pem').read_bytes(),old_key)

    def test_active_or_unconfirmed_identity_is_not_purged(self):
        with tempfile.TemporaryDirectory() as directory:
            registry,entries=self.make_registry(Path(directory))
            with self.assertRaises(ValueError):registry.purge_removed('primary')
            registry.sync([])
            with self.assertRaises(ValueError):registry.purge_removed('primary')
            self.assertTrue((registry.directory/'native/device-key.pem').exists())

    def test_additional_identity_is_removed_without_leaving_history(self):
        with tempfile.TemporaryDirectory() as directory:
            registry,entries=self.make_registry(Path(directory));key=entries[1]['key']
            registry.deactivate_from_protect(key);registry.purge_removed(key)
            self.assertNotIn(key,[e['key'] for e in ChimeRegistry(directory).snapshot()])
            self.assertFalse(registry.identity_dir(entries[1]).exists());self.assertFalse(registry.audio_dir(entries[1]).exists())

    def test_failed_registry_write_retains_disabled_marker_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry,entries=self.make_registry(Path(directory));registry.deactivate_from_protect('primary')
            with patch.object(registry,'_save',side_effect=OSError('full')):
                with self.assertRaises(OSError):registry.purge_removed('primary')
            self.assertEqual(registry.snapshot()[0]['removal_source'],'protect')
            registry.purge_removed('primary');self.assertIsNone(registry.snapshot()[0]['entity_id'])

    def test_cleanup_refuses_symlinks_and_keeps_external_files(self):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as external:
            root=Path(directory);registry=ChimeRegistry(root);registry.sync(['media_player.a'])
            marker=Path(external)/'secret';marker.write_text('untouched');(root/'native').symlink_to(external,target_is_directory=True)
            registry.deactivate_from_protect('primary')
            with self.assertRaises(ValueError):registry.purge_removed('primary')
            self.assertEqual(marker.read_text(),'untouched')

    def test_history_cleanup_verifies_remote_before_erasing_local_history(self):
        with tempfile.TemporaryDirectory() as directory:
            registry,entries=self.make_registry(Path(directory));registry.sync(['media_player.a'])
            worker=Mock();worker.registry=registry;worker.selection_lock=__import__('threading').RLock()
            with patch.object(server,'NATIVE',worker),patch.object(server,'MANAGEMENT') as manager,patch.object(server,'load_options',return_value={'controller_ip':'10.23.45.1'}):
                manager.status.return_value={'ready':True}
                server.cleanup_old_chimes()
                manager.remove_virtual_chimes.assert_called_once_with('10.23.45.1',[entries[1]['key']],worker,allow_absent=True)
                worker.complete_removal.assert_not_called() # Manager owns confirmed teardown.

    def test_failed_selection_save_does_not_leave_inactive_memory_history(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory);registry.sync(['media_player.a'])
            with patch.object(registry,'_save',side_effect=OSError('full')):
                with self.assertRaises(OSError):registry.sync([])
            self.assertEqual(registry.selected_entities(),['media_player.a'])

    def test_network_state_is_erased_only_after_successful_owned_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=NetworkManager(Path(directory)/'network','10.23.45.20','10.23.45.1',runner=Mock())
            path=manager._directory('primary');path.mkdir(parents=True);(path/'config.json').write_text('old mac')
            with patch.object(manager,'stop',return_value={'state':'error'}):
                with self.assertRaises(NetworkError):manager.forget('primary')
            self.assertTrue(path.exists())
            with patch.object(manager,'stop',return_value={'state':'pending'}):manager.forget('primary')
            self.assertFalse(path.exists())

class AsyncCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_reset_ack_is_not_interrupted_and_cleanup_then_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory);registry.sync(['media_player.a']);registry.deactivate_from_protect('primary')
            worker=NativeWorker();worker.registry=registry;worker.network=Mock()
            pending=asyncio.create_task(asyncio.sleep(60));native=Mock();native.task=pending;native.stop=AsyncMock()
            player=Mock();worker.nodes={'primary':{'native':native,'player':player}}
            deferred=await worker._cleanup_confirmed();self.assertEqual(deferred,{'primary'});native.stop.assert_not_awaited()
            pending.cancel()
            try:await pending
            except asyncio.CancelledError:pass
            self.assertEqual(await worker._cleanup_confirmed(),set());native.stop.assert_awaited_once()
            self.assertIsNone(registry.snapshot()[0]['entity_id']);worker.network.forget.assert_called_once_with('primary')

    async def test_concurrent_cleanup_never_erases_newly_reused_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory);registry.sync(['media_player.a']);registry.deactivate_from_protect('primary');entry=registry.snapshot()[0]
            worker=NativeWorker();worker.registry=registry
            await asyncio.gather(worker._purge_removed_entry(entry),worker._purge_removed_entry(entry))
            registry.sync(['media_player.new']);(registry.directory/'native').mkdir();(registry.directory/'native/identity.json').write_text('new')
            await worker._purge_removed_entry(entry)
            self.assertEqual((registry.directory/'native/identity.json').read_text(),'new')
