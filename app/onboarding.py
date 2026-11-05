"""Explicit, bounded new-speaker sound jobs. Import and inspection stay silent."""
import copy
import re
import threading

from audio import SpeakerBackend


class SpeakerOnboarding:
    def __init__(self, options_provider, backend_factory=SpeakerBackend):
        self._options_provider = options_provider
        self._backend_factory = backend_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seen = set()
        self._running = False
        self._results = []
        self._state = "idle"

    def submit(self, entities):
        """Caller passes only entities newly added by an explicit successful save."""
        if not isinstance(entities, list):
            raise ValueError("New speaker entities must be a list.")
        if len(entities) > 16:
            raise ValueError("At most 16 new speakers can be tested in one save.")
        for entity in entities:
            if not isinstance(entity, str) or not re.fullmatch(r"media_player\.[a-z0-9_]+", entity):
                raise ValueError("Select valid media player entities.")
        with self._lock:
            if self._running:
                return {"state": "busy", "audio_played": None}
            selected = list(dict.fromkeys(entity for entity in entities if entity not in self._seen))
            if not selected:
                return {"state": "empty", "audio_played": False}
            options = copy.deepcopy(self._options_provider())
            self._seen.update(selected)
            self._results = []
            self._stop.clear()
            self._running = True
            self._state = "queued"
            threading.Thread(target=self._run, args=(selected, options), daemon=True).start()
            return {"state": "queued", "audio_played": None}

    def _run(self, entities, options):
        for entity in entities:
            if self._stop.is_set():
                break
            try:
                configured = dict(options, speaker_entity=entity,
                                  audio_url=options.get("success_url", ""),
                                  group_members=options.get('speaker_groups',{}).get(entity,[entity]))
                backend = self._backend_factory(configured, allow_playback=True)
                approval = backend.arm_once(confirmed=True, volume=10)
                if self._stop.is_set():
                    break
                result = backend.play_once(approval["approval_id"])
                result = {"entity_id": entity,
                          "ha_call_accepted": result.get("ha_call_accepted") is True,
                          "acoustic_playback_verified": False,
                          "volume": 10, "audio_played": None}
            except Exception:
                # Backend or integration errors must not disclose URLs or credentials.
                result = {"entity_id": entity, "ha_call_accepted": False,
                          "acoustic_playback_verified": False, "volume": 10,
                          "audio_played": False,
                          "error": "The new-speaker audio request failed; check the speaker in Home Assistant."}
            with self._lock:
                self._results.append(result)
        with self._lock:
            self._running = False
            self._state = "stopped" if self._stop.is_set() else "complete"

    def status(self):
        with self._lock:
            return {"state": self._state, "busy": self._running,
                    "audio_played": None if self._results or self._running else False,
                    "results": copy.deepcopy(self._results)}

    def stop(self):
        """Cancel remaining work without issuing any Home Assistant calls."""
        self._stop.set()
        return self.status()
