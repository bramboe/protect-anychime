"""Bounded speaker announcement adapter for Home Assistant's internal Core API proxy.

Importing this module does nothing. Playback is disabled by default and every
playback requires a short-lived, one-use approval issued by the caller after
explicit user consent. No token is persisted, returned or logged.

API reference: https://contributors.home-assistant.io/apps/guides/communication/
Uses the generic media_player services; integrations may accept a call silently.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
from announcement_sync import ALIGNMENT

CORE_API = "http://supervisor/core/api"
MAX_RESPONSE_BYTES = 256 * 1024
VOLUME_SET = 4
MEDIA_ANNOUNCE = 1048576


class SpeakerError(RuntimeError):
    """An operational error whose text never includes tokens or media URLs."""

    def __init__(self, message, *, member_results=None):
        super().__init__(message)
        self.member_results=member_results or {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _supervisor_request(method, path, body=None):
    """Fixed-origin, no-proxy, no-redirect, no-retry Core API request."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise SpeakerError("The app cannot access the Home Assistant API.")
    data = None if body is None else json.dumps(body, allow_nan=False).encode()
    request = Request(CORE_API + path, data=data, method=method,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SpeakerError("The Home Assistant response is too large.")
            return json.loads(raw) if raw else None
    except HTTPError as error:
        raise SpeakerError(f"Home Assistant returned HTTP {error.code}.") from None
    except (URLError, OSError, ValueError):
        raise SpeakerError("Home Assistant is unreachable or returned an invalid response.") from None


def scaled_volume(volume, level):
    """Protect's volume scaled by a speaker's level (percent).

    Never silences a speaker on its own: only Protect volume 0 is silent.
    """
    if volume <= 0:
        return 0
    return max(1, min(100, int(volume * level / 100 + 0.5)))


def _volume(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Speaker volume must be between 0 and 100.")
    return value


class SpeakerBackend:
    """One configured player and one configured media URL; no arbitrary services.

    `requester` is an injectable (method, relative_path, body) function for tests.
    `allow_playback` must only be enabled by an explicitly authorized outer flow.
    The caller must not expose arm_once as an automatically invoked operation.
    """

    def __init__(self, options, *, allow_playback=False, requester=None, clock=None):
        entity = options.get("speaker_entity", "")
        media = options.get("audio_url", "")
        if not isinstance(entity, str) or not re.fullmatch(r"media_player\.[a-z0-9_]+", entity):
            raise ValueError("Select a valid speaker entity first.")
        if not isinstance(media, str):
            raise ValueError("Set a valid sound URL first.")
        parsed = urlsplit(media)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or any(ord(char) < 32 for char in media)):
            raise ValueError("Use a complete HTTP(S) sound URL without embedded credentials.")
        members = options.get('group_members') or [entity]
        if (not isinstance(members,(list,tuple)) or len(members)>16
                or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in members)
                or entity not in members):
            raise ValueError('Select a valid speaker group.')
        self._members = tuple(dict.fromkeys(members))
        platforms=options.get('member_platforms')
        self._platforms={e:platforms.get(e,'') for e in self._members} if isinstance(platforms,dict) else {}
        self._entity=entity
        self._media=media
        self._delays=options.get('member_delays') or {}
        if not isinstance(self._delays,dict) or any(e not in self._members or type(v) is not int or not 0<=v<=500 for e,v in self._delays.items()):
            raise ValueError('Invalid speaker delays.')
        self._levels=options.get('member_levels') or {}
        if not isinstance(self._levels,dict) or any(e not in self._members or type(v) is not int or not 10<=v<=100 for e,v in self._levels.items()):
            raise ValueError('Invalid speaker volume levels.')
        self._enabled = allow_playback is True
        self._request = requester or _supervisor_request
        self._clock = clock or time.monotonic
        self._approval = None
        self.last_alignment_token = None
        self._lock = threading.Lock()

    def plan(self, volume=25):
        """Preview the generic service request, without inspecting or arming HA."""
        _volume(volume)
        return {"method": "POST", "path": "/services/media_player/play_media",
                "body": {"entity_id": self._entity,
                         "media_content_type": "music", "media_content_id": self._media},
                "volume": volume}

    def check_entity(self,entity=None):
        entity=entity or self._entity
        if entity not in self._members:raise SpeakerError('This player is not part of the configured chime.')
        return self._inspect(entity)

    def _inspect(self,entity):
        if not isinstance(entity,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',entity):
            raise SpeakerError('Invalid HA group coordinator.')
        result = self._request("GET", "/states/" + entity, None)
        if not isinstance(result, dict) or result.get("entity_id") != entity:
            raise SpeakerError("Home Assistant did not return the selected speaker.")
        state = result.get("state")
        if not isinstance(state, str):
            raise SpeakerError("Home Assistant did not return a valid speaker state.")
        attributes = result.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        features = attributes.get("supported_features")
        features = features if type(features) is int and features >= 0 else None
        level = attributes.get("volume_level")
        level = (level if type(level) in (int, float) and math.isfinite(level)
                 and 0 <= level <= 1 else None)
        group = attributes.get('group_members')
        if group is not None and (not isinstance(group,list) or len(group)>16
                or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in group)):
            raise SpeakerError('The speaker group changed. Refresh speakers before playing audio.')
        return {"entity_id": entity, "state": state, "group_members": group,
                "available": state not in ("unavailable", "unknown"),
                "supported_features": features, "volume_level": level,
                "is_volume_muted": attributes.get("is_volume_muted") is True,
                "audio_played": False}

    def arm_once(self, *, confirmed=False, volume=25, expires_in=60):
        """Create one in-memory grant after the outer flow obtains sound consent.

        The grant is tied to this configured player, URL and volume. Arming again
        revokes the old grant; restarting the app discards all grants.
        """
        if not self._enabled or confirmed is not True:
            raise SpeakerError("Playback is disabled or has not been explicitly approved.")
        if (type(expires_in) not in (int, float) or not math.isfinite(expires_in)
                or not 0 < expires_in <= 120):
            raise ValueError("Approval must expire within 120 seconds.")
        plan = self.plan(volume)
        grant = secrets.token_urlsafe(32)
        with self._lock:
            self._approval = (grant, self._clock() + expires_in, plan)
        return {"approval_id": grant, "expires_in": expires_in,
                "entity_id": self._entity, "volume": volume}

    def cancel(self):
        with self._lock:
            self._approval = None

    def play_once(self, approval_id):
        """Consume approval before ANY HA request; never retries an attempt.

        A failed or timed-out request consumes the grant too. Success only proves
        that HA accepted the service call, never that acoustic playback occurred.
        """
        with self._lock:
            approval = self._approval
            if (not self._enabled or not isinstance(approval_id, str) or not approval
                    or not hmac.compare_digest(approval[0].encode(), approval_id.encode())):
                raise SpeakerError("There is no matching one-use playback approval.")
            self._approval = None
        _, expires, plan = approval
        if self._clock() >= expires:
            raise SpeakerError("Playback approval has expired.")
        volume=plan['volume']
        if volume==0:return {'ha_call_accepted':True,'mode':'muted','volume':0,'audio_played':False,'effective_requests':0}
        # A rejected member must not cancel independent outputs or their repeats.
        failures={};skipped=[];players=[]
        def failed(members, stage):
            for member in members:failures[member]={'status':'failed','stage':stage}
        with ThreadPoolExecutor(max_workers=len(self._members)) as pool:
            pending={pool.submit(self.check_entity,e):e for e in self._members}
            for future in as_completed(pending):
                try:players.append(future.result())
                except Exception:failed([pending[future]],'inspect')
        by_id={p['entity_id']:p for p in players}
        # Preserve configured order, including group coordinator selection.
        active=[by_id[e] for e in self._members if e in by_id and self._playable(by_id[e])]
        skipped=[p['entity_id'] for p in players if not self._playable(p)]
        targets=[];seen={}
        for player in active:
            origin=player['entity_id'];entity=origin
            announce=bool((player['supported_features'] or 0)&MEDIA_ANNOUNCE)
            force_normal=self._platforms.get(entity)=='sonos' and not announce
            group=player['group_members'] or []
            independent=self._platforms.get(entity)=='sonos' and announce
            if self._platforms.get(entity)=='sonos' and any(
                    q['entity_id'] in group and bool((q['supported_features'] or 0)&MEDIA_ANNOUNCE)!=announce
                    for q in active):
                failed([origin],'mixed_capabilities');continue
            contributors=[origin]
            try:
                if not independent and len(group)>1 and entity in group:
                    signature=frozenset(group);leader=group[0]
                    reports=[p['group_members'] for p in players if p['entity_id'] in signature and p['group_members']]
                    if all(frozenset(g)==signature and g[0]==leader for g in reports):
                        if signature in seen:continue
                        contributors=[p['entity_id'] for p in active if p['entity_id'] in signature]
                        # Mark even failed plans, so a sibling cannot retry the coordinator.
                        seen[signature]=True
                        player=by_id.get(leader) or self._inspect(leader)
                        if not self._playable(player):
                            skipped.extend(contributors);continue
                        entity=leader;announce=not force_normal and bool((player['supported_features'] or 0)&MEDIA_ANNOUNCE)
                existing=next((t for t in targets if t['entity']==entity),None)
                if existing:
                    existing['members'].extend(e for e in contributors if e not in existing['members']);continue
                volume_targets=[]
                volume_players=[q for q in active if q['entity_id'] in group] if len(group)>1 else [player]
                if player not in volume_players:volume_players.append(player)
                # A shared playback target uses its coordinator's level, else the first selected member's.
                level=self._levels.get(entity,self._levels.get(origin,100))
                if not announce:
                    for volume_player in volume_players:
                        # Normal playback sets each speaker's own volume, so each keeps its level.
                        own=scaled_volume(volume,self._levels.get(volume_player['entity_id'],level))
                        features=volume_player['supported_features']
                        if features is None or features&VOLUME_SET:volume_targets.append((volume_player['entity_id'],own))
                        elif volume_player['volume_level'] is None or volume_player['volume_level']>own/100:
                            raise SpeakerError('A selected speaker cannot set its volume safely. No audio was started.')
                targets.append({'entity':entity,'members':contributors,'announce':announce,'volume_targets':volume_targets,
                    'volume':scaled_volume(volume,level),
                    'offset':self._delays.get(entity,self._delays.get(origin,0))})
            except Exception:failed(contributors,'preflight')
        if not targets:
            if any(v['stage']=='mixed_capabilities' for v in failures.values()):
                raise SpeakerError('This Sonos music group mixes announcement capabilities. No audio was started.',member_results=failures)
            if any(v['stage']=='preflight' for v in failures.values()):
                raise SpeakerError('A selected speaker cannot set its volume safely or its coordinator is unavailable. No audio was started.',member_results=failures)
            raise SpeakerError('No online, active, unmuted speakers are available.',member_results=failures)
        urls,token=ALIGNMENT.register(self._media,{t['entity']:t['offset'] for t in targets})
        self.last_alignment_token=token
        def send(target):
            entity=target['entity']
            levels={}
            for volume_entity,own in target['volume_targets']:levels.setdefault(own,[]).append(volume_entity)
            for own,entities in levels.items():
                self._request('POST','/services/media_player/volume_set',{'entity_id':entities if len(entities)>1 else entities[0],'volume_level':own/100})
            body={'entity_id':entity,'media_content_type':'music','media_content_id':urls[entity]}
            if target['announce']:body.update(announce=True,extra={'volume':target['volume']})
            return self._request('POST','/services/media_player/play_media',body)
        accepted=[]
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            pending={pool.submit(send,target):target for target in targets}
            for future in as_completed(pending):
                target=pending[future]
                try:future.result();accepted.append(target)
                except Exception:
                    failed(target['members'],'request')
                    ALIGNMENT.release(token)
        if not accepted:raise SpeakerError('Home Assistant did not accept any speaker request. No automatic retry.',member_results=failures)
        return {'ha_call_accepted':True,'entity_id':self._entity,'volume':volume,
            'state':'partial' if failures else 'ha_accepted',
            'member_results':{**{e:{'status':'accepted'} for t in accepted for e in t['members']},**failures},
            'failed_members':list(failures),'skipped_members':list(dict.fromkeys(skipped)),
            'accepted_requests':len(accepted),'failed_requests':len(targets)-len(accepted),
            'mode':'announce' if all(t['announce'] for t in accepted) else 'play_media',
            'may_interrupt':any(not t['announce'] for t in accepted),
            'effective_requests':len(targets),'download_alignment':token is not None,
            'acoustic_playback_verified':False}

    def exclude_members(self, members):
        """Narrow subsequent repetitions only; a new ring creates a fresh backend."""
        self._members=tuple(e for e in self._members if e not in set(members))

    @staticmethod
    def _playable(player):
        return player['state'] not in ('off','standby','unavailable','unknown') and not player['is_volume_muted']
