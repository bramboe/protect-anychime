"""Read-only Protect pairing observations; all transports are fake."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from management import ProtectManagement, ManagementError
from runtime import NativeWorker

HOST='10.23.45.1';MAC='025253000001'

class Client:
    def __init__(self):
        self.devices=[{'mac':MAC,'type':'UP Chime PoE','anonymousDeviceId':'own','isAdopted':True,'cameraIds':[]}]
        self.reads=0
    def factory(self,_):return self
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass
    async def chimes(self):self.reads+=1;return self.devices

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();path=Path(self.temp.name)/'account.json'
        path.write_text(json.dumps({'host':HOST,'username':'test-user','password':'test-password','fingerprint':'00'*32}))
        path.chmod(0o600)
        self.client=Client();self.manager=ProtectManagement(path,client_factory=self.client.factory)
        self.worker=Mock();self.worker.registry.snapshot.return_value=[{'key':'primary','active':True}]
        self.worker.removal_identity.return_value={'mac':MAC,'device_id':'own'}
    def tearDown(self):self.temp.cleanup()
    def observed(self):return self.manager.chime_configuration(HOST,self.worker).get('primary',{})
    def test_adopted_without_camera_is_not_configured(self):
        self.assertIs(self.observed()['protect_configured'],False)
        self.assertIs(self.observed()['protect_adopted'],True)
    def test_camera_or_ring_setting_is_positive_pairing_evidence(self):
        for fields in ({'cameraIds':['a'*24]}, {'ringSettings':[{'cameraId':'b'*24}]}):
            self.client.devices[0].update(fields)
            self.assertIs(self.observed()['protect_configured'],True)
    def test_missing_or_invalid_fields_remain_unknown(self):
        for fields in ({}, {'cameraIds':'bad'}, {'ringSettings':[{'volume':30}]}, {'cameraIds':['not-id']}):
            remote=self.client.devices[0];remote.pop('cameraIds',None);remote.pop('ringSettings',None);remote.update(fields)
            self.assertIsNone(self.observed()['protect_configured'])
    def test_foreign_ambiguous_or_unavailable_identities_are_not_reported(self):
        original=self.client.devices[0].copy()
        for devices in ([{**original,'anonymousDeviceId':'foreign'}],[{**original,'type':'physical'}],[original,original],[]):
            self.client.devices=devices;self.assertEqual(self.observed(),{})
    def test_unconfigured_account_does_not_connect(self):
        self.assertEqual(self.manager.chime_configuration('10.23.45.2',self.worker),{})
        self.assertEqual(self.client.reads,0)
    def test_removed_and_held_entries_are_not_polled(self):
        for flag in ('removal_pending','migration_hold'):
            self.worker.registry.snapshot.return_value=[{'key':'primary','active':True,flag:True}]
            self.assertEqual(self.observed(),{})
        self.assertEqual(self.client.reads,0)

class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_throttle_and_failure_clear_stale_success(self):
        worker=NativeWorker();worker.configuration_checker=Mock(return_value={'primary':{'_device_id':'own','protect_configured':True}})
        with patch('runtime.time.monotonic',return_value=100):
            await worker._check_configuration();await worker._check_configuration()
        worker.configuration_checker.assert_called_once()
        self.assertTrue(worker._configuration_state['primary']['protect_configured'])
        worker.configuration_checker.side_effect=ManagementError('private-response')
        with patch('runtime.time.monotonic',return_value=111):await worker._check_configuration()
        self.assertEqual(worker._configuration_state,{})
        self.assertTrue(worker._configuration_failed)
    async def test_log_reference_has_a_safe_log_entry_without_repeated_spam(self):
        worker=NativeWorker();worker.configuration_checker=Mock(side_effect=ManagementError('secret-response'))
        with patch('builtins.print') as log:
            with patch('runtime.time.monotonic',return_value=100):await worker._check_configuration()
            with patch('runtime.time.monotonic',return_value=111):await worker._check_configuration()
        log.assert_called_once()
        self.assertIn('Protect configuration check failed (ManagementError)',log.call_args.args[0])
        self.assertNotIn('secret-response',log.call_args.args[0])

    async def test_network_problem_logs_once_and_logs_again_after_recovery(self):
        worker=NativeWorker();problem={'state':'permission_required','reason':'Permission denied: /proc/net/route'}
        with patch('builtins.print') as log:
            worker._record_network_state('primary',problem);worker._record_network_state('primary',problem)
            log.assert_called_once()
            self.assertIn('/proc/net/route',log.call_args.args[0])
            worker._record_network_state('primary',{'state':'ready'})
            worker._record_network_state('primary',problem)
            self.assertEqual(log.call_count,2)

    async def test_status_does_not_apply_observation_to_replaced_identity(self):
        worker=NativeWorker();worker._statuses=[{'key':'primary','device_id':'new','connected':True}]
        worker._configuration_state={'primary':{'_device_id':'old','protect_configured':True}}
        self.assertNotIn('protect_configured',worker.status()['chimes'][0])
        worker._configuration_state['primary']['_device_id']='new'
        observed=worker.status()['chimes'][0]
        self.assertTrue(observed['protect_configured']);self.assertNotIn('_device_id',observed)
