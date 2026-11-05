from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,MagicMock,patch
import artwork
import speakers

class ArtworkTests(unittest.TestCase):
    def test_apple_tv_alternative_names_keep_labels_and_offline_state(self):
        for model in ['Apple TV 4','Apple TV (4th generation)','A1625']:
            with self.subTest(model=model):
                match=artwork.lookup('Apple Inc.',model)
                self.assertEqual(match['image'],'assets/unifi-14.png')
                self.assertFalse(match['image_is_fallback'])
        row={'entity_id':'media_player.tv','name':'Media Player B',
            'manufacturer':'Apple','model':'Apple TV 4','features':512,'state':'unavailable'}
        result=speakers.discover(Mock(return_value=[row]))[0]
        self.assertEqual(result['image'],'assets/unifi-14.png')
        self.assertFalse(result['available'])
        for field in ['name','model','manufacturer']:
            self.assertEqual(result[field],row[field])

    def test_generic_family_and_related_generations_are_marked_as_illustrative(self):
        for maker,model,key in [('Apple','Apple TV','14'),('Google','Chromecast','39'),
            ('Apple','Apple TV 4K (3rd generation)','4405'),
            ('NVIDIA','Shield TV Pro Gen 2','3155')]:
            with self.subTest(model=model):
                match=artwork.lookup(maker,model)
                self.assertEqual(match['image'],'assets/unifi-'+key+'.png')
                self.assertTrue(match['image_is_fallback'])

    def test_related_search_preserves_brand_and_capabilities_and_rejects_ambiguity(self):
        for maker,model in [('Unknown','Apple TV 4'),('Apple','TV 4K Ultra Gen 2'),
            ('Google','Chromecast Audio Pro Gen 2')]:
            self.assertIsNone(artwork.lookup(maker,model))
        with patch('artwork.catalog',return_value=({('example','speaker'):['1'],
            ('example','speakergen2'):['2']},{'1':'Speaker','2':'Speaker Gen 2'})):
            self.assertIsNone(artwork.lookup('Example','Speaker Gen 3'))
            self.assertEqual(artwork.lookup('Example','Speaker Gen 2')['image'],'assets/unifi-2.png')

    def test_exact_match_wins_and_related_model_is_explicit(self):
        self.assertEqual(artwork.lookup('Sonos','Play:1')['image'],'assets/unifi-2036.png')
        self.assertFalse(artwork.lookup('Sonos','Play:1')['image_is_fallback'])
        substitute=artwork.lookup('Sonos','One SL')
        self.assertTrue(substitute['image_is_fallback']);self.assertEqual(substitute['image_model'],'Sonos One')
        self.assertIsNone(artwork.lookup('Different brand','One SL'))
        self.assertIsNone(artwork.lookup('Sonos','One SL ultra'))

    def test_real_model_is_not_overwritten_by_substitute(self):
        rows=speakers.discover(Mock(return_value=[{'entity_id':'media_player.a','manufacturer':'Sonos','model':'One SL','features':512,'state':'idle'}]))
        self.assertEqual(rows[0]['model'],'One SL');self.assertTrue(rows[0]['image_is_fallback'])

    def test_other_brands_resolve_without_network_access(self):
        with patch('artwork.build_opener') as network:
            for maker,model in [('Apple','HomePod Mini'),('Bose','Home Speaker 500'),('Marshall','Acton Multiroom Wifi Speaker')]:
                with self.subTest(maker=maker):self.assertIsNotNone(artwork.lookup(maker,model))
            network.assert_not_called()

    def test_download_is_fixed_origin_small_and_cached_for_offline_use(self):
        data=(Path(__file__).with_name('assets')/'chime-256.png').read_bytes()
        response=MagicMock();response.__enter__.return_value=response;response.headers.get_content_type.return_value='image/png';response.read.return_value=data
        opener=Mock();opener.open.return_value=response
        with tempfile.TemporaryDirectory() as directory,patch('artwork.build_opener',return_value=opener):
            cache=artwork.ImageCache(directory);self.assertEqual(cache.get('2036'),data);self.assertEqual(cache.get('2036'),data)
            opener.open.assert_called_once();self.assertEqual(opener.open.call_args.args[0].full_url,'https://static.ui.com/fingerprint/0/2036_129x129.png')
            self.assertEqual(opener.open.call_args.kwargs['timeout'],4)

    def test_unknown_or_malformed_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory,patch('artwork.build_opener') as network:
            cache=artwork.ImageCache(directory)
            for key in ['../private','https://example.com','9999999999']:
                with self.assertRaises(ValueError):cache.get(key)
            network.assert_not_called()
        for body in [b'<html>fallback page</html>',b'x'*(artwork.MAX_IMAGE_BYTES+1)]:
            with self.assertRaises(ValueError):artwork._png(body)
