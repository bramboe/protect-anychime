"""Migrate only this app's options; never enable playback or reset device identity."""
import json
import os
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from audio import _NoRedirect, SpeakerError
import connection

MANAGED = {'audio_url', 'native_ip', 'controller_ip', 'controller_fingerprint',
           'unifi_compatibility_profile', 'native_enabled', 'playback_enabled', 'volume_limit'}

LEGACY = {}

def normalize(options):
    result = dict(options)
    for old, new in LEGACY.items():
        if old in result:
            if new not in result:
                result[new] = result[old]
            result.pop(old)
    return result


def self_options(method, body=None):
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        raise SpeakerError('Home Assistant app configuration is unavailable.')
    path = '/addons/self/info' if method == 'GET' else '/addons/self/options'
    request = Request('http://supervisor' + path, method=method,
        data=None if body is None else json.dumps(body, allow_nan=False).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('result') != 'ok':
            raise ValueError()
        return value.get('data', {})
    except (OSError, ValueError):
        raise SpeakerError('App configuration could not be migrated; existing settings remain in use.') from None


def migrate(options, *, requester=None, data_dir='/data', effective=None):
    if not any(key in options for key in set(LEGACY) | MANAGED):
        return None
    request = requester or self_options
    # Ask only for this app's saved values; do not overwrite unrelated options.
    info = request('GET')
    raw = info.get('options')
    if not isinstance(raw, dict):
        raise SpeakerError('App configuration could not be read for migration.')
    if not any(key in raw for key in set(LEGACY) | MANAGED):
        return None
    clean = normalize(raw)
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / 'configuration-before-managed-settings.json'
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as stream:
            json.dump(raw, stream)
    import speakers
    # Preserve the legacy target privately before deleting the old config key.
    if not speakers.SELECTION_PATH.exists() and clean.get('speaker_entity'):
        speakers.save(speakers.selected(clean))
    clean.pop('speaker_entity', None)
    with connection.LOCK:
        managed = connection.read_managed()
        for key in MANAGED - {'native_enabled'}:
            if key in clean:
                value = (effective or {}).get(key, clean[key])
                if key == 'audio_url' and not clean[key]:
                    value = ''
                managed.setdefault(key, value)
        connection.save_managed(managed)
    for key in MANAGED:
        clean.pop(key, None)
    request('POST', {'options': clean})
    return clean
