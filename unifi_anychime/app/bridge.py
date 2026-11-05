"""Pure Protect protocol device state; no sockets or HA requests."""
import argparse
import json
import re
from pathlib import Path
from ucp import Decoder, encode


class Device:
    def __init__(self, script="script.protect_virtual_chime"):
        if not re.fullmatch(r"script\.[a-z0-9_]+", script):
            raise ValueError("Expected a Home Assistant script entity")
        self.script = script
        self.plans = []
        self.volume = 80
        self.repeat = 1

    def _plan(self, track, volume, repeat):
        # Firmware accepts 0..100 / 1..10. Keep the same bounds, without coercion.
        if type(track) is not int or not 1 <= track <= 65535:
            raise ValueError("Unsupported track")
        if type(volume) not in (float, int) or not 0 <= volume <= 100:
            raise ValueError("Volume must be between 0 and 100")
        if type(repeat) is not int or not 1 <= repeat <= 10:
            raise ValueError("Repeat must be between 1 and 10")
        plan = {"method": "POST", "path": "/api/services/script/turn_on",
                "body": {"entity_id": self.script, "variables": {
                    "track_no": track, "volume": volume / 100, "repeat": repeat}}}
        self.plans.append(plan)
        return plan

    def handle(self, header, body):
        if header.get("type") != "request" or not isinstance(header.get("id"), str):
            raise ValueError("Expected a correlated UCP request")
        response = {"id": header["id"], "type": "response", "errorCode": 0, "error": ""}
        action = header.get("action")
        try:
            if not isinstance(body, dict):
                raise ValueError("Expected an object body")
            if action == "getAudioInfo":
                result = {"status": "ok", "speaker": [
                    {"track_no": 1, "name": "Default", "state": "exist"}]}
            elif action == "getInfo":
                # Proposed profile, not a captured physical-chime response.
                result = {"version": "1.3.7", "uptime": 1,
                          "featureFlags": {"hasWifi": False,
                                           "supportCustomRingtone": False,
                                           "hasHttpsClientOTA": True}}
            elif action in ("networkStatus", "getNetworkStatus"):
                result = {"linkSpeedMbps": 1000}
            elif action in ("playSpeaker", "playBuzzer"):
                self._plan(body.get("track_no", 1), body.get("volume", self.volume),
                           body.get("repeat", self.repeat))
                result = {"status": "ok"}
            else:
                # Do not falsely acknowledge password rotation, SSH, OTA or streams.
                response.update(errorCode=1, error="Command not implemented")
                result = {"status": "unsupported"}
        except ValueError as error:
            response.update(errorCode=1, error=str(error))
            result = {"status": "invalid"}
        return response, result


def replay(data, script="script.protect_virtual_chime"):
    decoder = Decoder()
    device = Device(script)
    responses = []
    for header, body in decoder.feed(data):
        rh, rb = device.handle(header, body)
        responses.append(encode(rh, rb))
    if decoder.buffer or decoder.header is not None:
        raise ValueError("Incomplete UCP message")
    return b"".join(responses), device.plans


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, help="Read a local UCP binary fixture")
    parser.add_argument("--script", default="script.protect_virtual_chime")
    args = parser.parse_args()
    data = args.replay.read_bytes() if args.replay else encode(
        {"type": "request", "id": "synthetic-demo", "action": "playSpeaker"},
        {"track_no": 1, "volume": 25, "repeat": 1})
    response, plans = replay(data, args.script)
    print(json.dumps({"mode": "silent-local-replay", "ha_requests_sent": 0,
                      "native_adoption_verified": False,
                      "responses": Decoder().feed(response), "ha_plans": plans}, indent=2))


if __name__ == "__main__":
    main()
