"""Physical-product matching uses synthetic registries and never plays audio."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
import artwork
import products
import speakers

class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patch=patch.object(products,'LINKS_PATH',Path(self.temp.name)/'product-links.json')
        self.patch.start()
        self.source={'id':'a'*32,'manufacturer':'Example','model':'Audio Renderer','device_name':'Desk',
            'connections':[['mac','02:00:00:00:00:01']]}
        self.product={'id':'b'*32,'manufacturer':'Sonos','model':'Play:1','device_name':'Physical product',
            'connections':[['mac','02:00:00:00:00:01']]}
        self.row={'device_id':self.source['id'],'entity_id':'media_player.example','name':'My custom name',
            'features':512,'state':'idle','manufacturer':'Example','model':'Audio Renderer'}
    def tearDown(self):
        self.patch.stop();self.temp.cleanup()
    def test_shared_hardware_connection_enriches_image_without_renaming_speaker(self):
        result=speakers.discover(Mock(return_value={'speakers':[self.row],'products':[self.source,self.product]}))[0]
        self.assertEqual(result['image'],'assets/unifi-2036.png')
        for field in ['entity_id','name','model','manufacturer']:self.assertEqual(result[field],self.row[field])
        self.assertEqual(result['product_link'],'automatic')
    def test_ambiguous_hardware_match_is_not_guessed(self):
        other={**self.product,'id':'c'*32}
        self.assertIsNone(products.resolve(self.row,[self.source,self.product,other],{}))
    def test_same_name_without_hardware_link_is_not_proof(self):
        other={**self.product,'connections':[],'device_name':self.source['device_name']}
        self.assertIsNone(products.resolve(self.row,[self.source,other],{}))
    def test_explicit_link_is_private_persistent_and_bound_to_current_source_device(self):
        public=products.public_inventory([self.product])
        products.save_link(self.row['entity_id'],self.product['id'],public,[self.row])
        self.assertEqual(products.LINKS_PATH.stat().st_mode & 0o077,0)
        self.assertNotIn('mac',products.LINKS_PATH.read_text())
        target=products.resolve(self.row,[self.source,self.product],products.read_links())
        self.assertTrue(target['_chosen'])
        reused={**self.row,'device_id':'d'*32}
        new_source={**self.source,'id':'d'*32,'connections':[]}
        self.assertIsNone(products.resolve(reused,[new_source,self.product],products.read_links()))
        products.save_link(self.row['entity_id'],None,public,[self.row])
        self.assertEqual(products.read_links(),{})
    def test_unknown_speaker_and_product_are_rejected(self):
        for entity,target in [('media_player.unknown',self.product['id']), (self.row['entity_id'],'c'*32)]:
            with self.assertRaises(ValueError):products.save_link(entity,target,[self.product],[self.row])
        self.assertFalse(products.LINKS_PATH.exists())
    def test_public_products_do_not_expose_hardware_identifiers(self):
        product={**self.product,'serial_number':'private-serial','identifiers':[['example','private-id']]}
        public=products.public_inventory([product])
        for value in ['private-serial','private-id','02:00:00:00:00:01']:self.assertNotIn(value,json.dumps(public))
    def test_serial_match_requires_same_manufacturer(self):
        source={**self.source,'connections':[],'serial_number':'example-123'}
        target={**self.product,'connections':[],'serial_number':'example-123'}
        self.assertIsNone(products.resolve(self.row,[source,target],{}))
        source['manufacturer']='Sonos'
        self.assertEqual(products.resolve(self.row,[source,target],{})['id'],target['id'])
    def test_model_description_recognises_catalogue_brand_and_unique_model_code(self):
        # Rich product descriptions may name a retail brand even when the HA
        # manufacturer identifies community firmware. No personal model alias.
        with patch.object(artwork,'catalog',return_value=({('examplebrand','a100speaker'):['42']},{'42':'Examplebrand A100 Speaker'})):
            result=artwork.lookup_device({'manufacturer':'Example firmware','model':'Examplebrand A100 Network Audio Product'})
        self.assertEqual(result['image'],'assets/unifi-42.png')
        self.assertTrue(result['image_is_fallback'])
        self.assertIsNone(artwork.lookup_device({'manufacturer':'Example','model':'Unknown Audio Product 12345'}))
    def test_generic_description_does_not_override_an_exact_model_match(self):
        result=artwork.lookup_device({'manufacturer':'Sonos','model':'Era 100','device_name':'Sonos Play:1 Audio'})
        self.assertEqual(result['image'],'assets/unifi-5046.png')

class ProductEndpointTests(unittest.TestCase):
    def handler(self,route,source='172.30.32.2',body=None):
        import io,server
        handler=server.Handler.__new__(server.Handler)
        handler.path=route;handler.client_address=(source,123)
        raw=json.dumps(body or {}).encode()
        handler.headers={'X-AnyChime-Request':'1','Content-Length':str(len(raw))}
        handler.rfile=io.BytesIO(raw);handler.respond=Mock()
        return handler
    def test_loopback_and_spoofed_headers_cannot_change_product_links(self):
        import server
        for source in ['127.0.0.1','::1','10.23.45.50']:
            handler=self.handler('/api/products/link',source)
            handler.headers['X-Forwarded-For']='172.30.32.2'
            with patch.object(server.speakers,'discover') as discover,patch.object(products,'save_link') as save:
                handler.do_POST()
            self.assertEqual(handler.respond.call_args.args[0],403)
            discover.assert_not_called();save.assert_not_called()
    def test_metadata_change_never_configures_chimes_or_plays_audio(self):
        import server
        handler=self.handler('/api/products/link',body={'entity_id':'media_player.example','product_device_id':'b'*32})
        inventory=[{'id':'b'*32}];rows=[{'entity_id':'media_player.example','device_id':'a'*32}]
        with patch.object(server.speakers,'discover',return_value=([],inventory,rows)),patch.object(products,'save_link') as save,patch.object(server,'NATIVE') as native:
            handler.do_POST()
        self.assertEqual(handler.respond.call_args.args[0],200)
        save.assert_called_once_with('media_player.example','b'*32,inventory,rows)
        self.assertEqual(native.mock_calls,[])
