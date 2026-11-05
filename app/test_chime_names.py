import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from management import ProtectManagement,ManagementError
from discovery import MultiResponder,announcement,PROBE

HOST='10.23.45.1';MAC='025253000001';ID='1'*24
class Client:
    def __init__(self):self.devices=[{'id':ID,'mac':MAC,'type':'UP Chime PoE','anonymousDeviceId':'own','name':'Smart PoE Chime','isAdopted':True}];self.renames=[]
    def factory(self,_):return self
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass
    async def chimes(self):return self.devices
    async def rename(self,id,name):self.renames.append((id,name));next(d for d in self.devices if d['id']==id)['name']=name

class ChimeNameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();path=Path(self.temp.name)/'account.json'
        path.write_text(json.dumps({'host':HOST,'username':'test-user','password':'test-password','fingerprint':'00'*32}));path.chmod(0o600)
        self.client=Client();self.manager=ProtectManagement(path,client_factory=self.client.factory)
        self.worker=Mock();self.worker.registry.snapshot.return_value=[{'key':'primary','active':True,'name':'Office speaker'}]
        self.worker.removal_identity.return_value={'mac':MAC,'device_id':'own'}
    def tearDown(self):self.temp.cleanup()
    def test_generic_name_is_replaced_with_owned_speaker_name(self):
        result=self.manager.sync_names(HOST,['primary'],self.worker)
        self.assertEqual(self.client.renames,[(ID,'Office speaker')]);self.assertEqual(result['completed'],['primary']);self.assertFalse(result['audio_played'])
    def test_custom_names_and_unadopted_devices_are_not_changed(self):
        self.client.devices[0]['name']='My preferred name'
        self.assertEqual(self.manager.sync_names(HOST,['primary'],self.worker)['completed'],['primary']);self.assertEqual(self.client.renames,[])
        self.client.devices[0]['name']='Smart PoE Chime';self.client.devices[0]['isAdopted']=False
        self.assertEqual(self.manager.sync_names(HOST,['primary'],self.worker)['completed'],[]);self.assertEqual(self.client.renames,[])
    def test_wrong_identity_aborts_before_any_name_change(self):
        self.client.devices[0]['anonymousDeviceId']='foreign'
        with self.assertRaises(ManagementError):self.manager.sync_names(HOST,['primary'],self.worker)
        self.assertEqual(self.client.renames,[])
    def test_inactive_registry_entries_are_not_renamed(self):
        self.worker.registry.snapshot.return_value[0]['active']=False
        self.assertEqual(self.manager.sync_names(HOST,['primary'],self.worker)['completed'],[]);self.assertEqual(self.client.renames,[])

class NewlyReadyDiscoveryTests(unittest.TestCase):
    def test_recent_allowed_probe_receives_newly_ready_identity_once(self):
        rows=[];responder=MultiResponder('10.23.45.20',[HOST],lambda:rows)
        peer=(HOST,42000);self.assertEqual(responder.responses_to(PROBE,peer,10),[])
        packet=announcement(MAC,'10.23.45.20',name='Office')
        rows.append({'ip':'10.23.45.20','packet':packet})
        self.assertEqual(responder.delayed_responses(11),[(peer,'10.23.45.20',packet)])
        self.assertEqual(responder.delayed_responses(12),[])
    def test_no_unsolicited_or_expired_reply(self):
        rows=[];responder=MultiResponder('10.23.45.20',[HOST],lambda:rows)
        responder.responses_to(PROBE,('10.23.45.2',42000),10)
        rows.append({'ip':'10.23.45.20','packet':announcement(MAC,'10.23.45.20')})
        self.assertEqual(responder.delayed_responses(11),[])
        responder.responses_to(PROBE,(HOST,42000),12)
        rows.append({'ip':'10.23.45.87','packet':announcement('025253000002','10.23.45.87')})
        self.assertEqual(responder.delayed_responses(17),[])
