from test_support import ringtone_file, seeded_store
import asyncio
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, AsyncMock, patch
from registry import ChimeRegistry
from ringtones import RingtoneStore
from playback import TrackPlayer
from discovery import MultiResponder, PROBE, announcement, parse
from runtime import NativeWorker
from native import NativeChime
from ucp import encode, Decoder
import server

ROOT=Path(__file__).resolve().parent


class RegistryTests(unittest.TestCase):
    def test_first_speaker_preserves_existing_identity_and_second_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'native').mkdir()
            marker=root/'native/identity.json'
            marker.write_text('existing-private-identity-do-not-replace')
            registry=ChimeRegistry(root)
            entries=registry.sync(['media_player.speaker_a','media_player.tv'])
            self.assertEqual(entries[0]['key'],'primary')
            self.assertEqual(registry.identity_dir(entries[0]),root/'native')
            self.assertNotEqual(registry.identity_dir(entries[0]),registry.identity_dir(entries[1]))
            self.assertEqual(marker.read_text(),'existing-private-identity-do-not-replace')
            savedkey=entries[1]['key']
            registry.sync(['media_player.speaker_a'])
            reloaded=ChimeRegistry(root)
            self.assertFalse(reloaded.snapshot()[1]['active'])
            reloaded.sync(['media_player.tv','media_player.speaker_a'],[
                {'entity_id':'media_player.tv','name':'Renamed TV'}])
            self.assertEqual(reloaded.snapshot()[1]['key'],savedkey)
            self.assertEqual(reloaded.snapshot()[1]['name'],'Renamed TV')
            self.assertEqual(registry.path.stat().st_mode&0o077,0)

    def test_no_identity_or_network_created_when_registry_is_only_constructed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(Path(directory)/'new')
            self.assertFalse((registry.directory/'native').exists())
            self.assertTrue((registry.directory/'chime-schema.json').exists())


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=seeded_store(Path(self.temp.name)/'tracks')
        self.backend=Mock()
        self.backend.arm_once.return_value={'approval_id':'one-use'}
        self.backend.play_once.return_value={'ha_call_accepted':True}
        self.factory=Mock(return_value=self.backend)
        self.player=TrackPlayer('primary','media_player.tv',self.store,'http://10.23.45.20:48199',
                               backend_factory=self.factory,duration_probe=lambda _:0)
    def tearDown(self):
        self.player.stop()
        self.temp.cleanup()
    def complete(self):
        self.player.thread.join(2)
    def test_protect_volume_and_exact_selected_ringtone_are_forwarded_without_global_cap(self):
        self.player.submit({'track_no':3,'volume':82,'repeat':1})
        self.complete()
        options=self.factory.call_args.args[0]
        asset=self.store.get(3)
        self.assertEqual(options['speaker_entity'],'media_player.tv')
        self.assertEqual(options['audio_url'],f"http://10.23.45.20:48199/media/chimes/primary/3/{asset['md5']}.mp3")
        self.backend.arm_once.assert_called_once_with(confirmed=True,volume=82,expires_in=60)
        self.assertEqual(self.player.status()['last_result']['ha_calls_accepted'],1)
        self.assertFalse(self.player.status()['last_result']['acoustic_playback_verified'])
    def test_repeat_count_is_honoured_and_scheduled_once_per_iteration(self):
        self.player.submit({'track_no':2,'volume':65,'repeat':3})
        self.complete()
        self.assertEqual(self.backend.play_once.call_count,3)
        self.assertEqual(self.player.status()['last_result']['repeat'],3)
    def test_one_failure_stops_repetitions_without_retry(self):
        self.backend.play_once.side_effect=ValueError('HA rejected the request')
        self.player.submit({'track_no':1,'volume':40,'repeat':4})
        self.complete()
        self.backend.play_once.assert_called_once()
        self.assertEqual(self.player.status()['last_result']['state'],'failed')
    def test_missing_track_and_zero_volume_send_no_requests(self):
        self.assertEqual(self.player.submit({'track_no':6,'volume':45,'repeat':1})['forwarding'],'rejected')
        self.assertEqual(self.player.submit({'track_no':1,'volume':0,'repeat':1})['forwarding'],'muted')
        self.factory.assert_not_called()
    def test_invalid_controls_do_not_start_a_job(self):
        for values in [(1,101,1),(1,-1,1),(1,True,1),(1,float('nan'),1),(1,20,11),(0,20,1)]:
            track,volume,repeat=values
            self.assertEqual(self.player.submit(dict(track_no=track,volume=volume,repeat=repeat))['forwarding'],'rejected')
        self.factory.assert_not_called()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_protect_ucp_track_request_reaches_only_its_linked_generic_tv(self):
        calls=[]
        def request(method,path,body=None):
            calls.append((method,path,body))
            if method=='GET':return {'entity_id':'media_player.example_tv','state':'on',
                                    'attributes':{'supported_features':516,'volume_level':.2}}
            return []
        from audio import SpeakerBackend
        with tempfile.TemporaryDirectory() as directory:
            store=seeded_store(Path(directory)/'tracks')
            def factory(options,**kw):return SpeakerBackend(options,requester=request,**kw)
            player=TrackPlayer('primary','media_player.example_tv',store,'http://10.23.45.20:48199',backend_factory=factory)
            native=NativeChime({'controller_ip':'10.23.45.1','native_ip':'10.23.45.20','controller_fingerprint':'00'*32},Path(directory)/'identity','unused.pem',
                               on_play=player.submit,ringtone_store=store)
            header,body=Decoder().feed(encode({'id':'protect-command','type':'request','action':'playSpeaker'},
                                              {'track_no':4,'volume':73,'repeat':1}))[0]
            result,_=await native.process_request(header,body)
            player.thread.join(2)
            self.assertEqual(result['errorCode'],0)
            self.assertEqual(calls[1],('POST','/services/media_player/volume_set',
                                     {'entity_id':'media_player.example_tv','volume_level':.73}))
            self.assertEqual(calls[2][2]['entity_id'],'media_player.example_tv')
            self.assertNotIn('announce',calls[2][2])
            self.assertNotIn('extra',calls[2][2])
            self.assertIn('/4/',calls[2][2]['media_content_id'])
            player.stop()


class MultiDiscoveryTests(unittest.TestCase):
    def test_each_chime_is_advertised_with_its_own_ip_mac_and_name(self):
        descriptors=[{'ip':'10.23.45.20','packet':announcement('025253000001','10.23.45.20',name='Main Speaker')},
                     {'ip':'10.23.45.21','packet':announcement('025253000002','10.23.45.21',name='TV')}]
        responder=MultiResponder('10.23.45.20',['10.23.45.1'],lambda:descriptors)
        with patch('discovery.socket.socket') as socket_factory:
            replies=responder.responses_to(PROBE,('10.23.45.1',40000),100)
            socket_factory.assert_not_called()
        self.assertEqual([ip for ip,_ in replies],['10.23.45.20','10.23.45.21'])
        self.assertEqual(dict(parse(replies[0][1]))[11],b'Main Speaker')
        self.assertNotEqual(dict(parse(replies[0][1]))[1],dict(parse(replies[1][1]))[1])
        self.assertEqual(responder.responses_to(PROBE,('10.23.45.1',40000),100.5),[])
        self.assertEqual(responder.responses_to(PROBE,('10.23.45.2',40000),102),[])


class MediaTests(unittest.TestCase):
    def test_audio_range_and_head_are_supported_without_exposing_private_files(self):
        handler=server.Handler.__new__(server.Handler)
        handler.command='GET';handler.headers={'Range':'bytes=4-9'}
        handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
        handler.wfile=io.BytesIO()
        handler.serve_audio(ringtone_file())
        handler.send_response.assert_called_once_with(206)
        self.assertEqual(len(handler.wfile.getvalue()),6)
        handler.command='HEAD';handler.wfile=io.BytesIO()
        handler.serve_audio(ringtone_file())
        self.assertEqual(handler.wfile.getvalue(),b'')
    def test_invalid_range_is_rejected(self):
        handler=server.Handler.__new__(server.Handler)
        handler.headers={'Range':'bytes=99999999-'};handler.respond=Mock()
        handler.serve_audio(ringtone_file())
        self.assertEqual(handler.respond.call_args.args[0],416)


if __name__=='__main__':unittest.main()


class StartupDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_console_certificate_is_logged_without_opening_network(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            worker=NativeWorker()
            worker.options={'native_ip':'10.23.45.20','controller_ip':'10.23.45.1'}
            output=io.StringIO()
            with patch('runtime.NetworkManager') as network, contextlib.redirect_stdout(output):
                await worker._run(Path(directory))
            network.assert_not_called()
            self.assertIn('no saved console certificate',output.getvalue())
            self.assertIn('Connect to your Protect console',worker.error)
            self.assertTrue(worker.waiting_for_setup)
    def test_startup_diagnostic_does_not_read_or_print_private_identity_contents(self):
        import contextlib
        with tempfile.TemporaryDirectory() as directory:
            native=Path(directory)/'native';native.mkdir()
            (native/'identity.json').write_text('private-test-do-not-print')
            worker=NativeWorker();output=io.StringIO()
            with patch('runtime.threading.Thread'), contextlib.redirect_stdout(output):
                worker.start({'controller_ip':'10.23.45.1','controller_fingerprint':'private-test-fingerprint'},str(native))
            self.assertTrue(worker.status()['diagnostics']['saved_primary_identity'])
            self.assertIn('certificate_configured=True',output.getvalue())
            self.assertNotIn('private-test',output.getvalue())

class RemovalRegistryTests(unittest.TestCase):
    def test_protect_removal_persists_and_only_deselects_its_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory)
            entries=registry.sync(['media_player.a','media_player.b'])
            primary=entries[0]['key'];other=entries[1]['key']
            worker=NativeWorker();worker.registry=registry
            worker._discovery=[{'key':primary,'ip':'10.23.45.10'},{'key':other,'ip':'10.23.45.11'}]
            worker.nodes={primary:{'player':Mock()},other:{'player':Mock()}}
            self.assertTrue(worker.remove_from_protect(primary))
            worker.nodes[primary]['player'].stop.assert_called_once()
            worker.nodes[other]['player'].stop.assert_not_called()
            self.assertEqual(worker._descriptors(),[{'key':other,'ip':'10.23.45.11'}])
            saved=ChimeRegistry(directory).snapshot()
            self.assertFalse(saved[0]['active']);self.assertTrue(saved[1]['active'])
            self.assertEqual(worker.status()['selected_entities'],['media_player.b'])
            # A confirmed removed identity is never reused while cleanup is pending.
            restored=registry.sync(['media_player.a','media_player.b'])
            self.assertFalse(restored[0]['active']);self.assertTrue(any(e['active'] and e['entity_id']=='media_player.a' and e['key']!=primary for e in restored))

    def test_failed_save_keeps_selection_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory);registry.sync(['media_player.a'])
            with patch.object(registry,'_save',side_effect=OSError('disk')):
                with self.assertRaises(OSError):registry.deactivate_from_protect('primary')
            self.assertTrue(registry.snapshot()[0]['active'])

class UnassignedRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_unassigned_primary_is_not_resurrected_as_legacy_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'native').mkdir();(root/'native/identity.json').write_text('{}')
            registry=ChimeRegistry(root);registry.deactivate_from_protect('primary')
            worker=NativeWorker();worker.registry=ChimeRegistry(root)
            worker.network=Mock();worker.options={'native_ip':'10.23.45.20'}
            with patch('runtime.NativeChime') as native:
                await worker._reconcile(root)
            native.assert_not_called();self.assertEqual(worker._descriptors(),[])

class RemovalDuringStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_removal_while_listener_starts_cannot_readvertise_a_chime(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory);registry.sync(['media_player.a'])
            worker=NativeWorker();worker.registry=registry;worker.network=Mock()
            worker.options={'native_ip':'10.23.45.20','controller_fingerprint':'00'*32}
            worker.network.ensure.return_value={'state':'ready','ip':'10.23.45.40'}
            native=Mock();native.state={'mac':'02AABBCCDDEE'};native.listen_port=41000;native.start=AsyncMock(side_effect=lambda **_:registry.deactivate_from_protect('primary'))
            native.stop=AsyncMock()
            with patch('runtime.NativeChime',return_value=native),patch('runtime.TrackPlayer') as player:
                await worker._reconcile(Path(directory))
                player.return_value.stop.assert_called_once()
            native.stop.assert_awaited_once();self.assertEqual(worker.nodes,{})
            self.assertEqual(worker._descriptors(),[])
