"""Independent bounded UCP4 framing implementation. No network or vendor imports."""
import json
import struct
import zlib

LIMIT = 2 * 1024 * 1024


def _payload(value):
    if isinstance(value, bytes):
        return 3, value
    if isinstance(value, str):
        return 2, value.encode()
    return 1, json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def encode(header, body, compress=False):
    blocks = []
    for kind, value in [(1, header), (2, body)]:
        fmt, data = _payload(value)
        if len(data) > LIMIT:
            raise ValueError("UCP payload too large")
        if compress:
            data = zlib.compress(data)
        if len(data) > LIMIT:
            raise ValueError("UCP encoded payload too large")
        blocks.append(struct.pack(">BBBBI", kind, fmt, int(compress), 0, len(data)) + data)
    return b"".join(blocks)


class Decoder:
    """Accepts fragmented or coalesced messages, including compressed payloads."""

    def __init__(self):
        self.buffer = bytearray()
        self.header = None

    def feed(self, data):
        if len(self.buffer) + len(data) > 4 * LIMIT + 32:
            raise ValueError("UCP receive buffer too large")
        self.buffer.extend(data)
        messages = []
        while len(self.buffer) >= 8:
            kind, fmt, compressed, reserved, size = struct.unpack(">BBBBI", self.buffer[:8])
            if kind != (1 if self.header is None else 2):
                raise ValueError("Unexpected UCP block order")
            if fmt not in (1, 2, 3) or compressed not in (0, 1) or reserved != 0:
                raise ValueError("Unsupported UCP block metadata")
            if size > LIMIT:
                raise ValueError("UCP declared payload too large")
            if len(self.buffer) < 8 + size:
                break
            value = bytes(self.buffer[8:8 + size])
            del self.buffer[:8 + size]
            if compressed:
                inflater = zlib.decompressobj()
                value = inflater.decompress(value, LIMIT + 1)
                if len(value) > LIMIT or not inflater.eof or inflater.unused_data:
                    raise ValueError("Invalid or oversized compressed UCP payload")
            if fmt != 3:
                value = value.decode("utf-8")
            if fmt == 1:
                value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("Non-finite JSON constant")))
            if kind == 1:
                if not isinstance(value, dict):
                    raise ValueError("UCP header must be an object")
                self.header = value
            else:
                messages.append((self.header, value))
                self.header = None
        return messages
