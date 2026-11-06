"""Persistent entity-to-chime identities. Active identities remain stable; confirmed removals are purged."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from chime_migration import ChimeMigration


class ChimeRegistry:
    def __init__(self, directory='/data'):
        self.directory = Path(directory)
        self.path = self.directory / 'chimes.json'
        self.lock = threading.RLock()
        self.entries = json.loads(self.path.read_text()) if self.path.exists() else []
        if not isinstance(self.entries, list):
            raise ValueError('Invalid chime registry')
        keys = set()
        for entry in self.entries:
            if (not isinstance(entry, dict) or not re.fullmatch(r'primary|[a-f0-9]{24}', str(entry.get('key','')))
                    or entry['key'] in keys):
                raise ValueError('Invalid chime identity mapping')
            keys.add(entry['key'])
        if not self.entries:
            self.entries = [{'key':'primary', 'entity_id':None, 'name':'Protect AnyChime', 'active':False}]
        for entry in self.entries:
            entry['members']=entry.get('members') or ([entry['entity_id']] if entry.get('entity_id') else [])
            self._validate_entities(entry['members'])
        self.migration=ChimeMigration(self.directory)
        if self.migration.prepare(self.entries):self._save()
        self.migration.commit()

    def _save(self):
        if getattr(self,'_dry_run',False):
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('chimes-' + secrets.token_hex(8) + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.entries, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def selected_entities(self):
        with self.lock:
            return list(dict.fromkeys(entity for entry in self.entries if entry['active']
                for entity in entry.get('members', [entry['entity_id']]) if entity))

    @staticmethod
    def _validate_entities(entities):
        if (not isinstance(entities,list) or len(entities)>16
                or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in entities) or len(set(entities))!=len(entities)):
            raise ValueError('Choose up to 16 distinct speakers.')

    @staticmethod
    def _editable(entry):
        if entry.get('migration_hold') or entry.get('removal_pending'):
            raise ValueError('Finish the pending removal or migration before changing this chime.')

    def _metadata(self,entry,rows):
        lookup={r['entity_id']:r for r in rows};group=entry['members']
        entry['entity_id']=group[0] if group else None
        entry['playback_entity']=entry['entity_id']
        if group:
            entry['name']=' + '.join(lookup.get(e,{}).get('name',e.split('.',1)[1].replace('_',' ').title()) for e in group)[:100]
            entry['speaker_available']=any(lookup.get(e,{}).get('available',False) for e in group)
            entry['member_platforms']={e:lookup.get(e,{}).get('integration',entry.get('member_platforms',{}).get(e,'')) for e in group}
            entry['member_device_ids']={e:lookup.get(e,{}).get('ha_device_id',entry.get('member_device_ids',{}).get(e,'')) for e in group}
        entry.pop('chime_only_group',None)

    def _new(self,entity):
        owner=next((e for e in self.entries if not e['active'] and e.get('entity_id')==entity and not e.get('removal_source') and not e.get('removal_pending') and not e.get('migration_hold')),None)
        if owner is None:owner=next((e for e in self.entries if e['key']=='primary' and e.get('entity_id') is None),None)
        if owner is None:
            owner={'key':secrets.token_hex(12)};self.entries.append(owner)
        owner.update(entity_id=entity,members=[entity],active=True)
        return owner

    def _atomic(self,mutate):
        before=self.snapshot()
        try:
            result=mutate()
            if self.entries!=before or not self.path.exists():self._save()
            return result
        except Exception:
            self.entries=before;raise

    def sync(self,entities,rows=None,*,user_change=True):
        """Selection compatibility: retain sets; new players each create one chime."""
        self._validate_entities(entities)
        with self.lock:
            def update():
                selected=set(entities)
                owned={m for e in self.entries if e['active'] for m in e['members']}
                if user_change:
                    for entry in self.entries:
                        if not entry['active']:continue
                        desired=[m for m in entry['members'] if m in selected]
                        if desired!=entry['members']:
                            self._editable(entry);entry['active']=bool(desired)
                            if desired:entry['members']=desired
                    for entity in entities:
                        if entity not in owned:self._new(entity)
                if rows is not None:
                    for entry in self.entries:
                        if entry['active'] and not entry.get('migration_hold'):self._metadata(entry,rows)
                return self.snapshot()
            return self._atomic(update)

    def transfer(self,entity,target_key,rows,*,remove_empty=False,removal_controller=None):
        """Atomically move membership; confirmed empty sources become cleanup requests."""
        self._validate_entities([entity])
        if target_key is not None and (not isinstance(target_key,str) or not re.fullmatch(r'primary|[a-f0-9]{24}',target_key)):
            raise ValueError('Choose a valid destination chime.')
        with self.lock:
            row=next((r for r in rows if r['entity_id']==entity),None)
            if not row or not row.get('available') or row.get('excluded') or row.get('supported') is False:
                raise ValueError('Only an online supported speaker can be added or moved.')
            source=next((e for e in self.entries if e['active'] and entity in e['members']),None)
            target=next((e for e in self.entries if e['active'] and e['key']==target_key),None) if target_key else None
            if target_key and target is None:raise ValueError('The destination chime no longer exists.')
            if source and target and source['key']==target['key']:return self.snapshot()
            if source:
                self._editable(source)
                if len(source['members'])==1 and not (remove_empty is True and target is not None):
                    raise ValueError('This is the last speaker in its chime. Remove that chime through the confirmed Protect flow first, then add the speaker to its destination.')
            if target:self._editable(target)
            if not source and len(self.selected_entities())>=16:raise ValueError('Choose up to 16 speakers.')
            def update():
                empty=bool(source and len(source['members'])==1)
                if empty:
                    source.update(active=False,removal_pending=True,removal_requested=True)
                    if removal_controller:source['removal_controller']=removal_controller
                elif source:source['members'].remove(entity)
                destination=target or self._new(entity)
                if entity not in destination['members']:destination['members'].append(entity)
                if source and not empty:self._metadata(source,rows)
                self._metadata(destination,rows)
                return self.snapshot()
            return self._atomic(update)

    def detach(self,key,entity,rows=None):
        with self.lock:
            entry=next((e for e in self.entries if e['key']==key and e['active']),None)
            if not entry or entity not in entry['members']:raise ValueError('The speaker is no longer in this chime.')
            self._editable(entry)
            if len(entry['members'])==1:raise ValueError('Confirm removal of the last speaker through Protect.')
            def update():
                entry['members'].remove(entity)
                if rows is not None:self._metadata(entry,rows)
                else:
                    entry['entity_id']=entry['members'][0];entry['playback_entity']=entry['entity_id']
                    for name in ('member_platforms','member_device_ids'):
                        entry[name]={e:v for e,v in entry.get(name,{}).items() if e in entry['members']}
                return self.snapshot()
            return self._atomic(update)

    def plan(self,entities,rows=None):
        clone=object.__new__(ChimeRegistry)
        clone.directory=self.directory;clone.path=self.path;clone.lock=threading.RLock()
        clone.entries=self.snapshot();clone._dry_run=True;clone.migration=self.migration
        return clone.sync(entities,rows)

    def request_removal(self,key,controller=None):
        """Unselect immediately; retain identity until controller cleanup is confirmed."""
        with self.lock:
            entry=next((e for e in self.entries if e['key']==key),None)
            if entry is None or not entry.get('entity_id'):
                raise ValueError('This chime has already been removed.')
            def update():
                entry.update(active=False,removal_pending=True,removal_requested=True)
                if controller:entry.setdefault('removal_controller',controller)
                return self.snapshot()
            return self._atomic(update)

    def mark_removal_pending(self, key):
        with self.lock:
            entry=next((e for e in self.entries if e['key']==key),None)
            if entry is None:
                raise ValueError('Unknown virtual chime identity.')
            previous=dict(entry);entry['removal_pending']=True
            try:
                self._save()
            except Exception:
                entry.clear();entry.update(previous)
                raise

    def deactivate_from_protect(self, key):
        """Persist a controller removal without deleting HA devices or identity files."""
        with self.lock:
            entry = next((e for e in self.entries if e['key'] == key), None)
            if entry is None:
                raise ValueError('Unknown virtual chime identity')
            previous = dict(entry)
            entry['active'] = False
            entry['removal_source'] = 'protect'
            entry.pop('removal_pending',None)
            try:
                self._save()
            except Exception:
                entry.clear()
                entry.update(previous)
                raise
            return True

    def purge_removed(self, key):
        """Erase only a confirmed removed identity; retain no old entity or keys."""
        with self.lock:
            entry=next((e for e in self.entries if e['key']==key),None)
            if entry is None:
                return False
            if entry['active'] or entry.get('removal_source')!='protect':
                raise ValueError('Protect removal must be confirmed before identity cleanup.')
            base=self.directory.resolve()
            paths=[self.identity_dir(entry),self.audio_dir(entry)]
            for path in paths:
                if path.is_symlink() or not path.resolve().is_relative_to(base) or path.resolve()==base:
                    raise ValueError('Unsafe identity cleanup path.')
            # Deletion precedes the registry update: a disk error retains the
            # confirmed cleanup marker for an idempotent retry, without advertising.
            for path in paths:
                if path.exists():shutil.rmtree(path)
            before=self.entries
            self.entries=[e for e in self.entries if e['key']!=key]
            if key=='primary':
                self.entries.insert(0,{'key':'primary','entity_id':None,'name':'Protect AnyChime','active':False})
            try:
                from speakers import save
                save(self.selected_entities(),path=self.directory/'speakers.json')
                self._save()
            except Exception:
                self.entries=before
                raise
            return True

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.entries))

    def identity_dir(self, entry):
        return self.directory/'native' if entry['key']=='primary' else self.directory/'identities'/entry['key']

    def audio_dir(self, entry):
        return self.directory/'ringtones'/entry['key']
