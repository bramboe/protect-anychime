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
from manual_timing import ManualTiming

ROOT = Path(__file__).resolve().parent
NATIVE = NativeWorker()
VERSION = "0.4.0"
DEFAULT_PORT = 48200
SELECTION_LOCK = NATIVE.selection_lock
DATA_ROOT = Path(os.environ.get("ANYCHIME_DATA_DIR", "/data"))
IMAGES = artwork.ImageCache(DATA_ROOT / "product-images")
MANUAL_TIMING=ManualTiming(DATA_ROOT)
CERTIFICATES = connection.CertificateManager()
MANAGEMENT = management.ProtectManagement(DATA_ROOT / "protect-management.json")


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

def sync_speaker_names(keys):
    if not NATIVE.selection_lock.acquire(blocking=False):return {'completed':[]}
    try:
        options=load_options();host=options.get('controller_ip','')
        if not MANAGEMENT.status(host).get('ready'):return {'completed':[]}
        return MANAGEMENT.sync_names(host,keys,NATIVE)
    finally:NATIVE.selection_lock.release()

NATIVE.name_synchronizer=sync_speaker_names

def remove_confirmed_chime(host,key):
    entry=next((e for e in NATIVE.registry.snapshot() if e['key']==key),None) if NATIVE.registry else None
    if entry and entry.get('removal_pending'):
        return MANAGEMENT.remove_virtual_chime(host,key,NATIVE,allow_absent=True)
    return MANAGEMENT.remove_virtual_chime(host,key,NATIVE)


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

    def serve_audio(self,path):
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
                if len(tokens)==1 and re.fullmatch(r'[a-f0-9]{32}\.[a-f0-9]{16}',tokens[0]):
                    ALIGNMENT.wait(tokens[0],parsed.path,self.client_address[0])
        self.send_response(status)
        self.send_header("Content-Type", "audio/mpeg")
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
        if route == '/api/timing':
            if self.client_address[0]!='172.30.32.2':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            try:
                key=parse_qs(urlsplit(self.path).query).get('key',[''])[0]
                entry=next((e for e in NATIVE.registry.snapshot() if e['key']==key and e['active']),None) if NATIVE.registry else None
                if not entry:raise ValueError('Choose an added chime.')
                return self.respond(200,MANUAL_TIMING.describe(entry,speakers.discover()))
            except (ValueError,OSError,SpeakerError):return self.respond(400,{'error':'Could not load timing settings.'})
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
        if route in ('/assets/timing.js','/assets/speakers.js'):
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
                inventory = speakers.discover(include_excluded=True)
                eligible,selected=speakers.selection_inventory(inventory,load_options()["speaker_entities"],
                    NATIVE.registry.snapshot() if NATIVE.registry is not None else [])
                NATIVE.refresh_groups(eligible)
                return self.respond(200,{"speakers":eligible,"selected":selected,
                    "chimes":NATIVE.registry.snapshot() if NATIVE.registry else [],
                    "membership_migration":NATIVE.registry.migration.status(NATIVE.registry.snapshot()) if NATIVE.registry else {}})
            except (SpeakerError, ValueError, OSError):
                return self.respond(503, {"error": "Could not load speakers from Home Assistant."})
        if route == "/api/connection":
            return self.respond(200, connection.public_status(load_options()))
        if route == "/api/status":
            options = load_options()
            return self.respond(200, {"version": VERSION, "build": "sets1",
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
                    if not entry or entry.get("migration_hold") or entry.get("removal_pending"):raise ValueError('Finish chime setup first.')
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
                        result = remove_confirmed_chime(options.get('controller_ip',''),key)
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
        if route in ('/api/chimes/members','/api/chimes/migration/finish'):
            if self.client_address[0]!='172.30.32.2':return self.respond(403,{'error':'Authenticated Home Assistant ingress required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192:raise ValueError('Invalid membership request.')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict) or body.get('confirmed') is not True:raise ValueError('Confirm this chime action.')
                with SELECTION_LOCK:
                    registry=NATIVE.registry
                    if registry is None:raise ValueError('The app is starting.')
                    if route.endswith('/finish'):
                        registry.migration.finish(registry.snapshot())
                    else:
                        action=body.get('action');entity=body.get('entity_id')
                        rows=speakers.discover()
                        if action=='remove':
                            entry=next((e for e in registry.snapshot() if e['key']==body.get('key') and e['active']),None)
                            if not entry or entity not in entry['members']:raise ValueError('This speaker is no longer in the chime.')
                            if len(entry['members'])==1:
                                if body.get('confirm_last') is not True:raise ValueError('Confirm Protect removal of this chime’s last speaker.')
                                options=load_options()
                                remove_confirmed_chime(options.get('controller_ip',''),entry['key'])
                            else:registry.detach(entry['key'],entity,rows)
                        elif action in ('add','move'):
                            if registry.migration.status(registry.snapshot())['required']:raise ValueError('Finish reviewing the older chime change first.')
                            registry.transfer(entity,body.get('target_key'),rows)
                        else:raise ValueError('Choose add, move or remove.')
                    NATIVE.refresh_players()
                    try:speakers.save(registry.selected_entities(),path=registry.directory/'speakers.json')
                    except OSError:pass
                return self.respond(200,{'selected':registry.selected_entities(),'audio_played':False})
            except (ValueError,OSError,SpeakerError) as error:return self.respond(400,{'error':str(error) if isinstance(error,ValueError) else 'Could not save the chime change.'})
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
        print(f"Protect AnyChime {VERSION} (sets1): web interface on {port}; playback controlled by Protect.", flush=True)
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
