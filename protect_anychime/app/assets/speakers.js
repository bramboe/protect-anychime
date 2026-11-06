/* Explicit, durable chime membership. HA music groups are read-only playback hints. */
let dialogHost=null;
const $=id=>document.getElementById(id)||dialogHost?.find(id);
let rows=[],chimes=[],nativeStates=new Map(),savedEntities=new Set(),viewMode='list',mutationBusy=false,dragging=false,managementReady=false,migrationReview={},actionChime=null,keyboardPickup=null,sorters=[],dragOrigin=null,dropPlan=null;
let autoAdoptEnabled=false,inventoryLoaded=false;
const expandedChimes=new Set(),cardMessages=new Map(),protectEventFeed=new AnyChimeProtectEvents();
let mutationKeys=new Set(),mutationLabel='',feedbackTimer=null;
let connectionLoaded=false,editingConnection=false,setupState={},addTarget=null;
function showMessage(id,text,error=false){const node=$(id);node.textContent=text;node.classList.toggle('error',error);}
function feedback(message,error=false,dismiss=false){clearTimeout(feedbackTimer);showMessage('feedback',message,error);if(dismiss)feedbackTimer=setTimeout(()=>showMessage('feedback',''),6000);}
function cancelKeyboardPickup(focus=true){const old=keyboardPickup;keyboardPickup=null;dragging=false;document.body.classList.remove('speaker-dragging');if(old&&focus)old.handle.focus();}
function handle(entity){const b=document.createElement('button');b.type='button';b.className='drag-handle';b.title='Move speaker';b.setAttribute('aria-label','Move speaker');b.setAttribute('aria-describedby','drag-instructions');b.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="9" r="1.5"/><circle cx="12" cy="9" r="1.5"/><circle cx="18" cy="9" r="1.5"/><circle cx="6" cy="15" r="1.5"/><circle cx="12" cy="15" r="1.5"/><circle cx="18" cy="15" r="1.5"/></svg>';b.onkeydown=e=>{if(e.key===' '&&!e.repeat){e.preventDefault();cancelKeyboardPickup(false);keyboardPickup={entity,handle:b};dragging=true;document.body.classList.add('speaker-dragging');$('drag-status').textContent='Speaker picked up. Tab to a chime or New chime and press Enter. Escape cancels.';}if(e.key==='ArrowUp'&&keyboardPickup){e.preventDefault();document.querySelector('[data-chime-drop]')?.focus();}if(e.key==='Enter'){e.preventDefault();cancelKeyboardPickup(false);void transfer(entity,null);}if(e.key==='Escape')cancelKeyboardPickup();};return b;}
window.addEventListener('keydown',e=>{if(e.key==='Escape'&&keyboardPickup){e.preventDefault();cancelKeyboardPickup();}});
function destination(element,key){element.tabIndex=0;element.dataset.chimeDrop='';element.onkeydown=e=>{if(e.target===element&&e.key==='Enter'&&keyboardPickup){e.preventDefault();const entity=keyboardPickup.entity;cancelKeyboardPickup(false);void transfer(entity,key);}};}
function setupSorting(container,key,available=false){if(typeof Sortable==='undefined')return;const sorter=new Sortable(container,{group:{name:'anychime-speakers',pull:true,put:!available},sort:false,handle:'.drag-handle',draggable:'.draggable-speaker',filter:'.speaker-action,.chime-toggle,img[role="button"]',preventOnFilter:false,animation:150,delayOnTouchOnly:true,delay:100,touchStartThreshold:4,scroll:true,ghostClass:'sortable-ghost',chosenClass:'sortable-chosen',dragClass:'sortable-drag',disabled:mutationBusy||!!migrationReview.required,
onStart:e=>{dragging=true;dropPlan=null;dragOrigin={parent:e.from,next:e.item.nextSibling,item:e.item};document.body.classList.add('speaker-dragging');},
onAdd:e=>{dropPlan={entity:e.item.dataset.entityId,key};},
onEnd:()=>{const plan=dropPlan,origin=dragOrigin;if(origin)origin.parent.insertBefore(origin.item,origin.next?.parentNode===origin.parent?origin.next:null);dropPlan=null;dragOrigin=null;dragging=false;document.body.classList.remove('speaker-dragging');if(plan)void transfer(plan.entity,plan.key);}});sorters.push(sorter);}
function image(row){const img=document.createElement('img');const fallback=['assets/tv.svg','assets/receiver.svg'].includes(row.fallback_image)?row.fallback_image:'assets/speaker.svg';img.src=/^assets\/[a-z0-9_-]+\.png$/.test(row.image||'')?row.image:fallback;img.alt=[row.manufacturer,row.model].filter(Boolean).join(' ');img.loading='lazy';img.draggable=false;img.onerror=()=>{img.onerror=null;img.src=fallback;};img.tabIndex=0;img.setAttribute('role','button');img.setAttribute('aria-label','Choose product information for '+row.name);img.onclick=()=>void openProduct(row);img.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();void openProduct(row);}};return img;}
function memberRow(row,chime=null){const item=document.createElement('div');item.className='member-row'+(!row.available?' offline':'');item.dataset.entityId=row.entity_id;const grip=handle(row.entity_id);grip.disabled=!row.available||mutationBusy||!!migrationReview.required||!!chime?.migration_hold||!!chime?.removal_pending;if(!grip.disabled)item.classList.add('draggable-speaker');item.append(grip,image(row));const details=document.createElement('div');details.className='member-details';const name=document.createElement('strong');name.textContent=row.name;const model=document.createElement('small');model.textContent=deviceLabel(row);const state=document.createElement('small');state.textContent=row.available?(chime?'Chime member':'Available'):'Offline';state.classList.toggle('warning',!row.available);const support=document.createElement('small');support.textContent=announcementSupport([row]);details.append(name,model,support,state);item.append(details);const action=document.createElement('button');action.type='button';action.className='speaker-action'+(chime?' remove-chime':row.available?' add-chime':' offline-info');action.textContent=chime?'×':row.available?'+':'i';action.disabled=mutationBusy||(!chime&&!!migrationReview.required);action.setAttribute('aria-label',chime?'Remove '+row.name:row.available?'Create a chime for '+row.name:'Speaker offline');action.title=chime?'Remove speaker':row.available?'Create new chime':'This speaker is offline. It can be added when online.';action.onclick=()=>{if(chime)void removeMember(chime,row);else if(row.available)void transfer(row.entity_id,null);};item.append(action);return item;}
function render(){
 if(!inventoryLoaded||dragging||$('chime-actions').open)return;
 for(const sorter of sorters)sorter.destroy();sorters=[];
 const area=$('speakers');area.replaceChildren();area.className='speaker-sections view-'+viewMode;
 const query=$('search').value.toLocaleLowerCase(),lookup=new Map(rows.map(r=>[r.entity_id,r]));
 const shown=chimes.filter(c=>(c.active||c.removal_pending||c.migration_hold)&&!c.removal_requested);
 for(const key of expandedChimes)if(!shown.some(c=>c.key===key))expandedChimes.delete(key);
 const h=document.createElement('h3');h.textContent='Chimes in Protect · '+shown.length;area.append(h);
 const pendingChimes=chimes.filter(c=>c.removal_requested&&c.removal_pending),pending=pendingChimes.length;
 if(pending){const note=document.createElement('div');note.className='pending-cleanup warning';note.setAttribute('role','status');const text=document.createElement('p');text.textContent=pending+' chime'+(pending===1?' is':'s are')+' removed here. '+(managementReady?'Protect cleanup is pending; retrying automatically.':'Remove '+(pending===1?'it':'them')+' in the Protect app too (shown as offline), then confirm below.');note.append(text);if(!managementReady)for(const chime of pendingChimes){const row=document.createElement('div');row.className='pending-cleanup-row';const name=document.createElement('span');name.textContent=chime.name;const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='I removed it in Protect';button.disabled=mutationBusy;button.onclick=()=>void confirmProtectCleanup(chime,button);row.append(name,button);note.append(row);}area.append(note);}
 for(const chime of shown){
  const members=chime.members||[chime.entity_id];
  if(query&&!members.some(e=>(lookup.get(e)?.name||e).toLocaleLowerCase().includes(query))&&!chime.name.toLocaleLowerCase().includes(query))continue;
  const card=document.createElement('section');card.className='chime-card';card.setAttribute('aria-busy',String(mutationKeys.has(chime.key)));
  const header=document.createElement('div');header.className='chime-heading';destination(header,chime.key);
  const title=document.createElement('h3'),toggle=document.createElement('button');toggle.type='button';toggle.className='chime-toggle';
  toggle.setAttribute('aria-expanded',String(expandedChimes.has(chime.key)));toggle.setAttribute('aria-controls','chime-content-'+chime.key);
  toggle.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>';
  const titleText=document.createElement('span');titleText.textContent=chime.name;toggle.append(titleText);title.append(toggle);
  const options=document.createElement('button');options.type='button';options.className='speaker-action chime-options';options.disabled=mutationBusy;
  options.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18" cy="12" r="1.6"/></svg>';
  options.dataset.chimeKey=chime.key;options.setAttribute('aria-label','Actions for '+chime.name);options.onclick=e=>{e.stopPropagation();openChimeActions(chime,options);};header.append(title,options);
  const content=document.createElement('div');content.className='chime-content';content.id='chime-content-'+chime.key;content.hidden=!expandedChimes.has(chime.key);
  toggle.onclick=e=>{e.stopPropagation();if(expandedChimes.has(chime.key))expandedChimes.delete(chime.key);else expandedChimes.add(chime.key);content.hidden=!expandedChimes.has(chime.key);toggle.setAttribute('aria-expanded',String(!content.hidden));};
  const state=document.createElement('p'),live=nativeStates.get(chime.key),lifecycle=AnyChimeStatus(chime,live,{autoAdoptEnabled,managementReady});state.textContent=lifecycle.label;state.className='chime-state '+lifecycle.tone;if(lifecycle.detail){const detail=document.createElement('small');detail.className='chime-state-detail';detail.textContent=lifecycle.detail;state.append(detail);}
  const list=document.createElement('div');list.className='member-list';
  for(const entity of [...members].sort((a,b)=>Number(lookup.get(b)?.available||false)-Number(lookup.get(a)?.available||false)))list.append(memberRow(lookup.get(entity)||{entity_id:entity,name:entity,available:false},chime));
  const add=document.createElement('button');add.className='secondary add-to-chime';add.type='button';add.textContent='+ Add speaker';add.disabled=mutationBusy||!!migrationReview.required||!!chime.removal_pending;add.onclick=()=>openAddMember(chime.key);
  content.append(state);
  if(!live?.adopted&&live?.protect_adopted!==true&&(lifecycle.tone==='info'||lifecycle.label==='Adopt chime in Protect')){
   const hint=document.createElement('p');hint.className='note chime-wait-hint';hint.textContent='It can take about a minute for a new chime to appear in Protect. No need to add the speaker again.';content.append(hint);
  }
  content.append(list,add);const ring=live?.playback?.last_result;
  if(ring?.state==='partial'||ring?.state==='failed'){const warning=document.createElement('small');warning.className='barrier-summary warning';warning.textContent=ring.state==='partial'?'Some speaker requests failed. Other requests were preserved; failed requests were not retried.':ring.reason||'The last playback request failed. Check app logs.';content.append(warning);}
  const timing=ring?.download_timing;if(timing){const note=document.createElement('small');note.className='barrier-summary';note.textContent=timing.distinct_downloads+' / '+timing.expected_downloads+' downloads · '+(timing.arrival_spread_ms??'—')+' ms arrival spread · '+timing.release_reason;content.append(note);}
  card.append(header,content);
  if(mutationKeys.has(chime.key)||cardMessages.has(chime.key)){const note=document.createElement('p');note.className='chime-action-feedback'+(mutationKeys.has(chime.key)?' working':' warning');note.textContent=mutationKeys.has(chime.key)?mutationLabel:cardMessages.get(chime.key);card.append(note);}
  setupSorting(header,chime.key);setupSorting(list,chime.key);area.append(card);
 }
 const drop=document.createElement('div');drop.className='new-chime-drop';drop.textContent='Drop a speaker here to create a new chime';destination(drop,null);setupSorting(drop,null);area.append(drop);
 const availableHeading=document.createElement('h3');availableHeading.textContent='Available speakers';const available=document.createElement('div');available.className='available-list';
 for(const row of [...rows].filter(r=>!savedEntities.has(r.entity_id)&&(!query||[r.name,r.manufacturer,r.model].join(' ').toLocaleLowerCase().includes(query))).sort((a,b)=>Number(b.available)-Number(a.available)))available.append(memberRow(row));
 if(!available.children.length){const note=document.createElement('p');note.className='note';note.textContent='No more speakers available.';available.append(note);}
 area.append(availableHeading,available);setupSorting(available,null,true);$('selected-summary').textContent=String(savedEntities.size);$('migration-review').hidden=!migrationReview.required;$('migration-message').textContent=migrationReview.message||'';$('migration-finish').disabled=!migrationReview.can_finish||mutationBusy;
}
let parentNavigationHandler=null;
function trackParentNavigation(){
 try{if(window.parent===window)return;if(parentNavigationHandler)window.parent.removeEventListener('location-changed',parentNavigationHandler);let previous=window.parent.location.pathname;parentNavigationHandler=()=>{const next=window.parent.location.pathname;if(next!==previous){previous=next;resetChimeExpansion();}};window.parent.addEventListener('location-changed',parentNavigationHandler);}catch{}
}
window.addEventListener('pageshow',trackParentNavigation);
window.addEventListener('pagehide',()=>{try{if(parentNavigationHandler)window.parent.removeEventListener('location-changed',parentNavigationHandler);}catch{}});
trackParentNavigation();
function resetChimeExpansion(){protectEventFeed.reset();expandedChimes.clear();if($('confirm-action-dialog').open)settleChimeConfirmation(false);if($('chime-actions').open)$('chime-actions').close();render();}
window.addEventListener('pageshow',resetChimeExpansion);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')resetChimeExpansion();else render();});

async function confirmProtectCleanup(chime,button){
 const accepted=await confirmChimeAction('Removed in Protect?','Confirm that '+chime.name+' no longer appears in the Protect app. AnyChime then deletes this chime\u2019s saved identity and ringtones. Your Home Assistant speakers are not changed.','Confirm removal');
 if(!accepted)return;button.disabled=true;feedback('Finishing cleanup…');
 try{await api('chimes/cleanup-confirmed',{key:chime.key,confirmed:true});feedback(chime.name+' is fully removed.',false,true);await status();}
 catch(e){feedback(e.message,true);button.disabled=false;}}
let confirmationResolve=null;
function confirmChimeAction(title,message,label='Remove chime'){
 if(confirmationResolve)return Promise.resolve(false);
 $('confirm-action-title').textContent=title;$('confirm-action-message').textContent=message;
 $('confirm-action-accept').textContent=label;$('confirm-action-accept').classList.toggle('destructive',label==='Remove chime');
 $('confirm-action-dialog').showModal();$('confirm-action-cancel').focus();
 return new Promise(resolve=>{confirmationResolve=resolve;});
}
function settleChimeConfirmation(accepted){const resolve=confirmationResolve;confirmationResolve=null;$('confirm-action-dialog').close();resolve?.(accepted);}
$('confirm-action-accept').onclick=()=>settleChimeConfirmation(true);
$('confirm-action-cancel').onclick=()=>settleChimeConfirmation(false);
$('confirm-action-dialog').addEventListener('cancel',event=>{event.preventDefault();settleChimeConfirmation(false);});
$('confirm-action-dialog').addEventListener('close',()=>{if(confirmationResolve){const resolve=confirmationResolve;confirmationResolve=null;resolve(false);}});
async function mutate(path,body){
 if(mutationBusy)return false;cancelKeyboardPickup(false);let expandAddedSpeaker=false;
 const owner=chimes.find(c=>c.active&&c.members?.includes(body.entity_id));mutationKeys=new Set([body.key,body.target_key,owner?.key].filter(Boolean));
 mutationLabel=path==='chimes/remove'||body.action==='remove'?'Removing chime membership…':body.confirm_source_removal?'Moving speaker and removing the empty chime…':'Updating chime membership…';
 for(const key of mutationKeys)cardMessages.delete(key);mutationBusy=true;feedback(mutationLabel);render();
 try{const result=await api(path,{...body,confirmed:true});const pending=result.cleanup_pending||result.protect_cleanup_pending;expandAddedSpeaker=path==='chimes/members'&&['add','move'].includes(body.action);
  const message=path==='chimes/remove'?'Chime removed.':body.action==='remove'?'Speaker removed.':body.entity_id?(owner?'Speaker moved.':'Speaker added.'):'Chime updated.';const waiting=expandAddedSpeaker&&!body.target_key?' It can take about a minute for the new chime to appear in Protect. No need to add the speaker again.':'';feedback(message+waiting+(pending?' Removal from Protect is pending and will retry automatically.':''),false,true);return true;
 }catch(e){for(const key of mutationKeys)cardMessages.set(key,e.message);feedback(e.message,true);return false;}
 finally{mutationBusy=false;mutationKeys=new Set();await load();if(expandAddedSpeaker){const receiving=chimes.find(c=>c.active&&c.members?.includes(body.entity_id));if(receiving){expandedChimes.add(receiving.key);render();}}await status();}
}
async function transfer(entity,target){
 const source=chimes.find(c=>c.active&&c.members?.includes(entity)),destination=chimes.find(c=>c.active&&c.key===target);
 const empty=!!(source&&source.members.length===1&&destination&&source.key!==target);
 if(empty&&!await confirmChimeAction('Move speaker?', 'Move this speaker to '+destination.name+'? The empty '+source.name+' chime will be removed from Protect. '+destination.name+' keeps its existing pairing and settings.', 'Move speaker'))return false;
 return mutate('chimes/members',{action:'move',entity_id:entity,target_key:target,confirm_source_removal:empty,source_key:empty?source.key:null});
}
async function removeMember(chime,row){const last=chime.members.length===1;if(last&&!await confirmChimeAction('Remove chime?', 'Remove this speaker and its chime? Any existing doorbell pairing will also be removed.'))return;await mutate('chimes/members',{action:'remove',key:chime.key,entity_id:row.entity_id,confirm_last:last});}
function openAddMember(key){addTarget=key;const select=$('add-member-choice');select.replaceChildren();const target=chimes.find(c=>c.key===key);const choices=rows.filter(r=>r.available&&!target?.members?.includes(r.entity_id));for(const row of choices){const option=document.createElement('option');option.value=row.entity_id;const owner=chimes.find(c=>c.active&&c.members?.includes(row.entity_id));option.textContent=row.name+(owner?' (in '+owner.name+')':'');select.append(option);}if(!choices.length){const option=document.createElement('option');option.textContent='No other online speakers available';option.disabled=true;select.append(option);}$('add-member-confirm').disabled=!choices.length;$('add-member-dialog').showModal();}
$('add-member-close').onclick=()=>$('add-member-dialog').close();$('add-member-confirm').onclick=()=>{const entity=$('add-member-choice').value;$('add-member-dialog').close();if(entity)void transfer(entity,addTarget);};
function openChimeActions(chime,anchor){actionChime=chime;const live=nativeStates.get(chime.key);$('chime-action-title').textContent=chime.name;const configured=live?.protect_configured===true;$('chime-test').hidden=!configured;$('test-volume-control').hidden=!configured;$('test-volume').value='10';$('test-volume-value').textContent='10%';$('chime-test').disabled=mutationBusy||!live?.connected||!live?.adopted||!managementReady||!!chime.migration_hold||!!chime.removal_pending;$('chime-timing').hidden=false;$('chime-timing').disabled=mutationBusy||!!chime.migration_hold||!!chime.removal_pending;$('chime-remove').disabled=mutationBusy;showMessage('chime-action-help',!managementReady?'Test rings need a local Protect account. Choose Add account at the top of the page. Removing speakers is always available.':!configured?'Configure the doorbell and chime in Protect to enable testing. You can remove it at any stage.':!live?.connected||!live?.adopted?'This chime is offline. Testing becomes available when it reconnects. You can remove it at any stage.':'Test volume applies only to this request. Announcement volume and music restoration depend on the integration.');const dialog=$('chime-actions'),box=anchor.getBoundingClientRect(),scroll={x:scrollX,y:scrollY};menuAnchor=anchor;dialog.style.top=(box.bottom+6+scrollY)+'px';dialog.style.left=(Math.max(12,box.right-280)+scrollX)+'px';if(!dialog.open)dialog.show();window.scrollTo(scroll.x,scroll.y);placeChimeMenu();(configured?$('test-volume'):$('chime-remove')).focus({preventScroll:true});}
// The menu stays in this document and is placed in page coordinates, so it scrolls
// together with its ⋯ button instead of floating over other chimes.
function placeChimeMenu(){
 const dialog=$('chime-actions');if(!dialog.open||!menuAnchor?.isConnected)return;
 const view=window.visualViewport,left=view?.offsetLeft||0,top=view?.offsetTop||0,width=view?.width||innerWidth,height=view?.height||innerHeight;
 dialog.style.maxWidth=Math.max(0,width-24)+'px';dialog.style.maxHeight=Math.max(0,height-24)+'px';
 const anchor=menuAnchor.getBoundingClientRect(),rect=dialog.getBoundingClientRect(),below=anchor.bottom+6,above=anchor.top-6-rect.height;
 const x=Math.max(left+12,Math.min(left+width-rect.width-12,anchor.right-rect.width));
 const y=below+rect.height>top+height-12&&above>=top+12?above:below;
 dialog.style.left=(x+scrollX)+'px';dialog.style.top=(y+scrollY)+'px';
}
const chimeMenu=$('chime-actions');let outsideMenuPointer=null,menuAnchor=null;
const menuEvents={
 pointerdown(event){
  outsideMenuPointer=null;if(!chimeMenu.open||$('confirm-action-dialog').open)return;
  const path=event.composedPath();
  if(path.includes(chimeMenu)||path.some(node=>node.classList?.contains('chime-options')))return;
  outsideMenuPointer={id:event.pointerId,x:event.clientX,y:event.clientY,moved:false};
 },
 pointermove(event){if(outsideMenuPointer?.id===event.pointerId&&Math.hypot(event.clientX-outsideMenuPointer.x,event.clientY-outsideMenuPointer.y)>6)outsideMenuPointer.moved=true;},
 pointerup(event){const start=outsideMenuPointer;outsideMenuPointer=null;if(start?.id===event.pointerId&&!start.moved&&!event.composedPath().includes(chimeMenu)&&!$('confirm-action-dialog').open)chimeMenu.close();},
 pointercancel(){outsideMenuPointer=null;},
 scroll(){if(outsideMenuPointer)outsideMenuPointer.moved=true;},
 keydown(event){if(event.key==='Escape'&&chimeMenu.open&&!$('confirm-action-dialog').open){event.preventDefault();chimeMenu.close();}}
};
chimeMenu.addEventListener('close',()=>{outsideMenuPointer=null;menuAnchor=null;render();document.querySelector('[data-chime-key="'+actionChime?.key+'"]')?.focus({preventScroll:true});});
if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>placeChimeMenu()).observe(chimeMenu);
window.addEventListener('resize',()=>placeChimeMenu());
window.visualViewport?.addEventListener('resize',()=>placeChimeMenu());
$('test-volume').oninput=()=>{$('test-volume-value').textContent=$('test-volume').value+'%';};
$('chime-test').onclick=async()=>{const key=actionChime?.key;if(!key)return;$('chime-test').disabled=true;showMessage('chime-action-help','Requesting a test chime…');try{const d=await api('chimes/test',{key,confirmed:true,volume:Number($('test-volume').value)});showMessage('chime-action-help',d.muted?'Muted; no playback requested.':'Test requested at '+d.volume+'%.');}catch(e){showMessage('chime-action-help',e.message,true);}finally{if($('chime-actions').open&&actionChime?.key===key)$('chime-test').disabled=false;}};
$('chime-timing').onclick=()=>{const chime=actionChime;$('chime-actions').close();void openTiming(chime.key);};
$('chime-remove').onclick=async()=>{const chime=actionChime;if(!await confirmChimeAction('Remove chime?', 'Remove '+chime.name+' and all its members from Protect? Your HA speakers remain unchanged.'))return;$('chime-actions').close();void mutate('chimes/remove',{key:chime.key});};
$('migration-finish').onclick=async()=>{if(await confirmChimeAction('Finish migration?', 'All affected old chimes must already have been removed through Protect.', 'Finish migration'))void mutate('chimes/migration/finish',{});};
async function load(){if(mutationBusy||dragging)return;$('refresh').disabled=true;try{const d=await api('speakers');rows=d.speakers;chimes=d.chimes||[];inventoryLoaded=true;savedEntities=new Set(d.selected);migrationReview=d.membership_migration||{};render();}catch(e){feedback(e.message,true);}finally{$('refresh').disabled=false;$('speakers').setAttribute('aria-busy','false');}}
async function status(){if(mutationBusy||dragging)return;try{const s=await api('status'),n=s.native,c=s.connection||{};nativeStates=new Map((n.chimes||[]).map(x=>[x.key,x]));managementReady=!!s.management?.ready;autoAdoptEnabled=n.auto_adopt_enabled===true;const view=connectionView(n,c);$('connection').textContent=view.label;$('connection').className='badge'+(view.online?' online':view.warning?' warning':view.info?' info':'');$('connection-summary').textContent=view.summary;$('connection-hint').classList.toggle('warning',!!view.warning);$('connection-hint').textContent=view.hint||'';$('connection-hint').hidden=!view.hint;renderSetup(c,s.setup||{});$('chime-instructions').textContent=n.auto_adopt_enabled?'Choose a fixed set of speakers per chime. Adoption is automatic. Configure the doorbell, ringtone, volume and repetitions in Protect.':'Choose a fixed set of speakers per chime. Adopt new chimes and configure their doorbell, sound, volume and repetitions in Protect.';$('bridge-error').textContent=n.runtime_error||'';$('bridge-error').hidden=!n.runtime_error;$('volume-summary').textContent='UniFi Protect';$('playback-badge').textContent='Track · volume · repeat';$('playback-badge').className='badge online';$('playback').textContent='Protect sends the sound to the selected chime. AnyChime delivers it to its speaker set.';$('setup').textContent='Supported integrations use announcements. Other players can interrupt music. HA music groups remain controlled by their own integration.';$('firmware').textContent='Update AnyChime through Home Assistant. Native UniFi device firmware updates do not apply to virtual chimes.';await load();const changes=protectEventFeed.consume(n.protect_events);if(changes.length){const messages=changes.map(event=>{const name=(n.known_chimes||[]).find(c=>c.key===event.key)?.name||'Chime';return name+': '+({removed:'removed in Protect. Its speakers are available again.',adopted:'adopted in Protect.',configured:'configured in Protect and active as a chime.',unconfigured:'no longer assigned to a doorbell. Configure it in Protect.',settings_changed:'chime settings updated in Protect.'}[event.type]||'updated in Protect.');});feedback(messages.join(' '),changes.some(e=>e.type==='unconfigured'),true);}}catch{feedback('Status unavailable. Refresh to try again.',true);}}
function setView(mode){viewMode=mode==='tiles'?'tiles':'list';for(const type of ['list','tiles']){$('view-'+type).classList.toggle('selected',viewMode===type);$('view-'+type).setAttribute('aria-pressed',String(viewMode===type));}try{localStorage.setItem('anychime-speaker-view',viewMode);}catch{}render();}
$('view-list').onclick=()=>setView('list');$('view-tiles').onclick=()=>setView('tiles');$('refresh').onclick=async()=>{const button=$('refresh');button.disabled=true;button.textContent='Refreshing…';feedback('Refreshing speakers…');try{await status();if(!$('feedback').classList.contains('error'))feedback('Speakers refreshed.',false,true);}finally{button.textContent='Refresh speakers';button.disabled=false;}};$('search').oninput=render;
function connectionView(native,connection){
const chimes=native.chimes||[],connected=native.connected_count||0,selected=Array.isArray(native.selected_entities)?native.selected_entities.length:chimes.length;
if(!connection.trusted)return {label:'Not connected',summary:'Setup required',hint:''};
if(native.runtime_error)return {label:'Setup needs attention',warning:true,summary:'Check setup',hint:'Check the message under Chimes in Protect.'};
if(!selected)return {label:'No chimes added',summary:'Ready to add',hint:'Add an online speaker using + or drag and drop.'};
const adopt=chimes.some(ch=>ch.state==='awaiting_adoption'&&!ch.adopted);
if(connected)return {label:connected+' connected to Protect',summary:connected+' / '+chimes.length+' connected',online:true,hint:adopt?(native.auto_adopt_enabled?'New chimes are being adopted automatically.':'Adopt the remaining chimes in Protect.'):''};
if(chimes.length&&chimes.every(ch=>ch.state==='offline'))return {label:'Speakers offline',warning:true,summary:'Speakers offline',hint:'Your chimes reconnect when the speakers are online.'};
if(adopt&&native.auto_adopt_enabled&&!native.auto_adoption_error)return {label:'Adopting in Protect',info:true,summary:'Automatic adoption',hint:'Finish doorbell, ringtone, volume and repeat setup in Protect.'};
if(adopt)return {label:'Adopt in Protect',warning:true,summary:'Adoption needed',hint:'Open UniFi Protect and adopt your new chime.'};
if(chimes.some(ch=>ch.adopted))return {label:'Reconnecting to Protect',warning:true,summary:'Reconnecting',hint:'Check that your Protect console is online.'};
return {label:'Preparing chimes',summary:'Getting ready',hint:'Setting up your virtual chime. Check its status below.'};
}
async function api(path,body){const r=await fetch('api/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-AnyChime-Request':'1'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||'The request could not be completed.');return d;}
function deviceLabel(row){const maker=row.manufacturer||'',model=row.model||row.model_id||'';return maker&&model?(model.toLocaleLowerCase().startsWith(maker.toLocaleLowerCase())?model:maker+' · '+model):model||maker||'Home Assistant speaker';}
function announcementSupport(members){
const values=members.map(r=>Number.isInteger(r.features)&&r.features>=0?!!(r.features&1048576):null);
if(values.every(v=>v===true))return 'Announcements supported';
if(values.every(v=>v===false))return 'Announcements not supported';
if(values.includes(true)&&values.includes(false))return 'Announcement support varies by speaker';
return 'Announcement support unknown';
}

let productRows=[],productSpeaker=null;
function renderProducts(){const q=$('product-search').value.toLocaleLowerCase(),choice=$('product-choice');const previous=choice.value;choice.replaceChildren();const automatic=document.createElement('option');automatic.value='';automatic.textContent='Automatic product information';choice.append(automatic);for(const p of productRows){if(q&&![p.device_name,p.manufacturer,p.model,p.model_id].join(' ').toLocaleLowerCase().includes(q))continue;const option=document.createElement('option');option.value=p.id;option.textContent=[p.device_name,p.model||p.model_id].filter(Boolean).join(' · ');choice.append(option);}choice.value=[...choice.options].some(o=>o.value===previous)?previous:'';}
async function openProduct(row){cancelKeyboardPickup(false);productSpeaker=row;$('product-speaker').textContent=row.name;showMessage('product-feedback','Loading Home Assistant product information…');$('product-search').value='';$('product-choice').replaceChildren();$('product-save').disabled=true;$('product-dialog').showModal();try{const d=await api('products');productRows=d.products||[];renderProducts();$('product-choice').value=row.product_link==='chosen'?(row.product_device_id||''):'';showMessage('product-feedback','Select a product only when it is the physical device providing this speaker.');$('product-save').disabled=false;}catch(e){showMessage('product-feedback',e.message,true);}}
$('product-search').addEventListener('input',renderProducts);
$('product-close').onclick=()=>$('product-dialog').close();
$('product-save').onclick=async()=>{if(!productSpeaker)return;$('product-save').disabled=true;showMessage('product-feedback','Saving product information…');try{await api('products/link',{entity_id:productSpeaker.entity_id,product_device_id:$('product-choice').value||null});$('product-dialog').close();await load();}catch(e){showMessage('product-feedback',e.message,true);}finally{$('product-save').disabled=false;}};

/* One-step connection: console first; a local Protect account is optional. */
function renderSetup(connection,setup){
setupState=setup;const required=!connection.trusted;
document.body.classList.toggle('setup-required',required);
for(const node of document.querySelectorAll('.summary,.layout'))node.inert=required;
const name=setup.console_name||connection.controller_ip||'';
$('protect-address').textContent=name?'Protect · '+name:'';
$('protect-address').title=[setup.console_model,setup.console_version?'Protect '+setup.console_version:'',connection.controller_ip].filter(Boolean).join(' · ');
$('connection-change').textContent=setup.account_configured?'Change':'Add account';
$('console-line').hidden=required||!name;
if(!connectionLoaded){fillSetup(connection);connectionLoaded=true;}
$('setup-cancel').hidden=required;
$('connection-setup').hidden=!(required||editingConnection);}
function fillSetup(connection,openAccount=false){
if(connection)$('console-address').value=connection.controller_ip||'';
$('protect-user').value=setupState.username||'';$('protect-password').value='';
$('protect-password').placeholder=setupState.account_configured?'Saved · leave empty to keep':'';
$('auto-adopt').checked=setupState.account_configured?setupState.auto_adopt===true:true;
$('remove-account').hidden=!setupState.account_configured;
$('setup-account').open=openAccount||!!setupState.account_configured;}
function setSetupBusy(busy){for(const id of ['connect-protect','remove-account','setup-cancel','console-address','protect-user','protect-password','auto-adopt'])$(id).disabled=busy;}
async function connectProtect(removeAccount=false){
const address=$('console-address').value.trim(),username=removeAccount?'':$('protect-user').value.trim(),password=removeAccount?'':$('protect-password').value;
const useAccount=!!username,keepPassword=setupState.account_configured&&username===setupState.username;
if(!address){showMessage('certificate-feedback','Enter the address of your Protect console.',true);$('console-address').focus();return;}
if(!useAccount&&password){$('setup-account').open=true;showMessage('certificate-feedback','Enter the username of the local UniFi account, or clear the password to connect without an account.',true);$('protect-user').focus();return;}
if(useAccount&&!password&&!keepPassword){$('setup-account').open=true;showMessage('certificate-feedback','Enter the password of the local UniFi account.',true);$('protect-password').focus();return;}
const removing=!useAccount&&setupState.account_configured;
setSetupBusy(true);showMessage('certificate-feedback',useAccount?'Connecting to Protect and signing in…':'Connecting to Protect…');
try{const d=await api('connection/connect',{controller_ip:address,use_account:useAccount,username,password:useAccount?password:'',auto_adopt:$('auto-adopt').checked});
editingConnection=false;$('protect-password').value='';showMessage('certificate-feedback','');
const name=d.console_name||d.controller_ip;
feedback(d.bridge==='restart_required'?'Connected to '+name+'. Restart AnyChime from its Home Assistant app page to switch to this console.':removing?'Protect account removed. Adopt new chimes yourself in the Protect app.':'Connected to '+name+'.'+(d.account_configured?(d.auto_adopt?' New chimes are adopted automatically.':' Adopt new chimes in Protect.'):' Add a speaker to create a chime, then adopt it in the Protect app.'),d.bridge==='restart_required',d.bridge!=='restart_required');
await status();}
catch(e){showMessage('certificate-feedback',e.message,true);}finally{setSetupBusy(false);}}
$('setup-form').onsubmit=event=>{event.preventDefault();void connectProtect();};
$('remove-account').onclick=()=>void connectProtect(true);
$('connection-change').onclick=()=>{editingConnection=true;fillSetup(null,!setupState.account_configured);showMessage('certificate-feedback','');$('setup-cancel').hidden=false;$('connection-setup').hidden=false;$('connection-setup').scrollIntoView({block:'nearest'});$('protect-user').focus({preventScroll:true});};
$('setup-cancel').onclick=()=>{editingConnection=false;showMessage('certificate-feedback','');$('connection-setup').hidden=true;$('connection-change').focus({preventScroll:true});};
try{viewMode=localStorage.getItem('anychime-speaker-view')||'list';}catch{}
dialogHost=new AnyChimeDialogHost(document,['confirm-action-dialog'],()=>placeChimeMenu(),menuEvents);
setView(viewMode);void status();setInterval(status,10000);
