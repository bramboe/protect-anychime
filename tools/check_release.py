"""Check a source tree or extracted release before publishing. No network access."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_NAMES={'options.json','managed-settings.json','speakers.json','chimes.json','identity.json',
    'tracks.json','latency-calibration.json','latency-delays.json','chime-layout.json','product-links.json','protect-management.json','protect-management-trust.json','configuration-before-managed-settings.json'}
BLOCKED_SUFFIXES={'.pem','.key','.mp3','.ogg','.wav','.log','.pyc','.pyo'}

def inspect(root):
    failures=[]
    for path in root.rglob('*'):
        if path.is_symlink():
            failures.append((str(path.relative_to(root)),'symlink'));continue
        if not path.is_file() or '.git' in path.relative_to(root).parts:continue
        rel=str(path.relative_to(root))
        if path.name in PRIVATE_NAMES or path.suffix.lower() in BLOCKED_SUFFIXES or path.name.startswith('.env') or path.name=='.DS_Store':
            failures.append((rel,'runtime, recording or generated artifact'))
        if path.suffix in {'.py','.md','.html','.json','.txt'}:
            text=path.read_text(encoding='utf-8')
            if any(marker in text for marker in ['-----BEGIN '+'CERTIFICATE-----','-----BEGIN '+'PRIVATE KEY-----','-----BEGIN '+'RSA PRIVATE KEY-----']):
                failures.append((rel,'embedded certificate or key'))
            if any(marker in text for marker in ['/'+'Users'+'/', '/'+'Volumes'+'/']):
                failures.append((rel,'developer-specific filesystem path'))
    return failures

if __name__=='__main__':
    issues=inspect(Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT)
    for path,reason in issues:print(path+': '+reason)
    if issues:raise SystemExit(1)
    print('Release hygiene check passed: no bundled certificates, recordings or runtime state.')
