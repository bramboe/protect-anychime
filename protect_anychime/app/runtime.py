"""Per-speaker native chimes, independent identities, tracks and DHCP addresses."""
import asyncio
import copy
import re
import threading
import time
import speakers
import connection
from pathlib import Path
from discovery import MultiResponder
from native import NativeChime
from network import NetworkManager, NetworkError
from playback import TrackPlayer
from registry import ChimeRegistry
from ringtones import RingtoneStore


class NativeWorker:
    def __init__(self):
        self.registry=None
        self.nodes={}
        self.responder=None
        self.network=None
        self.stop_event=threading.Event()
        self.lock=threading.RLock()
        self.selection_lock=threading.RLock()
        self.error=None
        self.cleanup_error=None
        self._cleanup_log_after=0
        self._purge_locks={}
        self.thread=None
        self.options={}
        self._network_keys=set()
        self._statuses=[]
        self._discovery=[]
        self.diagnostics={}
        self.loop=None
        self.removal_handler=None
        self._removal_retries={}
        self.configuration_checker=None
        self._configuration_state={}
        self._configuration_after=0
        self._configuration_failed=False
        self._configuration_failures=0
        self._configuration_first_failure=None
        self._configuration_last_success=None
        self._configuration_log_at=float('-inf')
        self._configuration_log_kind=None
        self._network_issues={}
        self.auto_adopter=None
        self._auto_adopt_state={}
        self.adoption_checker=None
        self.name_synchronizer=None
        self._names_synced={}
        self._name_sync_after=0
        self._adoption_checked={}

    def status(self):
        with self.lock:
            chimes=copy.deepcopy(self._statuses)
            for chime in chimes:
                observed=self._configuration_state.get(chime['key'],{})
                if observed.get('_device_id')==chime.get('device_id'):
                    chime.update({k:v for k,v in observed.items() if k!='_device_id'})
                chime['configuration_check_failed']=self._configuration_failed
                chime['configuration_check_retrying']=self._configuration_failures>0
                adoption=self._auto_adopt_state.get(chime['key'],{})
                chime['auto_adoption_failed']=bool(adoption.get('error'))
                chime['auto_adoption_exhausted']=adoption.get('attempts',0)>=3
            connected=sum(c.get('connected',False) for c in chimes)
            return {'auto_adoption_error':any(v.get('error') for v in self._auto_adopt_state.values()),'auto_adopt_enabled':self.options.get('auto_adopt_chimes') is True,'enabled':bool(self.thread and not self.stop_event.is_set()),
                    'connected':bool(connected),'connected_count':connected,
                    'chimes':chimes,'runtime_error':self.error or self.cleanup_error,'diagnostics':dict(self.diagnostics),
                    'known_chimes':(self.registry.snapshot() if self.registry is not None else []),
                    'membership_migration':self.registry.migration.status(self.registry.snapshot()) if self.registry is not None else {},
                    'selected_entities':(self.registry.selected_entities() if self.registry is not None else None),
                    'redundant_chimes':([e for e in self.registry.snapshot() if e.get('superseded_by')
                        and e.get('removal_source')!='protect'] if self.registry is not None else []),
                    'discovery_responses':self.responder.responses if self.responder else 0,
                    'audio_played':None if any(c.get('last_play') for c in chimes) else False}

    def configure_selection(self, entities, rows=None):
        if self.registry is None:
            raise ValueError('The bridge is still starting. Please try again shortly.')
        return self.registry.sync(entities,rows)

    def start(self, options, data_dir='/data/native'):
        if self.thread:
            return
        self.options=dict(options)
        self.registry=ChimeRegistry(Path(data_dir).parent)
        if not any(e.get('entity_id') for e in self.registry.snapshot()) and options.get('speaker_entities'):
            self.registry.sync(list(options.get('speaker_entities',[])))
        self.diagnostics={'saved_primary_identity':(Path(data_dir)/'identity.json').is_file(),
            'selected_speakers':len(self.registry.selected_entities()),
            'console_configured':bool(options.get('controller_ip')),
            'certificate_configured':bool(options.get('controller_fingerprint'))}
        print('Bridge setup: '+', '.join(f'{key}={value}' for key,value in self.diagnostics.items())+'.',flush=True)
        self.thread=threading.Thread(target=lambda:asyncio.run(self._run(Path(data_dir).parent)),
                                     daemon=True,name='native-chimes')
        self.thread.start()

    def refresh_groups(self, rows):
        if self.registry is None:
            return []
        if not self.selection_lock.acquire(blocking=False):
            return self.registry.snapshot()
        try:
            if any(e.get('removal_pending') and not e.get('removal_requested') for e in self.registry.snapshot()):
                return self.registry.snapshot()
            return self.registry.sync(self.registry.selected_entities(),rows,user_change=False)
        finally:
            self.selection_lock.release()

    @staticmethod
    def _update_player(node,entry):
        player=node['player']
        with player.lock:
            player.entity_id=entry.get('playback_entity',entry['entity_id'])
            player.group_members=tuple(entry.get('members',[entry['entity_id']]))
            player.member_platforms=dict(entry.get('member_platforms',{}))
            player.member_device_ids=dict(entry.get('member_device_ids',{}))
        node['native'].options['display_name']=entry['name']

    def refresh_players(self):
        for entry in self.registry.snapshot():
            with self.lock:node=self.nodes.get(entry['key'])
            if node and entry['active']:self._update_player(node,entry)

    def plan_selection(self, entities, rows, extra_removals=None):
        if self.registry is None:
            raise ValueError('The bridge is still starting.')
        planned=self.registry.plan(entities,rows)
        keep={e['key'] for e in planned if e['active']}
        previous={e['key']:e for e in self.registry.snapshot()}
        extra=set(extra_removals or [])
        if any(key not in previous or key in keep for key in extra):
            raise ValueError('Only deselected local chimes can be removed.')
        targets=[]
        for entry in self.registry.snapshot():
            key=entry['key']
            if key in keep or not (entry['active'] or entry.get('removal_pending') or key in extra):
                continue
            exists=(self.registry.identity_dir(entry)/'identity.json').is_file()
            if exists:
                targets.append(key)
            elif key in extra:
                raise ValueError('The saved identity for this previous chime is unavailable.')
        return targets

    def request_removal(self,key):
        if self.registry is None:raise ValueError('The app is still starting. Refresh and try again.')
        current=next((e for e in self.registry.snapshot() if e['key']==key),None)
        if not current or not current.get('entity_id'):return {'removed_locally':True,'protect_cleanup_pending':False,'audio_played':False}
        self.registry.request_removal(key,controller=self.options.get('controller_ip'))
        # Reject future rings immediately, even if stopping the native task is slow.
        node=self.nodes.get(key)
        if node:node['player'].stop_event.set()
        suspended=True
        try:self.begin_removal(key)
        except Exception as error:
            suspended=False
            print('Chime removal queued; native shutdown pending ('+type(error).__name__+').',flush=True)
        entry=next((e for e in self.registry.snapshot() if e['key']==key),None)
        if entry is None or not entry.get('entity_id'):return {'removed_locally':True,'protect_cleanup_pending':False,'audio_played':False}
        if suspended and entry and not self.registry.identity_dir(entry).exists() and key not in self.nodes:
            try:
                self.registry.deactivate_from_protect(key)
                self.registry.purge_removed(key)
                return {'removed_locally':True,'protect_cleanup_pending':False,'audio_played':False}
            except OSError:
                print('Local chime cleanup is waiting for writable app storage.',flush=True)
        return {'removed_locally':True,'protect_cleanup_pending':True,'audio_played':False}

    async def _process_requested_removals(self):
        if self.registry is None:return
        entries=[e for e in self.registry.snapshot() if e.get('removal_requested') and e.get('removal_pending')]
        self._removal_retries={k:v for k,v in self._removal_retries.items() if any(e['key']==k for e in entries)}
        for entry in entries:
            key=entry['key'];now=time.monotonic();attempt=self._removal_retries.get(key,{})
            if now<attempt.get('next',0):continue
            if entry.get('removal_controller') and entry['removal_controller']!=self.options.get('controller_ip'):
                if not attempt.get('scope_warned'):print('Protect cleanup is waiting for the original console. The chime is removed locally.',flush=True)
                self._removal_retries[key]={'next':now+60,'scope_warned':True};continue
            try:
                if not self.registry.identity_dir(entry).exists():
                    self.registry.deactivate_from_protect(key);self.registry.purge_removed(key);continue
                if self.removal_handler is None:continue
                result=await asyncio.to_thread(self.removal_handler,key)
                if result.get('retry_later'):
                    self._removal_retries[key]={'next':now+30};continue
                self._removal_retries.pop(key,None)
            except Exception as error:
                count=attempt.get('count',0)+1
                self._removal_retries[key]={'next':now+min(300,15*2**min(count-1,5)),'count':count,'logged':attempt.get('logged',float('-inf'))}
                if now-attempt.get('logged',float('-inf'))>=300:
                    kind=getattr(error,'kind',type(error).__name__)
                    print('Protect chime cleanup pending ('+kind+'). The speaker is already removed locally; retrying automatically.',flush=True)
                    self._removal_retries[key]['logged']=now

    def begin_removal(self, key):
        self.registry.mark_removal_pending(key)
        with self.lock:
            self._discovery=[item for item in self._discovery if item.get('key')!=key]
        async def suspend():
            node=self.nodes.pop(key,None)
            if node:
                node['player'].stop()
                await node['native'].stop()
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(suspend(),self.loop).result(timeout=15)
        else:
            asyncio.run(suspend())

    def _stored_identity(self, key):
        if self.registry is None:
            raise ValueError('The bridge is still starting.')
        entry = next((e for e in self.registry.snapshot() if e['key'] == key), None)
        if entry is None:
            raise ValueError('Unknown AnyChime identity.')
        directory = self.registry.identity_dir(entry)
        if not (directory / 'identity.json').is_file():
            raise ValueError('This virtual chime has no saved identity yet.')
        # A local identity read only: this helper never starts listeners or connects.
        device = NativeChime({**self.options,
            'controller_fingerprint':self.options.get('controller_fingerprint') or '00'*32},
            directory)
        device.initialize_identity()
        return device

    def removal_identity(self, key):
        device = self._stored_identity(key)
        return {'mac':device.state['mac'].replace(':','').upper(),
                'device_id':device.state['device_id']}

    def finalize_removal(self, key):
        async def finalize():
            node = self.nodes.get(key)
            device = node['native'] if node else self._stored_identity(key)
            device.reset_adoption()
            with self.lock:
                self._discovery=[item for item in self._discovery if item.get('key')!=key]
            if node:
                node['player'].stop()
                await device.stop()
                self.nodes.pop(key, None)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(finalize(), self.loop).result(timeout=15)
        else:
            asyncio.run(finalize())

    def remove_from_protect(self, key):
        if self.registry is None:
            raise ValueError('The bridge is still starting.')
        result = self.registry.deactivate_from_protect(key)
        with self.lock:
            self._discovery = [item for item in self._discovery if item.get('key') != key]
        node = self.nodes.get(key)
        if node:
            node['player'].stop()
        print('Virtual chime disabled after removal in Protect.', flush=True)
        return result

    async def _purge_removed_entry(self, entry):
        key=entry['key']
        lock=self._purge_locks.setdefault(key,asyncio.Lock())
        async with lock:
            current=next((e for e in self.registry.snapshot() if e['key']==key),None)
            if current is None or current.get('removal_source')!='protect':
                return True  # Already purged; never touch a newly re-added identity.
            return await self._purge_removed_locked(current)

    async def _purge_removed_locked(self, entry):
        key=entry['key']
        node=self.nodes.get(key)
        if node:
            task=node['native'].task
            # A Protect factoryReset response must be sent before teardown.
            if task is not None and not task.done():
                return False
            node['player'].stop()
            await node['native'].stop()
            self.nodes.pop(key,None)
        if self.network is not None:
            await asyncio.to_thread(self.network.forget,key)
        self._network_keys.discard(key)
        self.registry.purge_removed(key)
        with self.lock:
            self._statuses=[item for item in self._statuses if item.get('key')!=key]
            self._discovery=[item for item in self._discovery if item.get('key')!=key]
        self._adoption_checked.pop(key,None)
        self._names_synced.pop(key,None)
        return True

    def complete_removal(self, key):
        entry=next((e for e in self.registry.snapshot() if e['key']==key),None)
        if entry is None or (entry['entity_id'] is None and entry.get('removal_source')!='protect' and not (self.registry.identity_dir(entry)/'identity.json').exists()):return
        if entry['active'] or entry.get('removal_source')!='protect':
            raise ValueError('Only confirmed removed chimes can be cleaned up.')
        async def finish():
            if not await self._purge_removed_entry(entry):
                raise ValueError('Waiting for Protect removal acknowledgement before cleanup.')
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(finish(),self.loop).result(timeout=20)
        else:asyncio.run(finish())

    async def _cleanup_confirmed(self):
        deferred=set();self.cleanup_error=None
        for entry in self.registry.snapshot():
            if not entry['active'] and entry.get('removal_source')=='protect':
                try:
                    if not await self._purge_removed_entry(entry):deferred.add(entry['key'])
                except Exception:
                    deferred.add(entry['key'])
                    self.cleanup_error='A removed chime could not be fully cleaned up. Check the app logs.'
                    now=time.monotonic()
                    if now>=self._cleanup_log_after:
                        print('Confirmed chime cleanup pending; identity remains disabled. Check app storage permissions and free space.',flush=True)
                        self._cleanup_log_after=now+300
        return deferred

    def _descriptors(self):
        with self.lock:
            return list(self._discovery)

    async def _run(self, data_dir):
        self.loop = asyncio.get_running_loop()
        try:
            if not self.options.get('native_ip') or not self.options.get('controller_ip'):
                self.error='Open Connection to check your console address.'
                print('Bridge waiting: '+self.error,flush=True)
                return
            if not self.options.get('controller_fingerprint'):
                self.error='Open Connection, refresh the console certificate and confirm it before adoption.'
                print('Bridge waiting: no saved console certificate. Existing device identities remain unchanged.',flush=True)
                return
            self.options=await asyncio.to_thread(connection.ensure_saved_certificate,self.options)
            if not self.diagnostics.get('selected_speakers') and not self.diagnostics.get('saved_primary_identity'):
                print('Bridge idle: import the existing installation or select a speaker in the web interface.',flush=True)
            self.network=NetworkManager(data_dir/'network',self.options['native_ip'],self.options['controller_ip'])
            self.responder=MultiResponder(self.options['native_ip'],[self.options['controller_ip']],self._descriptors)
            def discover():
                try:
                    self.responder.serve(self.stop_event)
                except Exception:
                    self.error='Discovery could not start. Check UDP port 10001.'
                    print('Bridge stopped: '+self.error,flush=True)
                    self.stop_event.set()
            udp=threading.Thread(target=discover,daemon=True,name='chime-discovery')
            udp.start()
            last_metadata = float('-inf')
            while not self.stop_event.is_set():
                if time.monotonic() - last_metadata >= 30:
                    last_metadata = time.monotonic()
                    try:
                        self.refresh_groups(await asyncio.to_thread(speakers.discover))
                    except Exception:
                        print('Speaker metadata refresh unavailable; keeping saved chime sets.',flush=True)
                await self._reconcile(data_dir)
                await self._process_requested_removals()
                await self._auto_adopt_new_chimes()
                await self._repair_stale_adoptions()
                await self._sync_names()
                await self._check_configuration()
                await asyncio.sleep(1)
            udp.join(timeout=2)
        except Exception as error:
            self.error='The chime bridge could not start ('+type(error).__name__+').'
            print(self.error,flush=True)
        finally:
            self.stop_event.set()
            for node in list(self.nodes.values()):
                await node['native'].stop()
                node['player'].stop()
            if self.network:
                await asyncio.to_thread(self.network.stop)
            with self.lock:
                self.nodes.clear()
                self._discovery=[]
                self._statuses=[{**item,"connected":False,"state":"stopped"} for item in self._statuses]

    def _record_network_state(self, key, net):
        if net['state'] not in ('permission_required','error'):
            self._network_issues.pop(key,None)
            return
        reason=' '.join(str(net.get('reason','Network setup failed.')).split())[:240]
        issue=(net['state'],reason)
        if self._network_issues.get(key)!=issue:
            print('Chime network setup needs attention ('+net['state']+'): '+reason,flush=True)
            self._network_issues[key]=issue

    async def _check_configuration(self):
        now=time.monotonic()
        if self.configuration_checker is None or now<self._configuration_after:return
        # A successful poll already checks every owned chime in one request.
        self._configuration_after=now+30
        try:
            result=await asyncio.to_thread(self.configuration_checker)
            with self.lock:
                recovered=self._configuration_failed
                self._configuration_state=result
                self._configuration_failed=False
                self._configuration_failures=0
                self._configuration_first_failure=None
                self._configuration_last_success=time.monotonic()
            if recovered:print('Protect setup checks recovered. Current configuration confirmed.',flush=True)
        except Exception as error:
            now=time.monotonic()
            kind=getattr(error,'kind','timeout' if isinstance(error,TimeoutError) else 'management_error')
            if kind not in {'management_error','timeout','certificate','authentication','rate_limit','http_error','network','invalid_response'}:kind='management_error'
            http=getattr(error,'http_status',None)
            detail=kind+(', HTTP '+str(http) if type(http) is int and 100<=http<=599 else '')
            with self.lock:
                was_failed=self._configuration_failed
                self._configuration_failures+=1
                if self._configuration_first_failure is None:self._configuration_first_failure=now
                elapsed=now-self._configuration_first_failure
                stale=self._configuration_last_success is not None and now-self._configuration_last_success>=300
                self._configuration_failed=(self._configuration_failures>=3 and elapsed>=90) or stale
                # A short failed query cannot invalidate a confirmed pairing.
                # Expire observations after five minutes without any success.
                if stale:self._configuration_state={}
                delay=min(60,15*2**min(self._configuration_failures-1,2))
                self._configuration_after=now+delay
                should_log=(self._configuration_failed and not was_failed) or now-self._configuration_log_at>=300 or detail!=self._configuration_log_kind
                if should_log:
                    self._configuration_log_at=now;self._configuration_log_kind=detail
            if should_log:
                prefix='Protect setup checks remain unavailable' if self._configuration_failed else 'Protect setup check temporarily unavailable'
                print(prefix+' ('+detail+'). Retrying automatically in '+str(delay)+' seconds. The live chime connection is monitored separately.',flush=True)

    async def _auto_adopt_new_chimes(self):
        if self.options.get('auto_adopt_chimes') is not True or self.auto_adopter is None:return
        self._auto_adopt_state={k:v for k,v in self._auto_adopt_state.items() if k in self.nodes}
        for key,node in list(self.nodes.items()):
            native=node['native'];identity=native.state.get('device_id')
            if native.state.get('adopted'):
                self._auto_adopt_state.pop(key,None);continue
            if not self._entry_enabled(key):continue
            now=time.monotonic();state=self._auto_adopt_state.get(key,{})
            if state.get('identity')!=identity:state={'identity':identity,'attempts':0,'last':-float('inf')}
            if state['attempts']>=3 or now-state['last']<30:continue
            state['last']=now;self._auto_adopt_state[key]=state
            try:
                result=await asyncio.to_thread(self.auto_adopter,key)
                if not result.get('retry_later'):
                    state['attempts']+=1;state.pop('error',None)
                    if state['attempts']==3:print('Automatic chime adoption is not confirmed after three attempts. Check adoption and account permissions in Protect.',flush=True)
            except Exception:
                state['attempts']+=1;state['error']=True
                print('Automatic chime adoption unavailable; configure or adopt the chime in Protect.',flush=True)

    async def _repair_stale_adoptions(self):
        self._adoption_checked={key:device for key,device in self._adoption_checked.items() if key in self.nodes}
        if self.adoption_checker is None:
            return
        for key,node in list(self.nodes.items()):
            device=node['native']
            if (self._adoption_checked.get(key) is device or device.connected or not device.state.get('adopted')
                    or device.last_http_status not in (401,403)):
                continue
            self._adoption_checked[key]=device
            try:
                result=await asyncio.to_thread(self.adoption_checker,key)
                if result.get('retry_later'):
                    self._adoption_checked.pop(key,None)
            except Exception:
                print('Adoption repair needs attention; pairing kept unless Protect verified its removal.',flush=True)

    async def _sync_names(self):
        if self.name_synchronizer is None or self.registry is None or time.monotonic()<self._name_sync_after:return
        entries={e['key']:e for e in self.registry.snapshot() if e['active']}
        signatures={key:(entries[key]['name'],node['native'].state.get('device_id')) for key,node in self.nodes.items()
            if key in entries and node['native'].connected}
        keys=[key for key,signature in signatures.items() if self._names_synced.get(key)!=signature]
        if not keys:return
        self._name_sync_after=time.monotonic()+30
        try:
            result=await asyncio.to_thread(self.name_synchronizer,keys)
            for key in result.get('completed',[]):
                if key in signatures:self._names_synced[key]=signatures[key]
        except Exception:
            print('Protect speaker-name sync unavailable; existing names preserved.',flush=True)

    def _entry_enabled(self, key):
        entry = next((e for e in self.registry.snapshot() if e['key'] == key), None)
        return bool(entry and entry['active'] and entry.get('speaker_available',True) and not entry.get('removal_pending') and not entry.get('migration_hold'))

    async def _reconcile(self, data_dir):
        deferred=await self._cleanup_confirmed()
        entries=self.registry.snapshot()
        active={e['key']:e for e in entries if (e['active'] and not e.get('removal_pending') and not e.get('migration_hold')) or e['key'] in deferred}
        for key in list(self._network_keys - active.keys()):
            await asyncio.to_thread(self.network.stop,key)
            self._network_keys.discard(key)
        for key in list(self.nodes):
            if key not in active or (key not in deferred and not active[key].get('speaker_available',True)):
                node=self.nodes.pop(key,None)
                if node is None:
                    continue
                await node['native'].stop()
                node['player'].stop()
        statuses=[{**e,'connected':False,'state':'migration_pending','reason':'Review the unfinished change from the previous version before changing this chime.'}
            for e in entries if e.get('migration_hold') and e.get('entity_id')]
        for key,entry in active.items():
            if not entry.get('speaker_available',True) and not entry.get('removal_pending'):
                statuses.append({**entry,'connected':False,'state':'offline',
                    'reason':'Speaker offline. The paired chime will reconnect automatically when it is online.'})
                continue
            if not self._entry_enabled(key):
                continue
            try:
                identity=NativeChime({**self.options,'native_enabled':True},self.registry.identity_dir(entry))
                identity.initialize_identity()
                self._network_keys.add(key)
                net=await asyncio.to_thread(self.network.ensure,key,identity.state['mac'])
                self._record_network_state(key,net)
                address=net.get('ip','')
                existing=self.nodes.get(key)
                if existing and (net['state']!='ready' or existing['ip']!=address):
                    await existing['native'].stop()
                    existing['player'].stop()
                    self.nodes.pop(key)
                if net['state']!='ready':
                    statuses.append({**entry,'connected':False,'ip':address,'state':net['state'],
                                     'reason':net.get('reason','Waiting for a DHCP address.')})
                    continue
                if key not in self.nodes:
                    store=RingtoneStore(self.registry.audio_dir(entry))
                    base=f"http://{self.options['native_ip']}:{self.options.get('media_port',48200)}"
                    player=TrackPlayer(key,entry.get('playback_entity',entry['entity_id']),store,base,
                        group_members=entry.get('members',[entry['entity_id']]),
                        member_platforms=entry.get('member_platforms',{}),data_dir=data_dir,member_device_ids=entry.get('member_device_ids',{}))
                    native=NativeChime({**self.options,'native_enabled':True,'native_ip':address,
                        'bind_source_ip':True,'display_name':entry['name']},
                        self.registry.identity_dir(entry),
                        on_play=player.submit,ringtone_store=store,
                        on_remove=lambda key=key:self.remove_from_protect(key))
                    try:
                        await native.start(management_host=address,port=0,connect=False)
                        await asyncio.to_thread(self.network.redirect_management,key,address,native.listen_port)
                    except Exception:
                        await native.stop()
                        player.stop()
                        raise
                    if not self._entry_enabled(key):
                        await native.stop()
                        player.stop()
                        continue
                    native.begin_connection()
                    with self.lock:
                        self.nodes[key]={'native':native,'player':player,'store':store,'ip':address}
                node=self.nodes[key]
                self._update_player(node,entry)
                statuses.append({**entry,**node['native'].status(),'ip':address,
                                 'playback':node['player'].status(),'state':'connected' if node['native'].connected else 'awaiting_adoption'})
            except Exception as error:
                detail=str(error)[:240] if isinstance(error,NetworkError) else type(error).__name__
                print(f"Chime {key}: setup stopped ({detail}).",flush=True)
                statuses.append({**entry,'connected':False,'state':'error',
                    'reason':'Chime setup failed: '+detail+'.'})
        statuses.extend({**e,'connected':False,'state':'removal_pending',
            'reason':'Removal has not been confirmed. Check Protect access and save again.'}
            for e in entries if e.get('removal_pending'))
        with self.lock:
            self._statuses=statuses
            self._discovery=[{'key':key,'ip':node['ip'],'packet':node['native'].discovery_response()}
                             for key,node in self.nodes.items()
                             if not node['native'].removal_requested and self._entry_enabled(key)]

    def media(self, key, track, md5):
        if not re.fullmatch(r'primary|[a-f0-9]{24}',key) or not re.fullmatch(r'[a-f0-9]{32}',md5):
            return None
        with self.lock:
            node=self.nodes.get(key)
            store=node['store'] if node else None
        if store is None:
            return None
        try:
            asset=store.get(track)
        except OSError:
            return None
        if asset is None or asset['md5']!=md5:
            return None
        return asset

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)
