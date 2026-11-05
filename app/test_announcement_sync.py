from test_support import ringtone_file, seeded_store
"""No real speaker/network calls: align simulated downloads, not acoustic output."""
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
import server
from announcement_sync import DownloadAlignment
from audio import SpeakerBackend,SpeakerError,MEDIA_ANNOUNCE

PATH='/media/chimes/primary/1/'+'a'*32+'.mp3'
URL='http://10.23.45.20:48200'+PATH
ROOT=Path(__file__).parent

class AlignmentTests(unittest.TestCase):
    def test_early_download_waits_for_other_distinct_peer_then_releases_together(self):
        pool=DownloadAlignment(max_wait=.5)
        url,token=pool.register(URL,2)
        self.assertIn('ac_sync=',url)
        entered=threading.Event();done=threading.Event();released=[]
        def first():
            entered.set();pool.wait(token,PATH,'speaker-a');released.append(time.monotonic());done.set()
        thread=threading.Thread(target=first);thread.start();self.assertTrue(entered.wait(1))
        self.assertFalse(done.wait(.03))
        before=time.monotonic();pool.wait(token,PATH,'speaker-b');thread.join(1)
        self.assertTrue(done.is_set());self.assertGreaterEqual(released[0],before)

    def test_duplicate_peer_does_not_count_as_two_speakers(self):
        pool=DownloadAlignment(max_wait=.5);_,token=pool.register(URL,2)
        done=threading.Event()
        thread=threading.Thread(target=lambda:(pool.wait(token,PATH,'same-ip'),done.set()))
        thread.start()
        self.assertFalse(done.wait(.02))
        with pool.lock:
            self.assertEqual(pool.batches[token]['peers'],{'same-ip'})
        pool.wait(token,PATH,'different-ip');thread.join(1)
        self.assertTrue(done.is_set())

    def test_missing_peer_timeout_is_bounded_and_subsequent_requests_do_not_wait(self):
        pool=DownloadAlignment(max_wait=.03);_,token=pool.register(URL,2)
        before=time.monotonic();pool.wait(token,PATH,'one-ip')
        elapsed=time.monotonic()-before
        self.assertGreaterEqual(elapsed,.02);self.assertLess(elapsed,.5)
        before=time.monotonic();pool.wait(token,PATH,'one-ip')
        self.assertLess(time.monotonic()-before,.02)

    def test_each_ring_is_separate_and_path_mismatch_does_not_join_a_batch(self):
        pool=DownloadAlignment();_,one=pool.register(URL,2);_,two=pool.register(URL,2)
        self.assertNotEqual(one,two)
        pool.wait(one,PATH.replace('/1/','/2/'),'different-track')
        self.assertEqual(pool.batches[one]['peers'],set())
        pool.release(one);self.assertTrue(pool.batches[one]['ready'].is_set())
        self.assertFalse(pool.batches[two]['ready'].is_set())

    def test_only_local_chime_routes_are_eligible_and_storage_is_bounded(self):
        now=[0];pool=DownloadAlignment(capacity=1,clock=lambda:now[0])
        for url,count in [('http://example.test/ring.mp3',2),(URL,1),(URL,17),(URL+'?x=1',2)]:
            self.assertEqual(pool.register(url,count),(url,None))
        _,one=pool.register(URL,2)
        self.assertEqual(pool.register(URL,2),(URL,None))
        now[0]=61
        _,two=pool.register(URL,2)
        self.assertNotEqual(one,two);self.assertNotIn(one,pool.batches)
        self.assertEqual(len(pool.batches),1)

    def handler(self, *, command='GET',range_header=None):
        h=server.Handler.__new__(server.Handler);h.command=command
        h.path=PATH+'?ac_sync='+'b'*32;h.client_address=('speaker-a',1000)
        h.headers={} if range_header is None else {'Range':range_header}
        h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock();h.respond=Mock();h.wfile=io.BytesIO()
        return h

    def test_http_waits_only_for_valid_starting_get_not_head_or_later_ranges(self):
        for command,range_header,expected in [('GET',None,True),('GET','bytes=0-1023',True),
            ('GET','bytes=0-20',False),('GET','bytes=0-1',False),
            ('HEAD',None,False),('GET','bytes=4-9',False),('GET','bytes=99999999-',False)]:
            with self.subTest(command=command,range=range_header):
                h=self.handler(command=command,range_header=range_header)
                with patch.object(server,'ALIGNMENT') as alignment:
                    h.serve_audio(ringtone_file())
                    self.assertEqual(alignment.wait.called,expected)
                    if expected:alignment.wait.assert_called_once_with('b'*32,PATH,'speaker-a')
                if command=='HEAD':self.assertEqual(h.wfile.getvalue(),b'')

    def test_audio_disables_small_packet_buffering_and_ignores_socket_option_failure(self):
        for fail in (False,True):
            h=self.handler();h.path=PATH;h.connection=Mock()
            if fail:h.connection.setsockopt.side_effect=OSError('Unsupported option')
            h.serve_audio(ringtone_file())
            h.connection.setsockopt.assert_called_once_with(server.socket.IPPROTO_TCP,server.socket.TCP_NODELAY,1)
            self.assertEqual(h.wfile.getvalue(),(ringtone_file()).read_bytes())

    def test_sonos_batch_registers_the_same_unique_url_for_all_targets_and_releases_on_error(self):
        members=['media_player.a','media_player.b'];calls=[]
        def request(method,path,body):
            calls.append((method,path,body))
            if method=='GET':return {'entity_id':path.rsplit('/',1)[1],'state':'playing',
                'attributes':{'supported_features':516|MEDIA_ANNOUNCE,'group_members':members}}
            raise SpeakerError('Synthetic HA failure')
        backend=SpeakerBackend({'speaker_entity':members[0],'audio_url':URL,'group_members':members,
            'member_platforms':{e:'sonos' for e in members}},allow_playback=True,requester=request)
        with patch('audio.ALIGNMENT') as alignment:
            alignment.register.return_value=(URL+'?ac_sync='+'b'*32,'b'*32)
            with self.assertRaises(SpeakerError):backend.play_once(backend.arm_once(confirmed=True)['approval_id'])
            alignment.register.assert_called_once_with(URL,2);alignment.release.assert_called_once_with('b'*32)
        posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(len(posts),1);self.assertEqual(posts[0][2]['entity_id'],members)
        self.assertTrue(posts[0][2]['announce']);self.assertIn('ac_sync=',posts[0][2]['media_content_id'])

class TimingRegressionTests(unittest.TestCase):
    def test_group_still_waits_for_a_download_arriving_after_old_300ms_limit(self):
        pool=DownloadAlignment();_,token=pool.register(URL,2)
        entered=threading.Event();done=threading.Event()
        def first():
            entered.set();pool.wait(token,PATH,'first-speaker');done.set()
        thread=threading.Thread(target=first);thread.start()
        self.assertTrue(entered.wait(1));self.assertFalse(done.wait(.35))
        pool.wait(token,PATH,'second-speaker');thread.join(1)
        self.assertTrue(done.is_set())
        summary=pool.summary(token)
        self.assertEqual(summary['release_reason'],'all_ready')
        self.assertEqual(summary['distinct_downloads'],2)
        self.assertGreaterEqual(summary['arrival_spread_ms'],300)
        self.assertFalse(summary['acoustic_sync_verified'])
    def test_late_arrival_is_reported_after_timeout_and_duplicates_do_not_change_timing(self):
        now=[0];pool=DownloadAlignment(max_wait=0,clock=lambda:now[0]);_,token=pool.register(URL,2)
        pool.wait(token,PATH,'10.23.45.31')
        now[0]=.2;pool.wait(token,PATH,'10.23.45.31')
        self.assertEqual(pool.summary(token)['distinct_downloads'],1)
        now[0]=.6;pool.wait(token,PATH,'10.23.45.32')
        result=pool.summary(token)
        self.assertEqual(result['release_reason'],'timeout')
        self.assertEqual(result['arrival_spread_ms'],600)
        self.assertEqual(result['distinct_downloads'],2)
        for private in ['10.23.45.31',PATH,token]:self.assertNotIn(private,str(result))
    def test_full_response_including_headers_is_held_until_group_gate_releases(self):
        h=AlignmentTests().handler();events=[]
        h.send_response.side_effect=lambda *_:events.append('headers')
        with patch.object(server,'ALIGNMENT') as alignment:
            alignment.wait.side_effect=lambda *_:events.append('alignment')
            h.serve_audio(ringtone_file())
        self.assertEqual(events,['alignment','headers'])
        self.assertEqual(h.wfile.getvalue(),ringtone_file().read_bytes())
    def test_cancelled_group_releases_waiters_and_keeps_diagnostics_bounded(self):
        pool=DownloadAlignment();_,token=pool.register(URL,2)
        pool.release(token)
        self.assertEqual(pool.summary(token)['release_reason'],'cancelled')
        self.assertIsNone(pool.summary(None))
    def test_player_status_reports_timing_without_exposing_internal_token(self):
        from playback import TrackPlayer
        pool=DownloadAlignment(max_wait=0);_,token=pool.register(URL,2)
        player=TrackPlayer('primary','media_player.example',None,'http://10.23.45.20:48200')
        player.last_result={'state':'ha_accepted'};player.alignment_token=token
        pool.wait(token,PATH,'speaker-a')
        with patch('playback.ALIGNMENT',pool):result=player.status()
        self.assertEqual(result['last_result']['download_timing']['release_reason'],'timeout')
        self.assertNotIn(token,str(result))
