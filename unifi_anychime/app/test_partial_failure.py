"""Independent-output failures, using fake HA and Protect requests only."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from audio import SpeakerBackend, SpeakerError, MEDIA_ANNOUNCE
from playback import TrackPlayer
from test_support import seeded_store
import server


class PartialFailureTests(unittest.TestCase):
    def run_ring(self, stage):
        calls=[]
        def request(method,path,body):
            entity=path.rsplit('/',1)[-1] if method=='GET' else body['entity_id']
            calls.append((method,path,entity))
            if entity=='media_player.bad' and ((stage=='inspect' and method=='GET') or
                    (stage=='request' and path.endswith('play_media')) or
                    (stage=='volume' and path.endswith('volume_set'))):
                raise SpeakerError('secret-upstream-body-that-must-not-leak')
            return {'entity_id':entity,'state':'idle','attributes':{
                'supported_features':4 if stage=='volume' else MEDIA_ANNOUNCE}} if method=='GET' else []
        members=['media_player.bad','media_player.good']
        with tempfile.TemporaryDirectory() as directory:
            player=TrackPlayer('primary',members[0],seeded_store(Path(directory)/'tracks'),
                'http://example.test',group_members=members,
                member_platforms={e:'sonos' for e in members},duration_probe=lambda _:0,
                backend_factory=lambda options,**kw:SpeakerBackend(options,requester=request,**kw))
            player.submit({'track_no':1,'volume':10,'repeat':3});player.thread.join(3)
            result=player.status()['last_result'];player.stop()
        return calls,result

    def test_failed_output_is_not_retried_and_other_output_repeats(self):
        for stage in ('inspect','request','volume'):
            with self.subTest(stage=stage):
                calls,result=self.run_ring(stage)
                self.assertEqual(result['state'],'partial')
                self.assertEqual(result['ha_calls_accepted'],3)
                self.assertEqual(result['accepted_requests'],3)
                self.assertEqual(sum(p.endswith('play_media') and e=='media_player.good' for _,p,e in calls),3)
                self.assertEqual(sum(m=='GET' and e=='media_player.bad' for m,_,e in calls),1)
                self.assertNotIn('secret-upstream',json.dumps(result))
                self.assertEqual(result['member_results']['media_player.bad']['status'],'failed')
                if stage=='volume':self.assertFalse(any(p.endswith('play_media') and e=='media_player.bad' for _,p,e in calls))

    def test_new_ring_can_try_previously_failed_speaker_again(self):
        for _ in range(2):
            calls,_=self.run_ring('request')
            self.assertEqual(sum(p.endswith('play_media') and e=='media_player.bad' for _,p,e in calls),1)

    def test_failed_coordinator_excludes_all_its_members_only(self):
        calls=[];members=['media_player.a','media_player.b','media_player.c']
        def request(method,path,body):
            if method=='GET':
                entity=path.rsplit('/',1)[-1]
                return {'entity_id':entity,'state':'idle','attributes':{'supported_features':4,
                    'group_members':members[:2] if entity in members[:2] else []}}
            calls.append(body['entity_id'])
            if path.endswith('play_media') and body['entity_id']==members[0]:raise SpeakerError('rejected')
            return []
        backend=SpeakerBackend({'speaker_entity':members[0],'group_members':members,'audio_url':'http://example.test/a.mp3'},
            requester=request,allow_playback=True)
        result=backend.play_once(backend.arm_once(confirmed=True)['approval_id'])
        self.assertEqual(set(result['failed_members']),set(members[:2]))
        backend.exclude_members(result['failed_members']);calls.clear()
        backend.play_once(backend.arm_once(confirmed=True)['approval_id'])
        self.assertEqual(calls,[members[2],members[2]])

    def test_later_total_failure_preserves_earlier_success_and_updates_members(self):
        backend=Mock()
        backend.arm_once.return_value={'approval_id':'fake'}
        backend.play_once.side_effect=[{'ha_call_accepted':True,'accepted_requests':1,
            'member_results':{'media_player.a':{'status':'accepted'}}},
            SpeakerError('No automatic retry.',member_results={'media_player.a':{'status':'failed','stage':'request'}})]
        player=TrackPlayer('primary','media_player.a',None,'http://example.test',
            backend_factory=Mock(return_value=backend),duration_probe=lambda _:0)
        player._run({'path':'unused','track_no':1,'md5':'a'*32},10,4)
        result=player.status()['last_result']
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['accepted_requests'],1)
        self.assertEqual(result['member_results']['media_player.a']['status'],'failed')
        self.assertEqual(backend.play_once.call_count,2)

    def test_all_failures_report_members_without_disclosing_upstream_text(self):
        def request(*args):raise SpeakerError('private-upstream-response')
        backend=SpeakerBackend({'speaker_entity':'media_player.a','audio_url':'http://example.test/a.mp3'},
            allow_playback=True,requester=request)
        with self.assertRaises(SpeakerError) as caught:
            backend.play_once(backend.arm_once(confirmed=True)['approval_id'])
        self.assertEqual(caught.exception.member_results,{'media_player.a':{'status':'failed','stage':'inspect'}})
        self.assertNotIn('private-upstream',str(caught.exception))


class IngressBoundaryTests(unittest.TestCase):
    def handler(self, source, path):
        h=server.Handler.__new__(server.Handler);h.client_address=(source,1);h.path=path
        h.headers={'X-AnyChime-Request':'1','Content-Length':'2'};h.rfile=io.BytesIO(b'{}');h.respond=Mock()
        return h

    def test_loopback_and_forwarded_headers_never_authorize_read_endpoints(self):
        for source in ('127.0.0.1','::1','192.0.2.10'):
            for route in ('/api/status','/api/speakers','/api/connection'):
                h=self.handler(source,route);h.headers['X-Forwarded-For']='172.30.32.2'
                with patch.object(server,'load_options') as load,patch.object(server.speakers,'discover') as discover:
                    h.do_GET()
                self.assertEqual(h.respond.call_args.args[0],403);load.assert_not_called();discover.assert_not_called()

    def test_preview_endpoint_is_gone_and_old_header_is_rejected(self):
        h=self.handler('172.30.32.2','/api/preview');h.do_POST();self.assertEqual(h.respond.call_args.args[0],404)
        h=self.handler('172.30.32.2','/api/speakers');h.headers={'X-Prototype-Test':'silent'};h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],403)
