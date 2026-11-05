"""Opt-in manual latency compensation, separate from identities and ringtones."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading

_LOCK=threading.RLock()
DEVICE=re.compile(r'(?:[a-f0-9]{32}|media_player\.[a-z0-9_]+)')


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
        members=[lookup.get(e,{'entity_id':e,'name':e,'ha_device_id':entry.get('member_device_ids',{}).get(e,'')}) for e in entities]
        values=self.read()
        return {'supported':len(entities)>1,'reason':'Proxies, transcoding and HA music groups can reduce precision. A single effective playback target receives no delay.',
            'members':[{'entity_id':r['entity_id'],'device_id':r.get('ha_device_id') or r['entity_id'],'name':r['name'],
                'delay_ms':values.get(r.get('ha_device_id') or r['entity_id'],0)} for r in members]}
