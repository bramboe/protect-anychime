"""Synthetic test inputs only; no deployed certificates or private keys are bundled."""
from functools import lru_cache
from pathlib import Path
import subprocess
import tempfile

@lru_cache(maxsize=4)
def certificate(label='test-console.invalid'):
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout',str(path/'key.pem'),'-out',str(path/'cert.pem'),'-days','1',
            '-subj','/CN='+label],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
        return (path/'cert.pem').read_text()

# Header-valid synthetic bytes for storage and HTTP tests, never played/decoded.
SYNTHETIC_MP3=b'\xff\xfb\xe0\xc0'+b'\0'*2044
_AUDIO_TEMP=None

@lru_cache(maxsize=1)
def ringtone_file():
    global _AUDIO_TEMP
    _AUDIO_TEMP=tempfile.TemporaryDirectory()
    path=Path(_AUDIO_TEMP.name)/'synthetic.mp3'
    path.write_bytes(SYNTHETIC_MP3)
    return path

def seeded_store(directory):
    from ringtones import RingtoneStore
    store=RingtoneStore(directory)
    for track,name in enumerate(['Default','Traditional','Sundrops','Express-Line'],1):
        store.upload(track,name+'.mp3',SYNTHETIC_MP3)
    return store
