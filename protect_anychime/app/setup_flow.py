"""One-step Protect connection from the app's own web interface.

Connect confirms the console certificate and, optionally, verifies a local Protect
account. The account is stored in this app's private settings (not in the Home
Assistant Configuration tab) and takes effect without restarting the app. An
account still typed into the Configuration tab is imported once and then cleared,
so there is a single place to manage it.
"""
import hashlib
import json

import connection

ACCOUNT_KEYS = ('protect_username', 'protect_password', 'auto_adopt_chimes')
CONSOLE_KEYS = ('console_name', 'console_model', 'console_version')
IMPORT_DIGEST = 'imported_account_digest'


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(c) < 32 for c in value))


def without_account(options):
    """Home Assistant options never supply the account directly any more."""
    return {k: v for k, v in options.items() if k not in ACCOUNT_KEYS}


def public_setup(options):
    """Connection details for the web interface. Never includes the password."""
    username = options.get('protect_username')
    username = username if _text(username, 128) else ''
    return {'account_configured': bool(username and _text(options.get('protect_password'), 4096)),
            'username': username,
            'auto_adopt': options.get('auto_adopt_chimes') is True,
            **{key: options.get(key) if _text(options.get(key), 80) else '' for key in CONSOLE_KEYS}}


def import_home_assistant_account(raw_options, requester):
    """Move an account typed into the HA Configuration tab into the app, once.

    A digest of the imported values prevents a later restart from overwriting an
    account changed in the app when the HA fields could not be cleared.
    """
    if not isinstance(raw_options, dict) or not any(key in raw_options for key in ACCOUNT_KEYS):
        return False
    username, password = raw_options.get('protect_username'), raw_options.get('protect_password')
    imported = False
    if _text(username, 128) and _text(password, 4096):
        digest = hashlib.sha256(json.dumps([username, password]).encode()).hexdigest()
        with connection.LOCK:
            managed = connection.read_managed()
            if managed.get(IMPORT_DIGEST) != digest:
                for key in CONSOLE_KEYS:
                    managed.pop(key, None)
                managed.update(protect_username=username, protect_password=password,
                               auto_adopt_chimes=raw_options.get('auto_adopt_chimes') is True)
                managed[IMPORT_DIGEST] = digest
                connection.save_managed(managed)
                imported = True
    try:
        current = requester('GET').get('options')
        if isinstance(current, dict) and any(key in current for key in ACCOUNT_KEYS):
            requester('POST', {'options': without_account(current)})
    except Exception:
        # Never log account details. The import above already took effect.
        print('Home Assistant app options could not be cleared yet; the Protect account is managed in the app.', flush=True)
    if imported:
        print('Protect account moved from the Home Assistant Configuration tab into the app.', flush=True)
    return imported


def connect(payload, options, *, certificates, management):
    """Confirm the console and save or remove the account in one step.

    Nothing is saved unless the console answered and, when an account is used,
    the sign-in succeeded. No chime is adopted, removed or played.
    """
    if not isinstance(payload, dict):
        raise ValueError('Invalid connection request.')
    address = payload.get('controller_ip') or options.get('controller_ip') or ''
    if not isinstance(address, str) or not address.strip():
        raise ValueError('Enter the address of your Protect console.')
    controller = connection.lan_ip(address.strip())
    use_account = payload.get('use_account') is not False
    auto_adopt = payload.get('auto_adopt') is not False
    candidate = certificates.inspect(controller, options)
    changed_console = not candidate.get('matches_trusted')
    username = password = None
    info = {}
    if use_account:
        username = payload.get('username')
        username = username.strip() if isinstance(username, str) else ''
        password = payload.get('password') if isinstance(payload.get('password'), str) else ''
        if (not password and not changed_console and username
                and username == options.get('protect_username')):
            # Changing only automatic adoption keeps the saved password.
            password = options.get('protect_password') or ''
        if not _text(username, 128):
            raise ValueError('Enter the username of a local UniFi account.')
        if not password or len(password) > 4096:
            raise ValueError('Enter the password of the local UniFi account.')
        info = management.verify_account(controller, username, password,
                                         refresh_certificate=changed_console)
    with connection.LOCK:
        if candidate.get('confirmation_token'):
            certificates.confirm(candidate['confirmation_token'])
        managed = connection.read_managed()
        for key in ACCOUNT_KEYS + CONSOLE_KEYS:
            managed.pop(key, None)
        if use_account:
            managed.update(protect_username=username, protect_password=password,
                           auto_adopt_chimes=auto_adopt)
            managed.update({key: info[key] for key in CONSOLE_KEYS if _text(info.get(key), 80)})
        connection.save_managed(managed)
    return {'controller_ip': controller, 'console_changed': changed_console,
            'account_configured': bool(use_account), 'auto_adopt': bool(use_account and auto_adopt),
            **{key: info.get(key, '') if _text(info.get(key), 80) else '' for key in CONSOLE_KEYS},
            'audio_played': False}


def apply(worker, options, result):
    """Make the new connection effective without restarting the app."""
    worker.set_auto_adopt(result.get('auto_adopt') is True)
    if worker.resume_after_setup(options):
        return 'started'
    if result.get('console_changed') and getattr(worker, 'thread', None) is not None and worker.thread.is_alive():
        return 'restart_required'
    return 'running'
