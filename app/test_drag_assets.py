"""The HA-style drag dependency is local and only its fixed public path is served."""
import io
import unittest
from unittest.mock import Mock
import server

class DragAssetTests(unittest.TestCase):
    def handler(self,path,command='GET'):
        h=server.Handler.__new__(server.Handler);h.path=path;h.command=command;h.client_address=('172.30.32.2',123)
        h.headers={};h.respond=Mock();h.wfile=io.BytesIO()
        return h
    def test_only_pinned_local_sortable_asset_is_exposed(self):
        h=self.handler('/assets/sortable.min.js');h.do_GET()
        status,data,kind=h.respond.call_args.args
        self.assertEqual(status,200);self.assertEqual(kind,'application/javascript')
        self.assertIn(b'1.15.7',data[:200]);self.assertLess(len(data),128*1024)
        self.assertTrue((server.ROOT/'assets'/'Sortable-LICENSE.txt').is_file())
    def test_unlisted_javascript_and_path_traversal_are_not_served(self):
        for path in ['/assets/unlisted.js','/assets/../server.py']:
            h=self.handler(path);h.do_GET();self.assertEqual(h.respond.call_args.args[0],404)
