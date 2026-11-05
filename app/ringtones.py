"""Per-device ringtone storage for Protect's raw HTTPS track uploads."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets


MAX_RINGTONE_BYTES = 1024 * 1024


def _mp3(data):
    """Bounded header inspection only; never invoke decoders or subprocesses."""
    if not isinstance(data, bytes) or not 4 <= len(data) <= MAX_RINGTONE_BYTES:
        return False
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10 or data[3] not in (2, 3, 4) or any(x & 128 for x in data[6:10]):
            return False
        offset = 10 + sum(x << shift for x, shift in zip(data[6:10], (21, 14, 7, 0)))
        if data[3] == 4 and data[5] & 16:
            offset += 10
    # MPEG audio frame sync, supported version/layer, non-reserved bitrate/rate.
    if offset + 4 > len(data):
        return False
    a, b, c, _ = data[offset:offset + 4]
    return (a == 255 and b & 224 == 224 and (b >> 3) & 3 != 1
            and (b >> 1) & 3 == 1 and (c >> 4) not in (0, 15)
            and (c >> 2) & 3 != 3)


class RingtoneStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._manifest = self.directory / "tracks.json"
        self._tracks = {}
        if self._manifest.exists():
            records = json.loads(self._manifest.read_text())
            if not isinstance(records, dict):
                raise ValueError("Invalid ringtone manifest")
            for key, record in records.items():
                track = self._track(int(key))
                if (not isinstance(record, dict)
                        or not re.fullmatch(r"[0-9a-f]{32}\.mp3", record.get("file", ""))):
                    raise ValueError("Invalid ringtone manifest")
                name = self._name(record.get("name", "") + ".mp3")
                self._tracks[track] = {"file": record["file"], "name": name}

    @staticmethod
    def _track(track):
        if type(track) is not int or not 1 <= track <= 10:
            raise ValueError("Track must be between 1 and 10")
        return track

    @staticmethod
    def _name(filename):
        # The user-supplied name is metadata only; it never becomes a disk path.
        if (not isinstance(filename, str) or not 5 <= len(filename) <= 128
                or not filename.lower().endswith(".mp3")
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename)
                or filename[:-4] in ("", ".", "..")
                or filename != filename.strip()):
            raise ValueError("Expected a bounded MP3 filename without path separators")
        return filename[:-4]

    def _commit(self, tracks):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.directory / (secrets.token_hex(16) + ".tmp")
        try:
            with temporary.open("x", encoding="utf8") as stream:
                json.dump({str(k): v for k, v in tracks.items()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._manifest)
        finally:
            temporary.unlink(missing_ok=True)
        self._tracks = tracks

    def get(self, track):
        self._track(track)
        record = self._tracks.get(track)
        if record:
            path = self.directory / record["file"]
            name = record["name"]
        else:
            return None
        if not path.is_file() or path.stat().st_size > MAX_RINGTONE_BYTES:
            return None
        with path.open("rb") as stream:
            data = stream.read(MAX_RINGTONE_BYTES + 1)
        if not _mp3(data):
            return None
        return {"track_no": track, "name": name, "md5": hashlib.md5(data).hexdigest(),
                "state": "exist", "path": path, "size": len(data), "content_type": "audio/mpeg"}

    def audio_info(self):
        return {"status": "ok", "speaker": [
            {key: item[key] for key in ("track_no", "name", "md5", "state")}
            for track in range(1, 11) if (item := self.get(track)) is not None]}

    def status(self):
        return {'source':'protect_upload','stored_tracks':sorted(self._tracks)}

    def upload(self, track, filename, data):
        self._track(track)
        name = self._name(filename)
        if not _mp3(data):
            raise ValueError("Expected MP3 audio no larger than 1 MiB")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / (secrets.token_hex(16) + ".mp3")
        previous = self._tracks.get(track)
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._commit({**self._tracks, track: {"name": name, "file": path.name}})
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if previous:
            (self.directory / previous["file"]).unlink(missing_ok=True)
        return self.get(track)

    def delete(self, track):
        self._track(track)
        if track <= 4:
            raise ValueError("Default tracks cannot be deleted")
        previous = self._tracks.get(track)
        if previous:
            self._commit({k: v for k, v in self._tracks.items() if k != track})
            (self.directory / previous["file"]).unlink(missing_ok=True)
