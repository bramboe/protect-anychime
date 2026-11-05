import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
import speakers

class ProductImageTests(unittest.TestCase):
    def test_exact_model_variants_and_manufacturer_suffixes(self):
        for maker,model,expected in [('Sonos','Play:1','unifi-2036'),('SONOS, Inc.','Sonos Play 1','unifi-2036'),('Sonos','One SL','unifi-293'),('Sonos','One','unifi-293'),('Sonos','Era 100','unifi-5046')]:
            with self.subTest(maker=maker,model=model):
                self.assertEqual(speakers.product_image(maker,model),'assets/'+expected+'.png')

    def test_unknown_missing_or_wrong_manufacturer_never_guesses_product(self):
        for maker,model in [('Other','One SL'),('','One SL'),('Sonos','One SL Ultra'),('Sonos','Play:999'),(None,'One SL'),('Sonos',None)]:
            with self.subTest(maker=maker,model=model):self.assertIsNone(speakers.product_image(maker,model))

    def test_model_identifier_can_resolve_when_display_model_is_missing(self):
        self.assertEqual(speakers.product_image('Sonos','','One SL'),'assets/unifi-293.png')
        self.assertIsNone(speakers.product_image('Other','','One SL'))

    def test_catalogue_is_shared_product_data_not_a_local_device_inventory(self):
        import artwork
        index,valid=artwork.catalog()
        self.assertGreater(len(valid),1000)
        self.assertTrue(index)

    def test_discovery_keeps_metadata_and_ignores_media_artwork(self):
        requester=Mock(return_value=[{'entity_id':'media_player.room','name':'Room','state':'idle','features':512,'manufacturer':'Sonos','model':'Play:1','model_id':'S1','entity_picture':'https://example.com/album.png','state_class':'speaker'}])
        row=speakers.discover(requester)[0]
        self.assertEqual((row['manufacturer'],row['model'],row['model_id']),('Sonos','Play:1','S1'))
        self.assertEqual(row['image'],'assets/unifi-2036.png')
        self.assertEqual(row['fallback_image'],'assets/speaker.svg')
        self.assertEqual(requester.call_count,1)

    def test_unknown_tv_receiver_and_speaker_have_type_specific_local_art(self):
        for kind,file in [('tv','tv.svg'),('receiver','receiver.svg'),('speaker','speaker.svg'),('unknown','speaker.svg')]:
            row=speakers.discover(Mock(return_value=[{'entity_id':'media_player.device','features':512,'state_class':kind,'manufacturer':'Unknown','model':'Future model'}]))[0]
            self.assertIsNone(row['image']);self.assertEqual(row['fallback_image'],'assets/'+file)

    def test_unavailable_catalogue_does_not_break_discovery(self):
        with patch('artwork.lookup',return_value=None):
            self.assertIsNone(speakers.product_image('Example','Any speaker'))
