"""Local-user Protect management. Only app-owned chimes can be removed.

Protocol references: HA unifiprotect and uiprotect's private-session client.
No network activity on construction or local account status.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
import time

import aiohttp
from yarl import URL
import connection
from connection import fetch_certificate, lan_ip
from native import _PinnedConnector


class ManagementError(ValueError):
    def __init__(self, message, *, kind='management_error', http_status=None):
        super().__init__(message)
        self.kind=kind if kind in {'management_error','timeout','certificate','authentication','rate_limit','http_error','network','invalid_response'} else 'management_error'
        self.http_status=http_status if type(http_status) is int and 100<=http_status<=599 else None


def _mac(value):
    if not isinstance(value, str):
        raise ManagementError('The chime has an invalid device address.')
    value = value.replace(':', '').upper()
    if not re.fullmatch('[0-9A-F]{12}', value):
        raise ManagementError('The chime has an invalid device address.')
    return value


class SessionCache:
    """A signed-in Protect session reused between management actions.

    Consoles limit sign-ins, so actions share one session for up to TTL seconds
    instead of signing in each time. Only session cookies and the CSRF token are
    kept, in memory, for the exact console, certificate pin and account; nothing
    is written to disk. A refused or expired session signs in again once.
    """
    TTL = 20 * 60

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.entry = None

    @staticmethod
    def key(account):
        parts = [account.get(k, '') for k in ('host', 'fingerprint', 'username', 'password')]
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def get(self, account):
        with self.lock:
            entry = self.entry
            if entry and entry['key'] == self.key(account) and self.clock() < entry['expires']:
                return dict(entry['cookies']), entry['csrf']
            return None

    def put(self, account, cookies, csrf, *, fresh):
        key = self.key(account)
        with self.lock:
            current = self.entry if self.entry and self.entry['key'] == key else None
            if not fresh and current is None:
                return
            # Reuse never extends a session: sign in again at least every TTL.
            expires = self.clock() + self.TTL if fresh else current['expires']
            self.entry = {'key': key, 'cookies': dict(cookies), 'csrf': csrf, 'expires': expires}

    def clear(self):
        with self.lock:
            self.entry = None


class ProtectSession:
    def __init__(self, account, cache=None):
        self.account = account
        self.session = None
        self.csrf = None
        self.cache = cache
        self.reused = False

    async def __aenter__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Exact pin checked before sending credentials.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connector = _PinnedConnector(self.account['fingerprint'], ssl=context)
        self.session = aiohttp.ClientSession(connector=connector, trust_env=False,
            cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=15))
        try:
            cached = self.cache.get(self.account) if self.cache else None
            if cached:
                cookies, self.csrf = cached
                self.session.cookie_jar.update_cookies(cookies, URL('https://' + self.account['host'] + '/'))
                self.reused = True
            else:
                await self._login()
        except BaseException:
            await self.session.close()
            raise
        return self

    async def _login(self):
        self.csrf = None
        self.reused = False
        await self._request_once('POST', '/api/auth/login', {
            'username': self.account['username'], 'password': self.account['password'],
            'rememberMe': False}, login=True)
        if not self.csrf:
            raise ManagementError('Protect did not provide a complete local session. Reconnect with a local UniFi account.')
        if self.cache:
            self.cache.put(self.account, self._cookies(), self.csrf, fresh=True)

    def _cookies(self):
        try:
            return {cookie.key: cookie.value for cookie in self.session.cookie_jar}
        except TypeError:
            return {}

    async def __aexit__(self, *_):
        try:
            if self.cache and self.csrf:
                # Keep cookies or a CSRF token the console refreshed during this action.
                self.cache.put(self.account, self._cookies(), self.csrf, fresh=False)
        finally:
            await self.session.close()

    async def request(self, method, path, body=None, login=False):
        try:
            return await self._request_once(method, path, body, login)
        except ManagementError as error:
            if login or not self.reused or error.kind != 'authentication':
                raise
            # The remembered session expired or was revoked. A refused request was
            # not carried out, so sign in once and repeat it.
            if self.cache:
                self.cache.clear()
            await self._login()
            return await self._request_once(method, path, body, login)

    async def _request_once(self, method, path, body=None, login=False):
        # No general-purpose URL or method proxy is exposed to callers.
        rename=(method=='PATCH' and re.fullmatch('/proxy/protect/api/chimes/[0-9a-f]{24}',path)
            and isinstance(body,dict) and set(body)=={'name'} and isinstance(body['name'],str)
            and 1<=len(body['name'])<=100 and not any(ord(c)<32 for c in body['name']))
        play=(method=='POST' and re.fullmatch('/proxy/protect/api/chimes/[0-9a-f]{24}/play-speaker',path)
            and isinstance(body,dict) and set(body)<= {'volume','repeatTimes','ringtoneId'}
            and type(body.get('volume')) is int and 0<=body['volume']<=100
            and type(body.get('repeatTimes')) is int and 1<=body['repeatTimes']<=10
            and ('ringtoneId' not in body or (isinstance(body['ringtoneId'],str) and re.fullmatch('[0-9a-f]{24}',body['ringtoneId']))))
        adopt=(method=='POST' and path=='/proxy/protect/api/devices/adopt' and isinstance(body,dict)
            and set(body)=={'chimes'} and isinstance(body['chimes'],dict) and len(body['chimes'])==1
            and all(isinstance(k,str) and re.fullmatch('[0-9a-f]{24}',k) and v=={} for k,v in body['chimes'].items()))
        valid = (adopt or play or rename or (login and method == 'POST' and path == '/api/auth/login')
            or (method == 'GET' and path in ('/proxy/protect/api/chimes', '/proxy/protect/api/nvr'))
            or (method == 'DELETE' and re.fullmatch('/proxy/protect/api/chimes/[0-9a-f]{24}', path)))
        if not valid:
            raise ManagementError('Unsupported Protect management operation.')
        headers = {'X-CSRF-Token': self.csrf} if self.csrf else {}
        async with self.session.request(method, 'https://' + self.account['host'] + path,
                json=body if login or rename or play or adopt else None, headers=headers, allow_redirects=False) as response:
            if response.status in (401, 403):
                raise ManagementError('Protect refused access. Use a local user with Protect Full Management permissions.',kind='authentication',http_status=response.status)
            if response.status not in (200, 204):
                raise ManagementError('Protect did not accept the management request. Check the account and console.',kind='rate_limit' if response.status==429 else 'http_error',http_status=response.status)
            refreshed = response.headers.get('x-csrf-token') or response.headers.get('x-updated-csrf-token')
            if refreshed:
                self.csrf = refreshed
            if login:
                # Drain only a bounded response, never expose a returned user/token.
                if len(await response.content.read(2 * 1024 * 1024 + 1)) > 2 * 1024 * 1024:
                    raise ManagementError('Protect returned an oversized response.')
                return None
            raw = await response.content.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ManagementError('Protect returned an oversized response.')
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise ManagementError('Protect returned an invalid response.',kind='invalid_response') from None

    async def chimes(self):
        data = await self.request('GET', '/proxy/protect/api/chimes')
        if not isinstance(data, list) or len(data) > 512 or any(not isinstance(x, dict) for x in data):
            raise ManagementError('Protect did not return a valid chime list.')
        return data

    async def console_info(self):
        """Display name, model and Protect version, so the user recognises the console."""
        try:
            data = await self.request('GET', '/proxy/protect/api/nvr')
        except ManagementError:
            return {}
        if not isinstance(data, dict):
            return {}
        def clean(value):
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                return ''
            return value.strip()[:80]
        return {'console_name': clean(data.get('name')),
                'console_model': clean(data.get('marketName') or data.get('type')),
                'console_version': clean(data.get('version'))}

    async def remove(self, device_id):
        if not isinstance(device_id, str) or not re.fullmatch('[0-9a-f]{24}', device_id):
            raise ManagementError('Protect returned an invalid chime identifier.')
        result = await self.request('DELETE', '/proxy/protect/api/chimes/' + device_id)
        if result is not None and (not isinstance(result, dict) or result.get('success') is not True):
            raise ManagementError('Protect has not confirmed the removal. The chime remains paused.')

    async def adopt(self,device_id):
        if not isinstance(device_id,str) or not re.fullmatch('[0-9a-f]{24}',device_id):
            raise ManagementError('Protect returned an invalid chime identifier.')
        result=await self.request('POST','/proxy/protect/api/devices/adopt',{'chimes':{device_id:{}}})
        if not isinstance(result,dict) or result.get('chimes',{}).get(device_id,{}).get('adopted') is not True:
            raise ManagementError('Protect did not confirm adoption. Configure the chime in Protect manually.')

    async def play(self, device_id, settings):
        if not isinstance(device_id,str) or not re.fullmatch('[0-9a-f]{24}',device_id):
            raise ManagementError('Protect returned an invalid chime identifier.')
        await self.request('POST','/proxy/protect/api/chimes/'+device_id+'/play-speaker',settings)

    async def rename(self, device_id, name):
        if not isinstance(device_id,str) or not re.fullmatch('[0-9a-f]{24}',device_id):
            raise ManagementError('Protect returned an invalid chime identifier.')
        if not isinstance(name,str) or not 1<=len(name)<=100 or any(ord(c)<32 for c in name):
            raise ManagementError('Invalid speaker name.')
        result=await self.request('PATCH','/proxy/protect/api/chimes/'+device_id,{'name':name})
        if not isinstance(result,dict) or result.get('name')!=name:
            raise ManagementError('Protect did not confirm the speaker name.')



class ProtectManagement:
    def __init__(self, path='/data/protect-management.json', *, fetcher=None, client_factory=ProtectSession, clock=None):
        self.path = Path(path)
        self.fetcher = fetcher or (lambda host: fetch_certificate(host, 443))
        self.clock = clock or time.monotonic
        self.sessions = SessionCache(self.clock)
        # Management actions share one signed-in session (see SessionCache).
        self.client_factory = ((lambda account: ProtectSession(account, cache=self.sessions))
                               if client_factory is ProtectSession else client_factory)
        self.lock = threading.RLock()
        self.pending = None
        self.revision = 0
        self.configuration_mode = False
        self.configured_account = None
        self.legacy_fallback = False
        self._chime_count_cache = None

    def _read(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ManagementError('Protect account storage must be private.')
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k]
                for k in ('host', 'username', 'password', 'fingerprint')):
            raise ManagementError('Reconnect your Protect account.')
        lan_ip(data['host'])
        if not re.fullmatch('[0-9a-f]{64}', data['fingerprint']):
            raise ManagementError('Reconnect your Protect account.')
        return data

    def migrate_legacy_account(self, options):
        """Move an account saved by older versions into this app's private settings."""
        if not self.path.exists():
            return options
        try:
            old = self._read()
        except (ValueError,OSError):
            return options
        if not old or old['host'] != options.get('controller_ip'):
            return options
        if options.get('protect_username') and options.get('protect_password'):
            # A newer account already exists; keep only the old certificate pin.
            self._save_connection_trust(old)
            return options
        try:
            with connection.LOCK:
                managed = connection.read_managed()
                managed.update(protect_username=old['username'], protect_password=old['password'])
                connection.save_managed(managed)
        except (ValueError, OSError):
            # Keep the working legacy login; never put account details in this log.
            print('Protect account transfer pending; existing login preserved.',flush=True)
            return {**options,'protect_username':old['username'],'protect_password':old['password']}
        self._save_connection_trust(old)
        return {**options,'protect_username':old['username'],'protect_password':old['password']}

    def verify_account(self, host, username, password, *, refresh_certificate=False):
        """Sign in once over a pinned connection and return console details.

        Read-only: lists chimes but never adopts, removes, renames or plays.
        On success the console certificate used for management is remembered.
        """
        host = lan_ip(host)
        if not all(isinstance(v, str) and 0 < len(v) <= limit
                for v, limit in ((username, 128), (password, 4096))):
            raise ManagementError('Enter your local UniFi username and password.')
        fingerprint = None if refresh_certificate else self._saved_fingerprint(host)
        if fingerprint is None:
            pem, fingerprint = self.fetcher(host)
            actual = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
            if not hmac.compare_digest(actual, fingerprint):
                raise ManagementError('The console certificate could not be checked.')
        account = {'host': host, 'username': username, 'password': password, 'fingerprint': fingerprint}
        async def check():
            async with self.client_factory(account) as client:
                await client.chimes()
                return await client.console_info() if hasattr(client, 'console_info') else {}
        info = self._run(check())
        with self.lock:
            self.revision += 1
            self._chime_count_cache = None
        self._save_connection_trust(account)
        return info if isinstance(info, dict) else {}

    def configure(self, options):
        """Read canonical HA options without logging in or duplicating passwords."""
        username = options.get('protect_username', '')
        password = options.get('protect_password', '')
        host = options.get('controller_ip', '')
        legacy_fallback = False
        if not username and not password and self.path.exists():
            try:
                old = self._read()
            except (ValueError,OSError):
                old = None
            if old and old['host']==host:
                username,password=old['username'],old['password']
                legacy_fallback = True
        with self.lock:
            self.configuration_mode = True
            self.legacy_fallback = legacy_fallback
            self.configured_account = None
            if (isinstance(username,str) and 0 < len(username) <= 128
                    and isinstance(password,str) and 0 < len(password) <= 4096):
                self.configured_account = {'host':host,'username':username,'password':password}

    def _configured_session_account(self, host):
        with self.lock:
            account = dict(self.configured_account or {})
        if not account or account['host'] != host:
            raise ManagementError('Add a local Protect account at the top of this app’s page.')
        fingerprint = self._saved_fingerprint(host)
        if fingerprint is None:
            # Same first-use local-console trust model as HA's local integration;
            # pin every credential-bearing connection to this observed certificate.
            pem, fingerprint = self.fetcher(lan_ip(host))
            actual = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
            if not hmac.compare_digest(actual, fingerprint):
                raise ManagementError('The console certificate could not be checked.')
        if not isinstance(fingerprint,str) or not re.fullmatch('[0-9a-f]{64}',fingerprint):
            raise ManagementError('The saved Protect connection is invalid.')
        return {**account,'fingerprint':fingerprint}

    def _saved_fingerprint(self, host):
        """The remembered management certificate pin for this console, if any."""
        trust = self.path.with_name('protect-management-trust.json')
        if trust.exists():
            if trust.is_symlink() or trust.stat().st_mode & 0o077:
                raise ManagementError('Protect connection storage must be private.')
            saved = json.loads(trust.read_text())
            if not isinstance(saved,dict):
                raise ManagementError('The saved Protect connection is invalid.')
            return saved.get('fingerprint') if saved.get('host') == host else None
        if self.path.exists():
            # Reuse a previously confirmed pin, but never its old saved password.
            saved = self._read()
            if saved['host'] == host:
                return saved['fingerprint']
        return None

    def _save_connection_trust(self, account, *, clear_legacy=True):
        trust = self.path.with_name('protect-management-trust.json')
        trust.parent.mkdir(parents=True,exist_ok=True)
        temporary = trust.with_name('management-trust-'+secrets.token_hex(8)+'.tmp')
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as output:
                json.dump({'host':account['host'],'fingerprint':account['fingerprint']},output)
                output.flush();os.fsync(output.fileno())
            os.replace(temporary,trust)
            # The app's private settings are now the only persistent credential source.
            if clear_legacy:
                self.path.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self, host):
        if self.configuration_mode:
            with self.lock:
                account = self.configured_account
                return {'configured':bool(account),'ready':bool(account and account['host']==host),
                        'configuration_source':'app'}
        try:
            with self.lock:
                account = self._read()
            return {'configured': bool(account), 'ready': bool(account and account['host'] == host)}
        except (ValueError, OSError):
            return {'configured': False, 'ready': False}

    def chime_count(self,host):
        """Read-only count, including native Ubiquiti chimes; no identifiers leave here."""
        if not self.status(host).get('ready'):return {'count':None}
        with self.lock:
            signature=(host,self.revision)
            cached=self._chime_count_cache
            if cached and cached['signature']==signature and self.clock()-cached['at']<60:
                return {'count':cached['count']}
            # Cache failures too, so UI polling cannot repeatedly attempt login.
            self._chime_count_cache={'signature':signature,'at':self.clock(),'count':None}
            account=self._configured_session_account(host) if self.configuration_mode else self._read()
        async def read():
            async with self.client_factory(account) as client:
                return sum(remote.get('isAdopted') is True for remote in await client.chimes())
        try:count=self._run(asyncio.wait_for(read(),8))
        except (ManagementError,ValueError,OSError):return {'count':None}
        with self.lock:
            if self._chime_count_cache['signature']==signature:
                self._chime_count_cache['count']=count
        return {'count':count}

    def prepare(self, host, device_fingerprint=''):
        host = lan_ip(host)
        pem, fingerprint = self.fetcher(host)
        actual = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        if not hmac.compare_digest(actual, fingerprint):
            raise ManagementError('The console certificate could not be checked.')
        with self.lock:
            old = self._read()
            known = old['fingerprint'] if old and old['host'] == host else device_fingerprint
            token = secrets.token_urlsafe(32)
            self.revision += 1
            self.pending = (token, self.clock() + 120, host, fingerprint, self.revision)
        return {'confirmation_token': token, 'controller_ip': host,
                'confirm_console': not hmac.compare_digest(fingerprint, known), 'audio_played': False}

    def login(self, token, host, username, password):
        if not all(isinstance(v, str) and 0 < len(v) <= limit
                for v, limit in ((username, 128), (password, 4096))):
            raise ManagementError('Enter your local UniFi username and password.')
        with self.lock:
            pending = self.pending
            self.pending = None
        if (not pending or not isinstance(token, str) or not hmac.compare_digest(token, pending[0])
                or self.clock() >= pending[1] or host != pending[2]):
            raise ManagementError('The connection check expired. Connect again.')
        account = dict(host=host, fingerprint=pending[3], username=username, password=password)
        async def check():
            async with self.client_factory(account) as client:
                await client.chimes()  # Read-only account check; never tests delete or audio.
        self._run(check())
        with self.lock:
            if self.revision != pending[4]:
                raise ManagementError('The account setup changed while connecting. Connect again.')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name('management-' + secrets.token_hex(8) + '.tmp')
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(account, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        return {'configured': True, 'ready': True, 'audio_played': False}

    def forget(self):
        self.sessions.clear()
        with self.lock:
            self.revision += 1
            self.pending = None
            self.path.unlink(missing_ok=True)
        return {'configured': False, 'ready': False, 'audio_played': False}

    @staticmethod
    def _run(operation):
        async def bounded():
            return await asyncio.wait_for(operation, 30)
        try:
            return asyncio.run(bounded())
        except ManagementError:
            raise
        except (ssl.SSLError, aiohttp.ClientSSLError):
            raise ManagementError('Protect certificate verification failed.',kind='certificate') from None
        except TimeoutError:
            raise ManagementError('The Protect request timed out.',kind='timeout') from None
        except (OSError, aiohttp.ClientError):
            raise ManagementError('Could not reach Protect.',kind='network') from None
        except ValueError:
            raise ManagementError('Protect returned invalid data.',kind='invalid_response') from None

    def auto_adopt(self,host,key,worker):
        target=worker.removal_identity(key);node=worker.nodes.get(key)
        if not node or not worker._entry_enabled(key):return {'retry_later':True}
        account=self._configured_session_account(host) if self.configuration_mode else self._read()
        if not account or account['host']!=host:raise ManagementError('Configure a local Protect account to enable automatic adoption.')
        async def adopt():
            async with self.client_factory(account) as client:
                matches=[]
                for remote in await client.chimes():
                    try:
                        if _mac(remote.get('mac',''))==target['mac']:matches.append(remote)
                    except ManagementError:continue
                if not matches:return {'retry_later':True}
                if len(matches)!=1:raise ManagementError('The new chime could not be identified uniquely.')
                remote=matches[0]
                if (remote.get('type')!='UP Chime PoE' or remote.get('host')!=node['ip']
                        or remote.get('anonymousDeviceId') not in (None,target['device_id'])
                        or remote.get('isAdoptedByOther') is True):
                    raise ManagementError('Automatic adoption refused a device outside this installation.')
                if remote.get('isAdopted') is True or remote.get('isAdopting') is True:return {'already_adopting':True}
                if remote.get('isAdopted') is not False:return {'retry_later':True}
                await client.adopt(remote.get('id'))
                return {'adoption_requested':True,'setup_required_in_protect':True,'audio_played':False}
        return self._run(adopt())

    def check_owned_chimes(self,host,keys,worker):
        """Read-only preflight before a chime layout is paused or persisted."""
        targets=[worker.removal_identity(key) for key in keys]
        if self.configuration_mode:account=self._configured_session_account(host)
        else:
            with self.lock:account=self._read()
        if not account or account['host']!=host:
            raise ManagementError('Configure a local Protect account before changing chimes.')
        async def check():
            async with self.client_factory(account) as client:
                devices=await client.chimes()
                for target in targets:
                    matches=[]
                    for remote in devices:
                        try:
                            if _mac(remote.get('mac',''))==target['mac']:matches.append(remote)
                        except ManagementError:continue
                    if not matches:continue # Owned local identity already absent remotely.
                    if len(matches)!=1 or matches[0].get('type')!='UP Chime PoE' or matches[0].get('anonymousDeviceId') not in (None,target['device_id']):
                        raise ManagementError('A previous chime does not match this installation. No layout was changed.')
        self._run(check())
        return {'checked':True,'audio_played':False}

    @staticmethod
    def test_settings(remote):
        rings=remote.get('ringSettings') or []
        if not isinstance(rings,list) or any(not isinstance(r,dict) for r in rings):
            raise ManagementError('Protect returned invalid doorbell settings.')
        choices=[]
        for setting in rings or [remote]:
            volume=setting.get('volume',remote.get('volume'))
            repeat=setting.get('repeatTimes',remote.get('repeatTimes',1))
            if type(volume) is not int or not 0<=volume<=100 or type(repeat) is not int or not 1<=repeat<=10:
                raise ManagementError('Protect did not provide valid volume and repeat settings. No test was started.')
            choice={'volume':volume,'repeatTimes':repeat}
            ringtone=setting.get('ringtoneId')
            if ringtone is not None:
                if not isinstance(ringtone,str) or not re.fullmatch('[0-9a-f]{24}',ringtone):
                    raise ManagementError('Protect returned an invalid ringtone selection.')
                choice['ringtoneId']=ringtone
            if choice not in choices:choices.append(choice)
        if len(choices)!=1:
            raise ManagementError('Paired doorbells have different settings. Choose the doorbell and test its tone in Protect.')
        return choices[0]

    def test_virtual_chime(self, host, key, worker, *, volume=10):
        """Explicit user test through Protect; never used by refresh or discovery."""
        if type(volume) is not int or not 0<=volume<=100:
            raise ManagementError('Choose a test volume from 0 to 100%.')
        target=worker.removal_identity(key)
        if self.configuration_mode:
            account=self._configured_session_account(host)
        else:
            with self.lock:account=self._read()
        if not account or account['host']!=host:
            raise ManagementError('Configure a local Protect account in Home Assistant first.')
        async def play():
            async with self.client_factory(account) as client:
                matches=[]
                for remote in await client.chimes():
                    try:
                        if _mac(remote.get('mac',''))==target['mac']:matches.append(remote)
                    except ManagementError:continue
                if len(matches)!=1:raise ManagementError('This AnyChime could not be uniquely found in Protect.')
                remote=matches[0]
                if remote.get('type')!='UP Chime PoE' or remote.get('anonymousDeviceId') not in (None,target['device_id']):
                    raise ManagementError('The device identity does not match this installation.')
                if remote.get('isConnected') is not True or remote.get('isAdopted') is not True:
                    raise ManagementError('This chime must be online and adopted in Protect before testing.')
                settings=self.test_settings(remote)
                if settings['volume']==0 or volume==0:return {'test_requested':False,'muted':True,'volume':0,'audio_played':False}
                settings['volume']=volume
                await client.play(remote.get('id'),settings)
                return {'test_requested':True,'volume':settings['volume'],'audio_played':None}
        return self._run(play())

    def prepare_adoption(self, host, key, worker):
        """Repair stale local adoption only after an authenticated controller read."""
        target = worker.removal_identity(key)
        if self.configuration_mode:
            account = self._configured_session_account(host)
        else:
            with self.lock:
                account = self._read()
        if not account or account['host']!=host:
            raise ManagementError('Configure the local Protect account in Home Assistant first.')
        async def check():
            async with self.client_factory(account) as client:
                matches=[]
                for remote in await client.chimes():
                    try:
                        if _mac(remote.get('mac',''))==target['mac']:
                            matches.append(remote)
                    except ManagementError:
                        continue
                if len(matches)>1:
                    raise ManagementError('This chime identity is ambiguous in Protect.')
                if matches:
                    remote=matches[0]
                    if (remote.get('type')!='UP Chime PoE'
                            or remote.get('anonymousDeviceId') not in (None,target['device_id'])):
                        raise ManagementError('This record does not match the saved virtual chime.')
                    if remote.get('isAdopted') is True or remote.get('isConnected') is True:
                        raise ManagementError('Protect still manages this chime. Its local pairing was not changed.')
                    if remote.get('isAdopted') is not False or remote.get('isConnected') is not False:
                        raise ManagementError('Protect did not confirm that this chime is unadopted and disconnected.')
                if self.configuration_mode:
                    self._save_connection_trust(account,clear_legacy=not self.legacy_fallback)
        self._run(check())
        # Preserve the selected registry entry; restarting only this native node
        # advertises the same identity with default adoption credentials.
        worker.finalize_removal(key)
        return {'ready_for_adoption':True,'key':key,'audio_played':False}

    def inspect_previous_chimes(self, host, worker):
        if self.configuration_mode:
            account = self._configured_session_account(host)
        else:
            with self.lock:
                account = self._read()
        if not account or account['host']!=host:
            raise ManagementError('Configure the local Protect account in Home Assistant first.')
        owned = {}
        if worker.registry is None:
            raise ManagementError('The bridge is still starting.')
        for entry in worker.registry.snapshot():
            try:
                identity = worker.removal_identity(entry['key'])
                owned[identity['mac']] = (entry,identity)
            except (ValueError,OSError):
                continue
        async def inspect():
            async with self.client_factory(account) as client:
                found=[]
                for remote in await client.chimes():
                    try:
                        match=owned.get(_mac(remote.get('mac','')))
                    except ManagementError:
                        continue
                    if not match or remote.get('type')!='UP Chime PoE':
                        continue
                    entry,identity=match
                    if remote.get('anonymousDeviceId') not in (None,identity['device_id']):
                        continue
                    if entry['active'] and remote.get('isAdopted') is True and not entry.get('superseded_by'):
                        continue
                    found.append({**entry,'connected':remote.get('isConnected') is True,
                                  'adopted':remote.get('isAdopted') is True})
                if self.configuration_mode:
                    self._save_connection_trust(account,clear_legacy=not self.legacy_fallback)
                return found
        return {'chimes':self._run(inspect()),'audio_played':False}

    def chime_configuration(self, host, worker):
        """Read pairing state for verified local identities; never change Protect."""
        if not self.status(host).get('ready'):return {}
        entries=worker.registry.snapshot() if worker.registry is not None else []
        targets={}
        for entry in entries:
            if not entry.get('active') or entry.get('removal_pending') or entry.get('migration_hold'):continue
            try:targets[entry['key']]=worker.removal_identity(entry['key'])
            except (ValueError,OSError):continue
        if not targets:return {}
        if self.configuration_mode:account=self._configured_session_account(host)
        else:
            with self.lock:account=self._read()
        if not account or account['host']!=host:return {}
        async def inspect():
            async with self.client_factory(account) as client:
                devices=await client.chimes();result={}
                for key,identity in targets.items():
                    matches=[]
                    for device in devices:
                        try:
                            if _mac(device.get('mac',''))==identity['mac']:matches.append(device)
                        except ManagementError:continue
                    if len(matches)!=1:continue
                    remote=matches[0]
                    if remote.get('type')!='UP Chime PoE' or remote.get('anonymousDeviceId') not in (None,identity['device_id']):continue
                    configured=None
                    cameras=remote.get('cameraIds')
                    rings=remote.get('ringSettings')
                    # Positive evidence wins; malformed/missing fields stay unknown.
                    valid_id=lambda value:isinstance(value,str) and re.fullmatch('[0-9a-f]{24}',value) is not None
                    if isinstance(cameras,list) and all(valid_id(v) for v in cameras):configured=bool(cameras)
                    if isinstance(rings,list) and all(isinstance(v,dict) for v in rings):
                        if any(valid_id(v.get('cameraId')) for v in rings):configured=True
                        elif not rings and configured is None:configured=False
                    settings={k:remote[k] for k in ('volume','repeatTimes') if k in remote}
                    if isinstance(cameras,list):settings['cameraIds']=sorted(v for v in cameras if valid_id(v))
                    if isinstance(rings,list):
                        normalized=[{k:r[k] for k in ('cameraId','volume','repeatTimes','ringtoneId') if k in r} for r in rings if isinstance(r,dict)]
                        settings['ringSettings']=sorted(normalized,key=lambda r:json.dumps(r,sort_keys=True))
                    revision=hashlib.sha256(json.dumps(settings,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                    result[key]={'_settings_revision':revision,'_device_id':identity['device_id'],'protect_adopted':remote.get('isAdopted') is True,
                        'protect_adopting':remote.get('isAdopting') is True,'protect_configured':configured}
                return result
        return self._run(asyncio.wait_for(inspect(),8))

    def sync_names(self, host, keys, worker):
        """Replace generic Protect names only for verified active local identities."""
        entries={e['key']:e for e in worker.registry.snapshot() if e['active']}
        targets=[(key,entries[key]['name'],worker.removal_identity(key)) for key in keys if key in entries]
        if self.configuration_mode:account=self._configured_session_account(host)
        else:
            with self.lock:account=self._read()
        if not account or account['host']!=host:raise ManagementError('Configure the local Protect account first.')
        async def sync():
            async with self.client_factory(account) as client:
                devices=await client.chimes();plans=[]
                for key,name,identity in targets:
                    if not isinstance(name,str) or not 1<=len(name)<=100 or any(ord(c)<32 for c in name):raise ManagementError('Invalid speaker name.')
                    matches=[d for d in devices if re.sub('[^A-F0-9]','',str(d.get('mac','')).upper())==identity['mac']]
                    if not matches:continue
                    if len(matches)!=1:raise ManagementError('Ambiguous virtual chime identity.')
                    remote=matches[0]
                    if remote.get('type')!='UP Chime PoE' or remote.get('anonymousDeviceId') not in (None,identity['device_id']):
                        raise ManagementError('Protect chime ownership could not be verified.')
                    if remote.get('isAdopted') is not True:continue
                    device_id=remote.get('id')
                    if not isinstance(device_id,str) or not re.fullmatch('[0-9a-f]{24}',device_id):raise ManagementError('Invalid Protect chime identifier.')
                    current=remote.get('name')
                    generic=isinstance(current,str) and re.sub('[^a-z0-9]','',current.casefold()) in ('smartpoechime','chimepoe','unifianychime','protectanychime')
                    plans.append((key,device_id,name,generic and current!=name))
                # Validate the complete batch before changing any controller name.
                for key,device_id,name,change in plans:
                    if change:await client.rename(device_id,name)
                if self.configuration_mode:self._save_connection_trust(account,clear_legacy=not self.legacy_fallback)
                return [key for key,*_ in plans]
        return {'completed':self._run(sync()),'audio_played':False}

    def remove_virtual_chime(self, host, key, worker, *, allow_absent=False):
        self.remove_virtual_chimes(host,[key],worker,allow_absent=allow_absent)
        return {'removed':True,'key':key,'audio_played':False}

    def remove_virtual_chimes(self, host, keys, worker, *, allow_absent=False):
        if not isinstance(keys,list) or not keys or len(keys)>32 or len(set(keys))!=len(keys):
            raise ManagementError('Select distinct local chimes for removal.')
        targets=[(key,worker.removal_identity(key)) for key in keys]
        if self.configuration_mode:
            account=self._configured_session_account(host)
        else:
            with self.lock:
                account=self._read()
        if not account or account['host']!=host:
            raise ManagementError('Configure a local Protect account for this console first.')
        async def remove():
            async with self.client_factory(account) as client:
                devices=await client.chimes()
                plans=[]
                # Validate the whole requested batch before pausing/deleting anything.
                for key,target in targets:
                    matches=[]
                    for remote in devices:
                        try:
                            if _mac(remote.get('mac',''))==target['mac']:
                                matches.append(remote)
                        except ManagementError:
                            continue
                    if not matches and allow_absent:
                        plans.append((key,None));continue
                    if len(matches)!=1:
                        raise ManagementError('This AnyChime identity could not be uniquely found in Protect.')
                    chime=matches[0]
                    if chime.get('type')!='UP Chime PoE':
                        raise ManagementError('The Protect device is not the expected virtual PoE chime.')
                    if chime.get('anonymousDeviceId') not in (None,target['device_id']):
                        raise ManagementError('The device identity does not match this installation.')
                    device_id=chime.get('id')
                    if not isinstance(device_id,str) or not re.fullmatch('[0-9a-f]{24}',device_id):
                        raise ManagementError('Protect returned an invalid chime identifier.')
                    plans.append((key,device_id))
                if self.configuration_mode:
                    self._save_connection_trust(account,clear_legacy=not self.legacy_fallback)
                removed=[]
                for key,device_id in plans:
                    worker.begin_removal(key)
                    if device_id is not None:
                        await client.remove(device_id)
                    worker.finalize_removal(key)
                    worker.remove_from_protect(key)
                    worker.complete_removal(key)
                    removed.append(key)
                return removed
        return {'removed_keys':self._run(remove()),'audio_played':False}
