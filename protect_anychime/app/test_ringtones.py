from test_support import seeded_store, SYNTHETIC_MP3
"""Silent local storage/handler tests; no listeners, HA calls or playback."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from aiohttp import web
from native import NativeChime
from ringtones import RingtoneStore, MAX_RINGTONE_BYTES


MP3 = SYNTHETIC_MP3


class Content:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for start in range(0, len(self.data), size):
            yield self.data[start:start + size]


def request(data=b"", track="5", filename="Doorbell.mp3", remote="10.23.45.1",
            content_type="application/octet-stream", chunked=False):
    return SimpleNamespace(remote=remote, content=Content(data),
                           content_length=None if chunked else len(data),
                           content_type=content_type,
                           match_info={"track": track, "filename": filename})


class RingtoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = seeded_store(self.directory / "tracks")
        self.played = []
        self.native = NativeChime({"controller_ip":"10.23.45.1","native_ip":"10.23.45.20","controller_fingerprint": "00" * 32},
                                  self.directory / "identity", self.directory / "cert.pem",
                                  on_play=self.played.append, ringtone_store=self.store)
        self.native.state = {"mac": "020000000001", "adopted": True, "device_id": "local-test"}

    def tearDown(self):
        self.temp.cleanup()

    def test_default_catalog_and_actual_md5(self):
        tracks = self.store.audio_info()["speaker"]
        self.assertEqual([item["track_no"] for item in tracks], [1, 2, 3, 4])
        self.assertEqual(tracks[3]["name"], "Express-Line")
        self.assertEqual(tracks[0]["md5"], hashlib.md5(MP3).hexdigest())
        self.assertEqual(set(tracks[0]), {"track_no", "name", "md5", "state"})
        self.assertIsNone(self.store.get(5))

    async def test_empty_device_receives_all_default_bytes_without_any_playback(self):
        self.native.ringtone_store=RingtoneStore(self.directory/'empty-device')
        _,body=await self.native.process_request({'id':'sync','type':'request','action':'getAudioInfo'}, {})
        self.assertEqual(body['speaker'],[])
        for track in range(1,5):
            await self.native.handle_upload_ringtone(request(MP3,track=str(track),filename='Console tone.mp3'))
        self.assertEqual(self.native.status()['ringtones']['stored_tracks'],[1,2,3,4])
        self.assertEqual(self.played,[])

    def test_upload_persist_replace_and_immutable_default(self):
        uploaded = self.store.upload(1, "Updated default.mp3", MP3 + b"extra")
        self.assertEqual(uploaded["path"].read_bytes(), MP3 + b"extra")
        self.assertEqual(uploaded["name"], "Updated default")
        with self.assertRaises(ValueError):
            self.store.delete(1)
        second = RingtoneStore(self.directory / "tracks")
        self.assertEqual(second.get(1)["md5"], uploaded["md5"])
        first_path = uploaded["path"]
        self.store.upload(1, "Replacement.mp3", MP3)
        self.assertFalse(first_path.exists())
        self.store.upload(5, "Custom.mp3", MP3)
        self.store.delete(5)
        self.assertIsNone(RingtoneStore(self.directory / "tracks").get(5))

    def test_invalid_uploads_do_not_change_catalog(self):
        before = self.store.audio_info()
        for track in (0, 11, True, "5", 1.0):
            with self.assertRaises(ValueError):
                self.store.upload(track, "a.mp3", MP3)
        for filename in ("../a.mp3", "a/b.mp3", "a\\b.mp3", "a.wav", "a.mp3\n", ".mp3",
                         "x" * 129 + ".mp3", "a\x00.mp3"):
            with self.assertRaises(ValueError):
                self.store.upload(5, filename, MP3)
        for data in (b"not audio", b"ID3", b"ID3\x04\0\0\0\0\0\0", MP3 * 6000,
                     b"\xff\xfb\xfc\0" + b"x" * 100):
            with self.assertRaises(ValueError):
                self.store.upload(5, "a.mp3", data)
        self.assertEqual(before, self.store.audio_info())

    def test_mp3_id3_and_size_boundary(self):
        data = b"ID3\x04\0\0\0\0\0\x03tag" + MP3
        self.store.upload(5, "Tagged.mp3", data)
        self.assertEqual(self.store.get(5)["size"], len(data))
        self.store.upload(5, "Maximum.mp3", MP3 + b"\0" * (MAX_RINGTONE_BYTES - len(MP3)))
        self.assertEqual(self.store.get(5)["size"], MAX_RINGTONE_BYTES)

    def test_failed_manifest_commit_keeps_prior_track(self):
        previous = self.store.upload(5, "Before.mp3", MP3)
        with patch("ringtones.os.replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.upload(5, "After.mp3", MP3 + b"extra")
        self.assertEqual(self.store.get(5), previous)
        self.assertEqual(RingtoneStore(self.directory / "tracks").get(5), previous)
        self.assertEqual(len(list((self.directory / "tracks").glob("*.mp3"))), 5)

    async def test_raw_upload_controller_ip_and_no_credentials(self):
        result = await self.native.handle_upload_ringtone(request(MP3))
        self.assertEqual(json.loads(result.body), {"status": "ok"})
        self.assertEqual(self.store.get(5)["path"].read_bytes(), MP3)
        with self.assertRaises(web.HTTPForbidden):
            await self.native.handle_upload_ringtone(request(MP3, remote="10.23.45.2"))
        with self.assertRaises(web.HTTPUnsupportedMediaType):
            await self.native.handle_upload_ringtone(request(MP3, content_type="application/json"))
        with self.assertRaises(web.HTTPBadRequest):
            await self.native.handle_upload_ringtone(request(MP3, filename="../bad.mp3"))
        with self.assertRaises(web.HTTPBadRequest):
            await self.native.handle_delete_ringtone(request(track="1"))
        await self.native.handle_delete_ringtone(request())
        self.assertIsNone(self.store.get(5))

    async def test_chunked_upload_and_management_limits(self):
        with self.assertRaises(web.HTTPRequestEntityTooLarge):
            await self.native.handle_upload_ringtone(request(MP3 + b"0" * MAX_RINGTONE_BYTES,
                                                            chunked=True))
        self.assertIsNone(self.store.get(5))
        for chunked in (False, True):
            with self.assertRaises(web.HTTPRequestEntityTooLarge):
                await self.native._management_body(request(b" " * 16385, chunked=chunked))

    async def test_protocol_selects_real_track_and_forwards_exact_intent(self):
        self.assertTrue(self.native.info()["featureFlags"]["supportCustomRingtone"])
        header = {"id": "silent", "type": "request", "action": "getAudioInfo"}
        _, body = await self.native.process_request(header, {})
        self.assertEqual(body, self.store.audio_info())
        header["action"] = "playSpeaker"
        response, _ = await self.native.process_request(header, {"track_no": 5})
        self.assertEqual(response["errorCode"], 1)
        self.assertFalse(self.played)
        self.store.upload(5, "Chosen.mp3", MP3)
        response, _ = await self.native.process_request(header, {"track_no": 5, "volume": 100, "repeat": 10})
        self.assertEqual(response["errorCode"], 0)
        self.assertEqual({key: self.played[0][key] for key in ("track_no", "volume", "repeat")},
                         {"track_no": 5, "volume": 100, "repeat": 10})
        for payload in ({"track_no": 5, "volume": 101}, {"track_no": 5, "repeat": 11}):
            response, _ = await self.native.process_request(header, payload)
            self.assertEqual(response["errorCode"], 1)
        self.assertEqual(len(self.played), 1)

    async def test_buzzer_button_uses_the_protect_supplied_track_and_queues_only_once(self):
        response,_=await self.native.process_request({'id':'button','type':'request','action':'playBuzzer'}, {'track_no':2})
        self.assertEqual(response['errorCode'],0)
        self.assertEqual(len(self.played),1)
        self.assertEqual(self.played[0]['track_no'],2)
        self.assertEqual(self.played[0]['source_action'],'playBuzzer')
        self.assertEqual(self.played[0]['volume'],80)
        self.assertEqual(self.played[0]['repeat'],1)
        self.native.ringtone_store=RingtoneStore(self.directory/'not-synced')
        response,_=await self.native.process_request({'id':'missing','type':'request','action':'playBuzzer'}, {})
        self.assertEqual(response['errorCode'],1)
        self.assertEqual(len(self.played),1)

    async def test_source_address_is_only_bound_when_requested(self):
        class CancelBeforeNetwork:
            async def __aenter__(self):
                raise asyncio.CancelledError()

            async def __aexit__(self, *_):
                return False

        for enabled in (False, True):
            self.native.options["bind_source_ip"] = enabled
            context = Mock()
            with patch.object(self.native, "_client_ssl", return_value=context), \
                    patch("native._PinnedConnector") as connector, \
                    patch("native.aiohttp.ClientSession", return_value=CancelBeforeNetwork()):
                with self.assertRaises(asyncio.CancelledError):
                    await self.native._connection_loop()
            expected = {"ssl": context}
            if enabled:
                expected["local_addr"] = (self.native.native_ip, 0)
            connector.assert_called_once_with(self.native.controller_fingerprint, **expected)


if __name__ == "__main__":
    unittest.main()
