import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,AsyncMock,patch
import speakers
from registry import ChimeRegistry
from runtime import NativeWorker

A='media_player.a';B='media_player.b'
def row(entity,online,group=None):return {'entity_id':entity,'name':entity,'available':online,'group':group or []}

class AvailabilityTests(unittest.TestCase):
    def test_offline_add_is_rejected_but_existing_pair_can_be_retained_or_removed(self):
        rows=[row(A,False),row(B,True)]
        with self.assertRaisesRegex(ValueError,'Offline'):speakers.validate_selection([A],rows)
        self.assertEqual(speakers.validate_selection([A,B],rows,existing=[A]),[A,B])
        self.assertEqual(speakers.validate_selection([],rows,existing=[A]),[])
        rows[0]['available']=True;self.assertEqual(speakers.validate_selection([A],rows),[A])

    def test_group_requires_online_coordinator_but_allows_partial_member_outage(self):
        rows=[row(A,False,[A,B]),row(B,True,[A,B])]
        with self.assertRaisesRegex(ValueError,'Offline'):speakers.validate_selection([B],rows)
        self.assertEqual(speakers.validate_selection([A,B],rows,existing=[A,B]),[A,B])
        rows[0]['available']=True;rows[1]['available']=False
        self.assertEqual(speakers.validate_selection([A],rows),[A,B])

    def test_paired_missing_entity_is_preserved_as_offline(self):
        entry={'entity_id':A,'members':[A,B],'active':True}
        rows,selected=speakers.selection_inventory([row(B,True,[A,B])],[A,B],[entry])
        self.assertEqual(selected,[A,B]);self.assertFalse(next(r for r in rows if r['entity_id']==A)['available'])
        self.assertEqual(speakers.visual_groups(rows),[{'members':[A,B]}])
        self.assertEqual(speakers.validate_selection([A,B],rows,existing=[A,B]),[B,A])

class OfflineChimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_disconnects_without_deleting_identity_then_returns_with_same_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);registry=ChimeRegistry(root);registry.sync([A],[row(A,True)])
            (root/'native').mkdir();marker=root/'native/identity.json';marker.write_text('preserved-identity')
            native=Mock();native.stop=AsyncMock();player=Mock()
            worker=NativeWorker();worker.registry=registry;worker.network=Mock();worker.options={'native_ip':'10.23.45.20'}
            worker.nodes={'primary':{'native':native,'player':player,'ip':'10.23.45.20'}}
            worker.refresh_groups([row(A,False)])
            with patch('runtime.NativeChime') as factory:await worker._reconcile(root)
            factory.assert_not_called();native.stop.assert_awaited_once();player.stop.assert_called_once()
            self.assertEqual(worker.status()['chimes'][0]['state'],'offline')
            self.assertEqual(worker.status()['selected_entities'],[A]);self.assertEqual(worker._descriptors(),[])
            self.assertEqual(marker.read_text(),'preserved-identity');self.assertTrue(registry.snapshot()[0]['active'])
            worker.refresh_groups([row(A,True)])
            worker.network.ensure.return_value={'state':'ready','ip':'10.23.45.40'}
            returned=Mock();returned.state={'mac':'02AABBCCDDEE'};returned.listen_port=41000;returned.options={};returned.start=AsyncMock();returned.connected=True;returned.removal_requested=False
            returned.status.return_value={'connected':True};returned.discovery_response.return_value=b'identity'
            with patch('runtime.NativeChime',return_value=returned),patch('runtime.TrackPlayer'):
                await worker._reconcile(root)
            returned.start.assert_awaited_once();self.assertEqual(marker.read_text(),'preserved-identity')
            self.assertEqual(worker.status()['connected_count'],1)
            self.assertEqual(worker.status()['selected_entities'],[A])
