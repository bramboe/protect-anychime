"""Chime regrouping uses fake Protect deletion and local synthetic identities only."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from chime_layout import change,members
from registry import ChimeRegistry
from runtime import NativeWorker
from management import ManagementError
import speakers
import server

GROUP=['media_player.a','media_player.b']
ROWS=[{'entity_id':e,'name':e,'group':GROUP if e in GROUP else [],'available':True,'integration':'sonos'}
    for e in GROUP+['media_player.office']]

class FakeProtect:
    def __init__(self):self.deleted=[];self.fail_after=None;self.ready=True
    def status(self,host):return {'ready':self.ready}
    def check_owned_chimes(self,host,keys,worker):return {'checked':True,'audio_played':False}
    def remove_virtual_chimes(self,host,keys,worker,**kwargs):
        for key in keys:
            if self.fail_after is not None and len(self.deleted)>=self.fail_after:raise ManagementError('Synthetic Protect failure')
            self.deleted.append(key)
            worker.remove_from_protect(key);worker.complete_removal(key)
        return {'removed_keys':keys,'audio_played':False}

class ChimeLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.worker=NativeWorker()
        self.worker.registry=ChimeRegistry(self.temp.name);self.manager=FakeProtect()
        self.worker.registry.sync(GROUP+['media_player.office'],ROWS)
        self.seed()
    def tearDown(self):self.temp.cleanup()
    def seed(self):
        for e in self.active():
            p=self.worker.registry.identity_dir(e);p.mkdir(parents=True,exist_ok=True)
            (p/'identity.json').write_text(json.dumps({'synthetic_old_identity':e['key']}))
    def active(self):return [e for e in self.worker.registry.snapshot() if e['active']]
    def group_entries(self):return [e for e in self.active() if set(GROUP).intersection(e.get('members',[]))]
    def convert(self,separate):return change(self.worker,self.manager,'10.23.45.1',GROUP,separate,ROWS)
    def test_separate_and_combine_remove_old_identities_and_preserve_ha_and_unrelated_chime(self):
        before=copy.deepcopy(ROWS);office=next(e for e in self.active() if e['entity_id']=='media_player.office')
        office_path=self.worker.registry.identity_dir(office)/'identity.json';office_identity=office_path.read_bytes()
        old_group=self.group_entries()[0]['key']
        result=self.convert(True)
        self.assertTrue(result['adoption_required']);self.assertFalse(result['audio_played'])
        self.assertEqual(self.manager.deleted,[old_group])
        self.assertEqual(sorted(e['members'] for e in self.group_entries()),[[GROUP[0]],[GROUP[1]]])
        self.assertTrue(all(not (self.worker.registry.identity_dir(e)/'identity.json').exists() for e in self.group_entries()))
        self.assertEqual(office_path.read_bytes(),office_identity)
        self.assertEqual(ROWS,before)
        self.seed();individuals=[e['key'] for e in self.group_entries()]
        self.convert(False)
        self.assertEqual(set(self.manager.deleted[1:]),set(individuals))
        self.assertEqual(self.group_entries()[0]['members'],GROUP)
        self.assertEqual(len(self.group_entries()),1)
        self.assertFalse((self.worker.registry.identity_dir(self.group_entries()[0])/'identity.json').exists())
        self.assertEqual(office_path.read_bytes(),office_identity)
        self.assertIsNone(self.worker.registry.layouts.pending)
    def test_separation_survives_restart_refresh_and_individual_deselection(self):
        self.convert(True)
        restarted=ChimeRegistry(self.temp.name)
        restarted.sync(restarted.selected_entities(),ROWS,user_change=False)
        active=[e for e in restarted.snapshot() if e['active'] and e['entity_id'] in GROUP]
        self.assertEqual(len(active),2)
        decorated=restarted.layouts.decorate(ROWS)
        selected=speakers.validate_selection([GROUP[0],'media_player.office'],decorated,existing=GROUP)
        self.assertNotIn(GROUP[1],selected)
        self.assertEqual([r['group'] for r in decorated[:2]],[GROUP,GROUP])
    def test_failed_first_removal_never_creates_replacements_and_can_resume(self):
        self.manager.fail_after=0
        with self.assertRaises(ManagementError):self.convert(True)
        self.assertEqual(len(self.group_entries()),1)
        self.assertFalse(self.worker._entry_enabled(self.group_entries()[0]['key']))
        before=self.worker.registry.snapshot();self.worker.refresh_groups(ROWS)
        self.assertEqual(self.worker.registry.snapshot(),before)
        self.assertEqual(self.worker.registry.layouts.pending['phase'],'removing')
        with self.assertRaises(ValueError):self.worker.registry.sync([],ROWS)
        self.worker.registry=ChimeRegistry(self.temp.name);self.manager.fail_after=None
        self.convert(True);self.assertEqual(len(self.group_entries()),2)
    def test_partial_multi_chime_removal_is_resumable_without_recreating_deleted_chime(self):
        self.convert(True);self.seed();self.manager.deleted=[];self.manager.fail_after=1
        with self.assertRaises(ManagementError):self.convert(False)
        self.assertEqual(len(self.group_entries()),1)
        self.assertFalse(self.worker._entry_enabled(self.group_entries()[0]['key']))
        self.worker.registry=ChimeRegistry(self.temp.name);self.manager.fail_after=None
        self.convert(False)
        self.assertEqual(len(self.manager.deleted),2)
        self.assertEqual(len(set(self.manager.deleted)),2)
        self.assertEqual(len(self.group_entries()),1)
        self.assertEqual(self.group_entries()[0]['members'],GROUP)
    def test_restart_during_commit_never_deletes_the_new_identity_reusing_an_old_key(self):
        with patch.object(self.worker.registry.layouts,'finish',side_effect=OSError('Synthetic persistence failure')):
            with self.assertRaises(OSError):self.convert(True)
        deleted=list(self.manager.deleted)
        self.assertEqual(self.worker.registry.layouts.pending['phase'],'committing')
        self.assertTrue(all(not self.worker._entry_enabled(e['key']) for e in self.group_entries()))
        self.worker.registry=ChimeRegistry(self.temp.name)
        self.convert(True)
        self.assertEqual(self.manager.deleted,deleted)
        self.assertEqual(len(self.group_entries()),2)
    def test_missing_management_account_does_not_pause_or_modify_any_chime(self):
        self.manager.ready=False;before=self.worker.registry.snapshot()
        with self.assertRaises(ValueError):self.convert(True)
        self.assertIsNone(self.worker.registry.layouts.pending)
        self.assertEqual(self.worker.registry.snapshot(),before)
        self.assertEqual(self.manager.deleted,[])
    def test_unchanged_mode_is_idempotent_and_invalid_groups_are_rejected(self):
        result=self.convert(False)
        self.assertFalse(result['adoption_required']);self.assertEqual(self.manager.deleted,[])
        for group in [None,[GROUP[0]],[[GROUP[0]],GROUP[1]],['light.example',GROUP[1]]]:
            with self.assertRaises(ValueError):members(group)
        with self.assertRaises(ValueError):change(self.worker,self.manager,'10.23.45.1',[GROUP[0],'media_player.office'],True,ROWS)
        self.assertIsNone(self.worker.registry.layouts.pending)
    def test_layout_file_is_private_and_metadata_carries_only_chime_override(self):
        self.convert(True)
        self.assertEqual(self.worker.registry.layouts.path.stat().st_mode & 0o077,0)
        decorated=self.worker.registry.layouts.decorate(ROWS)
        self.assertTrue(all(r['chime_separate'] for r in decorated[:2]))
        self.assertEqual(decorated[0]['ha_group_members'],GROUP)
        self.assertEqual(ROWS[0]['group'],GROUP)
    def test_layout_endpoint_requires_confirmed_ingress_before_discovery(self):
        for source,confirmed in [('127.0.0.1',True),('10.23.45.50',True),('172.30.32.2',False)]:
            h=server.Handler.__new__(server.Handler);h.client_address=(source,123);h.path='/api/chimes/layout'
            body=json.dumps({'members':GROUP,'separate':True,'confirmed':confirmed}).encode()
            h.headers={'X-Prototype-Test':'silent','Content-Length':str(len(body)),'X-Forwarded-For':'172.30.32.2'}
            h.rfile=io.BytesIO(body);h.respond=Mock()
            with patch('server.chime_layout.change') as mutate,patch('server.speakers.discover') as discover:h.do_POST()
            self.assertIn(h.respond.call_args.args[0],(400,403));mutate.assert_not_called();discover.assert_not_called()
    def test_old_inactive_identity_cannot_be_reused_as_a_replacement(self):
        registry=self.worker.registry
        old={'key':'f'*24,'entity_id':GROUP[0],'members':[GROUP[0]],'name':'Old synthetic speaker','active':False}
        registry.entries.append(old);registry._save()
        path=registry.identity_dir(old);path.mkdir(parents=True);(path/'identity.json').write_text('{"old":true}')
        self.convert(True)
        self.assertIn(old['key'],self.manager.deleted)
        self.assertNotIn(old['key'],[e['key'] for e in registry.snapshot()])
        self.assertFalse(path.exists())
    def test_failed_read_only_preflight_preserves_working_chimes_and_preferences(self):
        before=self.worker.registry.snapshot()
        with patch.object(self.manager,'check_owned_chimes',side_effect=ManagementError('Synthetic account check failure')):
            with self.assertRaises(ManagementError):self.convert(True)
        self.assertIsNone(self.worker.registry.layouts.pending)
        self.assertEqual(self.worker.registry.snapshot(),before)
        self.assertEqual(self.manager.deleted,[])


class AvailableGroupLayoutTests(unittest.TestCase):
    setUp=ChimeLayoutTests.setUp
    tearDown=ChimeLayoutTests.tearDown
    seed=ChimeLayoutTests.seed
    active=ChimeLayoutTests.active
    group_entries=ChimeLayoutTests.group_entries
    convert=ChimeLayoutTests.convert
    def prepare_partial(self,entity):
        self.worker.registry=ChimeRegistry(Path(self.temp.name)/'partial')
        registry=self.worker.registry;registry.layouts.set_mode(GROUP,True)
        registry.sync(['media_player.office'],ROWS)
        registry.sync(['media_player.office',entity],ROWS)
        self.seed()
        return self.group_entries()[0]

    def test_unused_group_can_separate_and_restore_without_account_or_chime_creation(self):
        self.worker.registry=ChimeRegistry(Path(self.temp.name)/'unused')
        self.manager.ready=False;before=self.worker.registry.snapshot()
        for separate in (True,False):
            result=self.convert(separate)
            self.assertTrue(result['preference_only']);self.assertFalse(result['adoption_required'])
            self.assertEqual(result['selected'],[])
            self.assertEqual(self.worker.registry.snapshot(),before)
            self.assertEqual(self.worker.registry.layouts.separate(GROUP),separate)
        self.assertEqual(self.manager.deleted,[])
        self.assertFalse((self.worker.registry.directory/'native').exists())

    def test_partial_group_preserves_existing_nonprimary_chime_for_either_member(self):
        for entity in GROUP:
            with self.subTest(entity=entity):
                self.temp.cleanup();self.temp=tempfile.TemporaryDirectory()
                entry=self.prepare_partial(entity);registry=self.worker.registry
                self.assertNotEqual(entry['key'],'primary')
                identity=registry.identity_dir(entry)/'identity.json';original=identity.read_bytes()
                audio=registry.audio_dir(entry);audio.mkdir(parents=True);(audio/'stored.mp3').write_bytes(b'synthetic ringtone')
                office=next(e for e in self.active() if e['entity_id']=='media_player.office')
                office_before=(registry.identity_dir(office)/'identity.json').read_bytes()
                self.manager.ready=False
                result=self.convert(False)
                self.assertFalse(result['adoption_required']);self.assertEqual(result['preserved_key'],entry['key'])
                restored=self.group_entries();self.assertEqual(len(restored),1)
                self.assertEqual(restored[0]['key'],entry['key']);self.assertEqual(restored[0]['entity_id'],entity)
                self.assertEqual(restored[0]['members'],GROUP)
                self.assertEqual(identity.read_bytes(),original)
                self.assertEqual((audio/'stored.mp3').read_bytes(),b'synthetic ringtone')
                self.assertEqual((registry.identity_dir(office)/'identity.json').read_bytes(),office_before)
                self.assertEqual(self.manager.deleted,[])
                self.worker.registry=ChimeRegistry(registry.directory)
                self.worker.registry.sync(self.worker.registry.selected_entities(),ROWS,user_change=False)
                self.assertEqual(self.group_entries()[0]['key'],entry['key'])

    def test_partial_group_rejects_offline_new_member_without_changes(self):
        self.prepare_partial(GROUP[1]);registry=self.worker.registry;before=registry.snapshot()
        rows=copy.deepcopy(ROWS);rows[0]['available']=False
        with self.assertRaisesRegex(ValueError,'Offline'):
            change(self.worker,self.manager,'10.23.45.1',GROUP,False,rows)
        self.assertEqual(registry.snapshot(),before);self.assertTrue(registry.layouts.separate(GROUP))
        self.assertIsNone(registry.layouts.pending);self.assertEqual(self.manager.deleted,[])

    def test_partial_restore_after_commit_interruption_retains_identity(self):
        old=self.prepare_partial(GROUP[1]);registry=self.worker.registry
        identity=registry.identity_dir(old)/'identity.json';before=identity.read_bytes()
        with patch.object(registry.layouts,'finish',side_effect=OSError('Synthetic persistence failure')):
            with self.assertRaises(OSError):self.convert(False)
        self.assertEqual(registry.layouts.pending['preserve_key'],old['key'])
        self.worker.registry=ChimeRegistry(registry.directory)
        self.convert(False)
        self.assertEqual(self.group_entries()[0]['key'],old['key']);self.assertEqual(identity.read_bytes(),before)
        self.assertEqual(self.manager.deleted,[])

    def test_partial_restore_cleans_only_old_inactive_identity(self):
        kept=self.prepare_partial(GROUP[1]);registry=self.worker.registry
        inactive={'key':'f'*24,'entity_id':GROUP[0],'members':[GROUP[0]],'name':'Old','active':False}
        registry.entries.append(inactive);registry._save()
        path=registry.identity_dir(inactive);path.mkdir(parents=True);(path/'identity.json').write_text('{}')
        result=self.convert(False)
        self.assertEqual(result['preserved_key'],kept['key']);self.assertEqual(self.manager.deleted,['f'*24])
        self.assertFalse(path.exists());self.assertTrue((registry.identity_dir(kept)/'identity.json').exists())
