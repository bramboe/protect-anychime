"""Volume per speaker relative to Protect's volume. Fake transports only; no audio."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audio import SpeakerBackend, scaled_volume
from manual_timing import ManualTiming, SpeakerLevels

A, B, C = [f'media_player.{c}' for c in 'abc']
URL = 'http://example.test/media/chimes/primary/1/' + 'a' * 32 + '.mp3'


def state(entity, *, features=1049092, group=None, level=.1):
    return {'entity_id': entity, 'state': 'idle', 'attributes': {'supported_features': features,
            'group_members': group or [entity], 'volume_level': level, 'is_volume_muted': False}}


def play(states, platforms, levels, volume=100, members=None):
    calls = []
    def request(method, path, body):
        calls.append((method, path, body))
        return states[path[len('/states/'):]] if method == 'GET' else []
    members = members or list(states)
    backend = SpeakerBackend({'speaker_entity': members[0], 'audio_url': URL, 'group_members': members,
                              'member_platforms': platforms, 'member_levels': levels},
                             requester=request, allow_playback=True)
    result = backend.play_once(backend.arm_once(confirmed=True, volume=volume)['approval_id'])
    return result, [c for c in calls if c[0] == 'POST']


class ScalingTests(unittest.TestCase):
    def test_level_scales_protect_volume_and_never_silences_on_its_own(self):
        self.assertEqual(scaled_volume(100, 60), 60)
        self.assertEqual(scaled_volume(50, 60), 30)
        self.assertEqual(scaled_volume(10, 100), 10)
        self.assertEqual(scaled_volume(1, 10), 1)
        self.assertEqual(scaled_volume(0, 100), 0)
        self.assertEqual(scaled_volume(33, 50), 17)


class PlaybackLevelTests(unittest.TestCase):
    def test_individual_sonos_announcements_each_get_their_own_volume(self):
        states = {e: state(e, group=[A, B]) for e in [A, B]}
        _, posts = play(states, {A: 'sonos', B: 'sonos'}, {A: 100, B: 60})
        volumes = {c[2]['entity_id']: c[2]['extra']['volume'] for c in posts}
        self.assertEqual(volumes, {A: 100, B: 60})

    def test_normal_playback_sets_each_speakers_own_volume(self):
        states = {A: state(A, features=516), B: state(B, features=516)}
        _, posts = play(states, {A: 'cast', B: 'cast'}, {A: 100, B: 40}, volume=50)
        sets = {c[2]['entity_id']: c[2]['volume_level'] for c in posts if c[1].endswith('/volume_set')}
        self.assertEqual(sets, {A: .5, B: .2})
        self.assertTrue(all('announce' not in c[2] for c in posts if c[1].endswith('/play_media')))

    def test_shared_music_group_uses_the_coordinators_level(self):
        states = {e: state(e, group=[A, B]) for e in [A, B]}
        result, posts = play(states, {A: 'cast', B: 'cast'}, {A: 70, B: 30})
        plays = [c for c in posts if c[1].endswith('/play_media')]
        self.assertEqual(result['effective_requests'], 1)
        self.assertEqual(plays[0][2]['extra'], {'volume': 70})

    def test_default_level_keeps_previous_behaviour(self):
        states = {e: state(e, group=[A, B]) for e in [A, B]}
        _, posts = play(states, {A: 'sonos', B: 'sonos'}, {}, volume=35)
        self.assertTrue(all(c[2]['extra'] == {'volume': 35} for c in posts))

    def test_speaker_without_volume_control_is_checked_against_its_own_level(self):
        # 0.3 is above 60% of 40 (24), so this speaker would be too loud: refused safely.
        states = {A: state(A, features=0, level=.3)}
        with self.assertRaises(Exception):
            play(states, {A: 'cast'}, {A: 60}, volume=40)
        states = {A: state(A, features=0, level=.2)}
        _, posts = play(states, {A: 'cast'}, {A: 60}, volume=40)
        self.assertEqual(len([c for c in posts if c[1].endswith('/play_media')]), 1)

    def test_invalid_levels_are_refused(self):
        for levels in ({A: 0}, {A: 101}, {A: '50'}, {C: 50}):
            with self.subTest(levels=levels), self.assertRaises(ValueError):
                SpeakerBackend({'speaker_entity': A, 'audio_url': URL, 'group_members': [A, B],
                                'member_levels': levels}, requester=Mock(), allow_playback=True)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_levels_are_saved_per_device_and_described_with_defaults(self):
        SpeakerLevels(self.root).save({'a' * 32: 60})
        rows = [{'entity_id': A, 'name': 'Era 300', 'ha_device_id': 'a' * 32, 'manufacturer': 'Sonos', 'model': 'Era 300'},
                {'entity_id': B, 'name': 'Era 100'}]
        d = ManualTiming(self.root).describe({'members': [A, B]}, rows)
        self.assertEqual([m['volume_pct'] for m in d['members']], [60, 100])
        self.assertEqual(d['members'][0]['model'], 'Sonos Era 300')
        single = ManualTiming(self.root).describe({'members': [B]}, rows)
        self.assertFalse(single['supported'])
        self.assertEqual(single['members'][0]['volume_pct'], 100)

    def test_out_of_range_levels_are_refused_without_writing(self):
        for values in ({'a' * 32: 5}, {'a' * 32: 120}, {}, {'bad': 50}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SpeakerLevels(self.root).save(values)
        self.assertFalse((self.root / 'speaker-levels.json').exists())


class RingLevelTests(unittest.TestCase):
    def test_each_ring_reads_saved_levels_per_device(self):
        from playback import TrackPlayer
        with tempfile.TemporaryDirectory() as directory:
            SpeakerLevels(directory).save({'a' * 32: 45})
            factory = Mock()
            factory.return_value.arm_once.return_value = {'approval_id': 'grant'}
            factory.return_value.play_once.return_value = {'ha_call_accepted': True, 'mode': 'announce', 'may_interrupt': False}
            player = TrackPlayer('primary', A, None, 'http://example.test', group_members=[A, B],
                                 data_dir=directory, member_device_ids={A: 'a' * 32}, backend_factory=factory)
            player._run({'path': 'unused', 'track_no': 1, 'md5': 'b' * 32}, 80, 1)
        self.assertEqual(factory.call_args.args[0]['member_levels'], {A: 45, B: 100})
        factory.return_value.arm_once.assert_called_once_with(confirmed=True, volume=80, expires_in=60)


class SettingsRouteTests(unittest.TestCase):
    def post(self, body, members):
        import server
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/timing'; handler.client_address = ('172.30.32.2', 1)
        raw = json.dumps(body).encode()
        handler.headers = {'X-AnyChime-Request': '1', 'Content-Length': str(len(raw))}
        handler.rfile = io.BytesIO(raw); handler.respond = Mock()
        rows = [{'entity_id': e, 'name': e, 'ha_device_id': str(i) * 32} for i, e in enumerate(members, 1)]
        registry = Mock(); registry.snapshot.return_value = [{'key': 'primary', 'active': True, 'members': members}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'MANUAL_TIMING', ManualTiming(directory)), \
                patch.object(server, 'SPEAKER_LEVELS', SpeakerLevels(directory)), \
                patch.object(server.NATIVE, 'registry', registry), \
                patch.object(server.NATIVE, 'refresh_groups'), \
                patch.object(server.speakers, 'discover', return_value=rows):
            handler.do_POST()
            saved = {name: json.loads((Path(directory) / name).read_text())
                     for name in ('speaker-levels.json', 'latency-delays.json') if (Path(directory) / name).exists()}
        return handler.respond.call_args.args, saved

    def test_single_speaker_chime_can_save_its_volume(self):
        (status, body), saved = self.post({'key': 'primary', 'confirmed': True, 'levels': {'1' * 32: 55}}, [A])
        self.assertEqual(status, 200); self.assertFalse(body['audio_played'])
        self.assertEqual(saved, {'speaker-levels.json': {'1' * 32: 55}})

    def test_invalid_level_saves_neither_volume_nor_delay(self):
        (status, body), saved = self.post({'key': 'primary', 'confirmed': True,
                                           'levels': {'1' * 32: 55, '2' * 32: 5},
                                           'delays': {'1' * 32: 0, '2' * 32: 40}}, [A, B])
        self.assertEqual(status, 400); self.assertIn('10 to 100%', body['error'])
        self.assertEqual(saved, {})

    def test_volume_and_delay_save_together(self):
        (status, _), saved = self.post({'key': 'primary', 'confirmed': True,
                                        'levels': {'1' * 32: 100, '2' * 32: 60},
                                        'delays': {'1' * 32: 0, '2' * 32: 40}}, [A, B])
        self.assertEqual(status, 200)
        self.assertEqual(saved['speaker-levels.json'], {'1' * 32: 100, '2' * 32: 60})
        self.assertEqual(saved['latency-delays.json'], {'1' * 32: 0, '2' * 32: 40})


if __name__ == '__main__':
    unittest.main()
