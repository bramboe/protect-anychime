"""Validate discoverable repository layout and mandatory security boundaries."""
import json
from pathlib import Path
import unittest

ADDON=Path(__file__).resolve().parents[1]
REPOSITORY=ADDON.parent

class ReleaseLayoutTests(unittest.TestCase):
    def test_repository_contains_installable_subfolder_and_license(self):
        self.assertIn('name: Protect AnyChime',(REPOSITORY/'repository.yaml').read_text())
        self.assertTrue((ADDON/'Dockerfile').is_file())
        self.assertEqual(ADDON.name,'protect_anychime')
        self.assertEqual(json.loads((ADDON/'config.json').read_text())['slug'],'protect_anychime')
        self.assertIn('MIT License',(REPOSITORY/'LICENSE').read_text())
        self.assertFalse((REPOSITORY/'config.json').exists())
        self.assertFalse((REPOSITORY/'PUBLICATION.md').exists())
        self.assertFalse((REPOSITORY/'VERIFICATION-0.4.0.md').exists())

    def test_profile_is_enforced_and_does_not_grant_unbounded_file_or_execute(self):
        config=json.loads((ADDON/'config.json').read_text())
        self.assertIs(config['apparmor'],True)
        self.assertEqual(config['privileged'],['NET_ADMIN','NET_RAW'])
        self.assertFalse(config.get('full_access',False))
        self.assertFalse(config.get('docker_api',False))
        profile=(ADDON/'apparmor.txt').read_text()
        self.assertNotIn('complain',profile)
        self.assertNotIn('\n  file,',profile)
        self.assertNotIn('/** rw',profile.replace('/data/** rwk','').replace('/tmp/** rwk',''))
        self.assertNotIn('/bin/** ix',profile)
        self.assertIn('/app/** r,',profile)
