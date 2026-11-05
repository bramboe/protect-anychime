"""HA ingress UI; previews stay silent, native-to-speaker forwarding is opt-in."""
import json
import os
import re
import socket
import signal
import threading
import speakers
import artwork
import products
import chime_layout
from registry import ChimeRegistry
import configuration
import migration
import connection
import management
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import mimetypes
from bridge import replay
from ucp import encode
from runtime import NativeWorker
from audio import SpeakerBackend, SpeakerError
from onboarding import SpeakerOnboarding
from announcement_sync import ALIGNMENT
from ring_timing import TIMINGS
from calibration import Calibration,CalibrationError
from manual_timing import ManualTiming,DelayAudioCache

ROOT = Path(__file__).resolve().parent
NATIVE = NativeWorker()
VERSION = "0.3.49"
DEFAULT_PORT = 48200
SELECTION_LOCK = NATIVE.selection_lock
DATA_ROOT = Path(os.environ.get("ANYCHIME_DATA_DIR", "/data"))
IMAGES = artwork.ImageCache(DATA_ROOT / "product-images")
CALIBRATION=Calibration(DATA_ROOT)
MANUAL_TIMING=ManualTiming(DATA_ROOT)
DELAY_AUDIO=DelayAudioCache(DATA_ROOT)
CERTIFICATES = connection.CertificateManager()
MANAGEMENT = management.ProtectManagement(DATA_ROOT / "protect-management.json")


def layout_rows(rows):
    return NATIVE.registry.layouts.decorate(rows) if isinstance(NATIVE.registry,ChimeRegistry) else rows

def layout_pending():
    return NATIVE.registry.layouts.pending if isinstance(NATIVE.registry,ChimeRegistry) else None


def load_options():
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    options = json.loads(path.read_text()) if path.exists() else {}
    options = configuration.normalize(options)
    options = connection.runtime_options(options)
    MANAGEMENT.configure(options)
    options["speaker_entities"] = (NATIVE.registry.selected_entities()
        if NATIVE.registry is not None else speakers.selected(options))
    options['speaker_groups'] = ({e.get('playback_entity',e['entity_id']):e.get('members',[e['entity_id']])
        for e in NATIVE.registry.snapshot() if e['active']} if NATIVE.registry is not None else {})
    return options


ONBOARDING = SpeakerOnboarding(load_options)

def automatically_adopt(key):
    if not NATIVE.selection_lock.acquire(blocking=False):return {'retry_later':True}
    try:
        options=load_options()
        if options.get('auto_adopt_chimes') is not True:return {'retry_later':True}
        return MANAGEMENT.auto_adopt(options.get('controller_ip',''),key,NATIVE)
    finally:NATIVE.selection_lock.release()

NATIVE.auto_adopter=automatically_adopt

def repair_adoption(key):
    options=load_options()
    host=options.get('controller_ip','')
    if not MANAGEMENT.status(host).get('ready'):
        return {'retry_later':True,'audio_played':False}
    return MANAGEMENT.prepare_adoption(host,key,NATIVE)

NATIVE.adoption_checker=repair_adoption

def cleanup_old_chimes():
    if not NATIVE.selection_lock.acquire(blocking=False):
        return {'retry_later':True}
    try:
        options=load_options();host=options.get('controller_ip','')
        if not MANAGEMENT.status(host).get('ready'):
            return {'retry_later':True}
        entries=[e for e in NATIVE.registry.snapshot() if not e['active']
            and (e['entity_id'] is not None or (NATIVE.registry.identity_dir(e)/'identity.json').exists()) and e.get('removal_source')!='protect']
        keys=[e['key'] for e in entries if (NATIVE.registry.identity_dir(e)/'identity.json').is_file()]
        if keys:MANAGEMENT.remove_virtual_chimes(host,keys,NATIVE,allow_absent=True)
        # Entries without identity files contain no ownership material to retain.
        for entry in entries:
            if entry['key'] not in keys:
                NATIVE.remove_from_protect(entry['key']);NATIVE.complete_removal(entry['key'])
        return {'cleaned':len(entries),'audio_played':False}
    finally:NATIVE.selection_lock.release()

NATIVE.history_cleaner=cleanup_old_chimes

def sync_speaker_names(keys):
    if not NATIVE.selection_lock.acquire(blocking=False):return {'completed':[]}
    try:
        options=load_options();host=options.get('controller_ip','')
        if not MANAGEMENT.status(host).get('ready'):return {'completed':[]}
        return MANAGEMENT.sync_names(host,keys,NATIVE)
    finally:NATIVE.selection_lock.release()

NATIVE.name_synchronizer=sync_speaker_names

def preview(options, volume=25):
    if type(volume) not in (int, float) or not 0 <= volume <= 100:
        raise ValueError("Invalid volume.")
    request = encode({"type": "request", "id": "silent-ha-prototype",
                      "action": "playSpeaker"},
                     {"track_no": 1, "volume": volume, "repeat": 1})
    _, plans = replay(request)
    result = {"audio_played": False, "ha_calls_sent": 0,
              "tests_native_adoption": False, "protocol_replay_ok": True,
              "bridge_plan": plans[0], "speaker_plan": None}
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # No ingress paths, URLs, request data or tokens in logs.
        pass

    def allowed(self):
        return self.client_address[0] in ("172.30.32.2", "127.0.0.1", "::1")

    def respond(self, status, content, kind="application/json"):
        if isinstance(content, dict):
            content = json.dumps(content, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(content)

    def serve_audio(self, path):
        self._audio_write_ok=False
        peer=getattr(self,"client_address",("unknown",0))[0]
        trace=TIMINGS.download(getattr(self,"path",""),peer,getattr(self,"headers",{}).get("Range")) if getattr(self,"command","GET")=="GET" else None
        try:return self._serve_audio(path)
        finally:
            if trace:TIMINGS.end(trace,peer,getattr(self,"_audio_write_ok",False))

    def _serve_audio(self, path):
        try:
            data = path.read_bytes()
        except OSError:
            return self.respond(404, {"error": "Ringtone no longer available"})
        total = len(data)
        start, end, status = 0, total - 1, 200
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not any(match.groups()):
                return self.respond(416, {"error": "Invalid media range"})
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), total - 1) if match[2] else total - 1
            else:
                start = max(0, total - int(match[2]))
            if start > end or start >= total:
                return self.respond(416, {"error": "Invalid media range"})
            status = 206
        # Avoid delaying a small audio packet behind an unacknowledged header.
        # Some networks otherwise add different packet-buffering delays per speaker.
        client=getattr(self,'connection',None)
        if client is not None:
            try:client.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            except OSError:pass
        # Hold the response itself until the group is ready; sending headers
        # early lets each device enter its clip pipeline at a different time.
        if getattr(self, "command", "GET") != "HEAD":
            # Tiny byte-zero probes are not a speaker ready to consume audio.
            if start==0 and end-start+1>=min(256,total):
                parsed=urlsplit(getattr(self,'path',''))
                tokens=parse_qs(parsed.query).get('ac_sync',[])
                if len(tokens)==1 and re.fullmatch('[a-f0-9]{32}',tokens[0]):
                    ALIGNMENT.wait(tokens[0],parsed.path,self.client_address[0])
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav" if path.suffix==".wav" else "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end-start+1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(data[start:end+1])
            self._audio_write_ok=True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = urlsplit(self.path).path
        delayed=re.fullmatch(r'/media/delayed/([a-f0-9]{32})/([0-9]{1,3})\.wav',route)
        if delayed:
            try:asset=DELAY_AUDIO.media(delayed[1],int(delayed[2]))
            except ValueError:asset=None
            if asset is None:return self.respond(404,{'error':'Timing audio unavailable.'})
            return self.serve_audio(asset)
        if route == '/api/timing':
            if self.client_address[0]!='172.30.32.2':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            try:
                key=parse_qs(urlsplit(self.path).query).get('key',[''])[0]
                entry=next((e for e in NATIVE.registry.snapshot() if e['key']==key and e['active']),None) if NATIVE.registry else None
                if not entry:raise ValueError('Choose an added chime.')
                return self.respond(200,MANUAL_TIMING.describe(entry,speakers.discover()))
            except (ValueError,OSError,SpeakerError):return self.respond(400,{'error':'Could not load timing settings.'})
        calibration_media=re.fullmatch(r'/media/calibration/([a-f0-9]{32})\.mp3',route)
        if calibration_media:
            asset=CALIBRATION.media(calibration_media[1])
            if asset is None:return self.respond(404,{'error':'Calibration click expired.'})
            return self.serve_audio(asset)
        if route in ('/api/diagnostics/rings','/api/calibration/status','/api/calibration/clock'):
            if self.client_address[0]!='172.30.32.2':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            if route.endswith('/rings'):return self.respond(200,{'rings':TIMINGS.recent(),'compensation_applied':any(MANUAL_TIMING.read().values())})
            if route.endswith('/clock'):return self.respond(200,TIMINGS.stamp())
            return self.respond(200,CALIBRATION.status())
        match = re.fullmatch(r"/media/chimes/(primary|[a-f0-9]{24})/([1-9]|10)/([a-f0-9]{32})\.mp3", route)
        if match:
            asset = NATIVE.media(match[1], int(match[2]), match[3])
            if asset is None:
                return self.respond(404, {"error": "Ringtone not found"})
            return self.serve_audio(asset['path'])
        if not self.allowed():
            return self.respond(403, {"error": "Ingress required"})
        if route == "/":
            return self.respond(200, (ROOT / "index.html").read_bytes(),
                                "text/html; charset=utf-8")
        if route in ('/assets/calibration.js','/assets/onset-detector.js','/assets/onset-worklet.js'):
            return self.respond(200,(ROOT/'assets'/route.rsplit('/',1)[1]).read_bytes(),'application/javascript')
        if route == "/assets/sortable.min.js":
            return self.respond(200,(ROOT/'assets'/'sortable.min.js').read_bytes(),'application/javascript')
        if route.startswith("/assets/"):
            match=re.fullmatch(r'/assets/unifi-([0-9]{1,10})\.png',route)
            if match:
                try:return self.respond(200,IMAGES.get(match.group(1)),"image/png")
                except (OSError,ValueError):return self.respond(404,{"error":"Product image unavailable"})
            # Resolve below the bundled assets directory; no arbitrary file reads.
            assets = (ROOT / "assets").resolve()
            path = (ROOT / route.lstrip("/")).resolve()
            if path.parent != assets or path.suffix not in (".png", ".svg") or not path.is_file():
                return self.respond(404, {"error": "Not found"})
            return self.respond(200, path.read_bytes(), mimetypes.guess_type(path.name)[0])
        if route == "/api/management/chime-count":
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403,{"error":"Authenticated Home Assistant ingress required"})
            return self.respond(200,MANAGEMENT.chime_count(load_options().get('controller_ip','')))
        if route == "/api/products":
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403,{"error":"Authenticated Home Assistant ingress required"})
            try:
                _,inventory,_=speakers.discover(include_products=True)
                return self.respond(200,{"products":inventory})
            except (SpeakerError,ValueError,OSError):
                return self.respond(503,{"error":"Could not load product information from Home Assistant."})
        if route == "/api/speakers":
            try:
                inventory = layout_rows(speakers.discover(include_excluded=True))
                eligible,selected=speakers.selection_inventory(inventory,(layout_pending() or {}).get("entities",load_options()["speaker_entities"]),
                    NATIVE.registry.snapshot() if NATIVE.registry is not None else [])
                NATIVE.refresh_groups(eligible)
                return self.respond(200, {"speakers": eligible, "groups": speakers.visual_groups(eligible), "layout_change":layout_pending(),
                    "selected":selected})
            except (SpeakerError, ValueError, OSError):
                return self.respond(503, {"error": "Could not load speakers from Home Assistant."})
        if route == "/api/connection":
            return self.respond(200, connection.public_status(load_options()))
        if route == "/api/status":
            options = load_options()
            return self.respond(200, {"version": VERSION, "build": "groups3",
                "mode": "protect_controlled", "native": NATIVE.status(),
                "connection": connection.public_status(options),
                "management": MANAGEMENT.status(options.get('controller_ip','')),
                "migration": {"can_import": not (DATA_ROOT / "native").exists() and not (DATA_ROOT / "identities").exists()},
                "onboarding": ONBOARDING.status()})
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if urlsplit(self.path).path == '/api/timing':
            if self.client_address[0]!='172.30.32.2' or self.headers.get('X-Prototype-Test')!='silent':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192:raise ValueError('Invalid timing request.')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict) or body.get('confirmed') is not True:raise ValueError('Confirm saving timing settings.')
                with SELECTION_LOCK:
                    entry=next((e for e in NATIVE.registry.snapshot() if e['key']==body.get('key') and e['active']),None) if NATIVE.registry else None
                    if not entry or layout_pending():raise ValueError('Finish chime setup first.')
                    rows=speakers.discover();description=MANUAL_TIMING.describe(entry,rows)
                    values=body.get('delays')
                    if not description['supported'] or not isinstance(values,dict) or set(values)!={m['device_id'] for m in description['members']}:
                        raise ValueError('The speaker membership changed or this route cannot adjust individual timing.')
                    MANUAL_TIMING.save(values);NATIVE.refresh_groups(rows)
                    return self.respond(200,{'saved':True,'audio_played':False})
            except (ValueError,OSError,SpeakerError) as error:return self.respond(400,{'error':str(error)})

        if not self.allowed() or self.headers.get("X-Prototype-Test") != "silent":
            return self.respond(403, {"error": "Silent ingress test required"})
        route = urlsplit(self.path).path
        if route.startswith('/api/calibration/'):
            if self.client_address[0]!='172.30.32.2':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192:raise CalibrationError('Invalid calibration request.')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict) or body.get('confirmed') is not True:
                    raise CalibrationError('Start each calibration explicitly in the app.')
                if route.endswith('/start'):
                    if body.get('microphone_ready') is not True or body.get('secure_context') is not True:
                        raise CalibrationError('A secure browser session and microphone access are required. No sound was started.')
                    with SELECTION_LOCK:
                        if NATIVE.registry is None or layout_pending():raise CalibrationError('Finish chime setup before calibration.')
                        entry=next((e for e in NATIVE.registry.snapshot() if e['key']==body.get('key') and e['active']),None)
                        if not entry:raise CalibrationError('Choose an active local chime.')
                        options=load_options();base='http://'+options['native_ip']+':'+str(DEFAULT_PORT)
                        result=CALIBRATION.start(entry,speakers.discover(),base,body.get('volume'))
                elif route.endswith('/trial'):
                    with SELECTION_LOCK:
                        session=CALIBRATION._session(body.get('session'))
                        entry=next((e for e in NATIVE.registry.snapshot() if e['key']==session['key'] and e['active']),None)
                        if not entry or layout_pending() or set(entry.get('members',[entry['entity_id']]))!={m['entity_id'] for m in session['members']}:
                            raise CalibrationError('Chime membership changed. Cancel and start again.')
                        result=CALIBRATION.trial(body.get('session'),body.get('index'))
                elif route.endswith('/sample'):
                    result=CALIBRATION.sample(body.get('session'),body.get('index'),body.get('ha_latency_ms'),body.get('request_latency_ms'),body.get('clock_uncertainty_ms'))
                elif route.endswith('/cancel'):result=CALIBRATION.cancel(body.get('session'))
                else:return self.respond(404,{'error':'Unknown calibration action.'})
                return self.respond(200,result)
            except (ValueError,OSError,SpeakerError,TimeoutError) as error:
                return self.respond(400,{'error':str(error) if isinstance(error,ValueError) else 'Calibration could not complete. No automatic retry.'})
        if route == "/api/chimes/layout":
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403,{"error":"Authenticated Home Assistant ingress required"})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError('Invalid chime layout request.')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict) or payload.get('confirmed') is not True:
                    raise ValueError('Confirm the replacement of these chimes in Protect.')
                with SELECTION_LOCK:
                    rows=speakers.discover();options=load_options()
                    result=chime_layout.change(NATIVE,MANAGEMENT,options.get('controller_ip',''),
                        payload.get('members'),payload.get('separate'),rows,allow_custom=payload.get('custom') is True)
                return self.respond(200,result)
            except (ValueError,SpeakerError,OSError) as error:
                return self.respond(400,{'error':str(error) if isinstance(error,ValueError) else 'Could not finish the chime change. Resume when Protect is reachable.'})
        if route == "/api/products/link":
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403,{"error":"Authenticated Home Assistant ingress required"})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=4096:raise ValueError('Invalid product information request')
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):raise ValueError('Invalid product information request')
                _,inventory,rows=speakers.discover(include_products=True)
                products.save_link(payload.get('entity_id'),payload.get('product_device_id'),inventory,rows)
                return self.respond(200,{'status':'ok'})
            except (SpeakerError,ValueError,OSError) as error:
                return self.respond(400,{'error':str(error) if isinstance(error,ValueError) else 'Could not save product information.'})
        if route in ("/api/management/prepare", "/api/management/login", "/api/management/forget"):
            return self.respond(403, {"error": "Manage Protect credentials in this app's Home Assistant Configuration tab."})
        if route in ("/api/chimes/remove", "/api/chimes/inspect", "/api/chimes/prepare-adoption", "/api/chimes/test"):
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403, {"error": "Authenticated Home Assistant ingress required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid management request.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload,dict):
                    raise ValueError("Invalid management request.")
                options = load_options()
                if route.endswith('/inspect'):
                    return self.respond(200,MANAGEMENT.inspect_previous_chimes(options.get('controller_ip',''),NATIVE))
                key = payload.get('key')
                if (payload.get('confirmed') is not True or not isinstance(key,str)
                        or not re.fullmatch(r'primary|[a-f0-9]{24}',key)):
                    raise ValueError("Confirm the individual AnyChime action.")
                with SELECTION_LOCK:
                    if route.endswith('/test'):
                        result = MANAGEMENT.test_virtual_chime(options.get('controller_ip',''),key,NATIVE,volume=payload.get('volume',10))
                    elif route.endswith('/prepare-adoption'):
                        result = MANAGEMENT.prepare_adoption(options.get('controller_ip',''),key,NATIVE)
                    else:
                        result = MANAGEMENT.remove_virtual_chime(options.get('controller_ip',''),key,NATIVE)
                return self.respond(200,result)
            except (ValueError,OSError,TimeoutError) as error:
                message = str(error) if isinstance(error,ValueError) else 'Protect management could not be completed.'
                return self.respond(400,{"error":message})
        if route in ("/api/migration/export", "/api/migration/import"):
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403, {"error": "Authenticated Home Assistant ingress required"})
            try:
                options = load_options()
                if route.endswith("export"):
                    # A store reload can rewrite options.json while the running
                    # bridge still holds its verified startup connection. Preserve
                    # that consistent connection if the rendered file lost it.
                    live = NATIVE.options if isinstance(NATIVE.options, dict) else {}
                    if not options.get("controller_fingerprint") and live.get("controller_fingerprint"):
                        for key in migration.SETTINGS:
                            if live.get(key):
                                options[key] = live[key]
                    ONBOARDING.stop()
                    NATIVE.stop()
                    if NATIVE.thread and NATIVE.thread.is_alive():
                        raise ValueError("The bridge is still stopping. Wait a moment and export again.")
                    raw = migration.export_state(DATA_ROOT, options, options["speaker_entities"])
                    return self.respond(200, raw, "application/zip")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= migration.MAX_ARCHIVE:
                    raise ValueError("Select a migration backup smaller than 128 MB.")
                if (DATA_ROOT / "native").exists() or (DATA_ROOT / "identities").exists():
                    raise ValueError("Import into a fresh installation before configuring its connection.")
                ONBOARDING.stop()
                NATIVE.stop()
                if NATIVE.thread and NATIVE.thread.is_alive():
                    raise ValueError("The bridge is still stopping. Try the import again shortly.")
                result = migration.import_state(self.rfile.read(length), DATA_ROOT)
                return self.respond(200, result)
            except ValueError as error:
                return self.respond(400, {"error": str(error)})
            except OSError:
                return self.respond(500, {"error": "The migration data could not be read or saved."})
        if route in ("/api/connection/certificate", "/api/connection/trust"):
            if self.client_address[0] != "172.30.32.2":
                return self.respond(403, {"error": "Authenticated Home Assistant ingress required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid request.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request.")
                if route.endswith("certificate"):
                    options = load_options()
                    controller = payload.get("controller_ip", options.get("controller_ip", ""))
                    result = CERTIFICATES.inspect(controller, options)
                else:
                    result = CERTIFICATES.confirm(payload.get("confirmation_token"))
                return self.respond(200, result)
            except ValueError as error:
                return self.respond(400, {"error": str(error)})
            except (OSError, SpeakerError):
                return self.respond(503, {"error": "Could not read the console certificate. Check the address and connection."})
        if route == "/api/speakers":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid request.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Invalid request.")
                with SELECTION_LOCK:
                    inventory=layout_rows(speakers.discover(include_excluded=True))
                    options=load_options()
                    rows,_=speakers.selection_inventory(inventory,options['speaker_entities'],
                        NATIVE.registry.snapshot() if NATIVE.registry is not None else [])
                    previous = set(options["speaker_entities"])
                    entities = speakers.validate_selection(payload.get("entities"), rows,existing=previous)
                    extra=payload.get('remove_keys',[])
                    if (not isinstance(extra,list) or len(extra)>16
                            or any(not isinstance(key,str) or not re.fullmatch(r'primary|[a-f0-9]{24}',key) for key in extra)
                            or len(set(extra))!=len(extra)):
                        raise ValueError('Invalid previous-chime selection.')
                    removals=NATIVE.plan_selection(entities,rows,extra)
                    removed=[]
                    if removals:
                        removed=MANAGEMENT.remove_virtual_chimes(options.get('controller_ip',''),removals,NATIVE,
                            allow_absent=True)['removed_keys']
                    entries = NATIVE.configure_selection(entities, rows)
                    try:
                        speakers.save(entities)  # Legacy selection mirror; registry is authoritative.
                    except OSError:
                        print("Legacy selection mirror unavailable; chime registry saved.",flush=True)
                    added = [e.get('playback_entity',e['entity_id']) for e in entries if e['active']
                        and not previous.intersection(e.get('members',[e['entity_id']]))]
                    # Speaker setup stays silent. Only Protect-delivered tracks
                    # are played; no firmware-derived confirmation asset is shipped.
                    feedback = {'state':'disabled','audio_played':False}
                return self.respond(200, {"selected": entities, "onboarding": feedback,
                    "removed_chimes":removed,
                    "audio_played": None if feedback['state'] == 'queued' else False})
            except (ValueError, SpeakerError) as error:
                return self.respond(400, {"error": str(error)})
            except OSError:
                return self.respond(500, {"error": "Could not save the speaker selection."})
        if route != "/api/preview":
            return self.respond(404, {"error": "Not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid request.")
            result = preview(load_options(), payload.get("volume", 25))
            self.respond(200, result)
        except (ValueError, TypeError, KeyError):
            self.respond(400, {"error": "Check the volume (0–100) and app settings."})


def install_shutdown_handlers(server):
    """Let the native worker remove only its owned network state on HA stop."""
    previous={}
    if threading.current_thread() is not threading.main_thread():
        return previous
    requested=threading.Event()
    def shutdown(signum,frame):
        if requested.is_set():return
        requested.set()
        # BaseServer.shutdown must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown,daemon=True,name='anychime-shutdown').start()
    for signum in (signal.SIGTERM,signal.SIGINT):
        previous[signum]=signal.getsignal(signum)
        signal.signal(signum,shutdown)
    return previous


def restore_shutdown_handlers(previous):
    for signum,handler in previous.items():
        signal.signal(signum,handler)


def run_server():
    address = os.environ.get("BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    # Acquire the web socket before starting any native identity or service.
    server = ThreadingHTTPServer((address, port), Handler)
    previous_handlers=install_shutdown_handlers(server)
    try:
        options = load_options()
        try:
            raw_path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
            raw_options = json.loads(raw_path.read_text()) if raw_path.exists() else {}
            migrated = configuration.migrate(raw_options, effective=options)
            if migrated is not None:
                options["speaker_entities"] = speakers.selected(options)
        except (SpeakerError, ValueError, OSError):
            print("Configuration migration pending; existing settings remain in use.", flush=True)
        options = MANAGEMENT.migrate_to_configuration(options, configuration.self_options)
        MANAGEMENT.configure(options)
        NATIVE.start(options)
        print(f"Protect AnyChime {VERSION} (groups3): web interface on {port}; playback controlled by Protect.", flush=True)
        server.serve_forever()
    finally:
        try:
            ONBOARDING.stop()
            NATIVE.stop()
        finally:
            server.server_close()
            restore_shutdown_handlers(previous_handlers)


if __name__ == "__main__":
    run_server()
