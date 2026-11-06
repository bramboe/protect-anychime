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
    def test_notification_revision_ignores_order_and_unrelated_ring_metadata(self):
        first={'cameraId':'a'*24,'volume':10,'repeatTimes':1,'ringtoneId':'b'*24}
        second={'cameraId':'c'*24,'volume':20,'repeatTimes':1,'ringtoneId':'d'*24}
        remote=self.client.devices[0];remote.update(cameraIds=['a'*24,'c'*24],ringSettings=[first,second])
        revision=self.observed()['_settings_revision']
        remote.update(cameraIds=['c'*24,'a'*24],ringSettings=[{**second,'lastUpdated':999},first])
        self.assertEqual(self.observed()['_settings_revision'],revision)
        remote['ringSettings'][0]['volume']=25
        self.assertNotEqual(self.observed()['_settings_revision'],revision)

    def test_unconfigured_account_does_not_connect(self):
        self.assertEqual(self.manager.chime_configuration('10.23.45.2',self.worker),{})
        self.assertEqual(self.client.reads,0)
    def test_removed_and_held_entries_are_not_polled(self):
        for flag in ('removal_pending','migration_hold'):
            self.worker.registry.snapshot.return_value=[{'key':'primary','active':True,flag:True}]
            self.assertEqual(self.observed(),{})
        self.assertEqual(self.client.reads,0)

class PollTests(unittest.IsolatedAsyncioTestCase):
    async def poll(self, worker, now):
        with patch('runtime.time.monotonic',return_value=now):await worker._check_configuration()

    async def test_throttle_preserves_confirmed_pairing_on_one_failure(self):
        worker=NativeWorker();worker.configuration_checker=Mock(return_value={'primary':{'_device_id':'own','protect_configured':True}})
        await self.poll(worker,100);await self.poll(worker,111)
        worker.configuration_checker.assert_called_once()
        worker.configuration_checker.side_effect=ManagementError('private-response',kind='timeout')
        await self.poll(worker,131)
        self.assertTrue(worker._configuration_state['primary']['protect_configured'])
        self.assertFalse(worker._configuration_failed)
        self.assertEqual(worker._configuration_after,146)
        worker.configuration_checker.side_effect=None
        await self.poll(worker,146)
        self.assertEqual(worker._configuration_failures,0)
        self.assertFalse(worker._configuration_failed)

    async def test_warning_requires_repeated_failures_and_ninety_seconds(self):
        worker=NativeWorker();worker.configuration_checker=Mock(side_effect=ManagementError('private',kind='network'))
        for now in (100,115,145):
            await self.poll(worker,now)
            self.assertFalse(worker._configuration_failed)
        await self.poll(worker,205)
        self.assertTrue(worker._configuration_failed)
        self.assertEqual(worker._configuration_after,265)

    async def test_cached_evidence_expires_and_failure_flags_do_not_mask_live_disconnect(self):
        worker=NativeWorker();worker.configuration_checker=Mock(return_value={'primary':{'_device_id':'own','protect_configured':True}})
        worker._statuses=[{'key':'primary','device_id':'own','connected':False,'adopted':True}]
        await self.poll(worker,100)
        worker.configuration_checker.side_effect=TimeoutError()
        await self.poll(worker,401)
        self.assertEqual(worker._configuration_state,{})
        self.assertTrue(worker._configuration_failed)
        self.assertFalse(worker.status()['chimes'][0]['connected'])

    async def test_log_reference_has_safe_categorized_log_without_repeated_spam(self):
        worker=NativeWorker();worker.configuration_checker=Mock(side_effect=ManagementError('secret-response',kind='rate_limit',http_status=429))
        with patch('builtins.print') as log:
            for now in (100,115,145):await self.poll(worker,now)
            log.assert_called_once()
            self.assertIn('rate_limit, HTTP 429',log.call_args.args[0])
            self.assertNotIn('secret-response',log.call_args.args[0])
            await self.poll(worker,205)
            self.assertEqual(log.call_count,2)
            self.assertIn('remain unavailable',log.call_args.args[0])
            worker.configuration_checker.side_effect=None;worker.configuration_checker.return_value={}
            await self.poll(worker,265)
            self.assertEqual(log.call_count,3)
            self.assertIn('recovered',log.call_args.args[0])

    async def test_success_resets_failure_episode(self):
        worker=NativeWorker();worker.configuration_checker=Mock(side_effect=TimeoutError())
        await self.poll(worker,100)
        worker.configuration_checker.side_effect=None;worker.configuration_checker.return_value={}
        await self.poll(worker,115)
        worker.configuration_checker.side_effect=TimeoutError()
        await self.poll(worker,146)
        self.assertEqual(worker._configuration_failures,1)
        self.assertFalse(worker._configuration_failed)

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


class ErrorClassificationTests(unittest.TestCase):
    def test_transport_categories_do_not_expose_arbitrary_error_text(self):
        import ssl
        import aiohttp
        for exception,kind in ((TimeoutError('private'),'timeout'),(ssl.SSLError('private'),'certificate'),(aiohttp.ClientConnectionError('private'),'network'),(ValueError('private'),'invalid_response')):
            async def fail():raise exception
            with self.assertRaises(ManagementError) as caught:ProtectManagement._run(fail())
            self.assertEqual(caught.exception.kind,kind)
            self.assertNotIn('private',str(caught.exception))
