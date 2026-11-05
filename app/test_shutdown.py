import unittest
from unittest.mock import Mock,patch
import server

class ShutdownTests(unittest.TestCase):
    def test_signal_requests_shutdown_once_outside_server_thread_and_restores_handlers(self):
        http=Mock();old=object()
        with patch('server.signal.getsignal',return_value=old),patch('server.signal.signal') as signals,patch('server.threading.Thread') as thread:
            previous=server.install_shutdown_handlers(http)
            callback=signals.call_args_list[0].args[1]
            callback(server.signal.SIGTERM,None);callback(server.signal.SIGINT,None)
            thread.assert_called_once_with(target=http.shutdown,daemon=True,name='anychime-shutdown')
            thread.return_value.start.assert_called_once()
            server.restore_shutdown_handlers(previous)
            self.assertEqual(signals.call_args_list[-2].args,(server.signal.SIGTERM,old))
            self.assertEqual(signals.call_args_list[-1].args,(server.signal.SIGINT,old))

    def test_worker_thread_does_not_modify_global_signal_handlers(self):
        with patch('server.threading.current_thread',return_value=object()),patch('server.signal.signal') as setter:
            self.assertEqual(server.install_shutdown_handlers(Mock()),{})
            setter.assert_not_called()
