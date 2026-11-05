import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from registry import ChimeRegistry
from runtime import NativeWorker
import server
from management import ManagementError

class SelectionRemovalTests(unittest.TestCase):
    def test_group_removal_plan_is_read_only_and_one_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory)
            rows=[{'entity_id':e,'name':e,'group':['media_player.a','media_player.b']} for e in ['media_player.a','media_player.b']]
            entries=registry.sync(['media_player.a','media_player.b'],rows)
            path=registry.identity_dir(entries[0]);path.mkdir(parents=True,exist_ok=True)
            (path/'identity.json').write_text('{}')
            before=registry.path.read_bytes()
            worker=NativeWorker();worker.registry=registry
            self.assertEqual(worker.plan_selection([],rows),['primary'])
            self.assertEqual(registry.path.read_bytes(),before)
            self.assertEqual(worker.plan_selection(['media_player.a'],rows),[])
            with self.assertRaises(ValueError):worker.plan_selection(['media_player.a'],rows,['primary'])
            worker.begin_removal('primary')
            self.assertTrue(ChimeRegistry(directory).snapshot()[0]['removal_pending'])
            self.assertFalse(worker._entry_enabled('primary'))
            worker.refresh_groups(rows)
            self.assertTrue(registry.snapshot()[0]['removal_pending'])
            registry.sync(['media_player.a'],rows)
            self.assertNotIn('removal_pending',registry.snapshot()[0])

    def save_request(self,failed=False):
        handler=server.Handler.__new__(server.Handler)
        handler.client_address=('172.30.32.2',123);handler.path='/api/speakers'
        body=json.dumps({'entities':[]}).encode()
        handler.headers={'X-Prototype-Test':'silent','Content-Length':str(len(body))}
        handler.rfile=io.BytesIO(body);handler.respond=Mock()
        events=[]
        with patch('server.speakers.discover',return_value=[]),patch('server.load_options',return_value={'speaker_entities':['media_player.a'],'controller_ip':'10.23.45.1'}),patch('server.NATIVE') as worker,patch('server.MANAGEMENT') as manager,patch('server.ONBOARDING') as feedback,patch('server.speakers.save'):
            worker.plan_selection.return_value=['primary']
            def remove(*args,**kwargs):
                events.append('remove')
                if failed:raise ManagementError('Protect unavailable')
                return {'removed_keys':['primary']}
            manager.remove_virtual_chimes.side_effect=remove
            worker.configure_selection.side_effect=lambda *args:events.append('commit') or []
            feedback.submit.return_value={'state':'empty'}
            handler.do_POST()
            if failed:
                worker.configure_selection.assert_not_called();feedback.submit.assert_not_called()
            else:feedback.submit.assert_not_called()
        return handler.respond.call_args.args,events

    def test_uncheck_removes_before_committing_selection(self):
        result,events=self.save_request()
        self.assertEqual(events,['remove','commit']);self.assertEqual(result[0],200)
        self.assertEqual(result[1]['removed_chimes'],['primary']);self.assertFalse(result[1]['audio_played'])

    def test_removal_failure_does_not_commit_or_schedule_audio(self):
        result,events=self.save_request(True)
        self.assertEqual(events,['remove']);self.assertEqual(result[0],400)
