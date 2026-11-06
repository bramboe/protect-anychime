/* Verify iframe menu hosting without HA access or audio. */
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
function node(){return {isConnected:false,open:false,close(){this.open=false;},setAttribute(){},remove(){this.isConnected=false;},append(...children){for(const child of children){child.isConnected=true;child.ownerDocument=this.ownerDocument;}},attachShadow(){const root=Object.assign(node(),{ownerDocument:this.ownerDocument});root.getElementById=id=>dialogs.find(n=>n.id===id);return root;}};}
const dialogs=['chime-actions','confirm-action-dialog'].map(id=>Object.assign(node(),{id}));
function document(){const listeners=new Set(),doc={listeners,addEventListener(type,fn){listeners.add(fn);},removeEventListener(type,fn){listeners.delete(fn);},getElementById:id=>dialogs.find(n=>n.id===id),querySelector:()=>({textContent:'.chime-actions{position:fixed}'}),createElement:()=>Object.assign(node(),{ownerDocument:doc})};doc.body=Object.assign(node(),{ownerDocument:doc});doc.defaultView={document:doc,addEventListener(){},removeEventListener(){},visualViewport:{addEventListener(){},removeEventListener(){}}};return doc;}
const source=document(),outer=document();
source.defaultView.parent=source.defaultView.top=outer.defaultView;
source.defaultView.frameElement={offsetWidth:100,offsetHeight:100,clientLeft:1,clientTop:2,getBoundingClientRect:()=>({left:10,top:20,width:200,height:200})};
const context={};vm.createContext(context);vm.runInContext(fs.readFileSync('protect_anychime/app/assets/dialog-host.js','utf8')+';this.Host=AnyChimeDialogHost;',context);
const handler=()=>{},host=new context.Host(source,dialogs.map(n=>n.id),()=>{},{pointerdown:handler});
assert.equal(host.target,outer);assert.equal(host.find('chime-actions'),dialogs[0]);assert.equal(dialogs[0].ownerDocument,outer);
assert(source.listeners.has(handler));assert(outer.listeners.has(handler));
assert.deepEqual(JSON.parse(JSON.stringify(host.anchor({left:3,right:5,top:7,bottom:9}))),{left:18,right:22,top:38,bottom:42});
dialogs[0].open=true;host.release();assert.equal(dialogs[0].open,false);assert.equal(dialogs[0].ownerDocument,source);assert.equal(source.listeners.size,0);assert.equal(outer.listeners.size,0);
host.mount();assert.equal(dialogs[0].ownerDocument,outer);host.release();
console.log('Iframe menu placement and lifecycle checks passed. No audio or network requests.');
