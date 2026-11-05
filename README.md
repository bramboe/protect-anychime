# Protect AnyChime

Any speaker can be a chime.

For scope, architecture, installation, testing and known limitations, see [the project overview](PROJECT.md).

Expose a supported Home Assistant speaker or speaker group as a virtual PoE chime
in UniFi Protect. Choose the doorbell, ringtone, volume and repetition in Protect.
AnyChime delivers that request through Home Assistant.

Independent experimental project; not an official Ubiquiti app.

## Setup

1. Install and start Protect AnyChime in Home Assistant.
2. Open the web interface. Confirm the detected Protect console if requested.
3. Add speakers or groups with **+** or drag them into **Chimes in Protect**.
4. Adopt the virtual chimes in Protect and pair them with your doorbell.

The Protect console IP appears below the connection badge. Certificates, virtual
identities and networking are managed internally. No terminal is needed for normal
configuration. The app rejects physical UniFi firmware updates: update AnyChime
through Home Assistant instead. Its reported compatibility profile is emulated.

## Speakers and groups

The interface separates **Chimes in Protect** and **Available speakers**. Drag an
available speaker or group into Chimes in Protect to add it immediately, or use
**+** on touch devices and with the keyboard. Use **×** to remove a virtual chime;
it returns to Available after Protect confirms removal. Failed changes stay
visible with an error. Switch between **Tiles** and **List**; the browser remembers
the chosen view. Search matches group members as well as names and models. The
drop area remains visible while searching.

Matching HA `group_members` reports form **one group tile and one Protect chime**.
A group uses a stable existing chime identity, preferring the primary identity.
Changing the HA coordinator preserves that identity. Players advertising HA announcement support receive `announce: true`, without changing their music volume or queue. The Protect gong volume is passed as `extra.volume`; whether it is supported depends on the integration. Sonos groups receive per-speaker announcements on reachable, unmuted members because Sonos AudioClip is local to each speaker. Other integrations retain their coordinator/group semantics. Announcement timing across speakers depends on the integration. For Sonos groups, each announcement receives a unique local media URL. Initial audio downloads wait for distinct speaker clients for at most 300 ms, then release together. HEAD requests, tiny byte-zero probes and subsequent byte ranges do not enter this wait. Small TCP packet buffering is disabled for audio responses where supported by the operating system. This is a best-effort delivery alignment, not a shared playback clock: speaker buffering and firmware can still produce audible differences. Missing clients or a full alignment pool fall back to immediate or bounded normal delivery.

Players without announcement support retain standard coordinator playback and volume control, which may interrupt music. A rejected announcement is never retried as ordinary playback. Some integrations or legacy firmware can accept announcement requests but fall back internally; automatic resumption cannot be guaranteed for every device. No generic snapshot/restore is attempted. Each speaker row shows HA-reported announcement support (supported, not supported, unknown, or varying across group members), independent of online status.

Groups refresh automatically, and **Refresh speakers** reloads them immediately.
Adding, refreshing and regrouping speakers are silent. Audio is played only for a Protect playback request, using tracks received from that installation's console. No firmware-derived setup tone is shipped.

If groups split, their selected speakers can become individual chimes again.
Their stored identities are reused. Integrations without reliable group metadata
stay separate. Native UniFi devices and known unsupported audio routes are hidden.
An accepted HA request does not prove that a speaker produced sound.

## Protect management

Set **Protect username** and **Protect password** in this app's Home Assistant
**Configuration** tab. Use a **local UniFi user** with **Protect Full Management**
access. Cloud/SSO accounts requiring MFA are not supported by this local-login
flow. This is HTTPS authentication, not SSH. It follows the local-user session
pattern used by the HA UniFi Protect integration, independently of that integration.
A public integration API key is not needed for the private chime removal route.

An account already entered in an earlier AnyChime version is transferred to the
app's own HA options when Supervisor is available. Its working login is retained
if that transfer cannot finish. User-entered configuration takes precedence.
The web interface never returns credentials. Newly saved trust metadata contains
only the console address and certificate pin. Credential-bearing connections are
pinned; a changed certificate fails closed.

Drag a speaker or group from **Available speakers** to **Chimes in Protect**
(or use **+**) to add it immediately. Use **×** to remove its virtual chime from Protect. This requires the configured local Protect account. The
physical speakers and HA groups are preserved. There are no selection checkboxes or Save button.

A connected chime removed in Protect deselects its whole linked speaker group in
AnyChime. Offline removals cannot deliver that device command. No removal is
inferred from a network interruption.

Expand **Previous chimes** to find app-owned old records still present in Protect,
including obsolete individual chimes replaced by a group. Inactive history is
automatically reconciled through the configured local Protect account. The app
removes only records matching saved ownership; authentication or identity errors
keep the mapping available for recovery and show an error. The app verifies its saved MAC, type and
UUID when available and never accepts a remote device ID from the browser. If an
old installation's private identities were lost, ownership cannot be established
by this scan; those records need removal in Protect itself.

Before deletion the local virtual chime is paused. Failed or ambiguous deletions
remain visibly pending; the UI does not automatically repeat a failed delete. Use **×** again on the affected
chime to retry. Other selection changes are saved only after all
requested removals succeed. Successful removal clears its
local mapping, device MAC/UUID state, private keys, per-chime audio and owned
network state. Re-adding a removed speaker creates a fresh device identity. Active
identities and group coordinators remain stable. Confirmed cleanup is resumable
after a disk or process failure; removed identities are never advertised again.

## Backups

Use Home Assistant's normal app backups and include Protect AnyChime. They contain
its options, speaker/group mappings, identities, private keys, connection trust and
custom ringtones. There is no separate export/import interface. `backup: cold`
briefly stops the app while HA takes a consistent backup. Restore it under the
same installation ID; this does not back up Protect's own configuration.

## Compatibility and networking

Every virtual chime needs its own management IP and identity. The primary chime
uses the HA IP; extra chimes use app-owned wired macvlan interfaces and DHCP. Only
`NET_ADMIN` and `NET_RAW` are requested. The app does not change the main interface,
main routing table or DNS. It verifies ownership before reusing or deleting links.
Wi-Fi parents, bridge/VLAN parents, and hypervisors that block extra MACs are not
automatically supported. Address/permission errors are shown rather than bypassed.

Discovery uses UDP 10001, device management HTTPS 8080, and the pinned controller
connection TCP 7442. Ingress and hashed MP3 delivery use port 48200. The console
management account uses HTTPS 443. Management actions and private data are only
available through authenticated HA ingress. No generic Protect API proxy exists.

## Validation

Discovery, adoption, controller handshake, ringtone synchronization and Protect-controlled playback have been exercised during development. Automated tests use mocked transports and synthetic certificates/audio; accepted calls are not treated as proof of audible playback.

Installation identifier: `local_unifi_anychime`. Source release **0.3.45 / measure1**.

References:
- https://github.com/home-assistant/core/tree/dev/homeassistant/components/unifiprotect
- https://github.com/uilibs/uiprotect/blob/main/src/uiprotect/api.py
- https://contributors.home-assistant.io/apps/how-apps-work/#backups

### Re-adding a previously removed chime

An external/offline deletion can leave the local virtual identity marked adopted,
causing Protect to show Other Console instead of allowing adoption. After a 401/403
controller rejection, a configured management account verifies this installation's
saved MAC/UUID/type against Protect. Only an absent or explicitly unadopted and
disconnected record allows clearing the local adoption credentials. The selected
entry, MAC, UUID, keys and ringtones are retained. No Protect record is deleted,
no device is adopted automatically, and no sound is played.

The speaker menu also offers **Make adoptable** for a disconnected selected chime.
It runs the same verified repair after confirmation. A chime still managed by
Protect, an ambiguous identity, a failed account login or an unreachable console
never causes an automatic local reset. Authentication failures are checked once
per native connection instance; selecting again or using the explicit action can
retry after correcting credentials.

## Automatic product identification

AnyChime reads manufacturer, model, model identifier and device class from Home
Assistant, then matches them against the UniFi client catalogue. Small official
client images download on demand and are cached in `/data/product-images` for
offline use and HA app backups. Discovery itself makes no image network requests.
Exact matches take priority. Explicit related-model substitutes cover missing
variants such as One SL (Sonos One artwork); device details are never renamed.
The page footer and image tooltip explain approximate artwork. Original HA device names and physical parent-device metadata can also identify the hardware behind a media endpoint. Only public manufacturer/model identities and generic HA metadata are used; no installation-specific bridge-to-product mappings are shipped. Displayed labels and playback entity IDs remain unchanged. Numeric model identifiers are not used without manufacturer context, and connectivity hubs are not treated as the speaker. Unknown models
use a neutral speaker, TV or receiver illustration. Album covers are never used.

## Offline speakers

Unavailable speakers appear at the bottom of their section. Unpaired offline
speakers show an information button and cannot be added by click, drag/drop or
API. Already paired offline speakers stay in Chimes in Protect and can still be
removed with ×. The virtual chime disconnects while its HA playback endpoint is
offline, making it unavailable in Protect, and reconnects with the same saved
identity when the speaker returns. No adoption or confirmation sound is triggered
by this reconnect. HA availability is checked periodically and on Refresh speakers.
For groups, the coordinator must be online; other offline members are indicated
without splitting the group or changing its physical HA membership.

## Names and first adoption

Adding a speaker creates an adoptable virtual chime. Complete its first adoption
in UniFi Protect. The repair action is shown only for an existing pairing rejected
with HTTP 401/403; it is not part of normal first-time setup. A newly ready identity
also responds once to a valid recent Protect discovery scan, so a scan does not
miss a speaker that finished starting just after the probe.

After adoption, AnyChime copies the HA speaker/group name to verified owned chimes
that still use generic names such as Smart PoE Chime. Custom Protect names remain
unchanged. This uses the configured local Protect management account. Discovery
also carries the speaker name, although Protect 7.3.70 does not use that field to
name a chime. Active adopted chimes are not reset by naming or metadata updates.


## Installation-specific trust and audio

Every installation confirms its own Protect console. Its certificates and SHA-256 pins are saved privately in HA app data and verified on subsequent connections. A changed certificate requires confirmation; no developer certificate or fixed console/HA address is shipped. See [publication notes](PUBLICATION.md).

The ringtone store starts empty. On native chime connection Protect queries `getAudioInfo` and uploads missing default/custom track bytes. AnyChime stores those bytes in `/data` and plays the requested track number. Default files, confirmation sounds and firmware audio are not bundled. An unavailable track is rejected rather than substituted.


## Native management and test-button compatibility

All chimes, including the primary identity, obtain a dedicated DHCP address. Their HTTPS management listeners use a free internal port, with an owned forwarding rule exposing port 8080 only on that owned chime address and only to the configured console. The app does not bind the HA host's TCP port 8080. The rules are narrow, inserted before Docker host-port rules, persisted for cleanup and never implemented by flushing unrelated host rules. The existing NET_ADMIN capability is used; no additional capability or protection-mode change is requested.

Protect's `playBuzzer` command uses the same uploaded-track forwarding path as `playSpeaker`, including its supplied track number. When omitted, track 1 is used. This emulates a buzzer/test button using console-delivered audio, not a separate bundled buzzer recording.

Certificate inspection and trust changes require the HA ingress proxy source. Loopback requests and forged forwarded-source headers do not bypass this check.


HA stop/restart signals trigger owned network cleanup before exit. The app allows a 30-second shutdown grace period. If the host kills the process abruptly, private ownership records permit exact cleanup/replacement on the next start.


Management forwarding preserves the dedicated chime destination IP using DNAT and changes only its TCP port. REDIRECT is not used for new rules because its incoming-interface address substitution can select the HA host address. Legacy owned rules are cleaned up using their saved rule format.


## Measurement-only calibration (phase 1)

Open a chime's actions menu → **Diagnostics & calibration**. Ordinary rings now record the native request receipt, HA play request send/response, and per-speaker-IP first GET plus server response-write completion. Diagnostics keep the most recent 128 records in memory; they are not permanent logs and contain local IP addresses. The dedicated diagnostics endpoints require authenticated HA ingress. A completed server write does not prove the speaker received, decoded or sounded the audio. Group rings use the existing unique `ac_sync` request tokens; stable singleton URLs use explicitly labelled path/timing heuristics. Cached or proxy downloads can be absent or ambiguous. Normal audio URLs, gain, repeat scheduling and group routing remain unchanged.

Calibration is optional and never runs on load, refresh or upgrade. Open HA over HTTPS, Nabu Casa, or a secure Companion-app session; plain LAN HTTP cannot use getUserMedia. Press **Start calibration — 5 clicks per speaker** yourself and allow microphone access. This authorises that finite sequence only. The app generates a short synthetic click and sends each member five separate requests, one member at a time, through the existing SpeakerBackend/play_media adapter. Keep the microphone in one fixed position, the room quiet and other audio stopped. Sound travel adds approximately 3 ms per metre. Some HA integrations redirect a member request to its whole group; stop if an unintended member also sounds. Stop, close or leaving the tab prevents further trial requests; an already sent click cannot necessarily be recalled.

Microphone processing runs locally in an AudioWorklet with a 3 kHz bandpass and an onset threshold above the observed noise floor. Only onset measurements are sent back, never recordings or microphone sample arrays. Five clock pings estimate the browser/server monotonic-clock offset using the lowest round-trip delay. The app records both browser-request-to-onset and estimated HA-send-to-onset latency, together with clock uncertainty. Microphone capture/processing latency, position, threshold and room noise remain part of the measurement. These are estimates, not proof of acoustic synchronisation.

After exactly five valid samples for every member, `/data/latency-calibration.json` stores the samples, mean, sample standard deviation, range and clock uncertainty keyed by HA device ID. It is included in the normal app backup. A cancelled, incomplete, noisy or interrupted session does not overwrite valid profiles. The synthetic click cache is separate from native ringtone storage. The result is measurement only: no value is applied to playback. Manual Delay fields, latency compensation, repeat rendering, alternative synchronised modes and parallel state fetching are deliberately deferred. Stop here and run a user calibration before further phases.

If HTTPS microphone access is unavailable, the UI explains the limitation and starts no sound. Manual delay controls belong to phase 2 and are not pretended to work in this release. Browser/platform microphone policy can still deny access even over HTTPS.

### Optional automatic adoption

In the app's HA Configuration tab, **Automatically adopt chimes in Protect** is off by default. Enabling it requires the configured local Protect account. Only a currently selected, enabled, newly discovered virtual PoE chime with the matching local MAC, host and identity can be adopted. Already adopted/adopting devices and foreign devices are not taken over. Attempts are bounded and spaced out. This option does not pair a doorbell or change sounds/volumes/repeats: finish those settings in the Protect app. No automatic adoption was enabled or tested on real devices during development.

Browser API references: [getUserMedia secure-context requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia), [AudioWorklet](https://developer.mozilla.org/en-US/docs/Web/API/AudioWorklet), [audio/performance clock mapping](https://developer.mozilla.org/en-US/docs/Web/API/AudioContext/getOutputTimestamp).

## Available groups and restoring an existing chime

The grouping menu is also available beside speakers in **Available speakers**. **Separate chimes** and **Restore original group** change only AnyChime's representation of an existing HA speaker group; they never call HA grouping services.

If none of the group members is selected, changing the layout creates no Protect chimes and needs no Protect management account. If exactly one chime represents the selected part of a separated group, restoring the group adds its remaining online members to that existing chime. Its key, identity files, adoption, doorbell pairing and stored ringtones remain intact, even when the added member is the HA coordinator. An offline new member cannot be added. Any old inactive identities are cleaned up using the existing confirmed-removal workflow; failures remain resumable and never replace the retained chime. If multiple separate chimes are already selected, the existing replacement workflow still applies and requires new adoption and pairing.

**Diagnostics & calibration** is hidden for one single-speaker chime with no other known chimes. It remains available for a multi-speaker group or multiple chimes. A local Protect account allows a read-only lookup of the total adopted chime count, including Ubiquiti's own chimes, cached for 60 seconds. When this lookup is unavailable, the UI uses known AnyChime entries and does not claim to have discovered native chimes. This only determines menu visibility: native Ubiquiti chimes cannot be calibrated through AnyChime's HA playback adapter. Latency compensation remains deferred until the user has performed phase 1 calibration.

## Quiet test playback

The AnyChime **Test chime** menu starts at **10%** every time it is opened. Adjust **Test volume** before pressing Test chime if needed. Changing the slider never starts audio. This override is sent only with the explicit Protect test request; it never changes saved Protect volume or the volume used by real doorbell rings. Ringtone and repeat settings still come from Protect. A 0% test volume or a muted Protect chime sends no playback request. Testing directly in Protect or Home Assistant's Protect integration continues to use that application's own controls.

## Manual latency compensation — local HTTP supported (0.3.48)

Open a chime's **Adjust timing** menu. Increase **Delay (ms)** for the speaker that sounds early; leave the slowest speaker at zero. Choose 0–500 ms, press **Save timing**, then explicitly press **Test at 10%**. Saving, editing, opening and refreshing never request sound. The test uses the configured Protect ringtone through the normal Protect test path, with a temporary 10% volume override. Saved delays apply to real rings too; their Protect volume is unchanged. Set all delays back to zero and save to restore the original playback route. This works over local HTTP and requires no microphone, domain, certificate or Cloud subscription. Optional microphone measurements remain under a collapsed advanced section and still require a secure browser context.

This is **latency compensation**, not a shared playback clock. Individual adjustment currently supports Sonos announcements with stable HA device IDs. Other integrations keep their existing playback route and show an explanation instead of offering unsupported controls. Settings are private in `/data/latency-delays.json`, keyed by HA device ID and covered by the app backup. No default delays are enabled on upgrade. Nonzero settings prepare every member's audio in the same PCM WAV format, with sample-exact leading silence at 48 kHz. Cached variants are keyed by ringtone MD5, offset and repeat=1, capped at 128 MiB; individual files are bounded at 25 MiB/120.5 seconds. Recently prepared variants are protected from eviction for two minutes; a full protected cache refuses playback rather than dropping media promised to a speaker. Original Protect ringtone files remain unchanged. Cache files can be regenerated after restore.

Compensated group requests are sent per member concurrently, while retaining a single bounded download barrier across all member URLs. Diagnostics correlate those paths to one ring. Announcements and music resumption are retained. Repetitions still use the existing separate-announcement path, with the configured leading silence included in the wait; combined-repeat rendering and alternate playback modes remain deferred. Network and device variability can still cause audible drift. [Sonos AudioClip supports MP3 and WAV](https://docs.sonos.com/reference/audioclip-loadaudioclip-playerid).

The grouping menu now remembers explicitly known Sonos sets when HA later reports their members as separate. **Restore original group** remains available and restores only the virtual chime layout, never the HA music group. A partially selected set retains its existing chime; replacing multiple selected chimes still requires the existing confirmation and re-adoption workflow. No live speaker grouping, removal or audio test is performed during development verification.

## Group selection without saved HA group history (0.3.49)

For an individual Sonos announcement speaker, open **Group chimes…** and choose the other members. This also works when HA reports each player as a singleton and AnyChime has no previous group to restore. Only individually addressable Sonos announcements are offered; existing multi-member HA music groups cannot be partially reconfigured here. Continue shows the same scope-aware confirmation: unused speakers create only a display preference, one existing selected chime keeps its identity/pairing when adding other members, and multiple selected chimes use the existing replacement/re-adoption flow. The new virtual set is remembered for later Separate/Restore actions. No HA group service is called.
