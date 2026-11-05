"""Model-matched UniFi client artwork; bounded downloads into the app backup data."""
import json
import os
from pathlib import Path
import re
import secrets
import struct
import threading
import unicodedata
from functools import lru_cache
from urllib.request import Request,ProxyHandler,HTTPRedirectHandler,build_opener

CATALOG=Path(__file__).with_name('assets')/'unifi-products.json'
MAX_IMAGE_BYTES=256*1024
_LOCK=threading.Lock()
# Explicit visual substitutes only; never infer an older model by substring.
SUBSTITUTES={('sonos','onesl'):'293',('sonos','beamgen2'):'2674',('sonos','beam2ndgen'):'2674',
    ('apple','tv'):'14',('google','chromecast'):'39'}
# Apple calls the fourth-generation Apple TV the Apple TV HD (A1625).
# https://support.apple.com/en-us/101605
ALIASES={('sonos','one2ndgen'):'onegen2',
    ('apple','tv4'):'tvhd',('apple','tv4thgen'):'tvhd',
    ('apple','tv4thgeneration'):'tvhd',('apple','a1625'):'tvhd'}


def normalized(value):
    if not isinstance(value,str):return ''
    return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',value).casefold())

def manufacturer(value):
    if not isinstance(value,str):return ''
    return normalized(re.sub(r'(?:[ ,]+(?:incorporated|inc\.?|limited|ltd\.?|llc|corporation|corp\.?))+$','',value.casefold().strip()))

@lru_cache(maxsize=1)
def catalog():
    try:
        data=json.loads(CATALOG.read_text())
        devices=data['dev_ids'];vendors=data['vendor_ids']
        if not isinstance(devices,dict) or not isinstance(vendors,dict):return ({},{})
        index={};valid={}
        for key,row in devices.items():
            if not re.fullmatch(r'[0-9]{1,10}',key) or not isinstance(row,dict):continue
            maker=manufacturer(vendors.get(str(row.get('vendor_id')),''))
            name=row.get('name','');model=normalized(name)
            if not maker or not model:continue
            if model.startswith(maker):model=model[len(maker):]
            index.setdefault((maker,model),[]).append(key)
            valid[key]=name
        return index,valid
    except (OSError,ValueError,KeyError,TypeError):return ({},{})

def lookup(maker,model,model_id=''):
    maker=manufacturer(maker)
    if not maker:return None
    models=[]
    for value in (model,model_id):
        value=normalized(value)
        if value.startswith(maker):value=value[len(maker):]
        value=ALIASES.get((maker,value),value)
        if value and value not in models:models.append(value)
    index,valid=catalog()
    exact={key for value in models for key in index.get((maker,value),[])}
    if len(exact)==1:
        key=exact.pop();return {'image':'assets/unifi-'+key+'.png','image_model':valid[key],'image_is_fallback':False,'image_source':'UniFi'}
    if exact:return None # An ambiguous exact match must not become a guessed model.
    fallback={SUBSTITUTES[(maker,value)] for value in models if (maker,value) in SUBSTITUTES}
    if len(fallback)==1:
        key=fallback.pop()
        if key in valid:return {'image':'assets/unifi-'+key+'.png','image_model':valid[key],'image_is_fallback':True,'image_source':'UniFi'}
    # Search the same manufacturer's catalogue for the product family before
    # using a generic icon. Strip only generation suffixes, never arbitrary
    # words, model numbers or capability markers such as 4K, Pro or Audio.
    def family(value):
        return re.sub(r'(?:\d+(?:st|nd|rd|th)?(?:generation|gen)|gen\d+)$','',value)
    related={key for (vendor,value),keys in index.items() if vendor==maker
        for model in models if family(model)==family(value)
        for key in keys}
    if len(related)==1:
        key=related.pop()
        return {'image':'assets/unifi-'+key+'.png','image_model':valid[key],
            'image_is_fallback':True,'image_source':'UniFi','image_match':'related_generation'}
    return None

def lookup_description(value):
    if not isinstance(value,str):return None
    words=re.findall(r'[a-z0-9]+',value.casefold())
    if not words:return None
    index,valid=catalog()
    vendors={maker for maker,_ in index}
    brands=[v for v in vendors if normalized(words[0])==v]
    if len(brands)!=1:return None
    brand=brands[0]
    codes={w for w in words[1:] if len(w)>=3 and re.search('[a-z]',w) and re.search('[0-9]',w)}
    if not codes:return None
    candidates={key for (maker,model),keys in index.items() if maker==brand
        for key in keys if codes & set(re.findall(r'[a-z0-9]+',valid[key].casefold()))}
    if len(candidates)!=1:return None
    key=candidates.pop()
    return {'image':'assets/unifi-'+key+'.png','image_model':valid[key],
        'image_is_fallback':True,'image_source':'UniFi','image_match':'product_model_code'}


def lookup_device(row):
    """Use rich device metadata without borrowing names from unrelated hub devices."""
    primary=lookup(row.get('manufacturer'),row.get('model'),row.get('model_id'))
    if primary and not primary['image_is_fallback']:return primary
    candidates=[('device_name',row.get('manufacturer'),row.get('device_name'),row.get('model_id'))]
    # parent_device_id represents a component of the same physical product.
    # via_device_id is connectivity (such as a hub), and is deliberately not used.
    if row.get('parent_device_id'):
        candidates.extend([
            ('parent_device',row.get('parent_manufacturer'),row.get('parent_model'),row.get('parent_model_id')),
            ('parent_device',row.get('parent_manufacturer'),row.get('parent_name'),row.get('parent_model_id')),
        ])
    for source,maker,model,model_id in candidates:
        if not model:continue
        # HA may append its separately supplied model identifier to a display name.
        if model_id is not None:
            suffix=' ('+str(model_id)+')'
            if isinstance(model,str) and model.endswith(suffix):model=model[:-len(suffix)]
        match=lookup(maker,model,model_id)
        if match and not match['image_is_fallback']:return {**match,'image_metadata_source':source}
        if primary is None and match:primary={**match,'image_metadata_source':source}
    if primary:return primary
    for value in (row.get('model'),row.get('device_name')):
        match=lookup_description(value)
        if match:return {**match,'image_metadata_source':'product_description'}
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def _png(data):
    if (not 24<=len(data)<=MAX_IMAGE_BYTES or data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR'):
        raise ValueError('Invalid product image')
    width,height=struct.unpack('>II',data[16:24])
    if not (1<=width<=257 and 1<=height<=257):raise ValueError('Oversized product image')
    return data

class ImageCache:
    def __init__(self,directory):self.directory=Path(directory)
    def get(self,key):
        if not isinstance(key,str) or not re.fullmatch(r'[0-9]{1,10}',key) or key not in catalog()[1]:
            raise ValueError('Unknown UniFi artwork identifier')
        with _LOCK:
            path=self.directory/('unifi-'+key+'.png')
            if self.directory.is_symlink() or path.is_symlink():raise ValueError('Invalid image cache path')
            if path.exists():
                try:return _png(path.read_bytes())
                except ValueError:path.unlink()
            # Fixed HTTPS destination, no redirects, no controller credentials.
            request=Request('https://static.ui.com/fingerprint/0/'+key+'_129x129.png')
            opener=build_opener(ProxyHandler({}),NoRedirect())
            with opener.open(request,timeout=4) as response:
                if response.headers.get_content_type()!='image/png':raise ValueError('Product image unavailable')
                data=_png(response.read(MAX_IMAGE_BYTES+1))
            self.directory.mkdir(parents=True,exist_ok=True)
            temporary=self.directory/(path.name+'-'+secrets.token_hex(8)+'.tmp')
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(data)
                os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
            return data
