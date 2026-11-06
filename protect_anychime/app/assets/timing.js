/* Speaker settings: volume per speaker and, for several speakers, timing.
   Opening, editing and saving never play sound. */
let timingKey=null,timingDirty=false,timingSupported=false;
function settingControl(label,member,kind){
const row=document.createElement('label');row.className='timing-control';
const name=document.createElement('span');name.textContent=label;
const input=document.createElement('input');input.type='range';
const volume=kind==='volume',unit=volume?'%':' ms';
input.min=volume?'10':'0';input.max=volume?'100':'500';input.step=volume?'5':'1';
input.value=String(volume?member.volume_pct:member.delay_ms);
input.dataset.deviceId=member.device_id;input.dataset.kind=kind;
input.setAttribute('aria-label',label+' for '+member.name);
const output=document.createElement('output');output.textContent=input.value+unit;
input.oninput=()=>{
for(const other of $('timing-members').querySelectorAll('input[data-kind="'+kind+'"]')){
if(other.dataset.deviceId===input.dataset.deviceId){other.value=input.value;other.closest('.timing-control').querySelector('output').textContent=input.value+unit;}}
timingDirty=true;$('timing-test').disabled=true;showMessage('timing-feedback','Save before testing.');};
row.append(name,input,output);return row;}
async function openTiming(key){
timingKey=key;timingDirty=false;$('timing-members').replaceChildren();$('timing-note').textContent='';
showMessage('timing-feedback','Loading speaker settings…');$('timing-dialog').showModal();$('timing-save').disabled=true;$('timing-test').disabled=true;
try{const d=await api('timing?key='+encodeURIComponent(key));timingSupported=!!d.supported;
const list=$('timing-members');list.replaceChildren();
for(const member of d.members){const row=document.createElement('div');row.className='timing-row';
const title=document.createElement('span');title.className='timing-name';title.textContent=member.name;row.append(title);
if(member.model){const model=document.createElement('small');model.className='timing-model';model.textContent=member.model;row.append(model);}
row.append(settingControl('Volume',member,'volume'));if(timingSupported)row.append(settingControl('Delay',member,'delay'));list.append(row);}
$('timing-intro').textContent=timingSupported
?'Lower the volume of louder speakers so the chime sounds even, and delay a speaker that sounds early. 100% is the volume set in Protect: leave the quietest speaker at 100% and the latest at 0 ms.'
:'Adjust this speaker’s volume. 100% is the volume set in Protect.';
$('timing-note').textContent=(timingSupported?'Proxies, transcoding and Home Assistant music groups can reduce timing precision. Speakers in one HA music group share one volume and delay. ':'')+'Announcement volume depends on the integration.';
$('timing-save').disabled=false;$('timing-test').disabled=!managementReady;
showMessage('timing-feedback',managementReady?'':'Test rings need a local Protect account. Saved settings apply from the next ring.');}
catch(e){showMessage('timing-feedback',e.message,true);}}
$('timing-close').onclick=()=>$('timing-dialog').close();
$('timing-save').onclick=async()=>{
const levels={},delays={};
for(const input of $('timing-members').querySelectorAll('input'))(input.dataset.kind==='volume'?levels:delays)[input.dataset.deviceId]=Number(input.value);
$('timing-save').disabled=true;showMessage('timing-feedback','Saving speaker settings…');
try{await api('timing',{key:timingKey,levels,...(timingSupported?{delays}:{}),confirmed:true});timingDirty=false;$('timing-test').disabled=!managementReady;
showMessage('timing-feedback',managementReady?'Saved. Use Test at 10% to listen.':'Saved. The new settings apply from the next ring.');}
catch(e){showMessage('timing-feedback',e.message,true);}finally{$('timing-save').disabled=false;}};
$('timing-test').onclick=async()=>{if(timingDirty)return;$('timing-test').disabled=true;showMessage('timing-feedback','Requesting a test chime…');
try{const d=await api('chimes/test',{key:timingKey,volume:10,confirmed:true});showMessage('timing-feedback',d.muted?'Muted; no audio requested.':'Test requested at 10%.');}
catch(e){showMessage('timing-feedback',e.message,true);}finally{$('timing-test').disabled=timingDirty||!managementReady;}};
