"""Opt-in per-speaker settings: latency compensation and volume level.

Both are stored per HA device (or entity), separate from identities and
ringtones. Saving never plays audio.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import threading

_LOCK=threading.RLock()
DEVICE=re.compile(r'(?:[a-f0-9]{32}|media_player\.[a-z0-9_]+)')
LEVEL_DEFAULT=100


class _DeviceValues:
    """A small JSON map of device id -> bounded integer."""
    FILE='';LOW=0;HIGH=0
    INVALID='Invalid speaker settings.'
    CHOOSE='Choose a valid value for each speaker.'
    TOO_MANY='Too many saved speaker settings.'

    def __init__(self,root):self.path=Path(root)/self.FILE

    def _valid(self,data,limit):
        return (isinstance(data,dict) and len(data)<=limit and all(isinstance(k,str) and DEVICE.fullmatch(k)
                and type(v) is int and self.LOW<=v<=self.HIGH for k,v in data.items()))

    def read(self):
        with _LOCK:
            if not self.path.exists():return {}
            if self.path.is_symlink() or self.path.stat().st_size>131072:raise ValueError(self.INVALID)
            data=json.loads(self.path.read_text())
            if not self._valid(data,512):raise ValueError(self.INVALID)
            return data

    def validate(self,values):
        if not values or not self._valid(values,16):raise ValueError(self.CHOOSE)

    def save(self,values):
        self.validate(values)
        with _LOCK:
            data={**self.read(),**values}
            if len(data)>512:raise ValueError(self.TOO_MANY)
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(dir=self.path.parent,prefix='settings-',suffix='.tmp')
            try:
                with os.fdopen(fd,'w') as stream:json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
                os.replace(name,self.path)
            finally:Path(name).unlink(missing_ok=True)


class SpeakerLevels(_DeviceValues):
    """Volume per speaker as a percentage of the volume Protect requests."""
    FILE='speaker-levels.json';LOW=10;HIGH=100
    INVALID='Invalid speaker volume settings.'
    CHOOSE='Choose a volume from 10 to 100% for each speaker.'
    TOO_MANY='Too many saved speaker volume settings.'


class ManualTiming(_DeviceValues):
    FILE='latency-delays.json';LOW=0;HIGH=500
    INVALID='Invalid timing settings.'
    CHOOSE='Choose a delay from 0 to 500 ms for each speaker.'
    TOO_MANY='Too many saved speaker timing settings.'

    def describe(self,entry,rows):
        lookup={r['entity_id']:r for r in rows};entities=entry.get('members') or [entry.get('entity_id')]
        members=[lookup.get(e,{'entity_id':e,'name':e,'ha_device_id':entry.get('member_device_ids',{}).get(e,'')}) for e in entities]
        values=self.read();levels=SpeakerLevels(self.path.parent).read()
        def device(r):return r.get('ha_device_id') or r['entity_id']
        return {'supported':len(entities)>1,'reason':'Proxies, transcoding and HA music groups can reduce precision. A single effective playback target receives no delay.',
            'members':[{'entity_id':r['entity_id'],'device_id':device(r),'name':r['name'],
                'model':' '.join(x for x in (r.get('manufacturer'),r.get('model')) if isinstance(x,str) and x)[:80],
                'delay_ms':values.get(device(r),0),'volume_pct':levels.get(device(r),LEVEL_DEFAULT)} for r in members]}
