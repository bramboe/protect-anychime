import threading
import unittest
from unittest.mock import patch
from discovery import announcement, parse, PassiveResponder, PROBE, PORT

MAC = "02:52:53:00:00:01"
IP = "10.23.45.20"


class DiscoveryTests(unittest.TestCase):
    def test_candidate_profile(self):
        fields = dict(parse(announcement(MAC, IP)))
        self.assertEqual(fields[12], b"UP Chime PoE")
        self.assertEqual(fields[23], b"\x01")
        self.assertEqual(fields[15], b"\x00\x01\x1f\x90")
        self.assertEqual(fields[2], bytes.fromhex("0252530000010a172d14"))
        self.assertNotIn(16, fields)
        self.assertNotIn(43, fields)

    def test_adopted_flag(self):
        fields = dict(parse(announcement(MAC, IP, adopted=True)))
        self.assertEqual(fields[23], b"\x00")

    def test_invalid_packets(self):
        packet = announcement(MAC, IP)
        for invalid in [b"", packet[:3], packet[:-1], packet + b"x", b"\x02" + packet[1:],
                        b"\x01\x00\x00\x02\x0b\x00"]:
            with self.subTest(invalid=invalid[:8]), self.assertRaises(ValueError):
                parse(invalid)

    def test_identity_constraints(self):
        for value in ["90:41:B2:58:E9:7A", "03:52:53:00:00:01", "invalid"]:
            with self.assertRaises(ValueError):
                announcement(value, IP)

    def test_passive_allowlist(self):
        responder = PassiveResponder(IP, ["10.23.45.1"], lambda: announcement(MAC, IP))
        self.assertIsNone(responder.response_to(PROBE, ("10.23.45.2", 42000)))
        self.assertIsNone(responder.response_to(PROBE, ("10.23.45.1", PORT)))
        self.assertIsNone(responder.response_to(PROBE + b"x", ("10.23.45.1", 42000)))
        self.assertEqual(responder.response_to(PROBE, ("10.23.45.1", 42000)), announcement(MAC, IP))
        self.assertEqual(responder.probes, 1)

    def test_no_socket_before_explicit_serve(self):
        with patch("discovery.socket.socket") as socket_factory:
            responder = PassiveResponder(IP, ["10.23.45.1"], lambda: announcement(MAC, IP))
            responder.response_to(PROBE, ("10.23.45.1", 42000))
            socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
