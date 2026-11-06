import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import server


class StartupTests(unittest.TestCase):
    def test_busy_web_port_never_starts_native_services(self):
        with patch.object(server, "ThreadingHTTPServer", side_effect=OSError(98, "Address in use")), \
                patch.object(server, "NATIVE") as native:
            with self.assertRaises(OSError):
                server.run_server()
            native.start.assert_not_called()

    def test_normal_exit_stops_native_and_releases_web_port(self):
        http = Mock()
        with patch.object(server, "ThreadingHTTPServer", return_value=http), \
                patch.object(server, "load_options", return_value={}), \
                patch.object(server, "NATIVE") as native:
            server.run_server()
            native.start.assert_called_once_with({})
            native.stop.assert_called_once()
            http.server_close.assert_called_once()

    def test_ingress_configuration_matches_actual_listener(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        self.assertEqual(config["ingress_port"], server.DEFAULT_PORT)
        self.assertEqual(config["version"], server.VERSION)
        self.assertNotEqual(server.DEFAULT_PORT, 8099)


if __name__ == "__main__":
    unittest.main()
