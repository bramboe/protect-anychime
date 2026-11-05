import unittest
from unittest.mock import Mock
import artwork
import speakers

class ImageMetadataTests(unittest.TestCase):
    def test_custom_renderer_is_not_mapped_to_a_particular_personal_device(self):
        row={'entity_id':'media_player.bridge','name':'Example endpoint','state':'idle','features':512,
            'manufacturer':'Example Community','model':'Custom Audio Renderer','device_name':'Example endpoint'}
        result=speakers.discover(Mock(return_value=[row]))[0]
        self.assertIsNone(result['image'])
        for field in ['name','model','manufacturer','entity_id']:
            self.assertEqual(result[field],row[field])

    def test_unknown_numeric_model_does_not_assume_a_known_camera_or_speaker(self):
        self.assertIsNone(artwork.lookup_device({'manufacturer':'Example Community','model':'Renderer','model_id':'12345'}))

    def test_original_device_name_adds_information_without_using_friendly_name(self):
        match=artwork.lookup_device({'manufacturer':'Sonos','model':'Audio Renderer','device_name':'Sonos Play:1'})
        self.assertEqual(match['image'],'assets/unifi-2036.png')
        self.assertEqual(match['image_metadata_source'],'device_name')
        self.assertIsNone(artwork.lookup_device({'manufacturer':'Unknown','model':'Renderer','name':'Sonos Play:1'}))

    def test_explicit_physical_parent_is_used_for_a_component(self):
        match=artwork.lookup_device({'manufacturer':'Example','model':'Audio component','parent_device_id':'physical-device',
            'parent_manufacturer':'Sonos','parent_model':'Play:1'})
        self.assertEqual(match['image'],'assets/unifi-2036.png')
        self.assertEqual(match['image_metadata_source'],'parent_device')

    def test_hub_and_unqualified_numeric_id_are_not_product_identity(self):
        self.assertIsNone(artwork.lookup_device({'manufacturer':'Example','model':'Speaker','via_device_id':'hub',
            'parent_manufacturer':'Sonos','parent_model':'Play:1'}))
        for maker in ['IKEA of Sweden','Unknown','Example Community']:
            self.assertIsNone(artwork.lookup_device({'manufacturer':maker,'model':'Unknown','model_id':'12345'}))

    def test_specific_direct_model_wins_over_related_parent_or_name(self):
        match=artwork.lookup_device({'manufacturer':'Sonos','model':'Era 100','device_name':'Sonos Play:1',
            'parent_device_id':'parent','parent_manufacturer':'Sonos','parent_model':'One'})
        self.assertEqual(match['image'],'assets/unifi-5046.png')

    def test_ha_appended_model_identifier_is_removed_only_when_known(self):
        match=artwork.lookup_device({'manufacturer':'Sonos','model':'Renderer',
            'device_name':'Sonos Play:1 (12345)','model_id':'12345'})
        self.assertEqual(match['image'],'assets/unifi-2036.png')
