"""Persistent entity-to-chime identities. Active identities remain stable; confirmed removals are purged."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from chime_layout import ChimeLayouts


class ChimeRegistry:
    def __init__(self, directory='/data'):
        self.directory = Path(directory)
        self.path = self.directory / 'chimes.json'
        self.layouts=ChimeLayouts(self.directory/'chime-layout.json')
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

    def sync(self, entities, rows=None, *, user_change=True, layout_commit=False, preserve_key=None):
        from speakers import visual_groups
        if (not isinstance(entities,list) or len(entities)>16
                or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in entities)
                or len(set(entities))!=len(entities)):
            raise ValueError('Select up to 16 distinct Home Assistant media players')
        if self.layouts.pending and not layout_commit:
            if not user_change:return self.snapshot()
            raise ValueError('Resume the pending chime change before changing speakers.')
        metadata_known=rows is not None
        rows = self.layouts.decorate(rows or [])
        available={r['entity_id']:r.get('available',True) is True for r in rows}
        names = {r['entity_id']:str(r['name'])[:100] for r in rows}
        platforms = {r['entity_id']:str(r.get('integration') or '')[:80] for r in rows}
        groups = visual_groups(rows)
        known = {entity for group in groups for entity in group['members']}
        groups += [{'members':[entity]} for entity in entities if entity not in known]
        chosen = set(entities)
        desired = [g['members'] for g in groups if chosen.intersection(g['members'])]
        if len({e for group in desired for e in group}) > 16:
            raise ValueError('Select groups containing at most 16 speakers.')
        with self.lock:
            before = json.dumps(self.entries,sort_keys=True)
            used = set()
            owners = []
            for members in desired:
                leader = members[0]
                candidates = [e for e in self.entries if e['key'] not in used and e.get('removal_source')!='protect']
                owner = next((e for e in candidates if layout_commit and e['key']==preserve_key
                    and e['active'] and set(e.get('members',[e['entity_id']])).issubset(members)),None)
                if owner is None:
                    owner = next((e for e in candidates if e['key']=='primary' and e['entity_id'] in members),None)
                if owner is None:
                    owner = next((e for e in candidates if set(e.get('members',[]))==set(members)),None)
                if owner is None:
                    owner = next((e for e in candidates if e['entity_id']==leader),None)
                if owner is None:
                    owner = next((e for e in candidates if e['entity_id'] in members),None)
                if owner is None:
                    owner = next((e for e in candidates if e['key']=='primary' and e['entity_id'] is None),None)
                    if owner is not None:
                        owner['entity_id']=leader
                    else:
                        owner={'key':hashlib.sha256(leader.encode()).hexdigest()[:24], 'entity_id':leader}
                        self.entries.append(owner)
                used.add(owner['key'])
                owner.update(members=list(members),playback_entity=leader,active=True,
                    name=(' + '.join(names.get(e,e.split('.',1)[1].replace('_',' ').title()) for e in members))[:100])
                owner['speaker_available']=available.get(leader,False) if metadata_known else owner.get('speaker_available',True)
                if metadata_known:
                    owner['chime_only_group']=any(r.get('chime_only_group') for r in rows if r['entity_id'] in members)
                    owner['member_platforms']={e:platforms.get(e,'') for e in members}
                    owner['member_device_ids']={e:next((r.get('ha_device_id','') for r in rows if r['entity_id']==e),'') for e in members}
                owner.pop('removal_source',None);owner.pop('superseded_by',None)
                if user_change:
                    owner.pop('removal_pending',None)
                owners.append(owner)
            for entry in self.entries:
                if entry['key'] in used:
                    continue
                entry['active']=False
                replacement=next((e for e in owners if entry['entity_id'] in e['members']),None)
                if replacement:
                    entry['superseded_by']=replacement['key']
            if json.dumps(self.entries,sort_keys=True)!=before or not self.path.exists():
                try:self._save()
                except Exception:
                    self.entries=json.loads(before)
                    raise
            return self.snapshot()

    def plan(self, entities, rows=None):
        clone=object.__new__(ChimeRegistry)
        clone.directory=self.directory;clone.path=self.path;clone.lock=threading.RLock()
        clone.entries=self.snapshot();clone._dry_run=True;clone.layouts=self.layouts
        return clone.sync(entities,rows)

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
