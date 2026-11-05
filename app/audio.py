"""Bounded speaker announcement adapter for Home Assistant's internal Core API proxy.

Importing this module does nothing. Playback is disabled by default and every
playback requires a short-lived, one-use approval issued by the caller after
explicit user consent. No token is persisted, returned or logged.

API reference: https://contributors.home-assistant.io/apps/guides/communication/
Uses the generic media_player services; integrations may accept a call silently.
"""
from concurrent.futures import ThreadPoolExecutor
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
from ring_timing import TIMINGS

CORE_API = "http://supervisor/core/api"
MAX_RESPONSE_BYTES = 256 * 1024
VOLUME_SET = 4
MEDIA_ANNOUNCE = 1048576


class SpeakerError(RuntimeError):
    """An operational error whose text never includes tokens or media URLs."""


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
        self._chime_only_group=options.get('chime_only_group') is True and all(self._platforms.get(e)=='sonos' for e in self._members)
        self._entity = entity
        self._media = media
        self._member_media=options.get('member_media') or {}
        if (not isinstance(self._member_media,dict) or (self._member_media and set(self._member_media)!=set(self._members)) or any(e not in self._members or not isinstance(u,str)
                or urlsplit(u).netloc!=parsed.netloc or urlsplit(u).scheme!=parsed.scheme
                or urlsplit(u).query or urlsplit(u).fragment
                or not re.fullmatch(r'/media/delayed/[a-f0-9]{32}/[0-9]{1,3}\.wav',urlsplit(u).path)
                for e,u in self._member_media.items())):
            raise ValueError('Invalid per-speaker timing media.')
        self._enabled = allow_playback is True
        self._request = requester or _supervisor_request
        self._clock = clock or time.monotonic
        self._approval = None
        self.timing_id=options.get("timing_id")
        self.last_alignment_token = None
        self._lock = threading.Lock()

    def _send_play(self,method,path,body):
        targets=body['entity_id'] if isinstance(body['entity_id'],list) else [body['entity_id']]
        TIMINGS.correlate(self.timing_id,body['media_content_id'])
        event=TIMINGS.sent(self.timing_id,targets)
        try:result=self._request(method,path,body)
        except Exception:
            TIMINGS.response(self.timing_id,event,False);raise
        TIMINGS.response(self.timing_id,event,True)
        return result

    def plan(self, volume=25):
        """Preview the generic service request, without inspecting or arming HA."""
        _volume(volume)
        return {"method": "POST", "path": "/services/media_player/play_media",
                "body": {"entity_id": self._entity,
                         "media_content_type": "music", "media_content_id": self._media},
                "volume": volume}

    def check_entity(self, entity=None):
        """Read selected, safe player metadata; never return arbitrary attributes."""
        entity = entity or self._entity
        if entity not in self._members:
            raise SpeakerError('This player is not part of the configured group.')
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
        if self._entity in self._member_media:
            plan['body']['media_content_id']=self._member_media[self._entity]
        player = self.check_entity()
        if player["state"] in ("off", "unavailable", "unknown", "standby"):
            raise SpeakerError("The selected speaker is " + player["state"] +
                               "; activate it in Home Assistant before playing audio.")
        if player["is_volume_muted"]:
            raise SpeakerError("The selected speaker is muted; unmute it in Home Assistant first.")
        volume = plan["volume"]
        features = player["supported_features"]
        grouped = len(self._members)>1
        if grouped and not self._chime_only_group and player['group_members'] is not None and set(player['group_members'])!=set(self._members):
            raise SpeakerError('The speaker group changed. Refresh speakers before playing audio.')
        players = [player] + [self.check_entity(e) for e in self._members if e!=self._entity]
        announce = features is not None and bool(features & MEDIA_ANNOUNCE)
        self.last_alignment_token=None
        if self._chime_only_group and not announce:
            raise SpeakerError('This chime-only group requires individual announcements. No playback was started.')
        if announce:
            targets=[self._entity]
            alignment_token=None
            # Sonos AudioClip is local to each speaker, not its music coordinator.
            # Other integrations keep their normal coordinator/group semantics.
            if grouped and all(self._platforms.get(e)=='sonos' for e in self._members):
                targets=[]
                for member in players:
                    if member['state'] in ('off','unavailable','unknown','standby') or member['is_volume_muted']:
                        continue
                    if member['supported_features'] is None or not member['supported_features'] & MEDIA_ANNOUNCE:
                        raise SpeakerError('A group member does not support announcements. No playback was started.')
                    targets.append(member['entity_id'])
                if self._member_media:
                    urls,alignment_token=ALIGNMENT.register_many({e:self._member_media[e] for e in targets})
                else:
                    plan['body']['media_content_id'],alignment_token=ALIGNMENT.register(
                        plan['body']['media_content_id'],len(targets))
            self.last_alignment_token=alignment_token
            plan['body'].update(entity_id=targets if len(targets)>1 else targets[0],
                announce=True,extra={'volume':volume})
            # Do not change the music volume. Integrations that support a separate
            # announcement volume (including Sonos) consume extra.volume.
            try:
                if grouped and self._member_media:
                    bodies=[{**plan['body'],'entity_id':e,'media_content_id':urls[e]} for e in targets]
                    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
                        futures=[pool.submit(self._send_play,plan['method'],plan['path'],body) for body in bodies]
                        for future in futures:future.result()
                else:
                    self._send_play(plan['method'],plan['path'],plan['body'])
            except Exception:
                ALIGNMENT.release(alignment_token)
                raise
            return {'ha_call_accepted':True,'entity_id':self._entity,'volume':volume,
                'mode':'group_announce' if grouped else 'announce','may_interrupt':False,
                'download_alignment':alignment_token is not None,
                'acoustic_playback_verified':False}
        volume_targets = []
        for member in players:
            if member['state'] in ('off','unavailable','unknown','standby') or member['is_volume_muted']:
                continue
            member_features=member['supported_features']
            if member_features is None or member_features & VOLUME_SET:
                volume_targets.append(member['entity_id'])
            elif member['volume_level'] is None or member['volume_level']>volume/100:
                raise SpeakerError('A selected speaker cannot set its volume and its current level is unknown or above the requested level.')
        if volume_targets:
            self._request('POST','/services/media_player/volume_set',
                {'entity_id':volume_targets if grouped else volume_targets[0], 'volume_level':volume/100})
        self._send_play(plan["method"], plan["path"], plan["body"])
        return {"ha_call_accepted": True, "entity_id": self._entity,
                "volume": volume, "mode": "group" if grouped else "announce" if announce else "play_media",
                "may_interrupt": not announce,
                "acoustic_playback_verified": False}
