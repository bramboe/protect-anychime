"""Bounded local delivery diagnostics; measurements never alter playback settings."""
import copy
import secrets
import threading
import time
from urllib.parse import parse_qs,urlsplit

class RingTimings:
    def __init__(self,capacity=128,clock=time.monotonic_ns,wall=time.time_ns):
        self.capacity=capacity;self.clock=clock;self.wall=wall;self.lock=threading.RLock();self.records={}
    def stamp(self):return {'monotonic_ms':self.clock()/1e6,'unix_ms':self.wall()/1e6}
    def begin(self,key,action,received=None):
        token=secrets.token_hex(16)
        with self.lock:
            while len(self.records)>=self.capacity:self.records.pop(next(iter(self.records)))
            self.records[token]={'id':token,'chime_key':key,'action':action,
                'state':'received','received':received or self.stamp(),'ha_calls':[],'downloads':{},'media_path':None}
        return token
    def state(self,token,value):
        with self.lock:
            if token in self.records:self.records[token]['state']=value
    def bind(self,token,path):
        with self.lock:
            if token in self.records:self.records[token]['media_path']=path
    def sent(self,token,targets):
        with self.lock:
            record=self.records.get(token)
            if not record or len(record['ha_calls'])>=160:return None
            record['ha_calls'].append({'sent':self.stamp(),'targets':list(targets),'response':None,'accepted':None})
            return len(record['ha_calls'])-1
    def response(self,token,index,accepted):
        with self.lock:
            record=self.records.get(token)
            if record and index is not None:
                record['ha_calls'][index].update(response=self.stamp(),accepted=bool(accepted))
    def correlate(self,token,url):
        parsed=urlsplit(url);query=parse_qs(parsed.query)
        values=query.get('ac_sync') or query.get('ac_ring') or []
        with self.lock:
            record=self.records.get(token)
            if record:
                paths=record.setdefault('media_paths',[])
                if parsed.path not in paths and len(paths)<16:paths.append(parsed.path)
                if len(values)!=1:return
                tokens=record.setdefault('download_tokens',[])
                if values[0] not in tokens and len(tokens)<16:tokens.append(values[0])
    def download(self,url,peer,range_header=None):
        parsed=urlsplit(url);query=parse_qs(parsed.query);values=query.get('ac_sync') or query.get('ac_ring') or []
        with self.lock:
            candidates=[r for r in self.records.values() if (r['media_path']==parsed.path or parsed.path in r.get('media_paths',[]))]
            if len(values)==1:
                candidates=[r for r in candidates if values[0] in r.get('download_tokens',[]) or r['id']==values[0]]
                correlation='request_token'
            else:
                now=self.clock()/1e6
                candidates=[r for r in candidates if r['ha_calls'] and 0<=now-r['ha_calls'][0]['sent']['monotonic_ms']<=60000]
                initial=range_header is None or str(range_header).startswith('bytes=0-')
                unseen=[r for r in candidates if peer not in r['downloads']]
                seen=[r for r in candidates if peer in r['downloads']]
                candidates=(unseen[:1] or seen[-1:]) if initial else (seen[-1:] or unseen[:1])
                correlation='path_heuristic'
            if len(candidates)!=1:return None
            record=candidates[0];downloads=record['downloads'];token=record['id']
            if peer not in downloads:
                if len(downloads)>=32:return None
                downloads[peer]={'first_get':self.stamp(),'response_end':None,'responses':0,'write_failed':False,'correlation':correlation}
            downloads[peer]['responses']+=1
            return token
    def end(self,token,peer,ok):
        with self.lock:
            record=self.records.get(token)
            if record and peer in record['downloads']:
                record['downloads'][peer].update(response_end=self.stamp(),write_failed=not ok)
    def get(self,token):
        with self.lock:
            record=copy.deepcopy(self.records.get(token))
            if record:
                record.pop('media_path',None);record.pop('media_paths',None);record.pop('download_tokens',None)
            return record
    def recent(self):
        with self.lock:return [self.get(k) for k in reversed(self.records)]
    def interference(self,since,exclude):
        with self.lock:return any(k!=exclude and r['action']!='calibrationClick' and
            r['received']['monotonic_ms']>=since for k,r in self.records.items())

TIMINGS=RingTimings()
