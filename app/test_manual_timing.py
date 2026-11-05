"""Manual timing tests: synthetic media and fake HA transports, never speakers."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock,patch
from urllib.parse import urlsplit,parse_qs
from manual_timing import ManualTiming,DelayAudioCache
from announcement_sync import DownloadAlignment
from audio import SpeakerBackend,MEDIA_ANNOUNCE
from registry import ChimeRegistry
from chime_layout import change
from test_chime_layout import GROUP,ROWS,FakeProtect
from runtime import NativeWorker
from ring_timing import RingTimings
import server

class ManualTimingTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_profiles_are_opt_in_bounded_private_and_stable_by_device_id(self):
        settings=ManualTiming(self.root);self.assertEqual(settings.read(),{})
        settings.save({'a'*32:25,'b'*32:0})
        self.assertEqual(ManualTiming(self.root).read(),{'a'*32:25,'b'*32:0})
        self.assertEqual(settings.path.stat().st_mode&0o077,0)
        for values in [{'a'*32:501},{'a'*32:True},{'a'*32:-1},{'bad':1}]:
            with self.assertRaises(ValueError):settings.save(values)
        self.assertEqual(settings.read()['a'*32],25)
    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg required for PCM verification')
    def test_pcm_delay_is_exact_and_cache_reused_without_audio_output(self):
        source=self.root/'synthetic.wav'
        with wave.open(str(source),'wb') as w:
            w.setnchannels(2);w.setsampwidth(2);w.setframerate(48000)
            w.writeframes(b'\xff\x1f'*2+b'\0'*4*4799)
        asset={'md5':hashlib.md5(source.read_bytes()).hexdigest(),'path':source}
        cache=DelayAudioCache(self.root)
        p0=cache.render(asset,0);p1=cache.render(asset,100)
        with wave.open(str(p0),'rb') as w:base=w.readframes(w.getnframes())
        with wave.open(str(p1),'rb') as w:delayed=w.readframes(w.getnframes())
        self.assertEqual(delayed[:4800*4],b'\0'*4800*4)
        self.assertEqual(delayed[4800*4:],base)
        with patch.object(cache,'renderer') as render:self.assertEqual(cache.render(asset,100),p1);render.assert_not_called()
        with self.assertRaises(ValueError):cache.media(asset['md5'],501)
    def test_multiple_urls_share_one_download_barrier_and_reject_wrong_path(self):
        barrier=DownloadAlignment(max_wait=.05)
        urls,token=barrier.register_many({'a':'http://ha/media/delayed/'+'a'*32+'/0.wav','b':'http://ha/media/delayed/'+'a'*32+'/40.wav'})
        self.assertTrue(token);self.assertEqual({parse_qs(urlsplit(u).query)['ac_sync'][0] for u in urls.values()},{token})
        barrier.wait(token,'/wrong','wrong')
        threads=[threading.Thread(target=barrier.wait,args=(token,urlsplit(u).path,e)) for e,u in urls.items()]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(barrier.summary(token)['release_reason'],'all_ready')
        self.assertEqual(barrier.summary(token)['distinct_downloads'],2)
    def test_compensated_announcements_start_in_parallel_and_preserve_volume(self):
        arrived=threading.Barrier(2);calls=[]
        def request(method,path,body):
            if method=='GET':return {'entity_id':path[len('/states/'):] , 'state':'idle','attributes':{'supported_features':MEDIA_ANNOUNCE,'group_members':GROUP}}
            calls.append(body);arrived.wait(timeout=2);return []
        media={e:'http://ha/media/delayed/'+'a'*32+'/'+str(i*30)+'.wav' for i,e in enumerate(GROUP)}
        backend=SpeakerBackend({'speaker_entity':GROUP[0],'audio_url':'http://ha/media/chimes/primary/1/'+'a'*32+'.mp3',
            'group_members':GROUP,'member_platforms':{e:'sonos' for e in GROUP},'member_media':media},requester=request,allow_playback=True)
        grant=backend.arm_once(confirmed=True,volume=10);result=backend.play_once(grant['approval_id'])
        self.assertTrue(result['ha_call_accepted']);self.assertEqual(len(calls),2)
        self.assertEqual({b['entity_id'] for b in calls},set(GROUP))
        self.assertTrue(all(b['announce'] and b['extra']['volume']==10 for b in calls))
        self.assertEqual(len({parse_qs(urlsplit(b['media_content_id']).query)['ac_sync'][0] for b in calls}),1)
    def test_saved_original_group_can_restore_after_ha_ungroups_without_ha_services(self):
        registry=ChimeRegistry(self.root);registry.layouts.set_mode(GROUP,True)
        raw=[{**r,'group':[r['entity_id']]} for r in ROWS]
        decorated=registry.layouts.decorate(raw)
        self.assertEqual(decorated[0]['ha_group_members'],GROUP)
        self.assertTrue(decorated[0]['chime_only_group'])
        registry.sync([GROUP[1]],raw)
        old=next(e for e in registry.snapshot() if e['active']);p=registry.identity_dir(old);p.mkdir(parents=True);(p/'identity.json').write_text('{"stable":true}')
        worker=NativeWorker();worker.registry=registry
        result=change(worker,FakeProtect(),'10.23.45.1',GROUP,False,raw)
        self.assertEqual(result['preserved_key'],old['key']);self.assertFalse(result['adoption_required'])
        self.assertEqual((p/'identity.json').read_text(),'{"stable":true}')
        fresh=ChimeRegistry(self.root);fresh.sync(fresh.selected_entities(),raw,user_change=False)
        active=[e for e in fresh.snapshot() if e['active']]
        self.assertEqual(len(active),1);self.assertEqual(set(active[0]['members']),set(GROUP))
        self.assertTrue(active[0]['chime_only_group'])
    def test_timing_endpoints_reject_loopback_and_missing_confirmation_before_mutation(self):
        for source,confirmed in [('127.0.0.1',True),('172.30.32.2',False)]:
            h=server.Handler.__new__(server.Handler);h.path='/api/timing';h.client_address=(source,1)
            body=json.dumps({'confirmed':confirmed}).encode();h.headers={'Content-Length':str(len(body))};h.rfile=io.BytesIO(body);h.respond=Mock()
            with patch.object(server,'MANUAL_TIMING') as timing:h.do_POST();timing.save.assert_not_called()
            self.assertIn(h.respond.call_args.args[0],(400,403))
    def test_ring_diagnostics_correlate_each_delayed_path(self):
        trace=RingTimings();token=trace.begin('primary','playSpeaker');trace.bind(token,'/original.mp3')
        for offset,ip in [(0,'10.1.1.2'),(50,'10.1.1.3')]:
            url='http://ha/media/delayed/'+'a'*32+'/'+str(offset)+'.wav?ac_sync='+'b'*32
            trace.correlate(token,url);self.assertEqual(trace.download(url,ip),token);trace.end(token,ip,True)
        self.assertEqual(len(trace.get(token)['downloads']),2)

    def test_virtual_sonos_group_plays_members_without_changing_actual_ha_group(self):
        requests=[]
        def request(method,path,body):
            if method=='GET':
                entity=path[len('/states/'):]
                return {'entity_id':entity,'state':'idle','attributes':{'supported_features':MEDIA_ANNOUNCE,'group_members':[entity]}}
            requests.append((path,body));return []
        options={'speaker_entity':GROUP[0],'audio_url':'http://ha/media/chimes/primary/1/'+'a'*32+'.mp3',
            'group_members':GROUP,'member_platforms':{e:'sonos' for e in GROUP},'chime_only_group':True}
        backend=SpeakerBackend(options,requester=request,allow_playback=True)
        grant=backend.arm_once(confirmed=True,volume=10);backend.play_once(grant['approval_id'])
        self.assertEqual(len(requests),1);self.assertEqual(requests[0][0],'/services/media_player/play_media')
        self.assertEqual(requests[0][1]['entity_id'],GROUP);self.assertTrue(requests[0][1]['announce'])
    def test_unsupported_group_cannot_save_individual_timing(self):
        rows=[{'entity_id':e,'name':e,'ha_device_id':str(i)*32,'integration':'cast','features':MEDIA_ANNOUNCE} for i,e in enumerate(GROUP)]
        result=ManualTiming(self.root).describe({'members':GROUP},rows)
        self.assertFalse(result['supported']);self.assertTrue(result['reason'])
    def test_track_player_applies_saved_delay_to_both_member_urls_before_ha_call(self):
        from playback import TrackPlayer
        ManualTiming(self.root).save({'a'*32:40})
        store=Mock();asset={'track_no':1,'md5':'c'*32,'path':self.root/'synthetic.mp3'}
        backend=Mock();backend.arm_once.return_value={'approval_id':'fake'};backend.play_once.return_value={'ha_call_accepted':True};backend.last_alignment_token=None
        factory=Mock(return_value=backend)
        player=TrackPlayer('primary',GROUP[0],store,'http://ha',group_members=GROUP,
            member_platforms={e:'sonos' for e in GROUP},member_device_ids={GROUP[0]:'a'*32,GROUP[1]:'b'*32},
            data_dir=self.root,backend_factory=factory)
        with patch('playback.DelayAudioCache.render') as render:player._run(asset,10,1)
        self.assertEqual(render.call_count,2)
        options=factory.call_args.args[0]
        self.assertTrue(options['member_media'][GROUP[0]].endswith('/40.wav'))
        self.assertTrue(options['member_media'][GROUP[1]].endswith('/0.wav'))
        backend.play_once.assert_called_once()
    def test_timing_save_rejects_foreign_device_ids_without_mutation(self):
        registry=ChimeRegistry(self.root);registry.sync([GROUP[0]],ROWS)
        rows=[{**r,'ha_device_id':('a' if i==0 else 'b')*32,'features':MEDIA_ANNOUNCE} for i,r in enumerate(ROWS)]
        h=server.Handler.__new__(server.Handler);h.path='/api/timing';h.client_address=('172.30.32.2',1)
        data={'confirmed':True,'key':'primary','delays':{'f'*32:40}};body=json.dumps(data).encode()
        h.headers={'Content-Length':str(len(body)),'X-Prototype-Test':'silent'};h.rfile=io.BytesIO(body);h.respond=Mock()
        worker=Mock(registry=registry);timing=ManualTiming(self.root)
        with patch.object(server,'NATIVE',worker),patch.object(server,'MANUAL_TIMING',timing),patch('server.speakers.discover',return_value=rows):h.do_POST()
        self.assertEqual(h.respond.call_args.args[0],400);self.assertFalse(timing.path.exists())
    def test_render_failure_never_sends_ha_play_request(self):
        from playback import TrackPlayer
        ManualTiming(self.root).save({'a'*32:40});factory=Mock()
        player=TrackPlayer('primary',GROUP[0],Mock(),'http://ha',group_members=GROUP,
            member_platforms={e:'sonos' for e in GROUP},member_device_ids={GROUP[0]:'a'*32},data_dir=self.root,backend_factory=factory)
        with patch('playback.DelayAudioCache.render',side_effect=ValueError('Synthetic cache full')):
            player._run({'track_no':1,'md5':'a'*32,'path':self.root/'fake.mp3'},10,1)
        factory.assert_not_called();self.assertEqual(player.last_result['state'],'failed')
    def test_cache_limit_preserves_recent_files_and_evicts_only_old_variants(self):
        def render(args,**kwargs):Path(args[-1]).write_bytes(b'\0'*100);return Mock(returncode=0)
        cache=DelayAudioCache(self.root,renderer=render);cache.MAX_BYTES=150
        first=cache.render({'md5':'a'*32,'path':'synthetic'},10)
        with self.assertRaisesRegex(ValueError,'busy'):cache.render({'md5':'b'*32,'path':'synthetic'},20)
        self.assertTrue(first.exists())
        import os,time
        os.utime(first,(time.time()-180,time.time()-180))
        second=cache.render({'md5':'b'*32,'path':'synthetic'},20)
        self.assertFalse(first.exists());self.assertTrue(second.exists())
    def test_manual_controls_work_over_http_and_only_explicit_test_posts_audio(self):
        import subprocess
        asset=Path(__file__).with_name('assets')/'calibration.js'
        source=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
(async()=>{
 const nodes=new Map(),posts=[];
 function el(){return {disabled:false,textContent:'',get value(){return this._value||'';},set value(v){this._value=String(v);},dataset:{},style:{},children:[],append(x){this.children.push(x);},replaceChildren(){this.children=[];},querySelectorAll(){return this.children.flatMap(x=>x.children);},addEventListener(){},showModal(){},close(){}};}
 const node=id=>{if(!nodes.has(id))nodes.set(id,el());return nodes.get(id);};
 const c={window:{isSecureContext:false},navigator:{},document:{getElementById:node,createElement:el,addEventListener(){}},setTimeout,clearTimeout,console,
 fetch:async(path,options)=>{let data={};if(options?.method==='POST'){const body=JSON.parse(options.body);posts.push({path,body});data={test_requested:true,volume:10};}
 else if(path.includes('timing?'))data={supported:true,members:[{name:'A',device_id:'a'.repeat(32),delay_ms:0},{name:'B',device_id:'b'.repeat(32),delay_ms:0}]};
 return {ok:true,json:async()=>data};}};
 vm.createContext(c);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),c);
 await c.window.openChimeMeasurements('primary');assert.equal(posts.length,0);
 assert.equal(node('calibration-start').disabled,true);assert.equal(node('manual-timing-test').disabled,false);
 const input=node('manual-timing-members').querySelectorAll()[0];input.value='30';input.oninput();
 assert.equal(posts.length,0);assert.equal(node('manual-timing-test').disabled,true);
 await node('manual-timing-save').onclick();assert.equal(posts.length,1);assert.equal(posts[0].path,'api/timing');
 assert.equal(posts[0].body.delays['a'.repeat(32)],30);assert.equal(posts[0].body.confirmed,true);
 await node('manual-timing-test').onclick();assert.equal(posts.length,2);
 assert.equal(posts[1].path,'api/chimes/test');assert.equal(posts[1].body.volume,10);
})().catch(e=>{console.error(e);process.exit(1);});
"""
        subprocess.run(['node','-e',source,str(asset)],check=True,capture_output=True,timeout=5)
    def test_custom_group_without_history_can_preserve_existing_paired_member(self):
        raw=[{**r,'group':[r['entity_id']],'features':MEDIA_ANNOUNCE} for r in ROWS]
        worker=NativeWorker();worker.registry=ChimeRegistry(self.root)
        worker.registry.sync([GROUP[1]],raw)
        old=next(e for e in worker.registry.snapshot() if e['active']);path=worker.registry.identity_dir(old);path.mkdir(parents=True);(path/'identity.json').write_text('{"kept":true}')
        manager=FakeProtect();manager.ready=False
        result=change(worker,manager,'10.23.45.1',GROUP,False,raw,allow_custom=True)
        self.assertEqual(result['preserved_key'],old['key']);self.assertEqual(manager.deleted,[])
        self.assertEqual((path/'identity.json').read_text(),'{"kept":true}')
        self.assertEqual(set(worker.registry.selected_entities()),set(GROUP))
        self.assertEqual(len([e for e in worker.registry.snapshot() if e['active']]),1)
    def test_unused_custom_group_records_layout_without_adding_or_adopting(self):
        raw=[{**r,'group':[r['entity_id']],'features':MEDIA_ANNOUNCE} for r in ROWS]
        worker=NativeWorker();worker.registry=ChimeRegistry(self.root);manager=FakeProtect();manager.ready=False
        result=change(worker,manager,'10.23.45.1',GROUP,False,raw,allow_custom=True)
        self.assertTrue(result['preference_only']);self.assertEqual(result['selected'],[])
        self.assertEqual(worker.registry.layouts.snapshot()['original_groups'],[GROUP])
        self.assertEqual(manager.deleted,[])
    def test_custom_groups_reject_other_integrations_and_partial_ha_music_groups(self):
        worker=NativeWorker();worker.registry=ChimeRegistry(self.root)
        raw=[{**r,'group':[r['entity_id']],'features':MEDIA_ANNOUNCE} for r in ROWS]
        for changes in [{'integration':'cast'},{'features':0},{'group':[GROUP[0],'media_player.office']}]:
            rows=[{**raw[0],**changes},*raw[1:]]
            with self.assertRaises(ValueError):change(worker,FakeProtect(),'10.23.45.1',GROUP,False,rows,allow_custom=True)
        self.assertIsNone(worker.registry.layouts.pending);self.assertEqual(worker.registry.selected_entities(),[])
    def test_new_custom_set_replaces_overlapping_unused_group_preferences(self):
        from chime_layout import ChimeLayouts
        layouts=ChimeLayouts(self.root/'chime-layout.json')
        layouts.set_mode([GROUP[0],'media_player.office'],True)
        layouts.set_mode(GROUP,False)
        raw=[{**r,'group':[r['entity_id']]} for r in ROWS]
        decorated=layouts.decorate(raw)
        lookup={r['entity_id']:r for r in decorated}
        self.assertEqual(set(lookup[GROUP[0]]['ha_group_members']),set(GROUP))
        self.assertFalse(lookup[GROUP[0]]['chime_separate'])
        self.assertEqual(lookup['media_player.office']['ha_group_members'],['media_player.office'])
