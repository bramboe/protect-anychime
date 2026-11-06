"""Read-only HA discovery and private, persistent target selection."""
import json
import artwork
import products
import os
import re
import secrets
from pathlib import Path
from audio import _supervisor_request, SpeakerError

SELECTION_PATH = Path(os.environ.get('SPEAKERS_PATH', '/data/speakers.json'))
TEMPLATE = """{% set ns = namespace(items=[]) %}
{% for s in states.media_player %}{% set parent = device_attr(s.entity_id, 'parent_device_id') %}{% set ns.items = ns.items + [dict(entity_id=s.entity_id,device_id=device_id(s.entity_id) or '',
name=s.name, state=s.state, features=state_attr(s.entity_id, 'supported_features'),
state_class=state_attr(s.entity_id, 'device_class') or 'speaker',
model=device_attr(s.entity_id, 'model') or '',
model_id=device_attr(s.entity_id, 'model_id') or '',
manufacturer=device_attr(s.entity_id, 'manufacturer') or '',
device_name=device_attr(s.entity_id, 'name') or '',
parent_device_id=parent or '',
parent_manufacturer=(device_attr(parent, 'manufacturer') or '') if parent else '',
parent_model=(device_attr(parent, 'model') or '') if parent else '',
parent_model_id=(device_attr(parent, 'model_id') or '') if parent else '',
parent_name=(device_attr(parent, 'name') or '') if parent else '',
integration=(config_entry_attr(config_entry_id(s.entity_id), 'domain') or '') if config_entry_id(s.entity_id) else '',
group=state_attr(s.entity_id, 'group_members') or [])] %}{% endfor %}{% set ps = namespace(items=[], seen=[]) %}{% for s in states %}{% set d=device_id(s.entity_id) %}{% if d and d not in ps.seen and ps.seen|length < 512 %}{% set ps.seen=ps.seen+[d] %}{% set ps.items=ps.items+[dict(id=d, manufacturer=device_attr(d,'manufacturer') or '',model=device_attr(d,'model') or '',model_id=device_attr(d,'model_id') or '',device_name=device_attr(d,'name') or '',serial_number=device_attr(d,'serial_number') or '',connections=(device_attr(d,'connections') or [])|list,identifiers=(device_attr(d,'identifiers') or [])|list)] %}{% endif %}{% endfor %}{{ dict(speakers=ns.items,products=ps.items) | to_json }}"""


def product_image(manufacturer, model, model_id=''):
    """Use the shared public catalogue, never a local installation inventory."""
    match=artwork.lookup(manufacturer,model,model_id)
    return match['image'] if match else None


def device_icon(device_class):
    return {'tv':'assets/tv.svg','receiver':'assets/receiver.svg'}.get(device_class,'assets/speaker.svg')


def is_unifi_device(row):
    # Match device registry metadata, never a user-editable speaker name.
    maker = re.sub(r'[^a-z0-9]', '', str(row.get('manufacturer') or '').casefold())
    return bool(re.fullmatch(r'(?:ubiquiti(?:networks)?|unifi)(?:incorporated|inc|ltd|llc|corp|corporation)?', maker))


def exclusion_reason(row):
    if is_unifi_device(row) or row.get('integration') == 'unifiprotect':
        return 'Native UniFi devices do not need AnyChime.'
    if row.get('integration') == 'webostv':
        return 'This integration supports channel playback, not chime audio URLs.'
    features = row.get('features')
    if type(features) is not int:
        return 'Audio playback capability could not be determined.'
    if not features & 512:  # Home Assistant MediaPlayerEntityFeature.PLAY_MEDIA
        return 'This entity does not support media playback requests.'
    return None


def discover(requester=None, *, include_excluded=False, include_products=False):
    rows = (requester or _supervisor_request)('POST', '/template', {'template': TEMPLATE})
    physical=rows.get('products',[]) if isinstance(rows,dict) else []
    rows=rows.get('speakers') if isinstance(rows,dict) else rows
    links=products.read_links()
    if not isinstance(rows, list):
        raise SpeakerError('Home Assistant did not return a valid speaker list.')
    result = []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'media_player\.[a-z0-9_]+', str(row.get('entity_id', ''))):
            continue
        reason = exclusion_reason(row)
        excluded = reason is not None
        if excluded and not include_excluded:
            continue
        source=products.resolve(row,physical,links)
        image_match=artwork.lookup_device(source) if source else artwork.lookup_device(row)
        result.append({'excluded': excluded, 'exclusion_reason': reason, 'entity_id': row['entity_id'], 'name': str(row.get('name', row['entity_id']))[:200],
            'available': row.get('state') not in ('unavailable', 'unknown', 'off', 'standby'),
            'supported': True,
            'features': row.get('features') if type(row.get('features')) is int else None,
            'device_class': str(row.get('state_class') or 'speaker'),
            'group': [g for g in row.get('group', []) if isinstance(g, str)] if isinstance(row.get('group'), list) else [],
            'manufacturer': str(row.get('manufacturer') or '')[:160],
            'integration': str(row.get('integration') or '')[:80],
            'model': str(row.get('model') or '')[:160],
            'model_id': str(row.get('model_id') or '')[:160],
            'ha_device_id':str(row.get('device_id') or '')[:64],
            'product_device_id':source.get('id') if source else None,
            'product_link': 'chosen' if source and source.get('_chosen') else 'automatic',
            'image': image_match['image'] if image_match else product_image(row.get('manufacturer'), row.get('model') or '', row.get('model_id')),
            'fallback_image': device_icon(row.get('state_class')),
            **{k:v for k,v in (image_match or {}).items() if k!='image'}})
    result=sorted(result,key=lambda r:r['name'].casefold())
    if include_products:return result,products.public_inventory(physical),rows
    return result


def selection_inventory(inventory, selected, entries=()):
    """Keep paired missing/unsupported speakers visible as offline, never addable."""
    native={row['entity_id'] for row in inventory if row.get('excluded') and
        (not row.get('exclusion_reason') or row['exclusion_reason'].startswith('Native UniFi'))}
    selected=[entity for entity in selected if entity not in native]
    rows=[row for row in inventory if not row.get('excluded')]
    lookup={row['entity_id']:row for row in rows}
    original={row['entity_id']:row for row in inventory}
    saved={member:entry for entry in entries if entry['active'] for member in entry.get('members',[entry['entity_id']])}
    for entity in selected:
        if entity not in lookup:
            entry=saved.get(entity,{})
            row={**original.get(entity,{}),'entity_id':entity,'name':original.get(entity,{}).get('name',entity),
                 'available':False,'supported':False,'group':entry.get('members',[])}
            rows.append(row)
    return rows,selected


def validate_selection(entities,rows,*,existing=None):
    if (not isinstance(entities,list) or len(entities)>16 or len(set(entities))!=len(entities)
            or any(not isinstance(e,str) for e in entities)):
        raise ValueError('Choose up to 16 distinct speakers.')
    lookup={r['entity_id']:r for r in rows};previous=set(existing or [])
    for entity in entities:
        if entity not in lookup or (entity not in previous and (lookup[entity].get('available') is False or lookup[entity].get('supported') is False)):
            raise ValueError('Only an online supported speaker can be added.')
    return entities


def selected(options):
    if SELECTION_PATH.exists():
        payload = json.loads(SELECTION_PATH.read_text())
        values = payload['entities']
        if not isinstance(values, list) or len(values) > 16 or any(not isinstance(v, str) or not re.fullmatch(r'media_player\.[a-z0-9_]+', v) for v in values):
            raise ValueError('The saved speaker selection is invalid.')
        return values
    return [options['speaker_entity']] if options.get('speaker_entity') else []


def save(entities, *, path=None):
    path=Path(path) if path is not None else SELECTION_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'-'+secrets.token_hex(8)+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump({'entities':entities},stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)
