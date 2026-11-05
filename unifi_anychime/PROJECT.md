# Protect AnyChime project

Source app version: 0.4.2. Slug: `unifi_anychime`. UI, source and documentation are English. Native Protect adoption, identity, management and ringtone transport are preserved. There are no bundled proprietary ringtones or installation-specific certificates.

## Chimes and speakers

The durable ownership model is a fixed speaker set per chime, independent of HA music groups. `registry.py` owns atomic membership edits and stable existing keys; `speakers.py` discovers individual eligible media-player entities and safe metadata. `runtime.py` keeps listeners, discovery, network ownership and player membership in line with those saved sets. `native.py`, `ucp.py`, `ringtones.py`, `management.py`, `connection.py` and `network.py` retain their existing responsibilities. Background discovery does not invent, merge or split chimes.

Membership API: `POST /api/chimes/members`. Add/move uses a selected entity and optional destination key; non-last detach is a local registry update. The last member cannot be moved into another chime automatically: the user first removes its chime through the confirmed Protect flow. Last-member removal and entire-chime removal use existing management ownership verification and confirmed cleanup. No HA grouping service is ever called.

`chime_migration.py` migrates old entries without changing native identity or ringtone bytes. A private schema marker makes the migration repeatable. Interrupted legacy transitions and unconfirmed historical identity material become explicit review holds. Cleanup is restricted to obsolete preferences, microphone data and generated caches after review is complete. Held entries remain untouched until user-confirmed removal. `migration.py` validates archive checksums, excludes obsolete data from new exports, and carries review holds forward when importing older pending operations.

`index.html` contains the interface structure and styling. `assets/speakers.js` handles fixed-set cards, individual member rows, available speakers, drag handles, keyboard alternatives, temporary drag rollback, local-account test/removal actions, product information and console trust. A bundled, pinned SortableJS dependency supplies drag behaviour. `assets/timing.js` handles only manual timing. There is no microphone, worklet, clock-sync, group restoration or separation interface.

The deployment model remains a Home Assistant app with host networking, ingress/media port 48200 and owned per-device management forwarding. The existing NET_ADMIN/NET_RAW requirements and installation-specific console trust remain. Account settings stay in HA app configuration. Network portability beyond the live-tested HA OS installation remains a separate validation task.

Verification uses fake HA/Protect request boundaries and synthetic media bytes. Tests cover stable membership keys, member moves/detachment, last-member refusal/confirmed removal, rollback on persistence failure, fixed membership across HA group changes, migration with open transitions, old backup import, and native identity/ringtone preservation. Source hygiene is checked from the repository root with `python3 tools/check_release.py`. The root contains repository metadata and the MIT license; the installable app is in `unifi_anychime/`. No source commit is pushed automatically.

## Timing

`audio.py` consumes one-use playback approval, inspects each member independently, filters unavailable/muted members and forms an effective request plan. Platform/group metadata is used only for playback deduplication. Sonos announcements stay individual. Consistent non-Sonos music groups and normal Sonos music playback use a coordinator; incompatible mixed Sonos announcement capability fails before writes. All effective playback requests run in parallel and never call HA grouping services.

`playback.py` translates native Protect volume/track/repeat into the same backend route. It loads manual offsets by HA device ID with entity fallback. Zero volume remains silent. Repeat scheduling remains duration + 50 ms. Original ringtone bytes and media paths remain unchanged; there is no rendered audio path or cache.

`announcement_sync.py` owns a bounded one-second download barrier and a server-side offset of at most 500 ms. Every effective output receives a random member token beneath a random batch token, tied to the original media path. Counts are by member token rather than IP. Responses are held before headers; probes, HEAD and later ranges bypass holding. A shared absolute release epoch avoids accumulating scheduling jitter into another full delay. Unknown tokens cannot choose offsets. Single-output delivery has no barrier or delay. Compact summaries include only counts, arrival spread and release reason.

`manual_timing.py` stores the existing 0–500 ms values in private `latency-delays.json`. The UI exposes Save and an explicit Test at 10%. No editing, saving, loading, migration or refresh operation starts audio. A grouping integration may fan out a single effective request itself, and its outputs cannot then be independently compensated here. Announcement volume is integration-specific.

Download release timing is not a common acoustic clock. Whether it performs as well as leading digital silence must be checked by the user on real speakers. Keep numeric settings, but retest them after upgrade. The local pre-simplification Git snapshot preserves the previous implementation for source comparison. Removed microphone/timestamp modules and generated-cache logic must not be reintroduced as hidden alternate playback routes.

## Partial playback failures and security

A failed state lookup, volume request or playback request affects only its effective output. Independent successful outputs continue their remaining repetitions. Failed members are excluded for the remainder of that ring, without retry or fallback playback; a new ring can try them again. A failed shared coordinator excludes the selected members represented by that request. Status reports `partial` when some outputs succeeded, with per-member outcomes and accepted-request counts. HA acceptance does not prove audible playback.

All interface and administrative API requests require the actual Supervisor ingress peer, including read-only status and discovery. Loopback and forwarded-address headers cannot authorize access. Speaker media downloads remain intentionally separate from ingress authorization. The enforced AppArmor policy limits file writes and executable paths while retaining the network capabilities needed for virtual chimes. See [security details](../docs/SECURITY.md).
