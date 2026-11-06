/* Observe confirmed Protect changes once per open page, without replaying history. */
class AnyChimeProtectEvents {
  constructor(){this.reset();}
  reset(){this.session=null;this.cursor=0;}
  consume(feed){
    if(!feed||typeof feed.session!=='string'||!Array.isArray(feed.items))return [];
    const items=feed.items.filter(e=>Number.isSafeInteger(e.id)&&e.id>0&&typeof e.key==='string'&&['removed','adopted','configured','unconfigured','settings_changed'].includes(e.type));
    const next=Math.max(this.cursor,...items.map(e=>e.id));
    if(this.session!==feed.session){this.session=feed.session;this.cursor=items.length?Math.max(...items.map(e=>e.id)):0;return [];}
    const changes=items.filter(e=>e.id>this.cursor).sort((a,b)=>a.id-b.id);
    this.cursor=next;return changes;
  }
}
