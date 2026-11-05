"""Bounded alignment of local audio downloads, not a shared speaker playback clock."""
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit

MEDIA_PATH=re.compile(r'(?:/media/chimes/(?:primary|[a-f0-9]{24})/(?:[1-9]|10)/[a-f0-9]{32}\.mp3|/media/delayed/[a-f0-9]{32}/[0-9]{1,3}\.wav)')

class DownloadAlignment:
    def __init__(self, *, max_wait=1.0, lifetime=60, capacity=64, clock=time.monotonic):
        self.max_wait=max_wait
        self.lifetime=lifetime
        self.capacity=capacity
        self.clock=clock
        self.lock=threading.Lock()
        self.batches={}

    def register(self,url,expected):
        parsed=urlsplit(url)
        if type(expected) is not int or not 2<=expected<=16 or parsed.query or not MEDIA_PATH.fullmatch(parsed.path):
            return url,None
        now=self.clock()
        with self.lock:
            for key,batch in list(self.batches.items()):
                if batch['expires']<=now:
                    batch['ready'].set()
                    del self.batches[key]
            if len(self.batches)>=self.capacity:
                return url,None
            token=secrets.token_hex(16)
            self.batches[token]={'path':parsed.path,'expected':expected,'peers':set(),
                'expires':now+self.lifetime,'deadline':None,'ready':threading.Event(),
                'first_arrival':None,'last_arrival':None,'release_reason':None}
        return urlunsplit(parsed._replace(query='ac_sync='+token)),token

    def register_many(self,urls):
        values=list(urls.values())
        if not values:return urls,None
        first=urlsplit(values[0])
        if any(urlsplit(u).query or urlsplit(u).netloc!=first.netloc or not MEDIA_PATH.fullmatch(urlsplit(u).path) for u in values):
            return urls,None
        _,token=self.register(values[0],len(values))
        if not token:return urls,None
        with self.lock:self.batches[token]['paths']={urlsplit(u).path for u in values}
        return {e:urlunsplit(urlsplit(u)._replace(query='ac_sync='+token)) for e,u in urls.items()},token

    def _finish(self,batch,reason):
        if not batch['ready'].is_set():
            batch['release_reason']=reason
            batch['ready'].set()

    def wait(self,token,path,peer):
        with self.lock:
            batch=self.batches.get(token)
            now=self.clock()
            if not batch or path not in batch.get('paths',{batch['path']}) or batch['expires']<=now:
                return
            if peer not in batch['peers'] and len(batch['peers'])<32:
                batch['peers'].add(peer)
                if batch['first_arrival'] is None:batch['first_arrival']=now
                batch['last_arrival']=now
            # Keep recording late arrivals after release for timing diagnostics.
            if batch['ready'].is_set():return
            if batch['deadline'] is None:batch['deadline']=now+self.max_wait
            if len(batch['peers'])>=batch['expected']:self._finish(batch,'all_ready')
            remaining=max(0,batch['deadline']-now)
        if not batch['ready'].wait(remaining):
            with self.lock:self._finish(batch,'timeout')

    def release(self,token):
        with self.lock:
            batch=self.batches.get(token)
            if batch:self._finish(batch,'cancelled')

    def summary(self,token):
        # No addresses, media paths, URLs or tokens are exposed in diagnostics.
        if not isinstance(token,str):return None
        with self.lock:
            batch=self.batches.get(token)
            if not batch:return None
            spread=None if len(batch['peers'])<2 else round((batch['last_arrival']-batch['first_arrival'])*1000,1)
            return {'expected_downloads':batch['expected'],'distinct_downloads':len(batch['peers']),
                'arrival_spread_ms':spread,'wait_limit_ms':round(self.max_wait*1000),
                'release_reason':batch['release_reason'] or 'waiting',
                'acoustic_sync_verified':False}

ALIGNMENT=DownloadAlignment()
