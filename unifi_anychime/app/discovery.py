"""Bounded UniFi discovery codec and explicit-start, passive chime responder.

Derived from Protect 7.3.70 modules 813701 / 509120 / 861735 / 383985.
No network activity occurs on import. There are no unsolicited announcements.
"""
import ipaddress
import re
import socket
import struct

PORT = 10001
MULTICAST_ADDRESS = "233.89.188.1"
PROBE = b"\x01\x00\x00\x00"
MAX_PACKET = 2048


def mac_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{12}", value.replace(":", "")):
        raise ValueError("Invalid MAC address")
    data = bytes.fromhex(value.replace(":", ""))
    # A virtual device must have its own unicast, locally administered identity.
    if data[0] & 3 != 2:
        raise ValueError("Virtual device requires a locally administered unicast MAC")
    return data


def tlv(kind, payload):
    if not 0 <= kind <= 255 or len(payload) > MAX_PACKET - 7:
        raise ValueError("Invalid discovery TLV")
    return struct.pack(">BH", kind, len(payload)) + payload


def parse(packet):
    """Parse one complete VERSION=1 / CMD_INFO=0 datagram without truncation."""
    if not 4 <= len(packet) <= MAX_PACKET:
        raise ValueError("Invalid discovery packet size")
    version, command, size = struct.unpack(">BBH", packet[:4])
    if version != 1 or command != 0 or size != len(packet) - 4:
        raise ValueError("Invalid discovery header")
    result = []
    offset = 4
    while offset < len(packet):
        if offset + 3 > len(packet):
            raise ValueError("Truncated discovery TLV header")
        kind, size = struct.unpack(">BH", packet[offset:offset + 3])
        offset += 3
        if offset + size > len(packet):
            raise ValueError("Truncated discovery TLV body")
        result.append((kind, packet[offset:offset + size]))
        offset += size
    return result


def announcement(mac, ip, *, name="Protect AnyChime", firmware="1.3.7",
                 adopted=False, device_id=None, default_credentials=None):
    """Candidate PoE chime announcement. Native discovery acceptance unproven.

    No hardware sysid/GUID is invented. The known canonical platform selects
    chime. Firmware is an emulated protocol profile, not installed vendor code.
    Default credential version is omitted unless the caller explicitly supplies it.
    """
    hardware = mac_bytes(mac)
    address = ipaddress.IPv4Address(ip)
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise ValueError("Expected a LAN IPv4 address")
    if not isinstance(name, str) or not 1 <= len(name.encode()) <= 128:
        raise ValueError("Invalid virtual device name")
    if not isinstance(firmware, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:\+[-\w.]+)?", firmware):
        raise ValueError("Invalid emulated firmware version")
    ipinfo = hardware + address.packed
    fields = [(2, ipinfo), (47, ipinfo), (1, hardware),
              (11, name.encode()), (12, b"UP Chime PoE"),
              (14, b"\x00"), (13, b""), (3, firmware.encode()),
              (15, struct.pack(">HH", 1, 8080)),
              (23, b"\x00" if adopted else b"\x01"),
              (63, b"\x01")]
    if device_id:
        if not isinstance(device_id, str) or len(device_id.encode()) > 128:
            raise ValueError("Invalid device ID")
        fields.append((32, device_id.encode()))
    if default_credentials is not None:
        if type(default_credentials) is not int or not 0 <= default_credentials <= 255:
            raise ValueError("Invalid default credential version")
        fields.append((44, bytes([default_credentials])))
    payload = b"".join(tlv(kind, value) for kind, value in fields)
    packet = struct.pack(">BBH", 1, 0, len(payload)) + payload
    if len(packet) > MAX_PACKET:
        raise ValueError("Discovery response too large")
    return packet


class PassiveResponder:
    """Replies only to allowed controllers' exact info probes.

    The response factory can reflect current adopted state. `serve()` is an
    explicit long-running operation; caller owns its stop event and thread.
    Port conflicts fail, rather than joining an unrelated discovery listener.
    """
    def __init__(self, lan_ip, controller_ips, response_factory):
        self.lan_ip = str(ipaddress.IPv4Address(lan_ip))
        self.controller_ips = {str(ipaddress.IPv4Address(value)) for value in controller_ips}
        if not self.controller_ips:
            raise ValueError("At least one controller must be explicitly allowed")
        self.response_factory = response_factory
        self.probes = 0
        self.responses = 0

    def response_to(self, packet, peer):
        if (peer[0] not in self.controller_ips or peer[1] == PORT
                or packet != PROBE):
            return None
        response = self.response_factory()
        parse(response)
        self.probes += 1
        return response

    def serve(self, stop_event):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as transport:
            transport.bind(("0.0.0.0", PORT))
            membership = socket.inet_aton(MULTICAST_ADDRESS) + socket.inet_aton(self.lan_ip)
            transport.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            transport.settimeout(0.5)
            while not stop_event.is_set():
                try:
                    packet, peer = transport.recvfrom(MAX_PACKET + 1)
                except socket.timeout:
                    continue
                response = self.response_to(packet, peer)
                if response is not None:
                    transport.sendto(response, peer)
                    self.responses += 1


class MultiResponder:
    """One passive listener; each advertised identity responds from its own IP."""
    def __init__(self, lan_ip, controller_ips, descriptor_factory):
        self.lan_ip = str(ipaddress.IPv4Address(lan_ip))
        self.controllers = set(controller_ips)
        self.descriptors = descriptor_factory
        self.responses = 0
        self.last_probe = {}
        self.probe_sent = {}

    def responses_to(self, packet, peer, now):
        if packet != PROBE or peer[0] not in self.controllers or peer[1] == PORT:
            return []
        if now - self.last_probe.get(peer, float('-inf')) < 1:
            return []
        self.last_probe = {p:t for p,t in self.last_probe.items() if now-t<5}
        self.last_probe[peer] = now
        self.probe_sent={p:keys for p,keys in self.probe_sent.items() if p in self.last_probe}
        self.probe_sent[peer]=set()
        result=[]
        for descriptor in self.descriptors()[:16]:
            fields=dict(parse(descriptor['packet']))
            ip=str(ipaddress.IPv4Address(descriptor['ip']))
            if fields[47][-4:] != socket.inet_aton(ip):
                raise ValueError('Discovery address does not match its source')
            result.append((ip,descriptor['packet']))
            self.probe_sent[peer].add(fields[1])
        return result

    def delayed_responses(self, now):
        """Finish a recent authorised scan when a newly added identity becomes ready."""
        result=[]
        for peer,at in list(self.last_probe.items()):
            if now-at>=4:continue
            sent=self.probe_sent.setdefault(peer,set())
            for descriptor in self.descriptors()[:16]:
                fields=dict(parse(descriptor['packet']))
                ip=str(ipaddress.IPv4Address(descriptor['ip']))
                if fields[47][-4:]!=socket.inet_aton(ip):raise ValueError('Discovery source mismatch')
                if fields[1] in sent:continue
                sent.add(fields[1]);result.append((peer,ip,descriptor['packet']))
        return result

    def serve(self, stop_event):
        import time
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as listener:
            listener.bind(('0.0.0.0',PORT))
            listener.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(MULTICAST_ADDRESS)+socket.inet_aton(self.lan_ip))
            listener.settimeout(.5)
            while not stop_event.is_set():
                try:
                    packet,peer=listener.recvfrom(MAX_PACKET+1)
                except socket.timeout:
                    packet=None
                outgoing=[]
                if packet is not None:
                    outgoing.extend((peer,source,response) for source,response in self.responses_to(packet,peer,time.monotonic()))
                outgoing.extend(self.delayed_responses(time.monotonic()))
                for destination,source,response in outgoing:
                    if stop_event.is_set():
                        break
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as outbound:
                        outbound.bind((source,0))
                        outbound.sendto(response,destination)
                    self.responses+=1
                    if stop_event.wait(.04):
                        break
