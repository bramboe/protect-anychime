"""Fixed membership and migration with synthetic identities and fake Protect only."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from registry import ChimeRegistry
from runtime import NativeWorker
from manual_timing import ManualTiming
import migration
import server

A,B,C,D=[f'media_player.{c}' for c in 'abcd']
ROWS=[{'entity_id':e,'name':e.upper(),'available':True,'supported':True,'integration':'sonos','ha_device_id':str(i)*32,'group':[A,B],'features':1049092} for i,e in enumerate([A,B,C,D])]

class FixedChimeTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.r=ChimeRegistry(self.root)
    def tearDown(self):self.temp.cleanup()
    def seed(self,entry):
        path=self.r.identity_dir(entry);path.mkdir(parents=True,exist_ok=True);(path/'identity.json').write_bytes(b'synthetic stable identity')
        sounds=self.r.audio_dir(entry);sounds.mkdir(parents=True,exist_ok=True);(sounds/'stored').write_bytes(b'synthetic ringtone')
    def test_ha_group_changes_do_not_create_merge_or_split_chimes(self):
        self.r.sync([A,B],ROWS);before=self.r.snapshot()
        self.assertEqual(len([e for e in before if e['active']]),2)
        self.r.sync([A,B],[{**r,'group':[B,A,C]} for r in ROWS],user_change=False)
        self.assertEqual(self.r.snapshot(),before)
    def test_add_remove_move_and_leader_removal_keep_both_chime_identities(self):
        self.r.transfer(A,None,ROWS);self.r.transfer(B,'primary',ROWS);self.r.transfer(C,None,ROWS)
        entries=[e for e in self.r.snapshot() if e['active']]
        for e in entries:self.seed(e)
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.name in ('identity.json','stored')}
        dest=entries[1]['key'];self.r.transfer(B,dest,ROWS)
        self.r.transfer(D,'primary',ROWS);self.r.detach('primary',A,ROWS)
        active=[e for e in self.r.snapshot() if e['active']]
        self.assertEqual([e['key'] for e in active],[e['key'] for e in entries])
        self.assertEqual(active[0]['members'],[D]);self.assertEqual(active[1]['members'],[C,B])
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.assertEqual(ChimeRegistry(self.root).snapshot(),self.r.snapshot())
    def test_moving_or_detaching_the_last_member_is_refused_without_changes(self):
        self.r.transfer(A,None,ROWS);self.r.transfer(B,None,ROWS);before=self.r.snapshot()
        with self.assertRaisesRegex(ValueError,'last speaker'):self.r.transfer(A,before[1]['key'],ROWS)
        with self.assertRaisesRegex(ValueError,'last speaker'):self.r.detach('primary',A,ROWS)
        self.assertEqual(self.r.snapshot(),before)
    def test_failed_write_rolls_back_both_sides_of_a_move(self):
        self.r.transfer(A,None,ROWS);self.r.transfer(B,'primary',ROWS);self.r.transfer(C,None,ROWS);before=self.r.snapshot()
        with patch.object(self.r,'_save',side_effect=OSError('Synthetic full disk')):
            with self.assertRaises(OSError):self.r.transfer(B,before[1]['key'],ROWS)
        self.assertEqual(self.r.snapshot(),before);self.assertEqual(ChimeRegistry(self.root).snapshot(),before)
    def test_offline_add_rejected_existing_members_retained(self):
        self.r.transfer(A,None,ROWS)
        rows=[{**r,'available':False} for r in ROWS]
        self.r.sync([A],rows,user_change=False);self.assertEqual(self.r.selected_entities(),[A])
        with self.assertRaises(ValueError):self.r.transfer(B,'primary',rows)
    def test_manual_values_remain_valid_with_entity_fallback(self):
        timing=ManualTiming(self.root);timing.save({'a'*32:25,B:70})
        self.assertEqual(ManualTiming(self.root).read(),{'a'*32:25,B:70})
        d=timing.describe({'members':[A,B]},[{'entity_id':A,'name':'A','ha_device_id':'a'*32},{'entity_id':B,'name':'B'}])
        self.assertEqual([m['delay_ms'] for m in d['members']],[25,70])

class UpgradeTests(unittest.TestCase):
    def test_0349_sets_identity_ringtones_and_delays_survive_cleanup_and_second_start(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);entry={'key':'primary','entity_id':A,'name':'Original set','active':True,'members':[A,B]}
            (root/'chimes.json').write_text(json.dumps([entry]));(root/'native').mkdir();(root/'native/identity.json').write_bytes(b'identity');(root/'ringtones').mkdir();(root/'ringtones/track').write_bytes(b'ringtone')
            (root/'chime-layout.json').write_text(json.dumps({'separate_groups':[[A,B]],'pending':None}));(root/'latency-calibration.json').write_text('{}');(root/'latency-cache').mkdir();(root/'latency-cache/file.wav').write_bytes(b'cache')
            ManualTiming(root).save({'a'*32:40});r=ChimeRegistry(root)
            self.assertEqual(r.snapshot()[0],entry)
            self.assertEqual((root/'native/identity.json').read_bytes(),b'identity');self.assertEqual((root/'ringtones/track').read_bytes(),b'ringtone')
            self.assertEqual(ManualTiming(root).read(),{'a'*32:40})
            self.assertFalse((root/'chime-layout.json').exists());self.assertFalse((root/'latency-cache').exists());self.assertFalse((root/'latency-calibration.json').exists())
            self.assertEqual(ChimeRegistry(root).snapshot(),r.snapshot())
    def test_unfinished_removing_and_committing_changes_hold_identities_until_review(self):
        for phase in ('removing','committing'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as d:
                root=Path(d);entry={'key':'primary','entity_id':A,'name':'A','active':True,'members':[A,B]}
                (root/'chimes.json').write_text(json.dumps([entry]));(root/'native').mkdir();identity=root/'native/identity.json';identity.write_bytes(b'keep pending identity')
                (root/'chime-layout.json').write_text(json.dumps({'separate_groups':[],'pending':{'members':[A,B],'keys':['primary'],'phase':phase,'separate':True,'entities':[A,B]}}))
                r=ChimeRegistry(root);worker=NativeWorker();worker.registry=r
                self.assertTrue(r.migration.status(r.snapshot())['required']);self.assertFalse(worker._entry_enabled('primary'))
                self.assertEqual(identity.read_bytes(),b'keep pending identity')
                with self.assertRaises(ValueError):r.migration.finish(r.snapshot())
                self.assertTrue((root/'chime-layout.json').exists());self.assertTrue(ChimeRegistry(root).snapshot()[0]['migration_hold'])
                # Simulate only an already confirmed Protect removal, then explicit finish.
                r.deactivate_from_protect('primary');r.purge_removed('primary');r.migration.finish(r.snapshot())
                self.assertFalse(r.migration.status(r.snapshot())['required']);self.assertFalse((root/'chime-layout.json').exists())
    def test_pending_removal_flag_and_identity_are_not_cleared_on_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);entry={'key':'primary','entity_id':A,'name':'A','active':True,'members':[A],'removal_pending':True}
            (root/'chimes.json').write_text(json.dumps([entry]));r=ChimeRegistry(root)
            self.assertTrue(r.snapshot()[0]['removal_pending']);worker=NativeWorker();worker.registry=r;self.assertFalse(worker._entry_enabled('primary'))

class MembershipHTTPTests(unittest.TestCase):
    def handler(self,body,path='/api/chimes/members',source='172.30.32.2'):
        h=server.Handler.__new__(server.Handler);h.path=path;h.client_address=(source,1);raw=json.dumps(body).encode();h.headers={'Content-Length':str(len(raw)),'X-Prototype-Test':'silent'};h.rfile=io.BytesIO(raw);h.respond=Mock();return h
    def test_last_member_requires_explicit_confirmation_and_existing_protect_flow(self):
        with tempfile.TemporaryDirectory() as d:
            worker=NativeWorker();worker.registry=ChimeRegistry(d);worker.registry.transfer(A,None,ROWS)
            body={'confirmed':True,'action':'remove','key':'primary','entity_id':A}
            with patch.object(server,'NATIVE',worker),patch('server.speakers.discover',return_value=ROWS),patch('server.load_options',return_value={'controller_ip':'10.23.45.1'}),patch.object(server,'MANAGEMENT') as manager:
                h=self.handler(body);h.do_POST();self.assertEqual(h.respond.call_args.args[0],400);manager.remove_virtual_chime.assert_not_called()
                h=self.handler({**body,'confirm_last':True});h.do_POST();self.assertEqual(h.respond.call_args.args[0],200)
                manager.remove_virtual_chime.assert_called_once_with('10.23.45.1','primary',worker)
    def test_member_add_and_detach_never_invoke_protect_or_audio(self):
        with tempfile.TemporaryDirectory() as d:
            worker=NativeWorker();worker.registry=ChimeRegistry(d);worker.registry.transfer(A,None,ROWS)
            with patch.object(server,'NATIVE',worker),patch('server.speakers.discover',return_value=ROWS),patch.object(server,'MANAGEMENT') as manager:
                for body in [{'action':'add','target_key':'primary','entity_id':B},{'action':'remove','key':'primary','entity_id':A}]:
                    h=self.handler({**body,'confirmed':True});h.do_POST();self.assertEqual(h.respond.call_args.args[0],200)
                self.assertEqual(manager.mock_calls,[])
    def test_loopback_and_unconfirmed_writes_are_refused(self):
        for source,body in [('127.0.0.1',{'confirmed':True}),('172.30.32.2',{})]:
            h=self.handler(body,source=source)
            with patch.object(server,'NATIVE') as worker:h.do_POST();self.assertIn(h.respond.call_args.args[0],(400,403));worker.registry.transfer.assert_not_called()
    def test_removed_endpoints_return_not_found(self):
        for path in ['/api/diagnostics/rings','/api/calibration/clock','/api/calibration/status','/media/calibration/'+'a'*32+'.mp3','/media/delayed/'+'a'*32+'/50.wav']:
            h=self.handler({},path=path);h.command='GET';h.do_GET();self.assertEqual(h.respond.call_args.args[0],404)

class PlaybackPlanTests(unittest.TestCase):
    def play(self,states,platforms=None,delays=None,fail=False,barrier=None):
        from audio import SpeakerBackend,SpeakerError
        import threading
        calls=[]
        def request(method,path,body):
            calls.append((method,path,body))
            if method=='GET':return states[path[len('/states/'):]]
            if path.endswith('/play_media') and barrier:barrier.wait(timeout=2)
            if fail:raise SpeakerError('Synthetic transport failure')
            return []
        members=list(states)[:2]
        backend=SpeakerBackend({'speaker_entity':members[0],'audio_url':'http://example.test/media/chimes/primary/1/'+'a'*32+'.mp3','group_members':members,'member_platforms':platforms or {},'member_delays':delays or {}},requester=request,allow_playback=True)
        grant=backend.arm_once(confirmed=True,volume=10)
        return backend,grant,calls
    def state(self,entity,*,features=1049092,group=None,state='idle',muted=False):
        return {'entity_id':entity,'state':state,'attributes':{'supported_features':features,'group_members':group or [entity],'volume_level':.1,'is_volume_muted':muted}}
    def test_parallel_individual_sonos_calls_keep_announce_and_requested_volume(self):
        import threading
        states={e:self.state(e,group=[A,B]) for e in [A,B]}
        backend,grant,calls=self.play(states,{e:'sonos' for e in states},{A:20,B:0},barrier=threading.Barrier(2))
        result=backend.play_once(grant['approval_id']);posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(result['effective_requests'],2);self.assertEqual(len(posts),2)
        self.assertTrue(all(c[2]['announce'] and c[2]['extra']=={'volume':10} for c in posts))
        self.assertEqual({c[2]['entity_id'] for c in posts},{A,B})
        self.assertTrue(all(c[1]=='/services/media_player/play_media' for c in posts))
    def test_non_sonos_music_group_deduplicates_to_one_coordinator(self):
        states={e:self.state(e,group=[A,B]) for e in [A,B]}
        backend,grant,calls=self.play(states,{A:'cast',B:'cast'},{A:200,B:400})
        result=backend.play_once(grant['approval_id']);posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(result['effective_requests'],1);self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][2]['entity_id'],A);self.assertNotIn('ac_sync',posts[0][2]['media_content_id'])
    def test_sonos_without_announcement_uses_normal_group_route_once(self):
        states={e:self.state(e,features=516,group=[A,B]) for e in [A,B]}
        backend,grant,calls=self.play(states,{A:'sonos',B:'sonos'})
        result=backend.play_once(grant['approval_id'])
        plays=[c for c in calls if c[1].endswith('/play_media')]
        self.assertEqual(len(plays),1);self.assertNotIn('announce',plays[0][2]);self.assertTrue(result['may_interrupt'])
    def test_nonannouncement_sonos_member_keeps_normal_route_when_coordinator_is_not_selected(self):
        from audio import SpeakerBackend
        calls=[]
        states={B:self.state(B,features=516,group=[A,B]),A:self.state(A,group=[A,B])}
        def request(method,path,body):
            calls.append((method,path,body))
            return states[path[len('/states/'):]] if method=='GET' else []
        backend=SpeakerBackend({'speaker_entity':B,'audio_url':'http://example.test/ring.mp3','group_members':[B],'member_platforms':{B:'sonos'}},requester=request,allow_playback=True)
        backend.play_once(backend.arm_once(confirmed=True,volume=10)['approval_id'])
        plays=[c for c in calls if c[1].endswith('/play_media')]
        self.assertEqual(plays[0][2]['entity_id'],A);self.assertNotIn('announce',plays[0][2])

    def test_unavailable_first_member_does_not_block_online_other_member(self):
        states={A:self.state(A,state='unavailable'),B:self.state(B)}
        backend,grant,calls=self.play(states,{A:'sonos',B:'sonos'})
        backend.play_once(grant['approval_id'])
        self.assertEqual([c[2]['entity_id'] for c in calls if c[1].endswith('/play_media')],[B])
    def test_muted_member_skipped_and_no_ha_group_services_ever_called(self):
        states={A:self.state(A,muted=True),B:self.state(B)}
        backend,grant,calls=self.play(states,{A:'sonos',B:'sonos'})
        backend.play_once(grant['approval_id'])
        self.assertTrue(all(path.startswith('/states/') or path=='/services/media_player/play_media' for _,path,_ in calls))
        self.assertEqual([c[2]['entity_id'] for c in calls if c[0]=='POST'],[B])
    def test_failure_consumes_grant_without_plain_playback_retry(self):
        from audio import SpeakerError
        states={e:self.state(e) for e in [A,B]};backend,grant,calls=self.play(states,{A:'sonos',B:'sonos'},fail=True)
        with self.assertRaises(SpeakerError):backend.play_once(grant['approval_id'])
        before=len(calls)
        with self.assertRaises(SpeakerError):backend.play_once(grant['approval_id'])
        self.assertEqual(len(calls),before);self.assertTrue(all(c[2].get('announce') for c in calls if c[0]=='POST'))

class LegacyArchiveTests(unittest.TestCase):
    def archive(self,pending=False):
        import zipfile
        entry={'key':'primary','entity_id':A,'name':'Original','members':[A,B],'active':True}
        files={'managed-settings.json':b'{}','speakers.json':json.dumps({'entities':[A,B]}).encode(),'chimes.json':json.dumps([entry]).encode(),
            'native/identity.json':b'synthetic identity','latency-delays.json':json.dumps({'a'*32:30}).encode(),
            'latency-calibration.json':b'{}','latency-cache/old.wav':b'fake cache','chime-layout.json':json.dumps({'separate_groups':[[A,B]],'pending':{'members':[A,B],'keys':['primary']} if pending else None}).encode()}
        manifest={'format':'unifi-anychime-migration','version':1,'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as z:
            z.writestr('migration.json',json.dumps(manifest))
            for name,raw in files.items():z.writestr(name,raw)
        return output.getvalue()
    def test_old_archive_drops_obsolete_files_but_preserves_identity_and_offsets(self):
        with tempfile.TemporaryDirectory() as d:
            migration.import_state(self.archive(),d);root=Path(d);r=ChimeRegistry(root)
            self.assertEqual((root/'native/identity.json').read_bytes(),b'synthetic identity')
            self.assertEqual(ManualTiming(root).read(),{'a'*32:30})
            self.assertFalse((root/'latency-cache').exists());self.assertFalse((root/'latency-calibration.json').exists());self.assertFalse((root/'chime-layout.json').exists())
            exported=migration.inspect_archive(migration.export_state(root,{},r.selected_entities()))
            self.assertNotIn('chime-layout.json',exported);self.assertIn('chime-schema.json',exported)
    def test_old_archive_pending_change_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            migration.import_state(self.archive(True),d);root=Path(d);r=ChimeRegistry(root)
            self.assertTrue(r.migration.status(r.snapshot())['required']);self.assertTrue(r.snapshot()[0]['migration_hold'])
            self.assertEqual((root/'native/identity.json').read_bytes(),b'synthetic identity')
            self.assertFalse((root/'chime-layout.json').exists())

class RemovalResumeTests(unittest.TestCase):
    def test_retry_of_pending_remove_can_confirm_remote_absence_without_new_identity(self):
        with tempfile.TemporaryDirectory() as d:
            worker=NativeWorker();worker.registry=ChimeRegistry(d);worker.registry.transfer(A,None,ROWS)
            worker.registry.mark_removal_pending('primary')
            with patch.object(server,'NATIVE',worker),patch.object(server,'MANAGEMENT') as manager:
                server.remove_confirmed_chime('10.23.45.1','primary')
                manager.remove_virtual_chime.assert_called_once_with('10.23.45.1','primary',worker,allow_absent=True)
                self.assertTrue(worker.registry.snapshot()[0]['removal_pending'])
    def test_stale_inactive_identity_is_held_for_explicit_review_not_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);key='f'*24;entry={'key':key,'entity_id':A,'members':[A],'name':'Old','active':False}
            (root/'chimes.json').write_text(json.dumps([entry]));path=root/'identities'/key;path.mkdir(parents=True);(path/'identity.json').write_bytes(b'keep')
            r=ChimeRegistry(root)
            self.assertTrue(r.migration.status(r.snapshot())['required']);self.assertTrue(r.snapshot()[0]['migration_hold'])
            self.assertEqual((path/'identity.json').read_bytes(),b'keep')
    def test_legacy_options_selection_is_loaded_without_changing_primary_identity(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'native').mkdir();(root/'native/identity.json').write_bytes(b'legacy identity')
            worker=NativeWorker()
            with patch('runtime.threading.Thread') as thread:
                worker.start({'speaker_entities':[A]},str(root/'native'))
                thread.return_value.start.assert_called_once()
            self.assertEqual(worker.registry.selected_entities(),[A])
            self.assertEqual((root/'native/identity.json').read_bytes(),b'legacy identity')

class ImmediatePlayerUpdateTests(unittest.TestCase):
    def test_membership_changes_reach_existing_player_before_test_can_be_requested(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            worker=NativeWorker();worker.registry=ChimeRegistry(d);worker.registry.transfer(A,None,ROWS)
            player=Mock();player.lock=threading.Lock();native=Mock();native.options={}
            worker.nodes={'primary':{'player':player,'native':native}}
            worker.registry.transfer(B,'primary',ROWS);worker.refresh_players()
            self.assertEqual(player.group_members,(A,B));self.assertEqual(player.entity_id,A)
            self.assertEqual(player.member_device_ids[B],ROWS[1]['ha_device_id'])
