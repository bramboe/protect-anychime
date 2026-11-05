"""Phase 1 uses fake HA transports, clocks and clicks; never touches real audio."""
import asyncio
import io
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch,AsyncMock
from ring_timing import RingTimings
from calibration import Calibration,CalibrationError
from audio import SpeakerBackend,MEDIA_ANNOUNCE
from runtime import NativeWorker
import calibration,server
from test_support import SYNTHETIC_MP3,ringtone_file

PATH='/media/chimes/primary/1/'+'a'*32+'.mp3'
class TimingTests(unittest.TestCase):
    def setUp(self):
        self.now=[1_000_000_000];self.trace=RingTimings(capacity=2,clock=lambda:self.now[0],wall=lambda:self.now[0]+1000000000000)
    def test_ring_records_received_ha_send_response_and_per_ip_get_and_end(self):
        tid=self.trace.begin('primary','playBuzzer');self.trace.bind(tid,PATH)
        url='http://10.23.45.20:48200'+PATH+'?ac_sync='+'b'*32;self.trace.correlate(tid,url)
        self.now[0]+=1_000_000;call=self.trace.sent(tid,['media_player.a','media_player.b'])
        self.now[0]+=4_000_000;self.trace.response(tid,call,True)
        for peer in ['10.23.45.31','10.23.45.32']:
            self.now[0]+=1_000_000;self.assertEqual(self.trace.download(url,peer),tid)
            self.now[0]+=2_000_000;self.trace.end(tid,peer,True)
        r=self.trace.get(tid);self.assertEqual(r['action'],'playBuzzer')
        self.assertLess(r['received']['monotonic_ms'],r['ha_calls'][0]['sent']['monotonic_ms'])
        self.assertLess(r['ha_calls'][0]['sent']['monotonic_ms'],r['ha_calls'][0]['response']['monotonic_ms'])
        self.assertEqual(len(r['downloads']),2)
        for event in r['downloads'].values():
            self.assertLess(event['first_get']['monotonic_ms'],event['response_end']['monotonic_ms'])
            self.assertEqual(event['correlation'],'request_token')
        self.assertNotIn('media_path',r);self.assertNotIn('download_tokens',r)
    def test_singleton_url_is_unchanged_and_path_inference_is_labelled(self):
        tid=self.trace.begin('primary','playSpeaker');self.trace.bind(tid,PATH);self.trace.sent(tid,['media_player.a'])
        self.assertEqual(self.trace.download('http://example.test'+PATH,'10.23.45.31'),tid)
        self.assertEqual(self.trace.get(tid)['downloads']['10.23.45.31']['correlation'],'path_heuristic')
    def test_tracing_is_bounded_and_wrong_path_does_not_attach_a_download(self):
        tid=self.trace.begin('primary','playSpeaker');self.trace.bind(tid,PATH)
        self.assertIsNone(self.trace.download('http://example.test/wrong?ac_ring='+tid,'peer'))
        self.trace.begin('primary','playSpeaker');self.trace.begin('primary','playSpeaker')
        self.assertIsNone(self.trace.get(tid));self.assertEqual(len(self.trace.recent()),2)
    def test_adapter_keeps_payload_and_request_count_while_capturing_transport_times(self):
        calls=[];tid=self.trace.begin('primary','playSpeaker');self.trace.bind(tid,PATH)
        url='http://10.23.45.20:48200'+PATH
        def request(method,path,body):
            calls.append((method,path,body))
            if method=='GET':return {'entity_id':'media_player.a','state':'idle','attributes':{'supported_features':516|MEDIA_ANNOUNCE}}
            self.assertIsNotNone(self.trace.get(tid)['ha_calls'][0]['sent']);return []
        backend=SpeakerBackend({'speaker_entity':'media_player.a','audio_url':url,'timing_id':tid},requester=request,allow_playback=True)
        with patch('audio.TIMINGS',self.trace):backend.play_once(backend.arm_once(confirmed=True,volume=30)['approval_id'])
        posts=[c for c in calls if c[0]=='POST'];self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][2]['media_content_id'],url);self.assertEqual(posts[0][2]['extra']['volume'],30)
        self.assertTrue(self.trace.get(tid)['ha_calls'][0]['accepted'])
    def test_http_response_completion_and_failed_write_are_measured_without_head_noise(self):
        for command,fail in [('GET',False),('GET',True),('HEAD',False)]:
            tid=self.trace.begin('primary','playSpeaker');self.trace.bind(tid,PATH)
            h=server.Handler.__new__(server.Handler);h.path=PATH+'?ac_ring='+tid;h.command=command
            h.client_address=('10.23.45.31',1);h.headers={};h.wfile=Mock() if fail else io.BytesIO()
            h.send_response=Mock();h.send_header=Mock();h.end_headers=Mock()
            if fail:h.wfile.write.side_effect=BrokenPipeError()
            with patch('server.TIMINGS',self.trace):
                if fail:
                    with self.assertRaises(BrokenPipeError):h.serve_audio(ringtone_file())
                else:h.serve_audio(ringtone_file())
            downloads=self.trace.get(tid)['downloads']
            if command=='HEAD':self.assertEqual(downloads,{})
            else:
                self.assertIsNotNone(downloads['10.23.45.31']['response_end'])
                self.assertEqual(downloads['10.23.45.31']['write_failed'],fail)

class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.calls=[];self.trace=RingTimings()
        def factory(options,**kwargs):
            self.assertTrue(kwargs['allow_playback']);self.assertEqual(len(options['group_members']),1)
            self.calls.append(options);backend=Mock();backend.arm_once.return_value={'approval_id':'synthetic'}
            def play(_):
                self.trace.correlate(options['timing_id'],options['audio_url']);idx=self.trace.sent(options['timing_id'],[options['speaker_entity']]);self.trace.response(options['timing_id'],idx,True)
                return {'ha_call_accepted':True}
            backend.play_once.side_effect=play;return backend
        self.manager=Calibration(self.temp.name,backend_factory=factory,encoder=Mock(return_value=SYNTHETIC_MP3))
        self.entry={'key':'primary','entity_id':'media_player.a','members':['media_player.a','media_player.b'],'active':True}
        self.rows=[{'entity_id':e,'ha_device_id':str(i+1)*32,'available':True,'name':e,'integration':'example'} for i,e in enumerate(self.entry['members'])]
        self.patcher=patch('calibration.TIMINGS',self.trace);self.patcher.start()
    def tearDown(self):self.patcher.stop();self.temp.cleanup()
    def start(self):return self.manager.start(self.entry,self.rows,'http://10.23.45.20:48200',10)['session']
    def test_start_status_and_cancel_do_not_play_anything(self):
        self.manager.status();token=self.start();self.assertEqual(self.calls,[])
        self.manager.cancel(token);self.assertEqual(self.calls,[])
        with self.assertRaises(CalibrationError):self.manager.trial(token,0)
    def test_exactly_five_sequential_clicks_per_member_save_mean_and_spread_by_device_id(self):
        token=self.start();normal=Path(self.temp.name)/'existing-ringtone';normal.write_bytes(b'unchanged')
        for i in range(10):
            response=self.manager.trial(token,i);self.assertEqual(response['index'],i)
            result=self.manager.sample(token,i,100+i%5,120+i%5,2)
        self.assertTrue(result['complete']);self.assertFalse(result['compensation_applied'])
        self.assertEqual([c['speaker_entity'] for c in self.calls],['media_player.a']*5+['media_player.b']*5)
        profiles=self.manager.profiles();self.assertEqual(set(profiles),{'1'*32,'2'*32})
        for p in profiles.values():
            self.assertEqual(p['sample_count'],5);self.assertEqual(p['mean_ms'],102)
            self.assertAlmostEqual(p['stddev_ms'],statistics.stdev([100,101,102,103,104]))
            self.assertFalse(p['compensation_applied'])
        self.assertEqual(self.manager.path.stat().st_mode&0o077,0);self.assertEqual(normal.read_bytes(),b'unchanged')
    def test_trial_replay_and_out_of_order_requests_cannot_send_extra_clicks(self):
        token=self.start();self.manager.trial(token,0)
        for index in [0,1,-1,True]:
            with self.assertRaises(CalibrationError):self.manager.trial(token,index)
        self.assertEqual(len(self.calls),1)
    def test_invalid_measurement_and_interference_do_not_save_a_profile(self):
        token=self.start();self.manager.trial(token,0)
        for value in [float('nan'),-1,True,10001]:
            with self.assertRaises(CalibrationError):self.manager.sample(token,0,value,100,1)
        self.trace.begin('other','playSpeaker')
        with self.assertRaisesRegex(CalibrationError,'interrupted'):self.manager.sample(token,0,100,110,1)
        self.assertFalse(self.manager.path.exists())
    def test_endpoint_rejects_missing_explicit_click_or_microphone_before_any_start(self):
        for source,body in [('127.0.0.1',{'confirmed':True}),('172.30.32.2',{}),('172.30.32.2',{'confirmed':True})]:
            h=server.Handler.__new__(server.Handler);h.path='/api/calibration/start';h.client_address=(source,1)
            raw=json.dumps(body).encode();h.headers={'X-Prototype-Test':'silent','Content-Length':str(len(raw))};h.rfile=io.BytesIO(raw);h.respond=Mock()
            with patch('server.CALIBRATION') as manager:h.do_POST()
            self.assertIn(h.respond.call_args.args[0],(400,403));manager.start.assert_not_called()
    def test_synthetic_encoder_generates_only_a_bounded_local_wav_input(self):
        import wave
        with patch('calibration.subprocess.run',return_value=Mock(returncode=0,stdout=SYNTHETIC_MP3)) as execute:
            self.assertEqual(calibration.click_mp3(),SYNTHETIC_MP3)
        with wave.open(io.BytesIO(execute.call_args.kwargs['input'])) as data:
            self.assertEqual(data.getnchannels(),1);self.assertEqual(data.getframerate(),48000)
            self.assertLess(data.getnframes(),48000)
        self.assertNotIn('shell',execute.call_args.kwargs)

class OnsetTests(unittest.TestCase):
    def test_synthetic_browser_meter_noise_spike_click_and_reset(self):
        path=Path(__file__).with_name('assets')/'onset-detector.js'
        source="const D=require(process.argv[1]);const a=require('assert');const d=new D();a.throws(()=>d.arm());for(let i=0;i<100;i++)d.observe(i*.003,.0001);d.arm();a.strictEqual(d.observe(1,.1),null);a.strictEqual(d.observe(1.003,0),null);a.strictEqual(d.observe(2,.1),null);a.strictEqual(d.observe(2.003,.1),2);d.reset();a.throws(()=>d.arm());for(let i=0;i<40;i++)d.observe(i,.1);a.throws(()=>d.arm());"
        subprocess.run(['node','-e',source,str(path)],check=True,capture_output=True,timeout=5)

class AdoptionOptInTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_default_never_calls_auto_adoption(self):
        worker=NativeWorker();worker.auto_adopter=Mock();worker.nodes={'primary':Mock()}
        await worker._auto_adopt_new_chimes();worker.auto_adopter.assert_not_called()

class CalibrationBrowserAvailabilityTests(unittest.TestCase):
    def test_http_and_missing_microphone_explain_disabled_start_without_audio_or_capture(self):
        asset=Path(__file__).with_name('assets')/'calibration.js'
        source=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
(async()=>{for(const [secure,mic,enabled,help] of [[false,false,false,'HTTP'],[true,false,false,'unavailable'],[true,true,true,'fixed microphone']]){
 const nodes=new Map();let captures=0,posts=0;
 const node=id=>{if(!nodes.has(id))nodes.set(id,{disabled:true,textContent:'',classList:{},addEventListener(){},showModal(){},replaceChildren(){},append(){}});return nodes.get(id);};
 const c={window:{isSecureContext:secure},navigator:{mediaDevices:mic?{getUserMedia(){captures++;throw Error('Capture must not start');}}:undefined},document:{getElementById:node,addEventListener(){},createElement(){return {}; }},fetch:async(p,o)=>{if(o?.method==='POST')posts++;return {ok:true,json:async()=>({profiles:{},rings:[]})};},setTimeout,clearTimeout,console};
 vm.createContext(c);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),c);
 await c.window.openChimeMeasurements('primary');
 assert.equal(node('calibration-start').disabled,!enabled);
 assert.ok(node('calibration-context').textContent.includes(help));assert.equal(captures,0);assert.equal(posts,0);
}})().catch(e=>{console.error(e);process.exit(1);});
"""
        subprocess.run(['node','-e',source,str(asset)],check=True,capture_output=True,timeout=5)
