"""Opt-in manual latency compensation, separate from identities and ringtones."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

_LOCK=threading.RLock()
DEVICE=re.compile('[a-f0-9]{32}')


class ManualTiming:
    def __init__(self,root):self.path=Path(root)/'latency-delays.json'
    def read(self):
        with _LOCK:
            if not self.path.exists():return {}
            if self.path.is_symlink() or self.path.stat().st_size>131072:raise ValueError('Invalid timing settings.')
            data=json.loads(self.path.read_text())
            if (not isinstance(data,dict) or len(data)>512 or any(not isinstance(k,str) or not DEVICE.fullmatch(k)
                    or type(v) is not int or not 0<=v<=500 for k,v in data.items())):
                raise ValueError('Invalid timing settings.')
            return data
    def save(self,values):
        if not isinstance(values,dict) or not values or len(values)>16 or any(not isinstance(k,str) or not DEVICE.fullmatch(k)
                or type(v) is not int or not 0<=v<=500 for k,v in values.items()):
            raise ValueError('Choose a delay from 0 to 500 ms for each speaker.')
        with _LOCK:
            data={**self.read(),**values}
            if len(data)>512:raise ValueError('Too many saved speaker timing settings.')
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(dir=self.path.parent,prefix='timing-',suffix='.tmp')
            try:
                with os.fdopen(fd,'w') as stream:json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
                os.replace(name,self.path)
            finally:Path(name).unlink(missing_ok=True)
    def describe(self,entry,rows):
        lookup={r['entity_id']:r for r in rows};entities=entry.get('members') or [entry.get('entity_id')]
        members=[lookup[e] for e in entities if e in lookup]
        supported=len(members)==len(entities) and len({r.get('ha_device_id') for r in members})==len(members) and all(DEVICE.fullmatch(r.get('ha_device_id','')) for r in members)
        if members:
            supported=supported and all(r.get('integration')=='sonos' and (r.get('features') or 0)&1048576 for r in members)
        values=self.read()
        return {'supported':bool(supported),'reason':'' if supported else 'Individual timing is unavailable for this integration or device metadata. Existing group playback is retained.',
            'members':[{'entity_id':r['entity_id'],'device_id':r.get('ha_device_id',''),'name':r['name'],
                'delay_ms':values.get(r.get('ha_device_id'),0)} for r in members]}


class DelayAudioCache:
    MAX_BYTES=128*1024*1024
    MAX_FILE=25*1024*1024
    def __init__(self,root,*,renderer=subprocess.run,clock=time.time):
        self.directory=Path(root)/'latency-cache';self.renderer=renderer;self.clock=clock
    @staticmethod
    def filename(md5,delay):
        if not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5) or type(delay) is not int or not 0<=delay<=500:
            raise ValueError('Invalid delayed audio request.')
        return f'{md5}-{delay}-1.wav'
    def media(self,md5,delay):
        path=self.directory/self.filename(md5,delay)
        return path if path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=self.MAX_FILE else None
    def render(self,asset,delay):
        name=self.filename(asset['md5'],delay)
        with _LOCK:
            self.directory.mkdir(parents=True,exist_ok=True)
            existing=self.media(asset['md5'],delay)
            if existing:os.utime(existing,None);return existing
            fd,temp=tempfile.mkstemp(dir=self.directory,prefix='render-',suffix='.wav');os.close(fd)
            try:
                result=self.renderer(['ffmpeg','-v','error','-nostdin','-y','-protocol_whitelist','file,pipe',
                    '-i',str(asset['path']),'-vn','-af',f'aresample=48000,adelay={delay*48}S:all=1',
                    '-ar','48000','-ac','2','-c:a','pcm_s16le','-t','120.5','-f','wav',temp],
                    capture_output=True,timeout=20)
                size=Path(temp).stat().st_size
                if result.returncode or not 44<size<=self.MAX_FILE:raise ValueError('Could not prepare delayed audio. No playback started.')
                files=[p for p in self.directory.glob('*.wav') if p!=Path(temp) and not p.is_symlink()]
                total=sum(p.stat().st_size for p in files)
                for p in sorted(files,key=lambda p:p.stat().st_mtime):
                    if total+size<=self.MAX_BYTES:break
                    if self.clock()-p.stat().st_mtime<120:continue
                    total-=p.stat().st_size;p.unlink()
                if total+size>self.MAX_BYTES:raise ValueError('Timing audio cache is busy. Try again later.')
                path=self.directory/name;os.replace(temp,path);return path
            finally:Path(temp).unlink(missing_ok=True)
