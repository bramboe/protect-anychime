"""Automatic LAN address selection and explicit confirmation of console certificates."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import threading
import time

MANAGED_PATH = Path(os.environ.get('MANAGED_SETTINGS_PATH', '/data/managed-settings.json'))
LOCK = threading.RLock()


def lan_ip(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise ValueError('Enter a private LAN IPv4 address for your UniFi console.')
    return str(address)


def read_managed():
    if not MANAGED_PATH.exists():
        return {}
    value = json.loads(MANAGED_PATH.read_text())
    if not isinstance(value, dict):
        raise ValueError('Saved connection settings are invalid.')
    return value


def save_managed(value):
    with LOCK:
        MANAGED_PATH.parent.mkdir(parents=True, exist_ok=True)
        target = MANAGED_PATH.with_name('managed-' + secrets.token_hex(8) + '.tmp')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(target, MANAGED_PATH)
        finally:
            target.unlink(missing_ok=True)


def default_gateway(path='/proc/net/route'):
    try:
        for line in Path(path).read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == '00000000' and int(fields[3], 16) & 2:
                return lan_ip(socket.inet_ntoa(struct.pack('<I', int(fields[2], 16))))
    except (OSError, ValueError):
        pass
    return ''


def local_address(controller, fallback=''):
    if controller:
        try:
            # UDP connect selects a local route. No datagram is sent.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((lan_ip(controller), 7442))
                return lan_ip(route.getsockname()[0])
        except (OSError, ValueError):
            pass
    return lan_ip(fallback) if fallback else ''


def runtime_options(options):
    managed = read_managed()
    result = {**options, **managed}
    result['native_enabled'] = True
    result['controller_ip'] = result.get('controller_ip') or default_gateway()
    result['native_ip'] = local_address(result['controller_ip'], result.get('native_ip', ''))
    port = int(os.environ.get('PORT', '48200'))
    result['media_port'] = port
    result.pop('success_url',None)
    return result


def public_status(options):
    fingerprint = options.get('controller_fingerprint', '')
    return {'controller_ip': options.get('controller_ip', ''),
            'native_ip': options.get('native_ip', ''),
            'fingerprint': fingerprint,
            'trusted': bool(fingerprint),
            'audio_source': 'protect',
            'custom_protect_ringtones': True}


def fetch_certificate(controller, port=7442):
    """Read only the public peer certificate. Send no app token or client identity."""
    host = lan_ip(controller)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Observation only; does not establish trust.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((host, port), timeout=4) as transport:
        with context.wrap_socket(transport, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der or len(der) > 64 * 1024:
        raise ValueError('The console did not return a valid certificate.')
    return ssl.DER_cert_to_PEM_cert(der), hashlib.sha256(der).hexdigest()


def ensure_saved_certificate(options, *, fetcher=None):
    """Recover legacy pin-only trust without importing any developer certificate."""
    result=dict(options)
    pin=result.get('controller_fingerprint','').lower().replace(':','')
    if not pin:
        return result
    if len(pin)!=64:
        raise ValueError('The saved console certificate fingerprint is invalid.')
    bytes.fromhex(pin)
    pem=result.get('controller_certificate')
    recovered=not pem
    if recovered:
        pem,reported=(fetcher or fetch_certificate)(lan_ip(result.get('controller_ip','')))
        if not hmac.compare_digest(reported,pin):
            raise ValueError('The console certificate changed. Confirm the console again.')
    actual=hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if not hmac.compare_digest(actual,pin):
        raise ValueError('The console certificate changed. Confirm the console again.')
    result.update(controller_certificate=pem,controller_fingerprint=pin)
    if recovered:
        with LOCK:
            managed=read_managed()
            if (managed.get('controller_ip') not in (None,'',result['controller_ip']) or
                    managed.get('controller_fingerprint','').lower().replace(':','') not in ('',pin)):
                raise ValueError('Console settings changed while recovering the certificate.')
            managed.update(controller_ip=result['controller_ip'],controller_certificate=pem,controller_fingerprint=pin)
            save_managed(managed)
    return result


class CertificateManager:
    def __init__(self, fetcher=None, clock=None):
        self.fetcher = fetcher or fetch_certificate
        self.clock = clock or time.monotonic
        self.pending = None
        self.lock = threading.Lock()

    def inspect(self, controller, options):
        controller = lan_ip(controller)
        pem, fingerprint = self.fetcher(controller)
        actual = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        if not hmac.compare_digest(actual, fingerprint):
            raise ValueError('Certificate data does not match its fingerprint.')
        same = (controller == options.get('controller_ip') and
                hmac.compare_digest(fingerprint, options.get('controller_fingerprint', '')))
        with self.lock:
            self.pending = None
            token = None
            if not same:
                token = secrets.token_urlsafe(32)
                self.pending = (token, self.clock() + 120, controller, pem, fingerprint)
        return {'controller_ip': controller, 'fingerprint': fingerprint,
                'matches_trusted': same, 'confirmation_token': token,
                'restart_required': False, 'audio_played': False}

    def confirm(self, token):
        with self.lock:
            candidate = self.pending
            if (not candidate or not isinstance(token, str) or
                    not hmac.compare_digest(candidate[0], token)):
                raise ValueError('Refresh the console certificate before confirming it.')
            self.pending = None
        _, expires, controller, pem, fingerprint = candidate
        if self.clock() >= expires:
            raise ValueError('Certificate confirmation expired. Refresh it again.')
        with LOCK:
            managed = read_managed()
            managed.update(controller_ip=controller, controller_certificate=pem,
                           controller_fingerprint=fingerprint)
            save_managed(managed)
        return {'controller_ip': controller, 'fingerprint': fingerprint,
                'restart_required': True, 'audio_played': False}
