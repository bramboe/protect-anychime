"""Translate one Protect chime's track/volume/repeat to exactly one HA player."""
import json
from announcement_sync import ALIGNMENT
import math
import subprocess
import threading
from audio import SpeakerBackend, SpeakerError
from manual_timing import ManualTiming


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
                 duration_probe=duration, group_members=None, member_platforms=None, data_dir=None, member_device_ids=None):
        self.key,self.entity_id,self.store,self.media_base = key,entity_id,store,media_base.rstrip('/')
        self.backend_factory,self.duration_probe = backend_factory,duration_probe
        self.group_members=tuple(group_members or [entity_id])
        self.member_platforms=dict(member_platforms or {})
        self.data_dir=data_dir
        self.member_device_ids=dict(member_device_ids or {})
        self.lock=threading.Lock()
        self.stop_event=threading.Event()
        self.busy=False
        self.thread=None
        self.last_result=None
        self.alignment_token=None

    def status(self):
        with self.lock:
            result=dict(self.last_result) if self.last_result else None
            timing=ALIGNMENT.summary(self.alignment_token)
            if result is not None and timing is not None:result['download_timing']=timing
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
            self.alignment_token=None
            self.last_result={'state':'queued','track_no':track,'volume':volume,'repeat':repeat}
        self.thread=threading.Thread(target=self._run,args=(asset,volume,repeat),daemon=True,
                                     name='chime-'+self.key)
        self.thread.start()
        return {'forwarding':'queued','effective_volume':volume}

    def _run(self, asset, volume, repeat):
        completed=0;accepted_requests=0;member_results={};had_failures=False
        state={'state':'failed','reason':'The speaker request failed. No automatic retry.'}
        try:
            with self.lock:
                entity=self.entity_id;members=tuple(self.group_members);platforms=dict(self.member_platforms);devices=dict(self.member_device_ids)
            seconds=self.duration_probe(asset['path']) if repeat>1 else 0
            path=f"/media/chimes/{self.key}/{asset['track_no']}/{asset['md5']}.mp3"
            url=f"{self.media_base}{path}"
            delays=ManualTiming(self.data_dir).read() if self.data_dir else {}
            offsets={e:delays.get(devices.get(e) or e,0) for e in members}
            backend=self.backend_factory({'speaker_entity':entity,'audio_url':url,
                'group_members':list(members),'member_platforms':platforms,'member_delays':offsets},allow_playback=True)
            for index in range(repeat):
                if self.stop_event.is_set():
                    break
                if index and self.stop_event.wait(seconds+.05):
                    break
                grant=backend.arm_once(confirmed=True,volume=volume,expires_in=60)
                result=backend.play_once(grant['approval_id'])
                with self.lock:self.alignment_token=getattr(backend,'last_alignment_token',None)
                if result.get('ha_call_accepted') is not True:
                    raise SpeakerError('Home Assistant did not accept the audio request.')
                completed+=1
                accepted_requests+=result.get('accepted_requests',1)
                member_results.update(result.get('member_results',{}))
                failed=result.get('failed_members',[])
                had_failures=had_failures or bool(failed)
                if failed:backend.exclude_members(failed)

            state={'state':'partial' if had_failures else ('ha_accepted' if completed==repeat else 'stopped'),
                   'track_no':asset['track_no'],'volume':volume,'repeat':repeat,
                   'ha_calls_accepted':completed,'accepted_requests':accepted_requests,
                   'member_results':member_results,'acoustic_playback_verified':False}
            if completed:
                state.update(mode=result.get('mode'),may_interrupt=result.get('may_interrupt'),
                    download_alignment=result.get('download_alignment',False))
        except (ValueError,SpeakerError) as error:
            member_results.update(getattr(error,'member_results',{}))
            state={'state':'partial' if completed else 'failed','reason':str(error),'ha_calls_accepted':completed,'accepted_requests':accepted_requests,'member_results':member_results}
        except Exception:
            state={'state':'partial' if completed else 'failed','reason':'The speaker request failed. No automatic retry.',
                   'ha_calls_accepted':completed,'accepted_requests':accepted_requests,'member_results':member_results}
        finally:
            with self.lock:
                self.busy=False
                self.last_result=state

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
