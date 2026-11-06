"""All-stage removal and last-member moves, without real HA, Protect or audio."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from audio import SpeakerError
from registry import ChimeRegistry
from runtime import NativeWorker
import server

A,B='media_player.a','media_player.b'
ROWS=[{'entity_id':e,'name':e,'available':True,'supported':True,'integration':'sonos'} for e in (A,B)]

class ActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.worker=NativeWorker();self.worker.registry=ChimeRegistry(self.root)
        self.registry=self.worker.registry;self.registry.transfer(A,None,ROWS)
    def tearDown(self):self.temp.cleanup()
    def seed(self,key='primary',adopted=False):
        entry=next(e for e in self.registry.snapshot() if e['key']==key)
        directory=self.registry.identity_dir(entry);directory.mkdir(parents=True,exist_ok=True)
        path=directory/'identity.json';path.write_text(json.dumps({'adopted':adopted,'mac':'025253000001','device_id':'owned-fixture'}));return path
    def request(self,body,route='/api/chimes/members'):
        raw=json.dumps(body).encode();h=server.Handler.__new__(server.Handler);h.path=route;h.client_address=('172.30.32.2',1)
        h.headers={'X-AnyChime-Request':'1','Content-Length':str(len(raw))};h.rfile=io.BytesIO(raw);h.respond=Mock();h.do_POST();return h.respond.call_args.args
    def test_unstarted_removal_needs_no_ha_discovery_console_or_account(self):
        with patch.object(server,'NATIVE',self.worker),patch.object(server,'load_options') as options,patch.object(server.speakers,'discover') as discover,patch.object(server,'MANAGEMENT') as manager:
            status,_=self.request({'key':'primary','confirmed':True},'/api/chimes/remove')
        self.assertEqual(status,200);self.assertEqual(self.registry.selected_entities(),[])
        options.assert_not_called();discover.assert_not_called();manager.remove_virtual_chime.assert_not_called()
    def test_repeat_removal_after_completion_is_an_idempotent_success(self):
        self.worker.request_removal('primary')
        result=self.worker.request_removal('primary')
        self.assertTrue(result['removed_locally']);self.assertFalse(result['protect_cleanup_pending'])

    def test_x_removes_last_speaker_even_when_ha_discovery_is_unavailable(self):
        with patch.object(server,'NATIVE',self.worker),patch.object(server.speakers,'discover',side_effect=SpeakerError('offline')) as discover:
            status,_=self.request({'action':'remove','key':'primary','entity_id':A,'confirmed':True,'confirm_last':True})
        self.assertEqual(status,200);discover.assert_not_called();self.assertEqual(self.registry.selected_entities(),[])
    def test_remove_member_keeps_the_other_speaker_when_discovery_fails(self):
        self.registry.transfer(B,'primary',ROWS)
        with patch.object(server,'NATIVE',self.worker),patch.object(server.speakers,'discover',side_effect=SpeakerError('offline')):
            status,_=self.request({'action':'remove','key':'primary','entity_id':A,'confirmed':True})
        self.assertEqual(status,200);self.assertEqual(self.registry.selected_entities(),[B])
    def test_adopting_or_adopted_identity_is_unselected_but_retained_for_cleanup(self):
        for adopted in (False,True):
            with self.subTest(adopted=adopted):
                path=self.seed(adopted=adopted);original=path.read_bytes()
                result=self.worker.request_removal('primary')
                self.assertTrue(result['removed_locally']);self.assertTrue(result['protect_cleanup_pending'])
                self.assertEqual(path.read_bytes(),original);self.assertEqual(self.registry.selected_entities(),[])
                reloaded=ChimeRegistry(self.root);entry=reloaded.snapshot()[0]
                self.assertTrue(entry['removal_requested']);self.assertFalse(entry['active']);self.assertFalse(reloaded.migration.status(reloaded.snapshot())['required'])
    def test_readding_does_not_reuse_or_overwrite_a_pending_identity(self):
        path=self.seed();self.worker.request_removal('primary');self.registry.transfer(A,None,ROWS)
        active=next(e for e in self.registry.snapshot() if e['active'])
        self.assertNotEqual(active['key'],'primary');self.assertTrue(path.exists())
        self.registry.deactivate_from_protect('primary');self.registry.purge_removed('primary')
        self.assertEqual(self.registry.selected_entities(),[A]);self.assertTrue(next(e for e in self.registry.snapshot() if e['key']==active['key'])['active'])
    def test_last_move_is_atomic_preserves_destination_and_queues_only_source(self):
        self.seed();self.registry.transfer(B,None,ROWS);target=next(e for e in self.registry.snapshot() if e['active'] and e['key']!='primary')
        target_file=self.seed(target['key']);saved=target_file.read_bytes()
        self.registry.transfer(A,target['key'],ROWS,remove_empty=True)
        source=next(e for e in self.registry.snapshot() if e['key']=='primary')
        destination=next(e for e in self.registry.snapshot() if e['key']==target['key'])
        self.assertFalse(source['active']);self.assertTrue(source['removal_requested'])
        self.assertEqual(destination['members'],[B,A]);self.assertEqual(target_file.read_bytes(),saved)
        self.assertFalse(destination.get('removal_pending',False))
    def test_failed_move_write_leaves_both_chimes_unchanged(self):
        self.registry.transfer(B,None,ROWS);before=self.registry.snapshot();target=before[1]['key']
        with patch.object(self.registry,'_save',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):self.registry.transfer(A,target,ROWS,remove_empty=True)
        self.assertEqual(self.registry.snapshot(),before)
    def test_http_last_move_requires_confirmation_before_mutating_either_chime(self):
        self.seed();self.registry.transfer(B,None,ROWS);target=self.registry.snapshot()[1]['key'];before=self.registry.snapshot()
        body={'action':'move','entity_id':A,'target_key':target,'source_key':'primary','confirmed':True}
        with patch.object(server,'NATIVE',self.worker),patch.object(server.speakers,'discover',return_value=ROWS),patch.object(server,'MANAGEMENT') as manager:
            status,_=self.request(body);self.assertEqual(status,400);self.assertEqual(self.registry.snapshot(),before)
            status,_=self.request({**body,'confirm_source_removal':True,'source_key':'f'*24});self.assertEqual(status,400);self.assertEqual(self.registry.snapshot(),before)
            status,response=self.request({**body,'confirm_source_removal':True});self.assertEqual(status,200);self.assertEqual(response['cleanup_pending'],1)
            manager.remove_virtual_chime.assert_not_called()
        self.assertEqual(set(self.registry.selected_entities()),{A,B})
    def test_other_speaker_metadata_stays_live_during_pending_cleanup(self):
        self.seed();self.registry.transfer(B,None,ROWS);self.worker.request_removal('primary')
        self.worker.refresh_groups([{**r,'available':False} for r in ROWS])
        active=next(e for e in self.registry.snapshot() if e['active'])
        self.assertFalse(active['speaker_available'])

class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_cleanup_keeps_identity_then_confirmed_retry_purges_it(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=NativeWorker();worker.registry=ChimeRegistry(directory);r=worker.registry;r.transfer(A,None,ROWS)
            identity=Path(directory)/'native/identity.json';identity.parent.mkdir();identity.write_bytes(b'owned fixture identity')
            worker.request_removal('primary');worker.removal_handler=Mock(side_effect=OSError('private-upstream-text'))
            with patch('runtime.time.monotonic',return_value=100),patch('builtins.print') as log:
                await worker._process_requested_removals();await worker._process_requested_removals()
            worker.removal_handler.assert_called_once();self.assertTrue(identity.exists());self.assertEqual(r.selected_entities(),[])
            self.assertNotIn('private-upstream-text',log.call_args.args[0])
            def confirmed(key):r.deactivate_from_protect(key);r.purge_removed(key);return {'removed':True}
            worker.removal_handler.side_effect=confirmed
            with patch('runtime.time.monotonic',return_value=116):await worker._process_requested_removals()
            self.assertFalse(identity.exists());self.assertFalse(any(e.get('removal_pending') for e in r.snapshot()))
    async def test_pending_cleanup_never_runs_against_a_different_console(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=NativeWorker();worker.registry=ChimeRegistry(directory);worker.registry.transfer(A,None,ROWS)
            path=Path(directory)/'native/identity.json';path.parent.mkdir();path.write_bytes(b'owned fixture')
            worker.options={'controller_ip':'10.23.45.1'};worker.request_removal('primary')
            worker.options={'controller_ip':'10.23.45.2'};worker.removal_handler=Mock()
            await worker._process_requested_removals()
            worker.removal_handler.assert_not_called();self.assertTrue(path.exists())
            self.assertEqual(worker.registry.selected_entities(),[])

    async def test_no_account_defers_remote_cleanup_without_readding_the_speaker(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=NativeWorker();worker.registry=ChimeRegistry(directory);worker.registry.transfer(A,None,ROWS)
            path=Path(directory)/'native/identity.json';path.parent.mkdir();path.write_bytes(b'private fixture')
            worker.request_removal('primary');worker.removal_handler=Mock(return_value={'retry_later':True})
            await worker._process_requested_removals()
            self.assertTrue(path.exists());self.assertEqual(worker.registry.selected_entities(),[])
