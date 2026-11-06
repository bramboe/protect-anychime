"""Bounded download barrier and server-side per-output latency compensation."""
import re
import secrets
import threading
import time
from urllib.parse import urlsplit,urlunsplit

MEDIA_PATH=re.compile(r'/media/chimes/(?:primary|[a-f0-9]{24})/(?:[1-9]|10)/[a-f0-9]{32}\.mp3')
MEMBER_TOKEN=re.compile(r'([a-f0-9]{32})\.([a-f0-9]{16})')


class DownloadAlignment:
    def __init__(self,*,max_wait=1.0,lifetime=60,capacity=64,clock=time.monotonic,sleeper=time.sleep):
        self.max_wait=min(1.0,max(0,max_wait));self.lifetime=lifetime;self.capacity=capacity
        self.clock=clock;self.sleeper=sleeper;self.lock=threading.Lock();self.batches={}

    def register(self,url,offsets):
        """One opaque member token per effective request, including behind proxies."""
        parsed=urlsplit(url)
        if (not isinstance(offsets,dict) or not 2<=len(offsets)<=16 or parsed.query
                or not MEDIA_PATH.fullmatch(parsed.path)
                or any(type(v) is not int or not 0<=v<=500 for v in offsets.values())):
            return {e:url for e in offsets} if isinstance(offsets,dict) else {},None
        now=self.clock()
        with self.lock:
            for token,batch in list(self.batches.items()):
                if batch['expires']<=now:self._finish(batch,'expired',now);del self.batches[token]
            if len(self.batches)>=self.capacity:return {e:url for e in offsets},None
            token=secrets.token_hex(16);members={};urls={}
            for entity,offset in offsets.items():
                sub=secrets.token_hex(8);members[sub]={'offset':offset,'claimed':False}
                urls[entity]=urlunsplit(parsed._replace(query='ac_sync='+token+'.'+sub))
            self.batches[token]={'path':parsed.path,'members':members,'arrivals':[],
                'expires':now+self.lifetime,'deadline':None,'release_at':None,
                'ready':threading.Event(),'release_reason':None}
        return urls,token

    @staticmethod
    def _finish(batch,reason,at):
        if not batch['ready'].is_set():
            batch['release_at']=at;batch['release_reason']=reason;batch['ready'].set()

    def wait(self,token,path,peer=None):
        # peer is deliberately unused: NAT/proxies may serve many outputs from one IP.
        match=MEMBER_TOKEN.fullmatch(token) if isinstance(token,str) else None
        if not match:return
        started=self.clock()
        with self.lock:
            batch=self.batches.get(match[1]);member=batch['members'].get(match[2]) if batch else None
            if not batch or not member or batch['path']!=path or batch['expires']<=started or member['claimed']:return
            member['claimed']=True;batch['arrivals'].append(started)
            if batch['deadline'] is None:batch['deadline']=started+self.max_wait
            if len(batch['arrivals'])==len(batch['members']):self._finish(batch,'all_ready',started)
            remaining=max(0,batch['deadline']-started)
        if not batch['ready'].wait(remaining):
            with self.lock:self._finish(batch,'timeout',batch['deadline'])
        with self.lock:
            if batch['release_reason'] in ('cancelled','expired'):return
            target=batch['release_at']+member['offset']/1000
        # One shared release epoch; late wakeups never accumulate another full offset.
        now=self.clock();remaining=min(max(0,target-now),max(0,started+1.5-now))
        if remaining:self.sleeper(remaining)

    def release(self,token):
        with self.lock:
            batch=self.batches.get(token)
            if batch:self._finish(batch,'cancelled',self.clock())

    def summary(self,token):
        with self.lock:
            batch=self.batches.get(token) if isinstance(token,str) else None
            if not batch:return None
            arrivals=batch['arrivals']
            return {'expected_downloads':len(batch['members']),'distinct_downloads':len(arrivals),
                'arrival_spread_ms':round((max(arrivals)-min(arrivals))*1000,1) if len(arrivals)>1 else None,
                'release_reason':batch['release_reason'] or 'waiting'}


ALIGNMENT=DownloadAlignment()
