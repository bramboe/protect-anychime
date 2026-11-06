/* Show modal confirmations in the outer HA viewport rather than a scrolling iframe,
   and listen for outside clicks in every document between the app and HA.
   The chime menu is not hosted here: it stays in the app document so it scrolls
   together with its trigger button. */
class AnyChimeDialogHost {
  constructor(source, ids, onViewport, events={}) {
    this.source=source;this.nodes=ids.map(id=>source.getElementById(id)).filter(Boolean);
    this.style=source.querySelector('style')?.textContent||'';this.onViewport=onViewport;
    this.root=source;this.host=null;this.target=source;this.events=events;this.documents=[];
    this.mount();
    source.defaultView.addEventListener('pagehide',()=>this.release());
    source.defaultView.addEventListener('pageshow',()=>this.mount());
  }
  find(id) {return this.root.getElementById(id);}
  mount() {
    if(this.host?.isConnected)return;
    const view=this.source.defaultView;let target=this.source;
    try {if(view.top.document?.body)target=view.top.document;} catch {try {if(view.parent.document?.body)target=view.parent.document;} catch {}}
    this.target=target;this.bindEvents();if(target===this.source){this.root=this.source;return;}
    this.host=target.createElement('div');this.host.setAttribute('data-protect-anychime-dialog-host','');
    const shadow=this.host.attachShadow({mode:'open'}),style=target.createElement('style');
    style.textContent=':host{display:contents;color-scheme:light;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1e2d44;--blue:#0066ff;--muted:#6c7b90;--line:#e3e9f1;--warning:#a3680b}'+this.style;
    shadow.append(style,...this.nodes);target.body.append(this.host);this.root=shadow;
    target.defaultView.addEventListener('resize',this.onViewport);
    target.defaultView.visualViewport?.addEventListener('resize',this.onViewport);
    target.defaultView.visualViewport?.addEventListener('scroll',this.onViewport);
  }
  bindEvents() {
    this.unbindEvents();let view=this.source.defaultView;
    try {while(true){this.documents.push(view.document);if(view===this.target.defaultView)break;view=view.parent;}} catch {}
    for(const doc of this.documents)for(const [type,handler] of Object.entries(this.events))doc.addEventListener(type,handler,true);
  }
  unbindEvents() {
    for(const doc of this.documents)for(const [type,handler] of Object.entries(this.events))doc.removeEventListener(type,handler,true);
    this.documents=[];
  }
  release() {
    this.unbindEvents();
    for(const node of this.nodes){if(node.open)node.close();this.source.body.append(node);}
    this.host?.remove();this.host=null;this.root=this.source;
    if(this.target!==this.source){
      this.target.defaultView.removeEventListener('resize',this.onViewport);
      this.target.defaultView.visualViewport?.removeEventListener('resize',this.onViewport);
      this.target.defaultView.visualViewport?.removeEventListener('scroll',this.onViewport);
    }
  }
}
