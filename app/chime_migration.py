"""Idempotent 0.3.x data migration; interrupted operations require explicit review."""
import json
import os
import re
from pathlib import Path
import shutil
import tempfile


class ChimeMigration:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.path=self.directory/'chime-schema.json'
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_size>4096):raise ValueError('Invalid migration storage.')
        self.state=json.loads(self.path.read_text()) if self.path.exists() else None
        if self.state is not None and (not isinstance(self.state,dict) or self.state.get('version')!=1
                or type(self.state.get('review_required')) is not bool):
            raise ValueError('Invalid chime migration state.')

    def write(self,state):
        self.directory.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='chime-schema-',suffix='.tmp',dir=self.directory)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path);self.state=state
        finally:Path(name).unlink(missing_ok=True)

    def prepare(self,entries):
        if self.state is not None:return False
        legacy=self.directory/'chime-layout.json'
        pending=None
        if legacy.exists():
            if legacy.is_symlink() or legacy.stat().st_size>131072:raise ValueError('Invalid legacy chime state.')
            data=json.loads(legacy.read_text());pending=data.get('pending')
            if pending is not None and (not isinstance(pending,dict) or not isinstance(pending.get('members'),list)
                    or not isinstance(pending.get('keys'),list) or len(pending['members'])>16 or len(pending['keys'])>32
                    or any(not isinstance(v,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',v) for v in pending['members'])
                    or any(not isinstance(v,str) or not re.fullmatch(r'primary|[a-f0-9]{24}',v) for v in pending['keys'])):
                raise ValueError('Invalid unfinished legacy chime change.')
        targets=set(pending.get('members',[])) if pending else set()
        keys=set(pending.get('keys',[])) if pending else set()
        seen={};conflicts=set()
        for entry in entries:
            if entry['active']:
                for entity in entry.get('members',[]):
                    if entity in seen:conflicts.update((entry['key'],seen[entity]))
                    seen[entity]=entry['key']
        historical=False
        for entry in entries:
            identity=self.directory/'native'/'identity.json' if entry['key']=='primary' else self.directory/'identities'/entry['key']/'identity.json'
            stale=not entry['active'] and entry.get('entity_id') and entry.get('removal_source')!='protect' and identity.is_file()
            historical=historical or bool(stale)
            if (stale or entry['key'] in keys or entry['key'] in conflicts or targets.intersection(entry.get('members',[]))):
                entry['migration_hold']=True
        self._prepared={'version':1,'review_required':bool(pending or conflicts or historical)}
        return True

    def commit(self):
        if self.state is None:self.write(self._prepared)
        if not self.state['review_required']:self.cleanup()

    def status(self,entries):
        held=[e['key'] for e in entries if e.get('migration_hold') and e.get('entity_id')]
        required=bool(self.state and self.state['review_required'])
        return {'required':required,'blocked_keys':held,'can_finish':required and not held,
                'message':'An older chime change needs review. Remove the affected chimes through the confirmed Protect removal flow, then finish migration and add their speakers again.' if required else ''}

    def finish(self,entries):
        if any(e.get('migration_hold') and e.get('entity_id') for e in entries):
            raise ValueError('Confirm removal of the affected chimes before finishing migration.')
        self.write({'version':1,'review_required':False});self.cleanup()

    def cleanup(self):
        # Only obsolete files; never identities, ringtones, account or delay settings.
        for name in ('chime-layout.json','latency-calibration.json','calibration-click.mp3'):
            (self.directory/name).unlink(missing_ok=True)
        cache=self.directory/'latency-cache'
        if cache.is_symlink():cache.unlink()
        elif cache.is_dir():shutil.rmtree(cache)
