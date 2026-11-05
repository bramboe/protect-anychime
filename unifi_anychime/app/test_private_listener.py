"""Private management listeners are simulated; no sockets, rules or audio are used."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,AsyncMock,patch
from native import NativeChime
from runtime import NativeWorker
from registry import ChimeRegistry
from network import NetworkError

class PrivateListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_binds_a_free_port_and_defers_controller_connection(self):
        device=NativeChime({'native_enabled':True,'controller_ip':'10.23.45.1',
            'native_ip':'10.23.45.40','controller_fingerprint':'00'*32},'/unused')
        device.state={'adopted':True,'token':'synthetic'}
        runner=Mock();runner.setup=AsyncMock();runner.cleanup=AsyncMock();runner.addresses=[('10.23.45.40',41000)]
        site=Mock();site.start=AsyncMock()
        with patch.object(device,'initialize_identity'),patch.object(device,'_client_ssl'),patch.object(device,'_server_ssl',return_value='tls'), \
            patch('native.web.AppRunner',return_value=runner),patch('native.web.TCPSite',return_value=site) as factory:
            await device.start(management_host='10.23.45.40',port=0,connect=False)
        factory.assert_called_once_with(runner,'10.23.45.40',0,ssl_context='tls')
        self.assertEqual(device.listen_port,41000);self.assertIsNone(device.task)
        await device.stop()

    async def setup_worker(self, fail=False):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root=Path(directory.name);registry=ChimeRegistry(root);registry.sync(['media_player.example'])
        worker=NativeWorker();worker.registry=registry;worker.options={'native_ip':'10.23.45.20'}
        worker.network=Mock();worker.network.ensure.return_value={'state':'ready','ip':'10.23.45.40'}
        events=[]
        native=Mock();native.state={'mac':'02AABBCCDDEE'};native.options={};native.listen_port=41000
        native.removal_requested=False;native.status.return_value={'connected':True}
        native.start=AsyncMock(side_effect=lambda **_:events.append('listen'))
        native.stop=AsyncMock();native.begin_connection.side_effect=lambda:events.append('connect')
        def redirect(*_):
            events.append('redirect')
            if fail:raise NetworkError('Synthetic forwarding failure')
        worker.network.redirect_management.side_effect=redirect
        with patch('runtime.NativeChime',return_value=native),patch('runtime.TrackPlayer') as player:
            await worker._reconcile(root)
        return worker,native,events,player.return_value

    async def test_primary_uses_owned_ip_and_connects_only_after_forwarding_is_ready(self):
        worker,native,events,player=await self.setup_worker()
        native.start.assert_awaited_once_with(management_host='10.23.45.40',port=0,connect=False)
        worker.network.redirect_management.assert_called_once_with('primary','10.23.45.40',41000)
        self.assertEqual(events,['listen','redirect','connect'])
        self.assertEqual(worker.nodes['primary']['ip'],'10.23.45.40')
        self.assertTrue(native.options.get('bind_source_ip',True))

    async def test_failed_forwarding_closes_listener_and_does_not_advertise_or_connect(self):
        worker,native,events,player=await self.setup_worker(fail=True)
        native.stop.assert_awaited_once();player.stop.assert_called_once()
        native.begin_connection.assert_not_called()
        self.assertEqual(worker.nodes,{})
        self.assertEqual(worker._descriptors(),[])
