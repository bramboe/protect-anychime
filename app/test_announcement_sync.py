"""Download tests use synthetic bytes and fake HA, never real speakers."""
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs
from announcement_sync import DownloadAlignment
import server

URL='http://example.test/media/chimes/primary/1/'+'a'*32+'.mp3'
def token(url):return parse_qs(urlsplit(url).query)['ac_sync'][0]

class AlignmentTests(unittest.TestCase):
    def test_same_proxy_ip_counts_distinct_subtokens_and_uses_one_release_epoch(self):
        pool=DownloadAlignment(max_wait=.2);urls,batch=pool.register(URL,{'a':0,'b':50});done={}
        def run(e):pool.wait(token(urls[e]),urlsplit(URL).path,'10.0.0.1');done[e]=time.monotonic()
        a=threading.Thread(target=run,args=('a',));b=threading.Thread(target=run,args=('b',));a.start();time.sleep(.015);b.start();a.join();b.join()
        summary=pool.summary(batch)
        self.assertEqual(summary['distinct_downloads'],2);self.assertEqual(summary['release_reason'],'all_ready')
        self.assertGreaterEqual(done['b']-done['a'],.035);self.assertLess(done['b']-done['a'],.15)
        self.assertNotIn('10.0.0.1',str(summary));self.assertNotIn(batch,str(summary))
    def test_unknown_missing_duplicate_and_wrong_path_never_add_delay_or_count(self):
        pool=DownloadAlignment(max_wait=.01);urls,batch=pool.register(URL,{'a':500,'b':0})
        started=time.monotonic()
        for value,path in [(None,urlsplit(URL).path),(batch+'.'+'f'*16,urlsplit(URL).path),(token(urls['a']),'/wrong')]:pool.wait(value,path,'same')
        self.assertLess(time.monotonic()-started,.03);self.assertEqual(pool.summary(batch)['distinct_downloads'],0)
        pool.wait(token(urls['b']),urlsplit(URL).path,'same')
        started=time.monotonic();pool.wait(token(urls['b']),urlsplit(URL).path,'same')
        self.assertLess(time.monotonic()-started,.03);self.assertEqual(pool.summary(batch)['distinct_downloads'],1)
    def test_one_output_has_neither_barrier_nor_offset(self):
        urls,batch=DownloadAlignment().register(URL,{'a':500})
        self.assertIsNone(batch);self.assertEqual(urls,{'a':URL})
    def test_maximum_combined_wait_is_one_and_a_half_seconds(self):
        pool=DownloadAlignment(max_wait=100);urls,batch=pool.register(URL,{'a':500,'b':0})
        started=time.monotonic();pool.wait(token(urls['a']),urlsplit(URL).path)
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,1.45);self.assertLess(elapsed,1.7)
        self.assertEqual(pool.summary(batch)['release_reason'],'timeout')
    def test_offset_is_bound_server_side_and_cannot_be_supplied_in_url(self):
        sleeps=[];pool=DownloadAlignment(max_wait=0,sleeper=sleeps.append)
        urls,batch=pool.register(URL,{'a':30,'b':0})
        pool.wait(token(urls['a'])+'&delay=500',urlsplit(URL).path)
        self.assertEqual(sleeps,[])
        pool.wait(token(urls['a']),urlsplit(URL).path)
        self.assertTrue(0<sleeps[0]<=.03)
    def test_expiry_and_capacity_are_bounded(self):
        now=[0];pool=DownloadAlignment(capacity=1,clock=lambda:now[0])
        _,first=pool.register(URL,{'a':0,'b':0});_,blocked=pool.register(URL,{'a':0,'b':0})
        self.assertIsNone(blocked);now[0]=61
        _,second=pool.register(URL,{'a':0,'b':0})
        self.assertIsNotNone(second);self.assertIsNone(pool.summary(first));self.assertEqual(len(pool.batches),1)
    def test_error_release_unblocks_waiters_without_applying_offset(self):
        pool=DownloadAlignment();urls,batch=pool.register(URL,{'a':500,'b':0})
        t=threading.Thread(target=pool.wait,args=(token(urls['a']),urlsplit(URL).path));t.start();time.sleep(.015)
        start=time.monotonic();pool.release(batch);t.join(.15)
        self.assertFalse(t.is_alive());self.assertLess(time.monotonic()-start,.15)
    def test_http_holds_headers_only_for_initial_nonprobe_get(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'fake.mp3';path.write_bytes(b'fake'*512)
            for method,rng,waits in [('GET',None,True),('GET','bytes=0-1023',True),('GET','bytes=0-1',False),('GET','bytes=10-1023',False),('HEAD',None,False)]:
                with self.subTest(method=method,rng=rng):
                    h=server.Handler.__new__(server.Handler);h.command=method;h.path=urlsplit(URL).path+'?ac_sync='+'a'*32+'.'+'b'*16
                    h.headers={'Range':rng} if rng else {};h.client_address=('10.0.0.1',1);h.wfile=io.BytesIO();h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock()
                    def gate(*args):h.send_response.assert_not_called();h.end_headers.assert_not_called()
                    with patch('server.ALIGNMENT.wait',side_effect=gate) as wait:h.serve_audio(path)
                    self.assertEqual(wait.called,waits)
                    self.assertEqual(bool(h.wfile.getvalue()),method!='HEAD')
