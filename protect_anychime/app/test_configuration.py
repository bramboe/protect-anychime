import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import configuration
import connection
import speakers
import server
from audio import SpeakerError


class MigrationTests(unittest.TestCase):
    def test_current_settings_keep_their_names(self):
        raw={'controller_ip':'10.23.45.1','native_ip':'10.23.45.20'}
        self.assertEqual(configuration.normalize(raw),raw)
    def test_fresh_configuration_does_not_call_supervisor(self):
        request=Mock()
        self.assertIsNone(configuration.migrate({},requester=request))
        request.assert_not_called()


class ArtworkTests(unittest.TestCase):
    def test_product_photo_requires_exact_model_and_manufacturer(self):
        import artwork
        with patch('artwork.catalog',return_value=({('exampleaudio','speaker100'):['123']},{'123':'Example Audio Speaker 100'})):
            self.assertEqual(speakers.product_image('Example Audio','Speaker 100'),'assets/unifi-123.png')
            self.assertEqual(speakers.product_image('Example Audio, Inc.','Example Audio Speaker 100'),'assets/unifi-123.png')
            self.assertIsNone(speakers.product_image('Other','Speaker 100'))
            self.assertIsNone(speakers.product_image('Example Audio','Unknown'))

    def test_discovery_uses_registry_model_not_album_art(self):
        with patch('speakers.product_image',return_value='assets/era100.png'):
            result = speakers.discover(Mock(return_value=[{'entity_id': 'media_player.room',
            'state': 'idle', 'features': 512, 'supported': True, 'manufacturer': 'Example Audio', 'model': 'Era 100',
            'entity_picture': 'https://example.test/album.png'}]))
        self.assertEqual(result[0]['model'], 'Era 100')
        self.assertEqual(result[0]['image'], 'assets/era100.png')
        self.assertNotIn('entity_picture', result[0])

    def test_asset_route_rejects_traversal_even_from_ingress(self):
        for path in ['/assets/../server.py', '/assets/../../config.json', '/assets/products.json']:
            handler = server.Handler.__new__(server.Handler)
            handler.client_address = ('172.30.32.2', 123)
            handler.path = path
            handler.respond = Mock()
            handler.do_GET()
            self.assertEqual(handler.respond.call_args.args[0], 404)

    def test_official_chime_asset_is_used_verbatim_for_app_and_ui(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual((root/'icon.png').read_bytes(), (root/'app/assets/chime.png').read_bytes())
        self.assertTrue((root/'icon.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))

    def test_generic_configuration_has_no_legacy_brand_fields_and_english_labels(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root/'config.json').read_text())
        self.assertEqual(config['slug'],'protect_anychime')
        self.assertEqual(config['schema'],{'protect_username':'str','protect_password':'password','auto_adopt_chimes':'bool'})
        labels = json.loads((root/'translations/en.json').read_text())['configuration']
        self.assertEqual(set(labels), set(config['schema']))
