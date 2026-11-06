"""Explicit ingress-only identity transfer between installations; no network calls."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import stat
import zipfile

MAX_ARCHIVE=128*1024*1024
MAX_TOTAL=256*1024*1024
ROOT_FILES={'managed-settings.json','speakers.json','chimes.json','product-links.json','latency-delays.json','speaker-levels.json','chime-schema.json','protect-management-trust.json'}
DIRECTORIES={'native','identities','ringtones','network'}
SETTINGS={'native_ip','controller_ip','controller_fingerprint','controller_certificate','unifi_compatibility_profile'}
# The Protect account is never exported; an import keeps the one already set up here.
ACCOUNT_SETTINGS={'protect_username','protect_password','auto_adopt_chimes','console_name','console_model','console_version','imported_account_digest'}


def _keep_account(target,data):
    try:
        current=json.loads(target.read_text()) if target.is_file() else {}
    except (ValueError,OSError):
        current={}
    kept={k:v for k,v in current.items() if k in ACCOUNT_SETTINGS} if isinstance(current,dict) else {}
    return json.dumps({**json.loads(data),**kept}).encode() if kept else data


def allowed(name):
    path=PurePosixPath(name)
    return (bool(path.parts) and not path.is_absolute() and '..' not in path.parts
            and '\\' not in name and '\x00' not in name and path.as_posix()==name
            and (name in ROOT_FILES or (len(path.parts)>1 and path.parts[0] in DIRECTORIES))
            and path.name not in {'process.json','udhcpc.pid','lock'} and not path.name.endswith('.tmp'))


def obsolete(name):
    path=PurePosixPath(name)
    safe=bool(path.parts) and not path.is_absolute() and '..' not in path.parts and '\\' not in name and path.as_posix()==name
    return safe and (name in {'chime-layout.json','latency-calibration.json','calibration-click.mp3'} or path.parts[0]=='latency-cache')


def export_state(directory, options, selected):
    root=Path(directory).resolve()
    files={}
    if root.exists():
        for path in root.rglob('*'):
            name=path.relative_to(root).as_posix()
            if path.is_symlink() and path.relative_to(root).parts[0] in DIRECTORIES:
                raise ValueError('A migration backup cannot contain symbolic links.')
            if not allowed(name):continue
            if path.is_symlink():raise ValueError('A migration backup cannot contain symbolic links.')
            if not path.is_file():continue
            if path.stat().st_size>4*1024*1024:raise ValueError('A saved data file is too large.')
            files[name]=path.read_bytes()
    managed={k:options[k] for k in SETTINGS if options.get(k)}
    files['managed-settings.json']=json.dumps(managed).encode()
    files['speakers.json']=json.dumps({'entities':selected}).encode()
    if 'chimes.json' not in files:
        entries=[{'key':'primary','entity_id':selected[0] if selected else None,
                  'name':selected[0].split('.',1)[1].replace('_',' ').title() if selected else 'Protect AnyChime',
                  'active':bool(selected)}]
        for entity in selected[1:]:
            entries.append({'key':hashlib.sha256(entity.encode()).hexdigest()[:24],
                            'entity_id':entity,'name':entity.split('.',1)[1].replace('_',' ').title(),'active':True})
        files['chimes.json']=json.dumps(entries).encode()
    if sum(map(len,files.values()))>MAX_TOTAL or len(files)>2048:
        raise ValueError('The saved data is too large to transfer.')
    manifest={'format':'protect-anychime-migration','version':1,
              'files':{name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()}}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('migration.json',json.dumps(manifest))
        for name,raw in files.items():archive.writestr(name,raw)
    value=output.getvalue()
    if len(value)>MAX_ARCHIVE:raise ValueError('The migration backup is too large.')
    return value


def inspect_archive(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_ARCHIVE:
        raise ValueError('Select a valid migration backup.')
    files={}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos=archive.infolist()
            if len(infos)>2049 or len({i.filename for i in infos})!=len(infos):
                raise ValueError('The migration backup has duplicate or excessive entries.')
            if sum(i.file_size for i in infos)>MAX_TOTAL:
                raise ValueError('The migration backup is too large.')
            for info in infos:
                mode=info.external_attr>>16
                if stat.S_ISLNK(mode) or info.is_dir() or (info.file_size>4*1024*1024 and not obsolete(info.filename)):
                    raise ValueError('The migration backup contains an invalid file.')
                if info.filename!='migration.json' and not allowed(info.filename) and not obsolete(info.filename):
                    raise ValueError('The migration backup contains an unexpected path.')
                files[info.filename]=archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError):
        raise ValueError('The migration backup could not be read.') from None
    try:
        manifest=json.loads(files.pop('migration.json'))
        if (manifest.get('format') not in {'protect-anychime-migration','unifi-anychime-migration'} or manifest.get('version')!=1
                or manifest.get('files')!={name:hashlib.sha256(data).hexdigest() for name,data in files.items()}):
            raise ValueError('The migration backup checksum or version is invalid.')
        # Do not defer malformed settings/selections until the next startup.
        settings=json.loads(files['managed-settings.json'])
        selected=json.loads(files['speakers.json'])['entities']
        if not isinstance(settings,dict) or not set(settings)<=SETTINGS or not isinstance(selected,list):
            raise ValueError('The migration settings are invalid.')
        import re
        if len(selected)>16 or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in selected):
            raise ValueError('The migration speaker selection is invalid.')
    except (KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('The migration backup is incomplete.') from None
    legacy=json.loads(files.get('chime-layout.json',b'{}'))
    pending=legacy.get('pending') if isinstance(legacy,dict) else None
    if pending:
        if not isinstance(pending,dict) or not isinstance(pending.get('members'),list) or not isinstance(pending.get('keys'),list):
            raise ValueError('Invalid unfinished chime change in backup.')
        entries=json.loads(files['chimes.json'])
        for entry in entries:
            if entry['key'] in pending['keys'] or set(pending['members']).intersection(entry.get('members',[entry.get('entity_id')])):
                entry['migration_hold']=True
        files['chimes.json']=json.dumps(entries).encode()
        files['chime-schema.json']=json.dumps({'version':1,'review_required':True}).encode()
    return {name:value for name,value in files.items() if not obsolete(name)}


def import_state(raw, directory):
    root=Path(directory).resolve()
    # Never replace a configured identity. New installations may have an empty registry.
    if ((root/'native').exists() and any((root/'native').iterdir())
            or (root/'identities').exists() and any(p.is_file() for p in (root/'identities').rglob('*'))):
        raise ValueError('This installation already has a device identity. Import into a fresh installation.')
    files=inspect_archive(raw)
    root.mkdir(parents=True,exist_ok=True)
    staging=root/('.migration-'+secrets.token_hex(8))
    staging.mkdir(mode=0o700)
    before={}
    committed=[]
    try:
        for name,data in files.items():
            if name=='managed-settings.json':
                data=_keep_account(root/name,data)
            path=staging/name
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as output:output.write(data)
        for name in files:
            target=root/name
            if any(parent.is_symlink() for parent in [target,*target.parents] if parent!=root.parent):
                raise ValueError('Refusing to import through a symbolic link.')
            target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            before[name]=target.read_bytes() if target.is_file() else None
            os.replace(staging/name,target)
            committed.append(name)
    except Exception:
        for name in reversed(committed):
            target=root/name
            if before[name] is None:target.unlink(missing_ok=True)
            else:
                fd=os.open(target,os.O_WRONLY|os.O_TRUNC,0o600)
                with os.fdopen(fd,'wb') as output:output.write(before[name])
        raise
    finally:
        shutil.rmtree(staging,ignore_errors=True)
    return {'files_imported':len(files),'restart_required':True,'audio_played':False}
