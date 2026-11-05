"""Explicit, sequential microphone calibration. Statistics never change playback."""
import copy
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import statistics
import struct
import subprocess
import tempfile
import threading
import time
import wave
from audio import SpeakerBackend,SpeakerError
from ring_timing import TIMINGS

class CalibrationError(ValueError):pass

def click_mp3():
    buffer=io.BytesIO();rate=48000;frames=[]
    for i in range(int(rate*.12)):
        # A short windowed 3 kHz click, followed by silence. No bundled recording.
        envelope=math.sin(math.pi*i/(rate*.02))**2 if i<rate*.02 else 0
        frames.append(struct.pack('<h',round(16000*envelope*math.sin(2*math.pi*3000*i/rate))))
    with wave.open(buffer,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(rate);out.writeframes(b''.join(frames))
    result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-protocol_whitelist','file,pipe',
        '-f','wav','-i','pipe:0','-ar','48000','-ac','1','-codec:a','libmp3lame','-b:a','128k','-f','mp3','pipe:1'],
        input=buffer.getvalue(),capture_output=True,timeout=8)
    if result.returncode or not 128<=len(result.stdout)<=128*1024:
        raise CalibrationError('Could not prepare the calibration click.')
    return result.stdout

class Calibration:
    def __init__(self,directory,*,backend_factory=SpeakerBackend,encoder=click_mp3,clock=time.monotonic):
        self.directory=Path(directory);self.path=self.directory/'latency-calibration.json'
        self.backend_factory=backend_factory;self.encoder=encoder;self.clock=clock
        self.lock=threading.RLock();self.session=None
    def profiles(self):
        if self.path.is_symlink():raise CalibrationError('Invalid calibration storage.')
        if not self.path.exists():return {}
        value=json.loads(self.path.read_text())
        if not isinstance(value,dict):raise CalibrationError('Invalid saved calibration.')
        return value
    def status(self):
        with self.lock:
            session=self.session
            return {'profiles':self.profiles(),'running':bool(session and session['expires']>self.clock()),
                'compensation_applied':False}
    def _session(self,token):
        s=self.session
        if not s or not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token) or not secrets.compare_digest(s['token'],token) or s['expires']<=self.clock():
            raise CalibrationError('Calibration expired or was cancelled. Start it again explicitly.')
        return s
    def start(self,entry,rows,base,volume):
        if type(volume) is not int or not 1<=volume<=40:raise CalibrationError('Choose a calibration volume between 1 and 40%.')
        if not entry.get('active') or entry.get('removal_pending'):raise CalibrationError('Choose an active chime.')
        lookup={r['entity_id']:r for r in rows};targets=[]
        for entity in entry.get('members',[entry['entity_id']]):
            row=lookup.get(entity,{})
            device=row.get('ha_device_id','')
            if not row.get('available') or not re.fullmatch('[a-f0-9]{32}',str(device)):
                raise CalibrationError('Every member must be online and registered as a Home Assistant device.')
            targets.append({'entity_id':entity,'device_id':device,'name':row.get('name',entity),
                'platform':row.get('integration','')})
        if not targets or len(targets)>16 or len({r['device_id'] for r in targets})!=len(targets):
            raise CalibrationError('Calibration needs distinct physical HA device registrations.')
        with self.lock:
            if self.session and self.session['expires']>self.clock():raise CalibrationError('Cancel the existing calibration first.')
            token=secrets.token_hex(16);self.directory.mkdir(parents=True,exist_ok=True)
            sound=self.encoder();asset=self.directory/'calibration-click.mp3'
            fd=os.open(asset,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:out.write(sound)
            self.session={'token':token,'expires':self.clock()+900,'members':targets,'cursor':0,'waiting':None,
                'inflight':False,'samples':{},'asset':asset,'base':base.rstrip('/'),'volume':volume,'key':entry['key']}
            return {'session':token,'members':copy.deepcopy(targets),'trials_per_member':5,'volume':volume,'compensation_applied':False}
    def media(self,token):
        with self.lock:
            try:return self._session(token)['asset']
            except CalibrationError:return None
    def trial(self,token,index):
        with self.lock:
            s=self._session(token)
            if type(index) is not int or index!=s['cursor'] or s['inflight'] or s.get('failed') or s['waiting'] is not None or index>=5*len(s['members']):
                raise CalibrationError('Calibration trials must run once, sequentially.')
            member=s['members'][index//5];s['inflight']=True
            trace=TIMINGS.begin(s['key'],'calibrationClick');path='/media/calibration/'+token+'.mp3';TIMINGS.bind(trace,path)
            options={'speaker_entity':member['entity_id'],'group_members':[member['entity_id']],
                'member_platforms':{member['entity_id']:member['platform']},'timing_id':trace,
                'audio_url':s['base']+path+'?ac_ring='+trace}
            volume=s['volume']
        try:
            backend=self.backend_factory(options,allow_playback=True)
            grant=backend.arm_once(confirmed=True,volume=volume,expires_in=30)
            result=backend.play_once(grant['approval_id'])
            if result.get('ha_call_accepted') is not True:raise CalibrationError('Home Assistant did not accept the click.')
            record=TIMINGS.get(trace);call=(record or {}).get('ha_calls',[])
            if not call:raise CalibrationError('The playback adapter did not provide a send timestamp.')
            with self.lock:
                s=self._session(token);s['waiting']={'index':index,'trace':trace,'sent_ms':call[0]['sent']['monotonic_ms']}
            return {'index':index,'trace_id':trace,'sent_monotonic_ms':call[0]['sent']['monotonic_ms'],
                'response_monotonic_ms':call[0]['response']['monotonic_ms'] if call[0]['response'] else None}
        except Exception:
            with self.lock:
                if self.session and self.session['token']==token:self.session['failed']=True
            raise
        finally:
            with self.lock:
                if self.session and self.session['token']==token:self.session['inflight']=False
    def sample(self,token,index,latency,request_latency,uncertainty):
        for value in (latency,request_latency,uncertainty):
            if type(value) not in (float,int) or not math.isfinite(value) or not 0<=value<=10000:
                raise CalibrationError('Invalid or negative onset measurement. No profile was saved.')
        with self.lock:
            s=self._session(token);waiting=s['waiting']
            if not waiting or type(index) is not int or index!=waiting['index']:raise CalibrationError('No matching click awaits this measurement.')
            if TIMINGS.interference(waiting['sent_ms'],waiting['trace']):
                raise CalibrationError('A real chime interrupted calibration. Cancel and repeat in a quiet room.')
            member=s['members'][index//5];s['samples'].setdefault(member['device_id'],[]).append(
                {'ha_call_to_onset_ms':latency,'browser_request_to_onset_ms':request_latency,'clock_uncertainty_ms':uncertainty})
            s['cursor']+=1;s['waiting']=None;complete=s['cursor']==5*len(s['members'])
            if complete:
                profiles=self.profiles()
                for target in s['members']:
                    samples=s['samples'][target['device_id']];values=[v['ha_call_to_onset_ms'] for v in samples]
                    profiles[target['device_id']]={'entity_id_at_measurement':target['entity_id'],'measured_at_unix_ms':time.time_ns()/1e6,
                        'sample_count':5,'mean_ms':statistics.mean(values),'stddev_ms':statistics.stdev(values),
                        'min_ms':min(values),'max_ms':max(values),'clock_uncertainty_ms_max':max(v['clock_uncertainty_ms'] for v in samples),'samples':samples,'volume':s['volume'],
                        'reference':'estimated HA request send to browser microphone onset','compensation_applied':False}
                fd,name=tempfile.mkstemp(prefix='calibration-',suffix='.tmp',dir=self.directory)
                try:
                    with os.fdopen(fd,'w') as out:json.dump(profiles,out,allow_nan=False);out.flush();os.fsync(out.fileno())
                    os.replace(name,self.path)
                finally:Path(name).unlink(missing_ok=True)
                self.session=None
            return {'complete':complete,'completed_trials':index+1,'compensation_applied':False}
    def cancel(self,token):
        with self.lock:
            self._session(token);self.session=None
        return {'cancelled':True,'audio_played':False}
