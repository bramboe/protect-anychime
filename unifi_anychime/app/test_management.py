from test_support import certificate
import asyncio
import hashlib
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import management
import server

PEM = certificate()
PIN = hashlib.sha256(ssl.PEM_cert_to_DER_cert(PEM)).hexdigest()
HOST = '10.23.45.1'
MAC = '025253000001'
DEVICE_ID = '1' * 24


class FakeClient:
    def __init__(self):
        self.calls = []
        self.devices = [{'id': DEVICE_ID, 'mac': MAC, 'type': 'UP Chime PoE', 'anonymousDeviceId': 'own-uuid'}]
        self.failure = None
        self.delete_failure = None

    def factory(self, account):
        self.calls.append(('account', account.copy()))
        return self

    async def __aenter__(self):
        if self.failure:raise self.failure
        return self

    async def __aexit__(self, *_):pass

    async def chimes(self):
        self.calls.append(('list',))
        return self.devices

    async def remove(self, device_id):
        self.calls.append(('delete', device_id))
        if self.delete_failure:raise self.delete_failure


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'protect-management.json'
        self.client = FakeClient()
        self.fetcher = Mock(return_value=(PEM, PIN))
        self.manager = management.ProtectManagement(self.path, fetcher=self.fetcher,
            client_factory=self.client.factory, clock=lambda: 100)
        self.worker = Mock()
        self.worker.removal_identity.return_value = {'mac': MAC, 'device_id': 'own-uuid'}

    def tearDown(self):self.temp.cleanup()

    def connect(self):
        pending = self.manager.prepare(HOST)
        return self.manager.login(pending['confirmation_token'], HOST, 'local-user', 'private-test-password')

    def test_status_and_construction_never_make_network_calls(self):
        self.assertEqual(self.manager.status(HOST), {'configured': False, 'ready': False})
        self.assertEqual(self.client.calls, [])
        self.fetcher.assert_not_called()

    def test_login_is_read_only_private_and_status_never_discloses_credentials(self):
        result = self.connect()
        self.assertFalse(result['audio_played'])
        self.assertEqual([x[0] for x in self.client.calls], ['account', 'list'])
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        public = self.manager.status(HOST)
        self.assertTrue(public['ready'])
        self.assertNotIn('password', json.dumps(public))
        self.assertNotIn('local-user', json.dumps(public))
        self.assertFalse(self.manager.status('10.23.45.2')['ready'])

    def test_prepared_console_check_is_required_single_use_and_bound_to_host(self):
        pending = self.manager.prepare(HOST)
        with self.assertRaises(management.ManagementError):
            self.manager.login(pending['confirmation_token'], '10.23.45.2', 'user', 'pass')
        with self.assertRaises(management.ManagementError):
            self.manager.login(pending['confirmation_token'], HOST, 'user', 'pass')
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.path.exists())

    def test_certificate_change_requires_new_confirmation(self):
        first = self.manager.prepare(HOST, PIN)
        self.assertFalse(first['confirm_console'])
        second = self.manager.prepare(HOST, '00' * 32)
        self.assertTrue(second['confirm_console'])

    def test_expired_preparation_does_not_send_credentials(self):
        pending = self.manager.prepare(HOST)
        self.manager.clock = lambda: 221
        with self.assertRaises(management.ManagementError):
            self.manager.login(pending['confirmation_token'], HOST, 'user', 'pass')
        self.assertEqual(self.client.calls, [])

    def test_remove_only_resolved_owned_chime_and_pause_before_delete(self):
        self.connect();self.client.calls.clear()
        original = self.client.remove
        async def remove(device_id):
            self.worker.begin_removal.assert_called_once_with('primary')
            await original(device_id)
        self.client.remove = remove
        result = self.manager.remove_virtual_chime(HOST, 'primary', self.worker)
        self.assertTrue(result['removed'])
        self.assertFalse(result['audio_played'])
        self.assertEqual([x for x in self.client.calls if x[0] == 'delete'], [('delete', DEVICE_ID)])
        self.worker.finalize_removal.assert_called_once_with('primary')

    def test_wrong_device_identity_type_and_duplicate_matches_never_pause_or_delete(self):
        self.connect()
        base = self.client.devices[0]
        for devices in ([{**base, 'mac': '025253000002'}], [{**base, 'type': 'Camera'}],
                [{**base, 'anonymousDeviceId': 'foreign'}], [base, base], [{**base, 'id': '../cameras/x'}]):
            with self.subTest(devices=devices):
                self.client.devices=devices;self.client.calls.clear()
                with self.assertRaises(management.ManagementError):
                    self.manager.remove_virtual_chime(HOST, 'primary', self.worker)
                self.assertFalse(any(x[0] == 'delete' for x in self.client.calls))
                self.worker.remove_from_protect.assert_not_called()
                self.worker.finalize_removal.assert_not_called()

    def test_delete_timeout_does_not_retry_or_claim_success(self):
        self.connect();self.client.calls.clear();self.client.delete_failure = TimeoutError('private-network-info')
        with self.assertRaises(management.ManagementError) as error:
            self.manager.remove_virtual_chime(HOST, 'primary', self.worker)
        self.assertNotIn('private-network-info', str(error.exception))
        self.assertEqual(len([x for x in self.client.calls if x[0] == 'delete']), 1)
        self.worker.begin_removal.assert_called_once()
        self.worker.remove_from_protect.assert_not_called()
        self.worker.finalize_removal.assert_not_called()

    def test_batch_preflights_all_identities_before_any_deletion(self):
        self.connect();self.client.calls.clear()
        self.worker.removal_identity.side_effect=lambda key: {'mac':MAC if key=='primary' else '025253000002','device_id':'own-uuid'}
        self.client.devices.append({'id':'2'*24,'mac':'025253000002','type':'Camera'})
        with self.assertRaises(management.ManagementError):
            self.manager.remove_virtual_chimes(HOST,['primary','secondary'],self.worker)
        self.worker.begin_removal.assert_not_called()
        self.assertFalse(any(call[0]=='delete' for call in self.client.calls))

    def test_batch_uses_one_session_and_completes_absent_identity_without_delete(self):
        self.connect();self.client.calls.clear()
        self.worker.removal_identity.side_effect=lambda key: {'mac':MAC if key=='primary' else '025253000002','device_id':'own-uuid'}
        result=self.manager.remove_virtual_chimes(HOST,['primary','secondary'],self.worker,allow_absent=True)
        self.assertEqual(result['removed_keys'],['primary','secondary'])
        self.assertEqual([c[0] for c in self.client.calls],['account','list','delete'])
        self.assertEqual(self.worker.remove_from_protect.call_count,2)
        self.assertEqual(self.worker.finalize_removal.call_count,2)

    def test_batch_partial_failure_keeps_failed_identity_pending(self):
        self.connect()
        self.worker.removal_identity.side_effect=lambda key: {'mac':MAC if key=='primary' else '025253000002','device_id':'own-uuid'}
        self.client.devices.append({'id':'2'*24,'mac':'025253000002','type':'UP Chime PoE'})
        async def remove(device_id):
            if device_id=='2'*24:raise TimeoutError()
        self.client.remove=remove
        with self.assertRaises(management.ManagementError):
            self.manager.remove_virtual_chimes(HOST,['primary','secondary'],self.worker)
        self.assertEqual(self.worker.begin_removal.call_count,2)
        self.worker.remove_from_protect.assert_called_once_with('primary')
        self.worker.finalize_removal.assert_called_once_with('primary')

    def test_failed_login_stores_nothing_and_does_not_leak_secret(self):
        self.client.failure = ConnectionError('private-test-password')
        with self.assertRaises(management.ManagementError) as error:self.connect()
        self.assertNotIn('private-test-password', str(error.exception))
        self.assertFalse(self.path.exists())

    def test_disconnect_removes_only_management_account_without_chime_calls(self):
        self.connect();self.client.calls.clear()
        self.manager.forget()
        self.assertFalse(self.path.exists());self.assertEqual(self.client.calls, [])
        self.worker.assert_not_called()


    def test_disconnect_during_login_does_not_restore_forgotten_credentials(self):
        async def disconnect_while_reading():
            self.manager.forget()
            return []
        self.client.chimes = disconnect_while_reading
        with self.assertRaises(management.ManagementError):
            self.connect()
        self.assertFalse(self.path.exists())


class ManagementApiTests(unittest.TestCase):
    def handler(self, path, payload, peer='172.30.32.2'):
        handler=server.Handler.__new__(server.Handler)
        raw=json.dumps(payload).encode();handler.path=path;handler.client_address=(peer, 1)
        handler.headers={'X-AnyChime-Request':'1','Content-Length':str(len(raw))}
        handler.rfile=io.BytesIO(raw);handler.respond=Mock()
        return handler

    def test_management_requires_authenticated_ingress(self):
        for peer in ['127.0.0.1','10.23.45.5']:
            handler=self.handler('/api/management/login',{},peer)
            with patch.object(server,'MANAGEMENT') as manager:handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0],403);manager.login.assert_not_called()

    def test_delete_requires_confirmation_and_never_accepts_remote_id(self):
        for body in [{'key':'primary'},{'key':'../cameras','confirmed':True},{'id':DEVICE_ID,'confirmed':True}]:
            handler=self.handler('/api/chimes/remove',body)
            with patch.object(server,'load_options',return_value={'controller_ip':HOST}),patch.object(server,'MANAGEMENT') as manager:
                handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0],400)
            manager.remove_virtual_chime.assert_not_called()

    def test_confirmed_delete_passes_only_owned_key_not_browser_device_id(self):
        handler=self.handler('/api/chimes/remove',{'key':'primary','confirmed':True,'id':'foreign-id'})
        with patch.object(server,'load_options',return_value={'controller_ip':HOST}),patch.object(server,'MANAGEMENT') as manager:
            manager.remove_virtual_chime.return_value={'removed':True,'audio_played':False}
            handler.do_POST()
        manager.remove_virtual_chime.assert_called_once_with(HOST,'primary',server.NATIVE)
        self.assertEqual(handler.respond.call_args.args[0],200)

class AsyncResponse:
    def __init__(self, data=None, *, status=200, headers=None):
        self.status=status;self.headers=headers or {}
        self.content=Mock();self.content.read=AsyncMock(return_value=json.dumps(data).encode() if data is not None else b'')
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass


class SessionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_login_cookie_session_csrf_and_fixed_chime_routes(self):
        account={'host':HOST,'username':'test-user','password':'test-password','fingerprint':PIN}
        session=Mock();session.close=AsyncMock()
        session.request.side_effect=[AsyncResponse(headers={'x-csrf-token':'csrf-test'}),
            AsyncResponse([{'id':DEVICE_ID}]),AsyncResponse({'success':True})]
        with patch('management._PinnedConnector'),patch('management.aiohttp.ClientSession',return_value=session) as factory:
            async with management.ProtectSession(account) as client:
                self.assertEqual(await client.chimes(),[{'id':DEVICE_ID}])
                await client.remove(DEVICE_ID)
        self.assertFalse(factory.call_args.kwargs['trust_env'])
        calls=session.request.call_args_list
        self.assertEqual(calls[0].args,('POST','https://'+HOST+'/api/auth/login'))
        self.assertEqual(calls[0].kwargs['json'],{'username':'test-user','password':'test-password','rememberMe':False})
        self.assertEqual(calls[2].args,('DELETE','https://'+HOST+'/proxy/protect/api/chimes/'+DEVICE_ID))
        self.assertEqual(calls[2].kwargs['headers'],{'X-CSRF-Token':'csrf-test'})
        self.assertTrue(all(c.kwargs['allow_redirects'] is False for c in calls))
        session.close.assert_awaited_once()

    async def test_no_csrf_session_is_rejected_and_closed_before_management_calls(self):
        session=Mock();session.close=AsyncMock();session.request.return_value=AsyncResponse()
        with patch('management._PinnedConnector'),patch('management.aiohttp.ClientSession',return_value=session):
            with self.assertRaises(management.ManagementError):
                async with management.ProtectSession({'host':HOST,'username':'user','password':'pass','fingerprint':PIN}):pass
        self.assertEqual(session.request.call_count,1);session.close.assert_awaited_once()

class HAConfigurationAccountTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect

    def test_configure_is_silent_and_password_not_returned_by_status(self):
        self.manager.configure({'controller_ip':HOST,'protect_username':'config-user','protect_password':'config-password'})
        self.assertTrue(self.manager.status(HOST)['ready']);self.fetcher.assert_not_called()
        self.assertEqual(self.client.calls,[]);self.assertNotIn('config-password',json.dumps(self.manager.status(HOST)))

    def test_configured_removal_stores_only_trust_and_never_duplicates_credentials(self):
        self.manager.configure({'controller_ip':HOST,'protect_username':'config-user','protect_password':'config-password'})
        self.manager.remove_virtual_chime(HOST,'primary',self.worker)
        trust=json.loads(self.path.with_name('protect-management-trust.json').read_text())
        self.assertEqual(set(trust),{'host','fingerprint'});self.assertFalse(self.path.exists())
        self.assertEqual(self.client.calls[0][1]['username'],'config-user')

    def test_existing_working_account_moves_to_own_ha_options_without_login(self):
        self.connect();self.client.calls.clear();stored={'protect_username':'','protect_password':''};calls=[]
        def request(method,body=None):
            calls.append((method,body))
            if method=='POST':stored.update(body['options']);return {}
            return {'options':stored.copy()}
        result=self.manager.migrate_to_configuration({'controller_ip':HOST},request)
        self.assertEqual(result['protect_username'],'local-user')
        self.assertEqual(stored['protect_password'],'private-test-password')
        self.assertFalse(self.path.exists());self.assertEqual(self.client.calls,[])
        self.assertEqual([c[0] for c in calls],['GET','POST','GET'])

    def test_configuration_migration_failure_preserves_working_login(self):
        self.connect();self.client.calls.clear()
        result=self.manager.migrate_to_configuration({'controller_ip':HOST},Mock(side_effect=OSError('unavailable')))
        self.assertTrue(self.path.exists());self.assertEqual(result['protect_username'],'local-user')
        self.assertEqual(self.client.calls,[])

    def test_user_configured_account_is_never_overwritten_by_legacy_login(self):
        self.connect();request=Mock(return_value={'options':{'protect_username':'new-user','protect_password':'new-password'}})
        result=self.manager.migrate_to_configuration({'controller_ip':HOST},request)
        self.assertEqual(result['protect_username'],'new-user');request.assert_called_once_with('GET')

class PreviousChimeDiscoveryTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect

    def test_read_only_scan_finds_owned_inactive_records_without_needing_a_selected_speaker(self):
        self.connect();self.client.calls.clear()
        self.worker.registry.snapshot.return_value=[{'key':'primary','entity_id':'media_player.old','name':'Old room','active':False}]
        self.client.devices += [{'id':'2'*24,'mac':'025253000099','type':'UP Chime PoE'}]
        result=self.manager.inspect_previous_chimes(HOST,self.worker)
        self.assertEqual(len(result['chimes']),1);self.assertEqual(result['chimes'][0]['key'],'primary')
        self.assertFalse(any(c[0]=='delete' for c in self.client.calls));self.worker.remove_from_protect.assert_not_called()

    def test_healthy_adopted_active_chime_is_not_listed_as_previous(self):
        self.connect();self.worker.registry.snapshot.return_value=[{'key':'primary','active':True}]
        self.client.devices[0].update(isAdopted=True,isConnected=True)
        self.assertEqual(self.manager.inspect_previous_chimes(HOST,self.worker)['chimes'],[])

class AdoptionRepairTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect

    def test_verified_unadopted_record_resets_only_local_pairing_without_delete(self):
        self.connect();self.client.calls.clear();self.client.devices[0].update(isAdopted=False,isConnected=False)
        result=self.manager.prepare_adoption(HOST,'primary',self.worker)
        self.assertTrue(result['ready_for_adoption']);self.assertFalse(result['audio_played'])
        self.worker.finalize_removal.assert_called_once_with('primary')
        self.worker.remove_from_protect.assert_not_called()
        self.assertFalse(any(c[0]=='delete' for c in self.client.calls))

    def test_absent_record_allows_local_pairing_repair(self):
        self.connect();self.client.devices=[]
        self.assertTrue(self.manager.prepare_adoption(HOST,'primary',self.worker)['ready_for_adoption'])
        self.worker.finalize_removal.assert_called_once()

    def test_active_ambiguous_foreign_or_unknown_record_never_resets_pairing(self):
        self.connect();base=self.client.devices[0]
        variants=[[{**base,'isAdopted':True,'isConnected':False}],
            [{**base,'isAdopted':False,'isConnected':True}],
            [{**base,'isAdopted':False}],
            [{**base,'isAdopted':False,'isConnected':False,'type':'Camera'}],
            [{**base,'isAdopted':False,'isConnected':False,'anonymousDeviceId':'other'}],
            [base,base]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.client.devices=rows
                with self.assertRaises(management.ManagementError):self.manager.prepare_adoption(HOST,'primary',self.worker)
                self.worker.finalize_removal.assert_not_called()

    def test_failed_authentication_never_resets_local_identity(self):
        self.connect();self.client.failure=ConnectionError('unreachable')
        with self.assertRaises(management.ManagementError):self.manager.prepare_adoption(HOST,'primary',self.worker)
        self.worker.finalize_removal.assert_not_called()

class ChimeTestActionsTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def test_user_test_is_sent_only_via_protect_for_the_owned_online_chime(self):
        self.connect()
        self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80)
        self.client.play=AsyncMock(return_value=None)
        result=self.manager.test_virtual_chime(HOST,'primary',self.worker)
        self.client.play.assert_awaited_once_with(DEVICE_ID,{'volume':10,'repeatTimes':1})
        self.assertTrue(result['test_requested'])
        self.assertIsNone(result['audio_played'])
        self.assertEqual(self.worker.mock_calls,[unittest.mock.call.removal_identity('primary')])
    def test_foreign_duplicate_and_offline_devices_never_receive_a_test(self):
        self.connect()
        original=self.client.devices[0].copy()
        for devices in [[{**original,'mac':'025253000099'}], [original,original],
                        [{**original,'isConnected':False,'isAdopted':True}],
                        [{**original,'isConnected':True,'isAdopted':False}],
                        [{**original,'isConnected':True,'isAdopted':True,'anonymousDeviceId':'another-installation'}]]:
            with self.subTest(devices=devices):
                self.client.devices=devices;self.client.play=AsyncMock()
                with self.assertRaises(management.ManagementError):self.manager.test_virtual_chime(HOST,'primary',self.worker)
                self.client.play.assert_not_called()
    def test_missing_account_never_attempts_audio(self):
        self.client.play=AsyncMock()
        with self.assertRaises(management.ManagementError):self.manager.test_virtual_chime(HOST,'primary',self.worker)
        self.client.play.assert_not_called()
        self.assertEqual(self.client.calls,[])
    def test_test_endpoint_requires_ingress_and_explicit_action(self):
        for source,confirmed in [('127.0.0.1',True),('10.23.45.50',True),('172.30.32.2',False)]:
            handler=server.Handler.__new__(server.Handler);handler.path='/api/chimes/test';handler.client_address=(source,123)
            raw=json.dumps({'key':'primary','confirmed':confirmed}).encode()
            handler.headers={'X-AnyChime-Request':'1','Content-Length':str(len(raw)),'X-Forwarded-For':'172.30.32.2'}
            handler.rfile=io.BytesIO(raw);handler.respond=Mock()
            with patch.object(server,'MANAGEMENT') as manager,patch.object(server,'load_options',return_value={'controller_ip':HOST}):handler.do_POST()
            self.assertIn(handler.respond.call_args.args[0],(400,403))
            manager.test_virtual_chime.assert_not_called()

class PairedVolumeTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def test_test_defaults_to_ten_percent_and_keeps_paired_tone_and_repeat(self):
        self.connect();self.client.play=AsyncMock()
        self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80,repeatTimes=1,
            ringSettings=[{'cameraId':'2'*24,'volume':30,'repeatTimes':2,'ringtoneId':'3'*24}])
        result=self.manager.test_virtual_chime(HOST,'primary',self.worker)
        self.client.play.assert_awaited_once_with(DEVICE_ID,{'volume':10,'repeatTimes':2,'ringtoneId':'3'*24})
        self.assertEqual(result['volume'],10)
    def test_zero_volume_does_not_send_any_play_request(self):
        self.connect();self.client.play=AsyncMock()
        self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80,ringSettings=[{'volume':0,'repeatTimes':1}])
        result=self.manager.test_virtual_chime(HOST,'primary',self.worker)
        self.client.play.assert_not_called();self.assertTrue(result['muted']);self.assertFalse(result['test_requested'])
    def test_conflicting_doorbell_settings_are_not_replaced_by_an_arbitrary_volume(self):
        self.connect();self.client.play=AsyncMock()
        self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80,
            ringSettings=[{'volume':25,'repeatTimes':1},{'volume':60,'repeatTimes':1}])
        with self.assertRaisesRegex(management.ManagementError,'different settings'):
            self.manager.test_virtual_chime(HOST,'primary',self.worker)
        self.client.play.assert_not_called()
    def test_missing_and_invalid_volume_never_defaults_to_full_volume(self):
        for remote in [{},{'volume':True},{'volume':101},{'volume':-1},{'volume':None},
                       {'volume':30,'ringSettings':[{'volume':25,'repeatTimes':0}]}]:
            with self.subTest(remote=remote),self.assertRaises(management.ManagementError):management.ProtectManagement.test_settings(remote)

class ExplicitTestTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_play_transport_serialises_the_explicit_settings(self):
        from unittest.mock import MagicMock
        session=management.ProtectSession({'host':HOST});response=Mock(status=200,headers={})
        response.content.read=AsyncMock(return_value=b'{}')
        context=MagicMock();context.__aenter__=AsyncMock(return_value=response);context.__aexit__=AsyncMock()
        session.session=Mock();session.session.request.return_value=context
        settings={'volume':30,'repeatTimes':2,'ringtoneId':'3'*24}
        await session.play(DEVICE_ID,settings)
        self.assertEqual(session.session.request.call_args.kwargs['json'],settings)
        self.assertFalse(session.session.request.call_args.kwargs['allow_redirects'])

class LayoutPreflightTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def test_layout_preflight_reads_owned_chimes_without_pausing_or_deleting(self):
        self.connect();self.worker.reset_mock();self.client.calls=[]
        result=self.manager.check_owned_chimes(HOST,['primary'],self.worker)
        self.assertTrue(result['checked']);self.assertFalse(result['audio_played'])
        self.assertEqual([c[0] for c in self.client.calls],['account','list'])
        self.worker.begin_removal.assert_not_called();self.worker.finalize_removal.assert_not_called()
    def test_layout_preflight_rejects_a_mismatched_uuid_without_mutations(self):
        self.connect();self.client.devices[0]['anonymousDeviceId']='other-installation'
        with self.assertRaises(management.ManagementError):self.manager.check_owned_chimes(HOST,['primary'],self.worker)
        self.worker.begin_removal.assert_not_called()
        self.assertFalse(any(c[0]=='delete' for c in self.client.calls))

class AutomaticAdoptionTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def setup_candidate(self):
        self.connect();self.worker.nodes={'primary':{'ip':'10.23.45.40'}}
        self.worker._entry_enabled.return_value=True
        self.client.devices[0].update(host='10.23.45.40',isAdopted=False)
        self.client.adopt=AsyncMock()
    def test_opted_in_manager_adopts_only_the_new_owned_chime_and_requires_further_setup(self):
        self.setup_candidate();result=self.manager.auto_adopt(HOST,'primary',self.worker)
        self.client.adopt.assert_awaited_once_with(DEVICE_ID)
        self.assertTrue(result['setup_required_in_protect']);self.assertFalse(result['audio_played'])
    def test_other_address_owner_or_device_type_is_not_adopted(self):
        self.setup_candidate();original=self.client.devices[0].copy()
        for changes in [{'host':'10.23.45.99'},{'type':'UVC Camera'},{'anonymousDeviceId':'another-installation'},{'isAdoptedByOther':True}]:
            self.client.devices=[{**original,**changes}];self.client.adopt.reset_mock()
            with self.assertRaises(management.ManagementError):self.manager.auto_adopt(HOST,'primary',self.worker)
            self.client.adopt.assert_not_called()
    def test_adopted_or_currently_adopting_chime_is_not_adopted_again(self):
        self.setup_candidate();self.client.devices[0]['isAdopted']=True
        self.manager.auto_adopt(HOST,'primary',self.worker);self.client.adopt.assert_not_called()
    def test_manifest_default_remains_disabled_and_explains_setup(self):
        root=Path(__file__).parents[1];config=json.loads((root/'config.json').read_text())
        self.assertIs(config['options']['auto_adopt_chimes'],False)
        text=json.loads((root/'translations/en.json').read_text())['configuration']['auto_adopt_chimes']['description']
        for value in ['doorbell','ringtone','volume','repeats']:self.assertIn(value,text)


class ChimeCountTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def test_count_includes_native_chimes_and_is_cached_without_mutations(self):
        self.connect();self.client.devices=[{'isAdopted':True,'mac':'native-device'},
            {'isAdopted':True,'mac':MAC},{'isAdopted':False}];self.client.calls=[]
        self.assertEqual(self.manager.chime_count(HOST),{'count':2})
        count=len(self.client.calls)
        self.assertEqual(self.manager.chime_count(HOST),{'count':2})
        self.assertEqual(len(self.client.calls),count)
        self.assertFalse(any(c[0] in ('delete','play','adopt') for c in self.client.calls))
    def test_missing_account_returns_unknown_without_network(self):
        self.assertEqual(self.manager.chime_count(HOST),{'count':None})
        self.assertEqual(self.client.calls,[])
    def test_failed_lookup_is_cached_as_unknown(self):
        self.connect()
        with patch.object(self.client,'chimes',side_effect=management.ManagementError('Synthetic error')) as read:
            self.assertEqual(self.manager.chime_count(HOST),{'count':None})
            self.assertEqual(self.manager.chime_count(HOST),{'count':None})
            self.assertEqual(read.call_count,1)


class AdjustableTestVolumeTests(unittest.TestCase):
    setUp=ManagementTests.setUp
    tearDown=ManagementTests.tearDown
    connect=ManagementTests.connect
    def test_explicit_volume_is_request_only_and_does_not_write_protect_settings(self):
        self.connect();self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80)
        self.client.play=AsyncMock()
        for volume in (5,25):
            result=self.manager.test_virtual_chime(HOST,'primary',self.worker,volume=volume)
            self.assertEqual(result['volume'],volume)
            self.assertEqual(self.client.play.await_args.args[1]['volume'],volume)
            self.assertEqual(self.client.devices[0]['volume'],80)
    def test_zero_test_volume_never_sends_playback(self):
        self.connect();self.client.devices[0].update(isConnected=True,isAdopted=True,volume=80)
        self.client.play=AsyncMock()
        result=self.manager.test_virtual_chime(HOST,'primary',self.worker,volume=0)
        self.assertTrue(result['muted']);self.client.play.assert_not_called()
    def test_invalid_volume_is_rejected_before_any_transport_or_identity_lookup(self):
        self.worker.reset_mock();self.client.calls=[]
        for volume in (True,-1,101,1.5,'10',None):
            with self.assertRaises(management.ManagementError):
                self.manager.test_virtual_chime(HOST,'primary',self.worker,volume=volume)
        self.worker.removal_identity.assert_not_called();self.assertEqual(self.client.calls,[])
