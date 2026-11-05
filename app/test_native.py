import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from aiohttp import web
import aiohttp
from types import SimpleNamespace
from native import NativeChime, _PinnedConnector
from ucp import Decoder, encode


class AsyncContext:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *_):
        return False


class Request:
    def __init__(self, body, remote="10.23.45.1"):
        self.remote = remote
        self.body = body
    async def json(self):
        return self.body


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "identity"
        self.device = NativeChime({"controller_ip": "10.23.45.1",
                                   "native_ip": "10.23.45.20",
                                   "controller_fingerprint": "00" * 32}, self.path,
                                   Path(self.temp.name) / "controller.pem")
        # Identity generation is explicit and local, with no listeners or clients.
        self.device.initialize_identity()

    def tearDown(self):
        self.temp.cleanup()

    def credentials(self):
        return {"username": "ubnt", "password": "ubnt"}

    def test_stable_private_identity(self):
        mac = self.device.state["mac"]
        self.device.initialize_identity()
        self.assertEqual(mac, self.device.state["mac"])
        self.assertEqual(int(mac[:2], 16) & 3, 2)
        for name in ["identity.json", "device-key.pem", "device-cert.pem"]:
            self.assertEqual((self.path / name).stat().st_mode & 0o077, 0)
        self.assertFalse(self.device.status()["enabled"])

    async def test_info_controller_and_credentials(self):
        response = await self.device.handle_info(Request(self.credentials()))
        self.assertEqual(json.loads(response.body)["type"], "UP Chime PoE")
        for request in [Request(self.credentials(), "10.23.45.2"),
                        Request({"username": "ubnt", "password": "incorrect"})]:
            with self.assertRaises(web.HTTPForbidden):
                await self.device.handle_info(request)

    async def test_adoption_saves_only_expected_console(self):
        adopted = {**self.credentials(), "protocol": "wss", "mode": 0,
                   "token": "synthetic-token-for-local-test", "consoleId": "synthetic-console",
                   "hosts": ["https://malicious.example"]}
        self.device._connection_loop = AsyncMock()
        response = await self.device.handle_adopt(Request(adopted))
        self.assertEqual(response.status, 200)
        await self.device.task
        self.assertEqual(self.device.state["console_id"], "synthetic-console")
        self.assertEqual(self.device.controller_ip, "10.23.45.1")
        self.assertEqual(self.device.controller_path, "/camera/1.0/ws")
        with self.assertRaises(web.HTTPForbidden):
            await self.device.handle_adopt(Request({**adopted, "consoleId": "another-console"}))

    async def test_password_rotation_persists(self):
        header = {"type": "request", "id": "password-test", "action": "changeUserPassword"}
        response, _ = await self.device.process_request(header,
            {"username": "ubnt", "passwordOld": "wrong", "passwordNew": "new-test-password"})
        self.assertNotEqual(response["errorCode"], 0)
        response, _ = await self.device.process_request(header,
            {"username": "ubnt", "passwordOld": "ubnt", "passwordNew": "new-test-password"})
        self.assertEqual(response["errorCode"], 0)
        self.device.initialize_identity()
        self.assertEqual(self.device.state["password"], "new-test-password")

    async def test_play_is_only_recorded(self):
        header = {"type": "request", "id": "silent-test", "action": "playSpeaker"}
        response, body = await self.device.process_request(header,
            {"track_no": 1, "volume": 25, "repeat": 1})
        self.assertEqual(response["errorCode"], 0)
        self.assertFalse(self.device.last_play["audio_played"])
        self.assertFalse(self.device.status()["audio_played"])
        self.assertEqual(self.device.last_play["volume"], 25)

    async def test_unsafe_unknown_commands_are_rejected(self):
        for action in ["upgrade", "reboot", "sshService", "playFile"]:
            response, _ = await self.device.process_request(
                {"type": "request", "id": "reject-test", "action": action}, {})
            self.assertNotEqual(response["errorCode"], 0)

    async def test_bounded_reconnect(self):
        self.device._client_ssl = lambda: (_ for _ in ()).throw(ValueError("private secret"))
        with patch("native.asyncio.sleep", new_callable=AsyncMock) as delay:
            await self.device._connection_loop()
        self.assertEqual(self.device.connection_attempts, 3)
        self.assertEqual(delay.await_count, 2)
        self.assertNotIn("private secret", self.device.last_error)

    async def test_transient_outage_retries_beyond_three_without_playback(self):
        original = self.device.state.copy()
        self.device.on_play = Mock()
        with patch.object(self.device, "_client_ssl", side_effect=ConnectionRefusedError("offline")), \
                patch("native.asyncio.sleep", new_callable=AsyncMock) as delay:
            delay.side_effect = [None, None, None, None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await self.device._connection_loop()
        self.assertEqual(self.device.connection_attempts, 5)
        self.assertEqual([c.args[0] for c in delay.await_args_list], [5, 10, 20, 40, 60])
        self.device.on_play.assert_not_called()
        self.assertEqual(self.device.state, original)

    async def test_certificate_errors_remain_bounded_without_playback(self):
        import ssl
        self.device.on_play = Mock()
        with patch.object(self.device, "_client_ssl", side_effect=ssl.SSLError("mismatch")), \
                patch("native.asyncio.sleep", new_callable=AsyncMock) as delay:
            await self.device._connection_loop()
        self.assertEqual(self.device.connection_attempts, 3)
        self.assertEqual(delay.await_count, 2)
        self.device.on_play.assert_not_called()

    async def test_normal_socket_closure_reconnects_without_replaying_audio(self):
        async def incoming():
            if False:
                yield None
        class WebSocket:
            protocol = "ucp4"
            def __aiter__(self):
                return incoming()
        session = Mock()
        session.ws_connect.return_value = AsyncContext(WebSocket())
        self.device.on_play = Mock()
        with patch.object(self.device, "_client_ssl", return_value=Mock()), \
                patch("native._PinnedConnector"), \
                patch("native.aiohttp.ClientSession", return_value=AsyncContext(session)), \
                patch("native.asyncio.sleep", new_callable=AsyncMock) as delay:
            delay.side_effect = [None, None, None, asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await self.device._connection_loop()
        self.assertEqual(session.ws_connect.call_count, 4)
        self.device.on_play.assert_not_called()
        self.assertFalse(self.device.connected)

    async def test_runtime_disabled_by_default(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            await self.device.start()

    async def test_missing_protocol_echo_still_processes_ucp_get_info(self):
        async def incoming():
            yield SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=encode(
                {"type": "request", "id": "controller-get-info", "action": "getInfo"}, {}))
            raise asyncio.CancelledError()
        for protocol in (None, "ucp4"):
            with self.subTest(protocol=protocol):
                class WebSocket:
                    def __aiter__(self):
                        return incoming()
                ws = WebSocket()
                ws.protocol = protocol
                ws.send_bytes = AsyncMock()
                session = Mock()
                session.ws_connect.return_value = AsyncContext(ws)
                with patch.object(self.device, "_client_ssl", return_value=Mock()), \
                        patch("native._PinnedConnector"), \
                        patch("native.aiohttp.ClientSession", return_value=AsyncContext(session)):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.device._connection_loop()
                ws.send_bytes.assert_awaited_once()
                header, body = Decoder().feed(ws.send_bytes.call_args.args[0])[0]
                self.assertEqual(header["id"], "controller-get-info")
                self.assertEqual(header["errorCode"], 0)
                self.assertEqual(body["type"], "UP Chime PoE")
                self.assertTrue(self.device.state["adopted"])

    async def test_explicit_other_protocol_is_rejected(self):
        session = Mock()
        session.ws_connect.return_value = AsyncContext(Mock(protocol="unrelated-protocol"))
        self.device.state["adopted"] = False
        with patch.object(self.device, "_client_ssl", return_value=Mock()), \
                patch("native._PinnedConnector"), \
                patch("native.aiohttp.ClientSession", return_value=AsyncContext(session)), \
                patch("native.asyncio.sleep", new_callable=AsyncMock):
            await self.device._connection_loop()
        self.assertFalse(self.device.state["adopted"])
        self.assertEqual(self.device.last_error_type, "ValueError")

    async def test_exact_peer_certificate_pin_before_connection_return(self):
        peer_der = b"synthetic-peer-certificate"
        transport = Mock()
        transport.get_extra_info.return_value.getpeercert.return_value = peer_der
        connector = _PinnedConnector(hashlib.sha256(peer_der).hexdigest())
        try:
            with patch("aiohttp.TCPConnector._wrap_create_connection", new_callable=AsyncMock,
                       return_value=(transport, "protocol")):
                result = await connector._wrap_create_connection()
                self.assertEqual(result, (transport, "protocol"))
                connector.expected_fingerprint = "00" * 32
                with self.assertRaises(Exception):
                    await connector._wrap_create_connection()
                transport.close.assert_called_once()
        finally:
            await connector.close()

    def test_compatibility_profile_consistent_on_all_wire_fields(self):
        from discovery import parse
        from compatibility import profile_version
        self.assertEqual(profile_version({}), "1.7.20")
        for version in ("1.3.7", "1.7.20"):
            device = NativeChime({"controller_ip": "10.23.45.1",
                "native_ip": "10.23.45.20", "controller_fingerprint": "00" * 32,
                "unifi_compatibility_profile": version}, self.path,
                Path(self.temp.name) / "controller.pem")
            device.initialize_identity()
            self.assertEqual(device.info()["version"], "1.7.20")
            self.assertEqual(device._headers()["x-version"], "1.7.20")
            self.assertEqual(dict(parse(device.discovery_response()))[3], b"1.7.20")
        with self.assertRaises(ValueError):
            profile_version({"unifi_compatibility_profile": "999.0.0"})

    async def test_firmware_commands_rejected_without_reading_payload(self):
        class UnreadablePayload(dict):
            def get(self, *_):
                raise AssertionError("Firmware payload must not be inspected")
        original = dict(self.device.state)
        self.device.on_play = Mock()
        with patch("native.aiohttp.ClientSession") as session:
            for action in ("updateFirmware", "updateSoftware"):
                header, body = await self.device.process_request(
                    {"type": "request", "id": "firmware-test", "action": action},
                    UnreadablePayload(uri="https://invalid.example/firmware"))
                self.assertEqual(header["id"], "firmware-test")
                self.assertNotEqual(header["errorCode"], 0)
                self.assertEqual(body["reason"], "virtual_device")
            session.assert_not_called()
        self.device.on_play.assert_not_called()
        self.assertEqual(self.device.state, original)
        self.assertEqual(self.device.status()["firmware_policy"], "reject_vendor_firmware")

    def test_incomplete_identity_is_not_replaced(self):
        (self.path / "device-key.pem").unlink()
        with self.assertRaises(ValueError):
            self.device.initialize_identity()


if __name__ == "__main__":
    unittest.main()

class RemovalTests(unittest.IsolatedAsyncioTestCase):
    setUp = NativeTests.setUp
    tearDown = NativeTests.tearDown

    async def test_factory_reset_disables_only_linked_chime_and_preserves_identity(self):
        old_mac=self.device.state['mac'];old_id=self.device.state['device_id']
        key=(self.path/'device-key.pem').read_bytes()
        self.device.state.update(adopted=True,token='old-token',console_id='console',password='old-password')
        self.device.on_remove=Mock(return_value=True);self.device.on_play=Mock()
        header={'type':'request','id':'remove','action':'factoryReset'}
        response,body=await self.device.process_request(header,{})
        self.assertEqual(response['errorCode'],0);self.assertEqual(body['status'],'ok')
        self.assertTrue(self.device.removal_requested)
        self.assertFalse(self.device.state['adopted']);self.assertIsNone(self.device.state['token'])
        self.assertIsNone(self.device.state['console_id']);self.assertEqual(self.device.state['password'],'ubnt')
        self.assertEqual(self.device.state['mac'],old_mac);self.assertEqual(self.device.state['device_id'],old_id)
        self.assertEqual((self.path/'device-key.pem').read_bytes(),key)
        await self.device.process_request(header,{})
        self.device.on_remove.assert_called_once();self.device.on_play.assert_not_called()

    async def test_factory_reset_is_not_acknowledged_without_a_removal_handler(self):
        response,_=await self.device.process_request({'type':'request','id':'remove','action':'factoryReset'},{})
        self.assertNotEqual(response['errorCode'],0);self.assertFalse(self.device.removal_requested)

    async def test_failed_removal_does_not_reset_identity(self):
        self.device.state.update(adopted=True,token='keep-token',console_id='keep-console')
        previous=self.device.state.copy();self.device.on_remove=Mock(return_value=False)
        response,_=await self.device.process_request({'type':'request','id':'remove','action':'factoryReset'},{})
        self.assertNotEqual(response['errorCode'],0);self.assertEqual(self.device.state,previous)

    async def test_ack_is_sent_before_disconnect_and_no_reconnect_or_play(self):
        async def incoming():
            yield SimpleNamespace(type=aiohttp.WSMsgType.BINARY,data=encode(
                {'type':'request','id':'remove','action':'factoryReset'},{}))
        class WebSocket:
            protocol='ucp4'
            def __aiter__(self):return incoming()
        ws=WebSocket();ws.send_bytes=AsyncMock()
        session=Mock();session.ws_connect.return_value=AsyncContext(ws)
        self.device.on_remove=Mock(return_value=True);self.device.on_play=Mock()
        with patch.object(self.device,'_client_ssl',return_value=Mock()),patch('native._PinnedConnector'), \
                patch('native.aiohttp.ClientSession',return_value=AsyncContext(session)), \
                patch('native.asyncio.sleep',new_callable=AsyncMock) as delay:
            await self.device._connection_loop()
        self.assertEqual(session.ws_connect.call_count,1);delay.assert_not_awaited()
        header,body=Decoder().feed(ws.send_bytes.call_args.args[0])[0]
        self.assertEqual(header['id'],'remove');self.assertEqual(header['errorCode'],0)
        self.device.on_play.assert_not_called();self.assertFalse(self.device.connected)
