"""Announcement requests use mocked HA only; never access a real speaker."""
import tempfile
import unittest
from unittest.mock import Mock
from audio import SpeakerBackend, SpeakerError, MEDIA_ANNOUNCE
from registry import ChimeRegistry
from playback import TrackPlayer
from speakers import discover

MEMBERS=['media_player.a','media_player.b']

class AnnouncementTests(unittest.TestCase):
    def backend(self, *, sonos=True, grouped=True, offline=False, unsupported=False, fail=False):
        calls=[]
        def request(method,path,body):
            calls.append((method,path,body))
            if method=='GET':
                entity=path.rsplit('/',1)[1]
                return {'entity_id':entity,'state':'unavailable' if offline and entity==MEMBERS[1] else 'playing',
                    'attributes':{'supported_features':516 if unsupported and entity==MEMBERS[1] else 516|MEDIA_ANNOUNCE,
                        'volume_level':.31,'group_members':MEMBERS if grouped else [MEMBERS[0]]}}
            if fail:raise SpeakerError('Synthetic request failure')
            return []
        backend=SpeakerBackend({'speaker_entity':MEMBERS[0],'audio_url':'http://example.test/ring.mp3',
            'group_members':MEMBERS if grouped else [MEMBERS[0]],
            'member_platforms':{e:'sonos' if sonos else 'other' for e in MEMBERS}},
            requester=request,allow_playback=True)
        return backend,calls

    def ring(self,backend):
        return backend.play_once(backend.arm_once(confirmed=True,volume=67)['approval_id'])

    def test_sonos_group_announces_to_members_without_modifying_music_or_group(self):
        backend,calls=self.backend()
        result=self.ring(backend)
        posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][1],'/services/media_player/play_media')
        self.assertEqual(posts[0][2]['entity_id'],MEMBERS)
        self.assertEqual(posts[0][2]['extra'],{'volume':67})
        self.assertTrue(posts[0][2]['announce'])
        self.assertEqual(result['mode'],'group_announce')
        self.assertFalse(result['may_interrupt'])

    def test_sonos_single_uses_clip_volume_and_does_not_change_music_volume(self):
        backend,calls=self.backend(grouped=False)
        self.ring(backend)
        posts=[c for c in calls if c[0]=='POST']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][2]['entity_id'],MEMBERS[0])
        self.assertEqual(posts[0][2]['extra'],{'volume':67})

    def test_generic_group_keeps_coordinator_semantics(self):
        backend,calls=self.backend(sonos=False)
        self.ring(backend)
        self.assertEqual(calls[-1][2]['entity_id'],MEMBERS[0])
        self.assertTrue(calls[-1][2]['announce'])
        self.assertEqual(sum(c[0]=='POST' for c in calls),1)

    def test_offline_member_is_not_announced_to_or_woken(self):
        backend,calls=self.backend(offline=True)
        self.ring(backend)
        self.assertEqual(calls[-1][2]['entity_id'],MEMBERS[0])
        self.assertEqual(sum(c[0]=='POST' for c in calls),1)

    def test_unsupported_sonos_member_prevents_partial_playback(self):
        backend,calls=self.backend(unsupported=True)
        with self.assertRaises(SpeakerError):self.ring(backend)
        self.assertFalse(any(c[0]=='POST' for c in calls))

    def test_failed_announcement_never_retries_with_destructive_plain_playback(self):
        backend,calls=self.backend(fail=True)
        with self.assertRaises(SpeakerError):self.ring(backend)
        self.assertEqual(sum(c[0]=='POST' for c in calls),1)
        self.assertTrue(calls[-1][2]['announce'])

    def test_platform_metadata_reaches_group_mapping_and_player_options(self):
        rows=discover(Mock(return_value=[{'entity_id':e,'name':e,'state':'playing','features':516|MEDIA_ANNOUNCE,
            'integration':'sonos','group':MEMBERS} for e in MEMBERS]))
        with tempfile.TemporaryDirectory() as directory:
            registry=ChimeRegistry(directory)
            entry=next(e for e in registry.sync(MEMBERS,rows) if e['active'])
            self.assertEqual(entry['member_platforms'],{e:'sonos' for e in MEMBERS})
            factory=Mock();factory.return_value.play_once.return_value={'ha_call_accepted':True,'mode':'group_announce','may_interrupt':False}
            factory.return_value.arm_once.return_value={'approval_id':'test-grant'}
            player=TrackPlayer('primary',entry['playback_entity'],None,'http://example.test',
                group_members=entry['members'],member_platforms=entry['member_platforms'],backend_factory=factory)
            player._run({'path':'unused','track_no':1,'md5':'test'},67,1)
            self.assertEqual(factory.call_args.args[0]['member_platforms'],entry['member_platforms'])
            self.assertEqual(player.status()['last_result']['mode'],'group_announce')
            # Missing metadata refresh must not discard the known integration.
            retained=next(e for e in registry.sync(MEMBERS) if e['active'])
            self.assertEqual(retained['member_platforms'],entry['member_platforms'])
