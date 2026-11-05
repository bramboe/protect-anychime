# Protect AnyChime 0.4.0 verification

## Scope and local source history

The unmodified 0.3.49 source was committed locally as `d6c84c5` (Snapshot before simplification). No remote was added and no commit is pushed. The completed implementation is committed separately after verification.

Fixed sets retain existing registry keys and native identity/ringtone directories. Add, non-last detach and move are atomic local registry edits. Last-member moves are explicitly refused until that source chime is removed through the confirmed Protect flow. This prevents hidden deletions and empty active identities.

## Removed implementation

- `app/calibration.py`
- `app/ring_timing.py`
- `app/chime_layout.py`
- `app/assets/calibration.js`
- `app/assets/onset-detector.js`
- `app/assets/onset-worklet.js`
- `DelayAudioCache` and PCM/WAV rendering from `app/manual_timing.py`
- Automatic background deletion of unconfirmed historical chime identities

Obsolete test modules for microphone measurements, rendered audio and derived layouts were removed. Fixed-set, token barrier, migration and current playback tests replace their relevant coverage.

## Removed routes and UI

Removed routes: GET/POST `/api/calibration/*`, GET `/api/diagnostics/rings`, `/media/calibration/<session>.mp3`, `/media/delayed/<md5>/<offset>.wav`, POST `/api/chimes/layout`, POST `/api/speakers`, GET `/api/management/chime-count`, and the fixed microphone JS assets.

Removed UI: Separate chimes, Restore original group, Group chimes, microphone permission/HTTPS instructions, AudioWorklet calibration, raw ring diagnostics, group-derived speaker tiles and old layout-resume controls. The new UI uses fixed-set cards, individual members and available entities, with Test, Timing and Remove in the chime menu. Keyboard and + alternatives remain.

## Migration implemented

- Keep every saved key, identity directory, adoption and ringtone data. Existing members become the fixed set.
- Write a private idempotent schema marker, `chime-schema.json`.
- Hold identities involved in an unfinished legacy transition, conflicting membership or stale unconfirmed identity state. Retain outstanding removal flags. No old delete instructions are replayed.
- Require explicit confirmed removal of held entries and explicit Finish migration. Pending deletion retries can accept verified remote absence, without creating a replacement identity.
- Only after completion, remove old layout preferences, microphone statistics/click cache and generated latency cache. Keep manual delays.
- Validate old archive checksums before discarding obsolete files; preserve an unfinished old operation as review state when importing. New exports omit obsolete files and include the schema/review marker.

## Automated and isolated browser checks

- 311 unittest tests pass with fake HA/Protect transports and synthetic media only.
- JavaScript syntax checks pass for the new speaker and timing assets.
- `tools/check_release.py` passes; `git diff --check` passes.
- 20 isolated Safari interaction checks pass against a mock installed before all app scripts: initial load is silent; member add/detach; keyboard move; source/destination key retention; last-member move refusal; + creates a chime; 10% default; test menu remains open; timing input/save does not play; explicit timing test; last-member removal confirmation; Sortable drop; list/tile views.
- All browser test playback requests were mocked. Real HA requests from the fixture: zero. Real speaker test audio: zero.

## Live verification and limits

The existing 0.3.49 installation had two configured chimes, both already offline because their HA speakers were offline, with no pending removal. No speaker was woken or tested. After the final 0.4.0 build, read-only checks show two connected chimes, exactly the same saved keys and ordered members, no migration review required, two fixed-set cards, and the expected new timing/drop UI. Old diagnostics/calibration GET routes return 404. The previous offline condition was no longer present at this check. No playback was requested.

Acoustic alignment, audibility, integration-specific announcement volume, buffering/proxy/transcoding effects and real repetition behaviour have not been verified by playing on real devices. The user must compare using explicit Test at 10% once the speakers are online. Saved numeric offsets survive, but may require retuning because response release differs from silence in the file. Single effective targets deliberately receive no delay.

HA OS is the available live host. Other hosts, Docker/network backends and actual multi-output proxy downloads have not been exercised live. Fake tests cover shared proxy IPs, output subtokens, bounded waiting and coordinator deduplication. Runtime UUID/MAC comparison is unavailable while the speakers/chimes are offline; registry key/member comparison is performed without exporting private identity files.
