"""Translate one Protect chime's track/volume/repeat to exactly one HA player."""
import json
from announcement_sync import ALIGNMENT
from ring_timing import TIMINGS
import math
import subprocess
import threading
from audio import SpeakerBackend, SpeakerError
from manual_timing import ManualTiming,DelayAudioCache


def duration(path):
    result = subprocess.run(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-f','mp3','-i',str(path),
        '-show_entries','format=duration','-of','json'], capture_output=True, text=True, timeout=4)
    if result.returncode or len(result.stdout)>4096:
        raise ValueError('The stored ringtone could not be decoded.')
    seconds = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(seconds) or not 0 < seconds <= 120:
        raise ValueError('The stored ringtone has an invalid duration.')
    return seconds


class TrackPlayer:
    def __init__(self, key, entity_id, store, media_base, *, backend_factory=SpeakerBackend,
                 duration_probe=duration, group_members=None, member_platforms=None, data_dir=None, member_device_ids=None, chime_only_group=False):
        self.key,self.entity_id,self.store,self.media_base = key,entity_id,store,media_base.rstrip('/')
        self.backend_factory,self.duration_probe = backend_factory,duration_probe
        self.group_members=tuple(group_members or [entity_id])
        self.member_platforms=dict(member_platforms or {})
        self.chime_only_group=chime_only_group
        self.data_dir=data_dir
        self.member_device_ids=dict(member_device_ids or {})
        self.lock=threading.Lock()
        self.stop_event=threading.Event()
        self.busy=False
        self.thread=None
        self.last_result=None
        self.alignment_token=None
        self.timing_id=None

    def status(self):
        with self.lock:
            result=dict(self.last_result) if self.last_result else None
            timing=ALIGNMENT.summary(self.alignment_token)
            if result is not None and timing is not None:result['download_timing']=timing
            if result is not None:result['ring_timing']=TIMINGS.get(self.timing_id)
            return {'entity_id':self.entity_id,'busy':self.busy,'last_result':result}

    def submit(self, intent):
        track,volume,repeat=(intent.get(k) for k in ('track_no','volume','repeat'))
        if (type(track) is not int or not 1<=track<=10 or type(repeat) is not int or not 1<=repeat<=10
                or type(volume) not in (int,float) or not math.isfinite(volume) or not 0<=volume<=100):
            return {'forwarding':'rejected','reason':'Invalid Protect track, volume or repeat count.'}
        if not self.entity_id:
            return {'forwarding':'unassigned','reason':'Select a speaker for this chime.'}
        if volume==0:
            return {'forwarding':'muted','audio_played':False}
        asset=self.store.get(track)
        if asset is None:
            return {'forwarding':'rejected','reason':'The requested ringtone is not stored on this chime.'}
        with self.lock:
            if self.busy or self.stop_event.is_set():
                return {'forwarding':'busy','reason':'This chime is already playing or stopping.'}
            self.busy=True
            self.timing_id=intent.get('timing_id') if TIMINGS.get(intent.get('timing_id')) else TIMINGS.begin(self.key,intent.get('source_action','playSpeaker'),intent.get('received_timing'))
            TIMINGS.state(self.timing_id,'queued')
            self.alignment_token=None
            self.last_result={'state':'queued','track_no':track,'volume':volume,'repeat':repeat}
        self.thread=threading.Thread(target=self._run,args=(asset,volume,repeat),daemon=True,
                                     name='chime-'+self.key)
        self.thread.start()
        return {'forwarding':'queued','effective_volume':volume}

    def _run(self, asset, volume, repeat):
        completed=0
        state={'state':'failed','reason':'The speaker request failed. No automatic retry.'}
        try:
            seconds=self.duration_probe(asset['path']) if repeat>1 else 0
            path=f"/media/chimes/{self.key}/{asset['track_no']}/{asset['md5']}.mp3"
            TIMINGS.bind(self.timing_id,path)
            url=f"{self.media_base}{path}"
            media={}
            delays=ManualTiming(self.data_dir).read() if self.data_dir else {}
            offsets={e:delays.get(self.member_device_ids.get(e,''),0) for e in self.group_members}
            if any(offsets.values()) and all(self.member_platforms.get(e)=='sonos' for e in self.group_members):
                cache=DelayAudioCache(self.data_dir)
                for entity,offset in offsets.items():
                    cache.render(asset,offset)
                    media[entity]=f"{self.media_base}/media/delayed/{asset['md5']}/{offset}.wav"
            backend=self.backend_factory({'speaker_entity':self.entity_id,'audio_url':url,
                'group_members':list(self.group_members),'member_platforms':dict(self.member_platforms),'timing_id':self.timing_id,'member_media':media,'chime_only_group':self.chime_only_group},allow_playback=True)
            for index in range(repeat):
                if self.stop_event.is_set():
                    break
                if index and self.stop_event.wait(seconds+(max(offsets.values(),default=0)/1000 if media else 0)+.05):
                    break
                grant=backend.arm_once(confirmed=True,volume=volume,expires_in=60)
                result=backend.play_once(grant['approval_id'])
                with self.lock:self.alignment_token=getattr(backend,'last_alignment_token',None)
                if result.get('ha_call_accepted') is not True:
                    raise SpeakerError('Home Assistant did not accept the audio request.')
                completed+=1
            state={'state':'ha_accepted' if completed==repeat else 'stopped',
                   'track_no':asset['track_no'],'volume':volume,'repeat':repeat,
                   'ha_calls_accepted':completed,'acoustic_playback_verified':False}
            if completed:
                state.update(mode=result.get('mode'),may_interrupt=result.get('may_interrupt'),
                    download_alignment=result.get('download_alignment',False))
        except (ValueError,SpeakerError) as error:
            state={'state':'failed','reason':str(error),'ha_calls_accepted':completed}
        except Exception:
            state={'state':'failed','reason':'The speaker request failed. No automatic retry.',
                   'ha_calls_accepted':completed}
        finally:
            with self.lock:
                self.busy=False
                self.last_result=state
                TIMINGS.state(self.timing_id,state.get("state","finished"))

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
