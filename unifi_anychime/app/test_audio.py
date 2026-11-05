"""No real Home Assistant calls. All request boundaries are mocked."""
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from audio import SpeakerBackend, SpeakerError, _supervisor_request

OPTIONS = {"speaker_entity": "media_player.speaker_b",
           "audio_url": "https://example.test/chime.mp3"}


class FakeHA:
    def __init__(self, state="idle", fail_post=False, features=None, level=0.05, muted=False):
        self.calls = []
        self.state = state
        self.fail_post = fail_post
        self.features = features
        self.level = level
        self.muted = muted

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"entity_id": OPTIONS["speaker_entity"], "state": self.state,
                    "attributes": {"supported_features": self.features,
                                   "volume_level": self.level, "is_volume_muted": self.muted,
                                   "media_content_id": "private-url-do-not-return"}}
        if self.fail_post:
            raise SpeakerError("Synthetic timeout")
        return []


class GenericSpeakerTests(unittest.TestCase):
    def make(self, **kwargs):
        transport = kwargs.pop("requester", FakeHA())
        return SpeakerBackend(OPTIONS, requester=transport, **kwargs), transport

    def test_default_is_silent_and_plan_has_no_side_effects(self):
        backend, fake = self.make()
        self.assertNotIn("announce", backend.plan()["body"])
        self.assertEqual(backend.plan()["volume"], 25)
        self.assertEqual(fake.calls, [])
        with self.assertRaises(SpeakerError):
            backend.arm_once(confirmed=True)
        with self.assertRaises(SpeakerError):
            backend.play_once("anything")
        self.assertEqual(fake.calls, [])

    def test_wrong_grant_and_rearming(self):
        backend, fake = self.make(allow_playback=True)
        old = backend.arm_once(confirmed=True)["approval_id"]
        new = backend.arm_once(confirmed=True)["approval_id"]
        for wrong in (old, "wrong", "niet-ascii-\u2603", None):
            with self.assertRaises(SpeakerError):
                backend.play_once(wrong)
        self.assertEqual(fake.calls, [])
        backend.play_once(new)
        self.assertEqual(len(fake.calls), 3)

    def test_entity_check_is_exactly_one_read_only_get(self):
        backend, fake = self.make()
        self.assertTrue(backend.check_entity()["available"])
        self.assertEqual(fake.calls, [("GET", "/states/media_player.speaker_b", None)])

    def test_explicit_consent_required(self):
        backend, fake = self.make(allow_playback=True)
        for value in (False, None, "yes", 1):
            with self.assertRaises(SpeakerError):
                backend.arm_once(confirmed=value)
        self.assertEqual(fake.calls, [])

    def test_one_use_exact_target_url_and_volume(self):
        backend, fake = self.make(allow_playback=True)
        grant = backend.arm_once(confirmed=True, volume=35)["approval_id"]
        self.assertEqual(fake.calls, [])
        result = backend.play_once(grant)
        self.assertTrue(result["ha_call_accepted"])
        self.assertFalse(result["acoustic_playback_verified"])
        self.assertEqual(fake.calls[1], ("POST", "/services/media_player/volume_set", {
            "entity_id": OPTIONS["speaker_entity"], "volume_level": 0.35}))
        self.assertEqual(fake.calls[2], ("POST", "/services/media_player/play_media", {
            "entity_id": OPTIONS["speaker_entity"],
            "media_content_type": "music", "media_content_id": OPTIONS["audio_url"]}))
        self.assertEqual(result["mode"], "play_media")
        self.assertTrue(result["may_interrupt"])
        with self.assertRaises(SpeakerError):
            backend.play_once(grant)
        self.assertEqual(len(fake.calls), 3)

    def test_expired_and_cancelled_grants_send_nothing(self):
        now = [0]
        backend, fake = self.make(allow_playback=True, clock=lambda: now[0])
        grant = backend.arm_once(confirmed=True, expires_in=10)["approval_id"]
        now[0] = 10
        with self.assertRaises(SpeakerError):
            backend.play_once(grant)
        grant = backend.arm_once(confirmed=True)["approval_id"]
        backend.cancel()
        with self.assertRaises(SpeakerError):
            backend.play_once(grant)
        self.assertEqual(fake.calls, [])

    def test_post_failure_is_not_retryable(self):
        backend, fake = self.make(allow_playback=True, requester=FakeHA(fail_post=True))
        grant = backend.arm_once(confirmed=True)["approval_id"]
        for _ in range(2):
            with self.assertRaises(SpeakerError):
                backend.play_once(grant)
        self.assertEqual(len(fake.calls), 2)

    def test_unavailable_speaker_does_not_receive_service_call(self):
        backend, fake = self.make(allow_playback=True, requester=FakeHA(state="unavailable"))
        grant = backend.arm_once(confirmed=True)["approval_id"]
        with self.assertRaises(SpeakerError):
            backend.play_once(grant)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "GET")

    def test_volume_lifetime_and_target_validation(self):
        backend, fake = self.make(allow_playback=True)
        for value in (True, -1, 101, float("nan"), float("inf"), "25"):
            with self.assertRaises(ValueError):
                backend.arm_once(confirmed=True, volume=value)
        for value in (True, 0, 121, float("nan")):
            with self.assertRaises(ValueError):
                backend.arm_once(confirmed=True, expires_in=value)
        for entity, url in (("switch.example", OPTIONS["audio_url"]),
                            ("media_player.a/../../evil", OPTIONS["audio_url"]),
                            (OPTIONS["speaker_entity"], "file:///tmp/chime.mp3"),
                            (OPTIONS["speaker_entity"], "https://user:password@example.test/a")):
            with self.assertRaises(ValueError):
                SpeakerBackend({"speaker_entity": entity, "audio_url": url})
        self.assertEqual(fake.calls, [])

    def test_concurrent_attempts_only_send_once(self):
        backend, fake = self.make(allow_playback=True)
        grant = backend.arm_once(confirmed=True)["approval_id"]
        results = []
        def play():
            try:
                results.append(backend.play_once(grant))
            except SpeakerError:
                results.append(None)
        threads = [threading.Thread(target=play) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(method == "POST" for method, _, _ in fake.calls), 2)

    def test_announce_only_when_feature_is_present(self):
        backend, fake = self.make(allow_playback=True,
                                  requester=FakeHA(features=4 | 1048576))
        result = backend.play_once(backend.arm_once(confirmed=True)["approval_id"])
        self.assertTrue(fake.calls[-1][2]["announce"])
        self.assertEqual(result["mode"], "announce")
        self.assertFalse(result["may_interrupt"])
        self.assertEqual(fake.calls[-1][2]['extra'],{'volume':25})
        self.assertFalse(any(path.endswith('/volume_set') for _,path,_ in fake.calls))

    def test_readonly_volume_must_be_known_and_no_higher_than_target(self):
        for level, allowed in ((0.05, True), (0.25, True), (0.26, False),
                               (None, False), (float("nan"), False)):
            with self.subTest(level=level):
                backend, fake = self.make(allow_playback=True,
                                          requester=FakeHA(features=512, level=level))
                grant = backend.arm_once(confirmed=True, volume=25)["approval_id"]
                if allowed:
                    backend.play_once(grant)
                    self.assertEqual(len(fake.calls), 2)
                    self.assertEqual(fake.calls[-1][1], "/services/media_player/play_media")
                else:
                    with self.assertRaisesRegex(SpeakerError, "cannot set its volume"):
                        backend.play_once(grant)
                    self.assertEqual(len(fake.calls), 1)

    def test_inactive_or_muted_players_are_not_changed(self):
        for state in ("off", "standby", "unknown", "unavailable"):
            backend, fake = self.make(allow_playback=True, requester=FakeHA(state=state))
            with self.assertRaises(SpeakerError):
                backend.play_once(backend.arm_once(confirmed=True)["approval_id"])
            self.assertEqual(len(fake.calls), 1)
        backend, fake = self.make(allow_playback=True, requester=FakeHA(muted=True))
        with self.assertRaises(SpeakerError):
            backend.play_once(backend.arm_once(confirmed=True)["approval_id"])
        self.assertEqual(len(fake.calls), 1)

    def test_metadata_is_allowlisted_and_get_failure_consumes_grant(self):
        backend, _ = self.make()
        self.assertNotIn("private-url", str(backend.check_entity()))
        def failing_get(*args):
            raise SpeakerError("Synthetic state failure")
        backend, _ = self.make(allow_playback=True, requester=failing_get)
        grant = backend.arm_once(confirmed=True)["approval_id"]
        with self.assertRaisesRegex(SpeakerError, "No online"):
            backend.play_once(grant)
        with self.assertRaisesRegex(SpeakerError, "no matching"):
            backend.play_once(grant)

    def test_full_protect_volume_range_is_supported(self):
        for volume in (0, 10, 37, 100):
            backend, fake = self.make(allow_playback=True)
            backend.play_once(backend.arm_once(confirmed=True, volume=volume)["approval_id"])
            if volume==0:self.assertEqual(fake.calls,[])
            else:self.assertEqual(fake.calls[1][2]["volume_level"], volume / 100)

    def test_missing_token_and_http_error_do_not_expose_secrets(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SpeakerError):
                _supervisor_request("GET", "/states/media_player.speaker_b")
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "synthetic-private-token"}):
            with patch("audio.build_opener") as build:
                build.return_value.open.side_effect = HTTPError(
                    "http://private.invalid/synthetic-private-token", 403,
                    "synthetic-private-token", {}, None)
                with self.assertRaises(SpeakerError) as caught:
                    _supervisor_request("GET", "/states/media_player.speaker_b")
                self.assertNotIn("synthetic-private-token", str(caught.exception))
                self.assertEqual(build.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()

class GroupAudioTests(unittest.TestCase):
    def setup_backend(self, *, changed=False, unsafe_volume=False):
        members=['media_player.a','media_player.b'];calls=[]
        def request(method,path,body):
            calls.append((method,path,body))
            if method=='GET':
                entity=path.rsplit('/',1)[1]
                return {'entity_id':entity,'state':'idle','attributes':{
                    'supported_features':512 if unsafe_volume and entity==members[1] else 516,
                    'volume_level':.9 if unsafe_volume else .1,
                    'group_members':['media_player.a','media_player.other'] if changed else members}}
            return []
        backend=SpeakerBackend({'speaker_entity':members[0],'audio_url':'http://example.test/group.mp3','group_members':members},
            allow_playback=True,requester=request)
        return backend,calls

    def test_group_sets_member_volumes_but_sends_media_once_to_coordinator(self):
        backend,calls=self.setup_backend();grant=backend.arm_once(confirmed=True,volume=40)
        result=backend.play_once(grant['approval_id'])
        posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0][2],{'entity_id':['media_player.a','media_player.b'],'volume_level':.4})
        self.assertEqual(posts[1][2]['entity_id'],'media_player.a')
        self.assertNotIn('announce',posts[1][2]);self.assertEqual(result['mode'],'play_media')
        self.assertFalse(result['acoustic_playback_verified'])

    def test_music_group_changes_do_not_change_chime_membership(self):
        backend,calls=self.setup_backend(changed=True)
        backend.play_once(backend.arm_once(confirmed=True)['approval_id'])
        self.assertTrue(any(path.endswith('/play_media') for _,path,_ in calls))

    def test_member_volume_is_preflighted_before_changing_any_speaker(self):
        backend,calls=self.setup_backend(unsafe_volume=True);grant=backend.arm_once(confirmed=True,volume=40)
        with self.assertRaises(SpeakerError):backend.play_once(grant['approval_id'])
        self.assertFalse(any(c[0]=='POST' for c in calls))
