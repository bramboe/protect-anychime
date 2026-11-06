"""Native chime runtime. Explicit start; sound delivery belongs to opt-in callback.

Management routes follow inspected Protect 7.3.70 modules443675/655592.
UCP4 follows3234/188339/412614. This is not a native-adoption success claim.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import uuid

import aiohttp
from aiohttp import web
from bridge import Device
from discovery import announcement
from ucp import Decoder, encode
from compatibility import profile_version
from ringtones import MAX_RINGTONE_BYTES


class _PinnedConnector(aiohttp.TCPConnector):
    """Check exact peer DER before aiohttp can send any HTTP headers.

    The bounded native implementation uses aiohttp's connection hook because
    aiohttp.Fingerprint cannot also carry our required mutual-TLS client key.
    """
    def __init__(self, fingerprint, **kwargs):
        super().__init__(**kwargs)
        self.expected_fingerprint = fingerprint

    async def _wrap_create_connection(self, *args, **kwargs):
        transport, protocol = await super()._wrap_create_connection(*args, **kwargs)
        tls = transport.get_extra_info("ssl_object")
        actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest() if tls else ""
        if not hmac.compare_digest(actual, self.expected_fingerprint):
            transport.close()
            raise ssl.SSLError("Controller certificate fingerprint mismatch")
        return transport, protocol


class NativeChime:
    def __init__(self, options, data_dir, controller_cert_path=None, on_play=None, ringtone_store=None, on_remove=None):
        self.options = dict(options)
        self.compatibility_version = profile_version(options)
        self.last_firmware_rejection = None
        self.directory = Path(data_dir)
        self.controller_ip = self._lan_ip(options.get("controller_ip", ""))
        self.native_ip = self._lan_ip(options.get("native_ip", ""))
        self.controller_fingerprint = options.get("controller_fingerprint", "").lower().replace(":", "")
        self.controller_path = "/camera/1.0/ws"
        if len(self.controller_fingerprint) != 64:
            raise ValueError("Expected an exact SHA256 controller certificate fingerprint")
        bytes.fromhex(self.controller_fingerprint)
        self.on_play = on_play
        self.on_remove = on_remove
        self.removal_requested = False
        self.ringtone_store = ringtone_store
        self.state = None
        self.runner = None
        self.listen_port = None
        self.task = None
        self.session = None
        self.connected = False
        self.started_at = time.monotonic()
        self.last_error = None
        self.last_error_type = None
        self.last_http_status = None
        self.controller_protocol = None
        self.last_play = None
        self.connection_attempts = 0
        self.protocol = Device()

    @staticmethod
    def _lan_ip(value):
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError("Expected a LAN IPv4 address")
        return str(address)

    def initialize_identity(self):
        """Only called on explicit native start. Never silently repair identity."""
        state_file = self.directory / "identity.json"
        key = self.directory / "device-key.pem"
        cert = self.directory / "device-cert.pem"
        if state_file.exists():
            if not key.is_file() or not cert.is_file():
                raise ValueError("Incomplete native identity; refusing to replace it")
            state = json.loads(state_file.read_text())
            required = {"mac", "device_id", "username", "password", "adopted", "token", "console_id"}
            if not required <= state.keys():
                raise ValueError("Incomplete native identity data")
            # Validate its own locally administered identity through our encoder.
            announcement(state["mac"], self.native_ip)
            for path in (state_file, key, cert):
                if path.stat().st_mode & 0o077:
                    raise ValueError("Native identity files must be private")
            self.state = state
            return
        if self.directory.exists() and any(self.directory.iterdir()):
            raise ValueError("Native identity directory is nonempty without identity state")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        mac = bytearray(secrets.token_bytes(6))
        mac[0] = (mac[0] | 2) & 0xfe
        self.state = {"mac": mac.hex().upper(), "device_id": str(uuid.uuid4()),
                      "username": "ubnt", "password": "ubnt", "adopted": False,
                      "token": None, "console_id": None}
        old_mask = os.umask(0o077)
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "3650", "-subj",
                f"/CN=AnyChime-{self.state['device_id']}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            os.chmod(key, 0o600)
            os.chmod(cert, 0o600)
            self._save()
        finally:
            os.umask(old_mask)

    def _save(self):
        target = self.directory / "identity.json"
        temporary = self.directory / f"identity-{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(self.state, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)

    def _client_ssl(self):
        pem = self.options.get("controller_certificate")
        if not isinstance(pem,str) or not pem:
            raise ValueError("Confirm this installation's console certificate before connecting")
        der = ssl.PEM_cert_to_DER_cert(pem)
        actual = hashlib.sha256(der).hexdigest()
        if not hmac.compare_digest(actual, self.controller_fingerprint):
            raise ValueError("Pinned controller certificate does not match")
        # Only the explicitly imported controller certificate is trusted. No system
        # roots. PARTIAL_CHAIN permits exact-leaf trust even if not self-signed.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False  # Configured literal IP, certificate CN localhost.
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=pem)
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        context.load_cert_chain(self.directory / "device-cert.pem",
                                self.directory / "device-key.pem")
        return context

    def _server_ssl(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.directory / "device-cert.pem",
                                self.directory / "device-key.pem")
        return context

    def status(self):
        return {"enabled": self.runner is not None, "connected": self.connected,
                "adopted": bool(self.state and self.state["adopted"]),
                "mac": self.state["mac"] if self.state else None,
                "device_id": self.state["device_id"] if self.state else None,
                "management_port": 8080, "listener_port": self.listen_port, "attempts": self.connection_attempts,
                "last_error": self.last_error, "last_error_type": self.last_error_type,
                "last_http_status": self.last_http_status,
                "controller_protocol": self.controller_protocol, "last_play": self.last_play,
                "compatibility_profile": self.compatibility_version,
                "firmware_policy": "reject_vendor_firmware",
                "ringtones": self.ringtone_store.status() if self.ringtone_store is not None else None,
                "last_firmware_rejection": self.last_firmware_rejection,
                "audio_played": self.last_play.get("audio_played", False) if self.last_play else False}

    def discovery_response(self):
        if not self.state:
            raise ValueError("Native identity has not been explicitly initialized")
        return announcement(self.state["mac"], self.native_ip,
            firmware=self.compatibility_version, name=self.options.get("display_name", "Protect AnyChime"),
            adopted=self.state["adopted"], device_id=self.state["device_id"],
            default_credentials=0)

    def info(self):
        return {"type": "UP Chime PoE", "mac": self.state["mac"], "version": self.compatibility_version,
                "adopted": self.state["adopted"],
                "anonymousDeviceId": self.state["device_id"],
                "uptime": int(time.monotonic() - self.started_at),
                "featureFlags": {"hasWifi": False, "supportCustomRingtone": self.ringtone_store is not None,
                                 "hasHttpsClientOTA": True}}

    def _credentials_match(self, body):
        return (isinstance(body, dict)
            and isinstance(body.get("username"), str)
            and isinstance(body.get("password"), str)
            and hmac.compare_digest(body["username"], self.state["username"])
            and hmac.compare_digest(body["password"], self.state["password"]))

    async def _management_body(self, request):
        if request.remote != self.controller_ip:
            raise web.HTTPForbidden(text="Controller required")
        if getattr(request, "content_length", None) is not None and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        try:
            if hasattr(request, "content"):
                raw = bytearray()
                async for chunk in request.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
                body = json.loads(raw)
            else:
                body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid JSON")
        if not self._credentials_match(body):
            raise web.HTTPForbidden(text="Invalid credentials")
        return body

    def _ringtone_request(self, request):
        # Protect's actual ringtone uploader supplies no credential body/header.
        # Authenticate the peer IP directly, never forwarded headers.
        if request.remote != self.controller_ip:
            raise web.HTTPForbidden(text="Controller required")
        if self.ringtone_store is None:
            raise web.HTTPNotFound(text="Ringtone storage is disabled")
        try:
            value = request.match_info["track"]
            if not value.isascii() or not value.isdecimal():
                raise ValueError()
            track = int(value)
            if not 1 <= track <= 10:
                raise ValueError()
            return track
        except (KeyError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="Track must be between 1 and 10")

    async def handle_upload_ringtone(self, request):
        track = self._ringtone_request(request)
        if request.content_type != "application/octet-stream":
            raise web.HTTPUnsupportedMediaType(text="Expected application/octet-stream")
        if request.content_length is not None and request.content_length > MAX_RINGTONE_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_RINGTONE_BYTES,
                                               actual_size=request.content_length)
        raw = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            raw.extend(chunk)
            if len(raw) > MAX_RINGTONE_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_RINGTONE_BYTES, actual_size=len(raw))
        try:
            self.ringtone_store.upload(track, request.match_info["filename"], bytes(raw))
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        return web.json_response({"status": "ok"})

    async def handle_delete_ringtone(self, request):
        track = self._ringtone_request(request)
        try:
            self.ringtone_store.delete(track)
        except ValueError as error:
            raise web.HTTPBadRequest(text=str(error))
        return web.json_response({"status": "ok"})

    async def handle_info(self, request):
        await self._management_body(request)
        return web.json_response(self.info())

    async def handle_adopt(self, request):
        body = await self._management_body(request)
        token = body.get("token")
        console_id = body.get("consoleId")
        if (body.get("protocol") != "wss" or type(body.get("mode")) is not int
                or body["mode"] != 0 or not isinstance(token, str)
                or not 16 <= len(token) <= 512 or not isinstance(console_id, str)
                or not console_id or len(console_id) > 128):
            raise web.HTTPBadRequest(text="Invalid adoption request")
        if self.state["console_id"] not in (None, console_id):
            raise web.HTTPForbidden(text="Already assigned to a different console")
        if self.task and not self.task.done():
            raise web.HTTPConflict(text="Connection already active")
        self.removal_requested = False
        self.state.update(token=token, console_id=console_id)
        self._save()
        # Never trust the supplied hosts array as a connection destination.
        self.task = asyncio.create_task(self._connection_loop())
        return web.json_response({"status": "ok"})

    def reset_adoption(self):
        """Clear this virtual device's adoption; never reset its physical speaker."""
        self.state.update(adopted=False, token=None, console_id=None,
                          username="ubnt", password="ubnt")
        self._save()
        self.removal_requested = True

    def _headers(self):
        der = ssl.PEM_cert_to_DER_cert((self.directory / "device-cert.pem").read_text())
        headers = {"x-ident": self.state["mac"], "x-type": "UP Chime PoE",
                   "x-mode": "0", "x-fingerprint": hashlib.sha256(der).hexdigest(),
                   "x-ip": self.native_ip, "x-version": self.compatibility_version,
                   "x-adopted": "true" if self.state["adopted"] else "false",
                   "x-device-id": self.state["device_id"]}
        if self.state["token"]:
            headers["x-token"] = self.state["token"]
        return headers

    async def process_request(self, header, body):
        if header.get("type") != "request" or not isinstance(header.get("id"), str):
            raise ValueError("Expected correlated request")
        response = {"id": header["id"], "type": "response", "errorCode": 0, "error": ""}
        action = header.get("action")
        if action in ("updateFirmware", "updateSoftware"):
            self.last_firmware_rejection = {"action": action, "at": int(time.time()),
                                            "reason": "virtual_device"}
            response.update(errorCode=1,
                error="Virtual chime: vendor firmware is not installable; update the Home Assistant app.")
            # Never read, fetch or follow the supplied URI, and never acknowledge
            # an installation that did not occur.
            return response, {"status": "unsupported", "reason": "virtual_device"}
        if not isinstance(body, dict):
            response.update(errorCode=1, error="Object body required")
            return response, {"status": "invalid"}
        if action == "factoryReset":
            if self.on_remove is None:
                response.update(errorCode=1, error="Removal is not configured for this virtual chime.")
                return response, {"status": "unsupported"}
            if self.removal_requested:
                return response, {"status": "ok"}
            # The caller is the authenticated, pinned Protect connection. Stop
            # advertising this identity rather than resetting the physical HA player.
            result = self.on_remove()
            if hasattr(result, "__await__"):
                result = await result
            if result is not True:
                response.update(errorCode=1, error="The virtual chime could not be disabled.")
                return response, {"status": "failed"}
            self.reset_adoption()
            return response, {"status": "ok"}
        if action == "getInfo":
            return response, self.info()
        if action == "getAudioInfo" and self.ringtone_store is not None:
            return response, self.ringtone_store.audio_info()
        if action == "changeUserPassword":
            username, old, new = body.get("username"), body.get("passwordOld"), body.get("passwordNew")
            if (not isinstance(username, str) or not isinstance(old, str)
                    or not isinstance(new, str) or not 1 <= len(new) <= 1024
                    or not hmac.compare_digest(username, self.state["username"])
                    or not hmac.compare_digest(old, self.state["password"])):
                response.update(errorCode=1, error="Invalid credentials")
                return response, {"status": "invalid"}
            self.state["password"] = new
            self._save()
            return response, {"status": "ok"}
        if action == "echo":
            return response, body
        if action == "timeSync":
            # Acknowledge without changing the host clock.
            return response, {"status": "ok", "timestamp": int(time.time() * 1000)}
        if action in ("networkStatus", "getNetworkStatus"):
            return response, {"linkSpeedMbps": 1000}
        if action in ("playSpeaker", "playBuzzer"):
            if self.ringtone_store is not None:
                try:
                    track = self.ringtone_store.get(body.get("track_no", 1))
                except (ValueError, OSError):
                    track = None
                if track is None:
                    response.update(errorCode=1, error="Requested ringtone track is unavailable")
                    return response, {"status": "invalid"}
            before = len(self.protocol.plans)
            rh, rb = self.protocol.handle(header, body)
            if len(self.protocol.plans) > before:
                self.protocol.plans[:] = self.protocol.plans[-32:]
                self.last_play = {"track_no": body.get("track_no", 1),
                                  "source_action": action,
                                  "volume": body.get("volume", 80),
                                  "repeat": body.get("repeat", 1),
                                  "received_at": int(time.time()), "audio_played": False}
                if self.on_play:
                    # The callback receives the exact Protect-selected intent.
                    result = self.on_play(dict(self.last_play))
                    if hasattr(result, "__await__"):
                        result = await result
                    if isinstance(result, dict):
                        self.last_play.update(result)
                        if result.get("forwarding") in ("unassigned", "rejected", "busy"):
                            rh.update(errorCode=1, error=result.get("reason", "Playback request was not queued"))
                            rb={"status":"unavailable"}
                        if result.get("forwarding") == "queued":
                            # Acoustic playback cannot be inferred from queueing.
                            self.last_play["audio_played"] = None
            return rh, rb
        return self.protocol.handle(header, body)

    async def handle_ucp(self, header, body):
        return await self.process_request(header, body)

    async def _connection_loop(self):
        attempt = 0
        fatal_failures = 0
        while not self.removal_requested:
            attempt += 1
            self.connection_attempts = attempt
            try:
                context = self._client_ssl()
                connector_options = {"ssl": context}
                if self.options.get("bind_source_ip") is True:
                    connector_options["local_addr"] = (self.native_ip, 0)
                connector = _PinnedConnector(self.controller_fingerprint, **connector_options)
                timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10)
                trace = aiohttp.TraceConfig()
                async def reject_redirect(*_):
                    raise ValueError("Controller redirects are forbidden")
                trace.on_request_redirect.append(reject_redirect)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                        trust_env=False, trace_configs=[trace]) as session:
                    self.session = session
                    # Exact destination; redirects disabled, certificate verified
                    # during TLS before any token-bearing HTTP headers are sent.
                    async with session.ws_connect(
                            f"wss://{self.controller_ip}:7442{self.controller_path}", protocols=("ucp4",),
                            headers=self._headers(), autoclose=True, autoping=True,
                            max_msg_size=4 * 1024 * 1024, heartbeat=30,
                            ssl=context) as websocket:
                        self.controller_protocol = websocket.protocol
                        # Some device-ingest proxies consume the requested UCP4
                        # protocol header without echoing a selected protocol.
                        # No selection is legal WebSocket negotiation. Still reject
                        # an explicitly different selection; TLS pinning and token
                        # verification remain unchanged, as does UCP framing.
                        if websocket.protocol not in (None, "ucp4"):
                            raise ValueError("Unexpected controller protocol")
                        print(f"Chime handshake accepted; subprotocol={websocket.protocol or 'not returned'}.", flush=True)
                        self.connected = True
                        fatal_failures = 0
                        self.last_error = None
                        self.last_error_type = None
                        self.last_http_status = None
                        self.state.update(adopted=True, token=None)
                        self._save()
                        decoder = Decoder()
                        async for message in websocket:
                            if message.type == aiohttp.WSMsgType.BINARY:
                                for header, body in decoder.feed(message.data):
                                    rh, rb = await self.process_request(header, body)
                                    await websocket.send_bytes(encode(rh, rb))
                                    if self.removal_requested:
                                        return
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Do not reveal headers, adoption token, credentials or ingress URLs.
                self.last_error_type = type(error).__name__
                self.last_http_status = error.status if isinstance(error,aiohttp.WSServerHandshakeError) else None
                transient = isinstance(error, (ConnectionError, TimeoutError, aiohttp.ClientConnectionError))
                if isinstance(error, (ssl.SSLError, aiohttp.ClientSSLError,
                                      aiohttp.ClientConnectorCertificateError)):
                    transient = False
                if not transient:
                    fatal_failures += 1
                self.last_error = ("Controller temporarily unreachable; reconnecting."
                    if transient else f"Controller connection failed ({fatal_failures}/3)")
                print(f"Chime connection stopped: {self.last_error_type}; "
                      + ("retrying." if transient else f"check connection settings ({fatal_failures}/3)."), flush=True)
            finally:
                self.connected = False
                self.session = None
            if fatal_failures >= 3:
                return
            # Retry transport interruptions and normal controller restarts without
            # replaying commands. Settings/TLS/protocol failures remain bounded.
            await asyncio.sleep(min(60, 5 * 2 ** min(attempt - 1, 4)))

    def begin_connection(self):
        if self.task is None and (self.state['adopted'] or self.state['token']):
            self.task=asyncio.create_task(self._connection_loop())

    async def start(self, management_host=None, port=0, connect=True):
        if self.options.get("native_enabled") is not True:
            raise ValueError("Native chime runtime is disabled")
        if self.runner is not None:
            return
        self.initialize_identity()
        self._client_ssl()  # Fail closed before exposing the management service.
        application = web.Application(client_max_size=MAX_RINGTONE_BYTES)
        application.router.add_post("/api/info", self.handle_info)
        application.router.add_post("/api/adopt", self.handle_adopt)
        application.router.add_post("/api/uploadRingtone/{track}/{filename}", self.handle_upload_ringtone)
        application.router.add_delete("/api/uploadRingtone/{track}", self.handle_delete_ringtone)
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, management_host or self.native_ip, port, ssl_context=self._server_ssl()).start()
            self.listen_port=runner.addresses[0][1]
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        if connect:self.begin_connection()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        self.listen_port = None
        self.connected = False
