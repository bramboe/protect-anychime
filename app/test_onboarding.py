"""New-speaker jobs use fake backends only; no network or acoustic playback."""
import threading
import time
import unittest

from onboarding import SpeakerOnboarding


class FakeFactory:
    def __init__(self, fail=False, gate=None):
        self.calls = []
        self.fail = fail
        self.gate = gate
        self.started = threading.Event()

    def __call__(self, options, *, allow_playback=False):
        self.calls.append(("create", options, allow_playback))
        owner = self
        class Backend:
            def arm_once(self, *, confirmed=False, volume=0):
                owner.calls.append(("arm", confirmed, volume))
                return {"approval_id": "synthetic-once"}

            def play_once(self, grant):
                owner.calls.append(("play", grant))
                owner.started.set()
                if owner.gate:
                    owner.gate.wait(2)
                if owner.fail:
                    raise RuntimeError("private-url-and-token-do-not-return")
                return {"ha_call_accepted": True, "acoustic_playback_verified": False}
        return Backend()


class OnboardingTests(unittest.TestCase):
    def wait_done(self, job):
        deadline = time.monotonic() + 2
        while job.status()["busy"] and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        self.assertFalse(job.status()["busy"])
        return job.status()

    def test_constructor_status_stop_are_silent(self):
        factory = FakeFactory()
        reads = []
        job = SpeakerOnboarding(lambda: reads.append(True), factory)
        self.assertEqual(job.status()["state"], "idle")
        job.stop()
        self.assertEqual(factory.calls, [])
        self.assertEqual(reads, [])
        self.assertEqual(job.submit([]), {"state": "empty", "audio_played": False})
        self.assertEqual(reads, [])

    def test_explicit_new_entities_once_with_success_asset_and_ten_percent(self):
        options = {"success_url": "https://example.test/original-success.mp3",
                   "audio_url": "https://example.test/other.mp3", "playback_enabled": False}
        factory = FakeFactory()
        job = SpeakerOnboarding(lambda: options, factory)
        self.assertEqual(job.submit(["media_player.tv", "media_player.tv", "media_player.speaker"]),
                         {"state": "queued", "audio_played": None})
        status = self.wait_done(job)
        self.assertEqual(len(status["results"]), 2)
        self.assertTrue(all(item["ha_call_accepted"] for item in status["results"]))
        self.assertTrue(all(item["acoustic_playback_verified"] is False for item in status["results"]))
        created = [call for call in factory.calls if call[0] == "create"]
        self.assertEqual([call[1]["speaker_entity"] for call in created],
                         ["media_player.tv", "media_player.speaker"])
        self.assertTrue(all(call[1]["audio_url"] == options["success_url"] for call in created))
        self.assertTrue(all(call[2] is True for call in created))
        self.assertEqual([call for call in factory.calls if call[0] == "arm"],
                         [("arm", True, 10), ("arm", True, 10)])
        before = len(factory.calls)
        self.assertEqual(job.submit(["media_player.tv"])["state"], "empty")
        job.status()
        self.assertEqual(len(factory.calls), before)

    def test_failure_is_per_entity_without_retry_or_error_leaks(self):
        factory = FakeFactory(fail=True)
        job = SpeakerOnboarding(lambda: {"success_url": "https://example.test/a.mp3"}, factory)
        job.submit(["media_player.a", "media_player.b"])
        status = self.wait_done(job)
        self.assertEqual(len(status["results"]), 2)
        self.assertTrue(all(item["ha_call_accepted"] is False for item in status["results"]))
        self.assertNotIn("private-url", str(status))
        self.assertEqual(len([call for call in factory.calls if call[0] == "play"]), 2)
        self.assertEqual(job.submit(["media_player.a"])["state"], "empty")

    def test_busy_snapshot_and_stop_prevent_remaining_entity(self):
        gate = threading.Event()
        factory = FakeFactory(gate=gate)
        options = {"success_url": "https://example.test/original.mp3"}
        job = SpeakerOnboarding(lambda: options, factory)
        job.submit(["media_player.a", "media_player.b"])
        self.assertTrue(factory.started.wait(1))
        self.assertEqual(job.submit(["media_player.c"])["state"], "busy")
        options["success_url"] = "https://example.test/replaced.mp3"
        self.assertEqual(factory.calls[0][1]["audio_url"], "https://example.test/original.mp3")
        before = len(factory.calls)
        job.stop()
        self.assertEqual(len(factory.calls), before)
        gate.set()
        status = self.wait_done(job)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(len(status["results"]), 1)
        self.assertEqual(len([call for call in factory.calls if call[0] == "play"]), 1)

    def test_bounded_input_and_valid_entities(self):
        factory = FakeFactory()
        job = SpeakerOnboarding(lambda: {}, factory)
        for entities in (["switch.invalid"], "media_player.a", [None],
                         [f"media_player.s{i}" for i in range(17)]):
            with self.assertRaises(ValueError):
                job.submit(entities)
        self.assertEqual(factory.calls, [])


if __name__ == "__main__":
    unittest.main()
