"""Durable chime-only grouping preferences and explicit Protect replacement."""
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import threading


def members(value):
    if (not isinstance(value,list) or not 2<=len(value)<=16
            or any(not isinstance(v,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',v) for v in value) or len(set(value))!=len(value)):
        raise ValueError('Choose an existing Home Assistant speaker group.')
    return sorted(value)


class ChimeLayouts:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()
        if self.path.is_symlink():raise ValueError('Invalid chime layout storage.')
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'separate_groups':[],'pending':None}
        if not isinstance(self.state,dict) or not isinstance(self.state.get('separate_groups'),list):
            raise ValueError('Invalid chime layout preferences.')
        for group in self.state['separate_groups']:members(group)
        history=self.state.get('original_groups',[])
        if not isinstance(history,list) or len(history)>64:raise ValueError('Invalid original groups.')
        for group in history:members(group)
        pending=self.state.get('pending')
        if pending:
            members(pending['members'])
            if (type(pending['separate']) is not bool or pending['phase'] not in ('removing','committing')
                    or not isinstance(pending['keys'],list) or len(pending['keys'])>32
                    or any(not isinstance(k,str) or not re.fullmatch(r'primary|[a-f0-9]{24}',k) for k in pending['keys'])
                    or (pending.get('preserve_key') is not None and (not isinstance(pending['preserve_key'],str)
                        or not re.fullmatch(r'primary|[a-f0-9]{24}',pending['preserve_key']) or pending['preserve_key'] in pending['keys']))
                    or not isinstance(pending['entities'],list) or len(pending['entities'])>16
                    or any(not isinstance(e,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',e) for e in pending['entities'])):
                raise ValueError('Invalid pending chime layout change.')

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.state)

    @property
    def pending(self):return self.snapshot().get('pending')

    def _save(self,state):
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            fd,name=tempfile.mkstemp(prefix='chime-layout-',suffix='.tmp',dir=self.path.parent)
            try:
                with os.fdopen(fd,'w') as stream:
                    json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
                os.replace(name,self.path);self.state=copy.deepcopy(state)
            finally:Path(name).unlink(missing_ok=True)

    def separate(self,group):return members(group) in self.snapshot()['separate_groups']

    def decorate(self,rows):
        from speakers import visual_groups
        result=[dict(r) for r in rows]
        lookup={r['entity_id']:r for r in result}
        # Keep explicitly known Sonos chime sets discoverable if HA later ungroups.
        known=self.snapshot()
        for group in reversed(known.get('original_groups',[])+known['separate_groups']):
            if (all(e in lookup and lookup[e].get('integration')=='sonos' and len(lookup[e].get('group') or [])<=1 for e in group)
                    and not any(lookup[e].get('chime_group_members') for e in group)):
                for e in group:
                    lookup[e]['chime_group_members']=list(group);lookup[e]['chime_only_group']=True
        for block in visual_groups(result,respect_layout=False):
            group=block['members'];separate=len(group)>1 and self.separate(group)
            for entity in group:
                lookup[entity]['ha_group_members']=list(group)
                lookup[entity]['chime_separate']=separate
        return result

    def blocked(self,entry):
        pending=self.pending
        return bool(pending and set(pending['members']).intersection(entry.get('members',[entry.get('entity_id')])))

    def begin(self,pending):
        state=self.snapshot()
        if state.get('pending'):raise ValueError('Finish the pending chime change first.')
        state['pending']=pending;self._save(state)

    def phase(self,phase):
        state=self.snapshot();state['pending']['phase']=phase;self._save(state)

    def set_mode(self,group,separate):
        group=members(group);state=self.snapshot()
        state['original_groups']=[g for g in state.get('original_groups',[]) if not set(g).intersection(group)][-63:]+[group]
        state['separate_groups']=[g for g in state['separate_groups'] if not set(g).intersection(group)]
        if separate:state['separate_groups'].append(group)
        self._save(state)

    def finish(self):
        state=self.snapshot();state['pending']=None;self._save(state)


def change(worker,manager,host,group,separate,rows,*,allow_custom=False):
    """Called under selection_lock. Never calls HA grouping or audio services."""
    from speakers import visual_groups,save
    group=members(group)
    if type(separate) is not bool:raise ValueError('Choose separate or combined chimes.')
    registry=worker.registry
    if registry is None:raise ValueError('The app is still starting.')
    layouts=registry.layouts;pending=layouts.pending
    rows=layouts.decorate(rows)
    known=any(sorted(block['members'])==group for block in visual_groups(rows,respect_layout=False))
    custom=not known and (allow_custom is True or bool(pending and pending.get('custom')))
    if custom:
        lookup={r['entity_id']:r for r in rows}
        if (separate or any(e not in lookup or lookup[e].get('integration')!='sonos'
                or not (lookup[e].get('features') or 0)&1048576 or len(lookup[e].get('group') or [])>1 for e in group)):
            raise ValueError('Choose individually addressable Sonos announcement speakers. Existing HA groups stay unchanged.')
    elif not known:
        raise ValueError('The Home Assistant group changed. Refresh before changing its chimes.')
    if pending:
        if pending['members']!=group or pending['separate']!=separate:
            raise ValueError('Resume the pending chime change before making another change.')
    else:
        selected=registry.selected_entities()
        if not custom and layouts.separate(group)==separate:
            return {'selected':selected,'separate':separate,'adoption_required':False,'audio_played':False}
        # An unused group is a display preference only: do not create or adopt chimes.
        selected_members=set(group).intersection(selected)
        if not selected_members:
            layouts.set_mode(group,separate)
            return {'selected':selected,'separate':separate,'adoption_required':False,
                'preference_only':True,'audio_played':False}
        targets=[e for e in registry.snapshot() if e.get('entity_id')
            and (set(group).intersection(e.get('members',[e['entity_id']])) or e['entity_id'] in group)]
        if len(targets)>32:raise ValueError('Clean up previous chime entries before changing this group.')
        if not targets or any(not set(e.get('members',[e['entity_id']])).issubset(group) for e in targets):
            raise ValueError('The chime membership changed. Refresh before trying again.')
        active=[e for e in targets if e['active']]
        # When only part of the group is selected, the existing chime owns the
        # result, even when its speaker is not HA's coordinator or the primary key.
        preserve=active[0]['key'] if len(active)==1 and selected_members!=set(group) else None
        if preserve and separate:
            layouts.set_mode(group,True)
            return {'selected':selected,'separate':True,'adoption_required':False,
                'preserved_key':preserve,'preference_only':True,'audio_played':False}
        if preserve:
            lookup={r['entity_id']:r for r in rows}
            if any(lookup[e].get('available') is not True for e in set(group)-set(selected)):
                raise ValueError('Offline speakers cannot be added. Wait until every new member is online.')
            selected=list(dict.fromkeys([*selected,*group]))
            if len(selected)>16:raise ValueError('Select up to 16 speakers.')
            targets=[e for e in targets if e['key']!=preserve]
        if any(e['active'] and not (registry.identity_dir(e)/'identity.json').is_file() for e in targets):
            raise ValueError('Wait until the chimes have finished starting, then try again.')
        # Read-only account preflight before persisting a transition or pausing.
        if targets and not manager.status(host).get('ready'):
            raise ValueError('Configure your local Protect account before changing chimes.')
        if targets:
            manager.check_owned_chimes(host,[e['key'] for e in targets
                if (registry.identity_dir(e)/'identity.json').is_file()],worker)
        pending={'members':group,'separate':separate,'entities':selected,
            'keys':[e['key'] for e in targets],'phase':'removing','preserve_key':preserve,'custom':custom}
        layouts.begin(pending)
    if pending['phase']=='removing':
        targets=[]
        for key in pending['keys']:
            entry=next((e for e in registry.snapshot() if e['key']==key and e.get('entity_id')),None)
            if entry is None:continue # Already removed and purged by an earlier attempt.
            if entry.get('removal_source')=='protect':
                worker.complete_removal(key);continue
            if not (registry.identity_dir(entry)/'identity.json').is_file():
                if not entry['active']:
                    worker.remove_from_protect(key);worker.complete_removal(key);continue
                raise ValueError('An old chime identity needs cleanup before the change can resume.')
            worker.begin_removal(key);targets.append(key)
        if targets:manager.remove_virtual_chimes(host,targets,worker,allow_absent=True)
        # Persist this boundary before creating anything. A restart must never
        # interpret a newly generated identity reusing a key as an old target.
        layouts.phase('committing')
    layouts.set_mode(group,separate)
    registry.sync(pending['entities'],rows,layout_commit=True,preserve_key=pending.get('preserve_key'))
    try:save(registry.selected_entities(),path=registry.directory/'speakers.json')
    except OSError:pass # The authoritative registry has already been saved.
    layouts.finish()
    return {'selected':registry.selected_entities(),'separate':separate,
        'adoption_required':not bool(pending.get('preserve_key')),
        'preserved_key':pending.get('preserve_key'),'audio_played':False}
