/* Verify iframe dialog hosting and the scroll-anchored chime menu without HA access or audio. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
function node(){return {isConnected:false,open:false,addEventListener(){},show(){this.open=true;},close(){this.open=false;},setAttribute(){},remove(){this.isConnected=false;},append(...children){for(const child of children){child.isConnected=true;child.ownerDocument=this.ownerDocument;}},attachShadow(){const root=Object.assign(node(),{ownerDocument:this.ownerDocument});root.getElementById=id=>dialogs.find(n=>n.id===id);return root;}};}
const dialogs=['confirm-action-dialog'].map(id=>Object.assign(node(),{id}));
function document(){const listeners=new Set(),doc={listeners,addEventListener(type,fn){listeners.add(fn);},removeEventListener(type,fn){listeners.delete(fn);},getElementById:id=>dialogs.find(n=>n.id===id),querySelector:()=>({textContent:'dialog{margin:auto}'}),createElement:()=>Object.assign(node(),{ownerDocument:doc})};doc.body=Object.assign(node(),{ownerDocument:doc});doc.defaultView={document:doc,addEventListener(){},removeEventListener(){},visualViewport:{addEventListener(){},removeEventListener(){}}};return doc;}
const source=document(),outer=document();
source.defaultView.parent=source.defaultView.top=outer.defaultView;
const context={};vm.createContext(context);vm.runInContext(fs.readFileSync('protect_anychime/app/assets/dialog-host.js','utf8')+';this.Host=AnyChimeDialogHost;',context);
const handler=()=>{},host=new context.Host(source,dialogs.map(n=>n.id),()=>{},{pointerdown:handler});
assert.equal(host.target,outer);assert.equal(host.find('confirm-action-dialog'),dialogs[0]);assert.equal(dialogs[0].ownerDocument,outer);
assert(source.listeners.has(handler));assert(outer.listeners.has(handler));
dialogs[0].open=true;host.release();assert.equal(dialogs[0].open,false);assert.equal(dialogs[0].ownerDocument,source);assert.equal(source.listeners.size,0);assert.equal(outer.listeners.size,0);
host.mount();assert.equal(dialogs[0].ownerDocument,outer);host.release();

// The chime menu must stay in the app document and scroll with its trigger button.
const speakers=fs.readFileSync('protect_anychime/app/assets/speakers.js','utf8');
const html=fs.readFileSync('protect_anychime/app/index.html','utf8');
assert.match(speakers,/new AnyChimeDialogHost\(document,\['confirm-action-dialog'\]/);
assert.doesNotMatch(speakers,/showPopover|visualViewport\?\.addEventListener\('scroll'/);
assert.match(html,/\.chime-actions\{position:absolute;/);
console.log('Dialog hosting, lifecycle and scroll-anchored chime menu checks passed. No audio or network requests.');
