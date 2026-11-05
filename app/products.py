"""Artwork-only physical product associations; never rename or redirect playback."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import artwork

LINKS_PATH=Path(os.environ.get('ANYCHIME_DATA_DIR','/data'))/'product-links.json'
LOCK=threading.RLock()

def read_links():
    with LOCK:
        if LINKS_PATH.is_symlink():raise ValueError('Invalid product information storage')
        try:
            data=json.loads(LINKS_PATH.read_text())
            return data if isinstance(data,dict) else {}
        except FileNotFoundError:return {}

def pairs(value):
    return {tuple(v) for v in value or [] if isinstance(v,(list,tuple)) and len(v)==2
        and all(isinstance(x,str) and x for x in v)}

def resolve(row, inventory, links):
    own=next((d for d in inventory if d.get('id')==row.get('device_id')),None)
    saved=links.get(row.get('entity_id'),{})
    if own and saved.get('source')==own.get('id'):
        target=next((d for d in inventory if d.get('id')==saved.get('product')),None)
        if target:return {**target,'_chosen':True}
    parent=next((d for d in inventory if d.get('id')==row.get('parent_device_id')),None)
    if parent and artwork.lookup_device(parent):return parent
    if not own:return None
    if artwork.lookup_device(own):return own
    candidates=[]
    for target in inventory:
        if target.get('id')==own.get('id'):continue
        shared=bool(pairs(own.get('connections')) & pairs(target.get('connections')) or
                    pairs(own.get('identifiers')) & pairs(target.get('identifiers')))
        # Serial numbers need the same manufacturer; no name, hub, room or
        # configuration URL is accepted as proof of physical identity.
        serial=own.get('serial_number')
        shared=shared or bool(serial and serial==target.get('serial_number') and
            artwork.manufacturer(own.get('manufacturer')) and
            artwork.manufacturer(own.get('manufacturer'))==artwork.manufacturer(target.get('manufacturer')))
        if shared and artwork.lookup_device(target):candidates.append(target)
    return candidates[0] if len(candidates)==1 else None

def public_inventory(inventory):
    # Do not expose hardware identifiers, serials or integration credentials.
    result=[]
    for d in inventory:
        if not isinstance(d,dict) or not re.fullmatch('[a-f0-9]{32}',str(d.get('id',''))):continue
        if not d.get('model') and not d.get('model_id'):continue
        match=artwork.lookup_device(d)
        result.append({k:str(d.get(k) or '')[:200] for k in ('id','manufacturer','model','model_id','device_name')} | (match or {}))
    return sorted(result,key=lambda d:(not bool(d.get('image')),d['device_name'].casefold(),d['id']))

def save_link(entity, product, inventory, rows):
    if not isinstance(entity,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',entity):
        raise ValueError('Choose a known speaker')
    source=next((r for r in rows if r.get('entity_id')==entity),None)
    if not source or not source.get('device_id'):raise ValueError('Speaker device information is unavailable')
    if product is not None and not any(d.get('id')==product for d in inventory):
        raise ValueError('Choose an existing Home Assistant product')
    with LOCK:
        links=read_links()
        if product is None:links.pop(entity,None)
        else:links[entity]={'source':source['device_id'],'product':product}
        LINKS_PATH.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='product-links-',dir=LINKS_PATH.parent)
        try:
            with os.fdopen(fd,'w') as f:json.dump(links,f)
            os.replace(name,LINKS_PATH)
        finally:Path(name).unlink(missing_ok=True)
