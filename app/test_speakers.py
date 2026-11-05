import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import speakers
import server


def row(entity='media_player.speaker_a', supported=True, group=None):
    return dict(entity_id=entity, name='Main Speaker', supported=supported,
                available=True, features=512, group=group or [])


class SpeakerTests(unittest.TestCase):
    def test_discovery_requests_metadata_only_and_retains_unsupported_players(self):
        request = Mock(return_value=[dict(row(), state='idle'),
            dict(row('media_player.tv', False), state='unavailable'),
            {'entity_id': 'light.lamp'}])
        result = speakers.discover(request)
        request.assert_called_once_with('POST', '/template', {'template': speakers.TEMPLATE})
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0]['supported'])
        self.assertFalse(result[1]['available'])
        self.assertTrue(result[1]['supported'])

    def test_select_only_supported_known_entities(self):
        for entities in [['media_player.unknown'], [12], None,
                         ['media_player.speaker_a'] * 2]:
            with self.subTest(entities=entities), self.assertRaises(ValueError):
                speakers.validate_selection(entities, [row(), row('media_player.tv', False)])
        self.assertEqual(speakers.validate_selection([], [row()]), [])
        with self.assertRaises(ValueError):speakers.validate_selection(['media_player.tv'], [row('media_player.tv',False)])

    def test_selecting_a_speaker_does_not_expand_ha_music_group(self):
        a, b = 'media_player.a', 'media_player.b'
        rows = [row(a, group=[a, b]), row(b, group=[a, b])]
        self.assertEqual(speakers.validate_selection([a,b], rows), [a,b])
        self.assertEqual(speakers.validate_selection([a], rows), [a])

    def test_selection_persists_and_empty_selection_overrides_legacy(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(speakers, 'SELECTION_PATH', Path(temp)/'speakers.json'):
            options = {'speaker_entity': 'media_player.old'}
            self.assertEqual(speakers.selected(options), ['media_player.old'])
            speakers.save(['media_player.new'])
            self.assertEqual(speakers.selected(options), ['media_player.new'])
            speakers.save([])
            self.assertEqual(speakers.selected(options), [])
            self.assertEqual(speakers.SELECTION_PATH.stat().st_mode & 0o077, 0)

class NativeDeviceFilterTests(unittest.TestCase):
    def test_ubiquiti_manufacturer_variants_are_excluded(self):
        for maker in ['Ubiquiti','Ubiquiti Inc.','Ubiquiti Networks, Inc.','UniFi','UBIQUITI NETWORKS']:
            with self.subTest(maker=maker):
                request=Mock(return_value=[dict(row(),manufacturer=maker)])
                self.assertEqual(speakers.discover(request),[])
                self.assertTrue(speakers.discover(request,include_excluded=True)[0]['excluded'])

    def test_names_and_missing_metadata_do_not_exclude_other_speakers(self):
        for maker in ['',None,'Other Manufacturer','Not Ubiquiti']:
            request=Mock(return_value=[dict(row(),manufacturer=maker,name='UniFi Chime')])
            self.assertEqual(len(speakers.discover(request)),1)

    def test_saved_native_device_is_not_reintroduced_in_speaker_response(self):
        handler=server.Handler.__new__(server.Handler)
        handler.path='/api/speakers';handler.client_address=('172.30.32.2',123);handler.respond=Mock()
        inventory=[dict(row('media_player.native'),excluded=True),row()]
        with patch('server.speakers.discover',return_value=inventory),patch('server.load_options',return_value={'speaker_entities':['media_player.native','media_player.speaker_a']}):
            handler.do_GET()
        payload=handler.respond.call_args.args[1]
        self.assertEqual(payload['selected'],['media_player.speaker_a'])
        self.assertEqual(len(payload['speakers']),1)

class CapabilityFilterTests(unittest.TestCase):
    def test_webos_route_hidden_but_same_tv_dlna_route_retained(self):
        request=Mock(return_value=[dict(row('media_player.tv'),integration='webostv',model='OLED42C31LA'),
            dict(row('media_player.tv_audio'),integration='dlna_dmr',model='OLED42C31LA')])
        self.assertEqual([r['entity_id'] for r in speakers.discover(request)],['media_player.tv_audio'])

    def test_missing_and_non_playback_capabilities_are_hidden(self):
        for features in [None,0,4,True,'512']:
            with self.subTest(features=features):
                request=Mock(return_value=[dict(row(),features=features)])
                self.assertEqual(speakers.discover(request),[])
                self.assertTrue(speakers.discover(request,include_excluded=True)[0]['exclusion_reason'])

    def test_unavailable_audio_entities_remain_eligible_and_names_do_not_filter(self):
        result=speakers.discover(Mock(return_value=[dict(row(),integration='cast',name='webostv TV',state='unavailable')]))
        self.assertEqual(len(result),1)
        self.assertFalse(result[0]['available'])

    def test_native_integration_hidden_even_without_manufacturer(self):
        self.assertEqual(speakers.discover(Mock(return_value=[dict(row(),integration='unifiprotect')])),[])
