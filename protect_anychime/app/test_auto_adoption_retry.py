"""Automatic adoption retries use fake transports and a controlled clock."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from runtime import NativeWorker
from management import ManagementError

class AdoptionRetryTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, error):
        worker=NativeWorker();worker.options={'auto_adopt_chimes':True}
        worker.nodes={key:{'native':SimpleNamespace(state={'device_id':key,'adopted':False})} for key in ('a','b')}
        worker._entry_enabled=Mock(return_value=True)
        worker.auto_adopter=Mock(side_effect=error)
        return worker

    async def poll(self,worker,now):
        with patch('runtime.time.monotonic',return_value=now),patch('builtins.print') as log:
            await worker._auto_adopt_new_chimes()
        return log

    async def test_rate_limit_pauses_other_chimes_and_preserves_attempt_budget(self):
        worker=self.worker(ManagementError('private upstream text',kind='rate_limit',http_status=429))
        log=await self.poll(worker,100)
        self.assertEqual(worker.auto_adopter.call_count,1)
        self.assertEqual(worker._auto_adopt_state['a']['attempts'],0)
        self.assertGreaterEqual(worker._configuration_after,160)
        self.assertIn('HTTP 429',log.call_args.args[0]);self.assertNotIn('private upstream text',log.call_args.args[0])
        await self.poll(worker,159);self.assertEqual(worker.auto_adopter.call_count,1)
        await self.poll(worker,160);self.assertEqual(worker.auto_adopter.call_count,2)
        self.assertEqual(worker._auto_adopt_state['a']['attempts'],0)
        worker.auto_adopter.side_effect=None;worker.auto_adopter.return_value={}
        await self.poll(worker,280)
        self.assertEqual(worker._auto_adopt_state['a']['attempts'],1)
        self.assertNotIn('error',worker._auto_adopt_state['a'])

    async def test_temporary_failures_never_exhaust_budget(self):
        for error in (TimeoutError(),ManagementError('server',kind='http_error',http_status=503),ManagementError('network',kind='network')):
            worker=self.worker(error)
            for now in (100,160,280,520):await self.poll(worker,now)
            self.assertEqual(worker._auto_adopt_state['a']['attempts'],0)
            self.assertEqual(worker._auto_adopt_state['a']['temporary_failures'],4)

    async def test_permanent_access_errors_keep_bounded_attempts(self):
        worker=self.worker(ManagementError('refused',kind='authentication',http_status=403))
        for now in (100,130,160,190):await self.poll(worker,now)
        self.assertEqual(worker._auto_adopt_state['a']['attempts'],3)
        self.assertEqual(worker.auto_adopter.call_count,6)

    async def test_removed_and_adopted_chimes_are_not_retried(self):
        worker=self.worker(TimeoutError());worker._entry_enabled.return_value=False
        await self.poll(worker,100);worker.auto_adopter.assert_not_called()
        worker.nodes['a']['native'].state['adopted']=True
        worker._auto_adopt_state={'a':{'attempts':3}}
        await self.poll(worker,200);self.assertNotIn('a',worker._auto_adopt_state)
