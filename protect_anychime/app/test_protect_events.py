"""Only confirmed external changes create bounded notification events."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from runtime import NativeWorker
from registry import ChimeRegistry

class ProtectEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_observation_is_quiet_then_changes_emit_once(self):
        worker=NativeWorker();state={'_device_id':'own','protect_adopted':False,'protect_configured':False,'_settings_revision':'one'}
        worker.configuration_checker=Mock(side_effect=lambda:{'primary':dict(state)})
        async def poll(now):
            with patch('runtime.time.monotonic',return_value=now):await worker._check_configuration()
        await poll(100);self.assertEqual(worker.status()['protect_events']['items'],[])
        state['protect_adopted']=True;await poll(130)
        state['protect_configured']=True;await poll(160)
        state['_settings_revision']='two';await poll(190);await poll(220)
        state['protect_configured']=False;await poll(250)
        self.assertEqual([e['type'] for e in worker.status()['protect_events']['items']],['adopted','configured','settings_changed','unconfigured'])
        worker.configuration_checker.side_effect=TimeoutError()
        with patch('runtime.time.monotonic',return_value=280),patch('builtins.print'):await worker._check_configuration()
        self.assertEqual(len(worker.status()['protect_events']['items']),4)

    async def test_new_identity_and_internal_fields_do_not_leak_or_emit_changes(self):
        worker=NativeWorker();worker._configuration_state={'primary':{'_device_id':'old','protect_configured':False}}
        worker.configuration_checker=Mock(return_value={'primary':{'_device_id':'new','_settings_revision':'hash','protect_configured':True}})
        worker._statuses=[{'key':'primary','device_id':'new'}]
        await worker._check_configuration()
        self.assertEqual(worker.status()['protect_events']['items'],[])
        self.assertNotIn('_settings_revision',worker.status()['chimes'][0])

    async def test_removal_is_emitted_once_only_for_external_active_chime(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=NativeWorker();worker.registry=ChimeRegistry(Path(directory))
            row={'entity_id':'media_player.example','name':'Example','available':True,'supported':True,'integration':'sonos'}
            worker.registry.transfer(row['entity_id'],None,[row])
            with patch('builtins.print'):
                worker.remove_from_protect('primary');worker.remove_from_protect('primary')
            self.assertEqual([e['type'] for e in worker.status()['protect_events']['items']],['removed'])
            self.assertEqual(worker.registry.selected_entities(),[])
            worker.registry.transfer(row['entity_id'],None,[row]);entry=next(e for e in worker.registry.snapshot() if e['active'])
            worker.registry.request_removal(entry['key'])
            with patch('builtins.print'):worker.remove_from_protect(entry['key'])
            self.assertEqual(len(worker.status()['protect_events']['items']),1)

    async def test_event_buffer_is_bounded_and_sessions_differ(self):
        worker=NativeWorker()
        for _ in range(75):worker._protect_event('primary','settings_changed')
        events=worker.status()['protect_events'];self.assertEqual(len(events['items']),50)
        self.assertEqual(events['items'][0]['id'],26)
        self.assertNotEqual(events['session'],NativeWorker().status()['protect_events']['session'])
