# Protect AnyChime

Any speaker can be a chime. Home Assistant app for delivering UniFi Protect chime requests to supported third-party Home Assistant media players. Independent project; not an official Ubiquiti product.

## Chimes and speakers

A chime is a fixed set of speakers you choose in AnyChime. Each chime has its own stable virtual Protect identity. Home Assistant music groups never create, merge or split these sets. A Cast group, Music Assistant group or HA media-player group entity appears as one speaker; its integration controls playback to its members.

Install the app on a supported Home Assistant installation, start it and open its web interface. Connect to your Protect console using the connection screen. Certificate trust is established separately for each installation and verified on subsequent connections. No personal console certificate, credentials, device identity or ringtone is bundled.

Add speakers to Home Assistant first, then refresh AnyChime. Native UniFi devices and entities without a supported media-playback route are excluded. Product images use available HA device metadata and a public product catalogue; a related-model or generic image may be shown. Speaker names stay as reported by HA.

- Drag an available speaker onto **New chime**, or press its **+**, to create a chime.
- Drag onto an existing chime, or use **+ Add speaker**, to add a member without changing its identity, adoption, doorbell pairing or stored ringtones.
- Drag a member to another chime to move it. The source and destination keep their identities. Moving the source's last member is refused: remove that chime through the confirmed Protect flow first, then add the speaker to its destination.
- Use **×** on a member to detach it. Removing the last member requires confirmation and uses the existing owned-device removal flow in Protect. Chime-menu **Remove** uses that same confirmed flow for the entire set.
- Use a drag handle with **Space**, focus a chime or New chime with **Tab** or **Arrow Up**, then press **Enter**. **Escape** cancels. Offline speakers stay visible, sort below online speakers and cannot be added or moved.

Adopt each new virtual chime in Protect, pair it with your doorbell and configure its ringtone, volume and repetitions there. **Automatically adopt chimes in Protect** is optional and disabled by default. Management, tests and automatic adoption require a local UniFi account with Protect Full Management access in the app's HA Configuration tab. Automatic adoption is limited to this installation's own virtual chimes and does not configure doorbell pairing or sounds.

Only original Protect-delivered or user-uploaded ringtones are played. There are no bundled Ubiquiti ringtone files. Opening the interface, editing membership, changing timing or saving settings never starts audio. Tests require an explicit click and request 10% by default. Announcement volume and restoration depend on the HA integration; non-announcement playback can interrupt music.

Back up and restore the app through Home Assistant. Include this app to retain fixed sets, identities, console trust, account and ringtones. The internal slug is `protect_anychime`. Existing installations must follow the [app ID migration guide](DOCS.md#moving-from-the-previous-app-id) before starting the renamed app. The web/media listener uses port 48200. Virtual chimes receive their own network addresses; their management endpoints use dedicated network rules rather than occupying HA's shared port 8080. The app requires host networking and the NET_ADMIN/NET_RAW capabilities. HA OS is the tested live host; other supported container environments still require independent network validation.

Upgrading preserves each saved chime's key, identity and current members. An interrupted older grouping operation is held for explicit review instead of silently deleting or recreating devices. Follow the migration message, remove affected chimes through the confirmed flow and finish migration before adding their speakers again. Unrelated identities remain unchanged.

## Timing

Open a multi-member chime's menu → **Timing**. Increase **Delay (ms)** for a speaker that sounds early and leave the latest speaker at zero. Choose 0–500 ms, press **Save**, then explicitly press **Test at 10%** to compare by ear. Saved delays also apply to real doorbell rings; Protect's saved volume is unchanged. This works on a local HTTP connection without a microphone, HTTPS setup or a Cloud subscription.

For each ring, AnyChime skips offline, standby and muted members, then sends effective playback requests in parallel. Players advertising announcement support receive `announce: true`; others use normal volume/playback services. Consistent non-Sonos music groups are deduplicated to their coordinator. Sonos announcements address individual speakers; normal Sonos group playback still uses its coordinator. A mixed Sonos music group with incompatible announcement support is refused before audio rather than producing overlapping playback.

For two or more effective requests, each output receives a unique opaque sub-token for the original ringtone. The server holds initial media responses before sending headers, for at most one second or until all outputs arrive. It then applies each saved offset against a shared release time, adding at most 500 ms. Downloads are counted per sub-token, not IP, so several outputs can share a proxy address. HEAD requests, small probes, later byte ranges and unknown tokens do not wait. A single effective request has no barrier or delay.

This is **latency compensation**, not a shared playback clock. Buffering, proxies and transcoding can reduce precision. A deduplicated HA group shares one offset: its coordinator's when selected, otherwise the first selected member's. Existing numeric delay values are retained, but need listening checks after moving from rendered silence to download release timing. Repetitions remain separate announcements after duration + 50 ms.

The chime status shows only expected/distinct downloads, arrival spread and release reason. It exposes no diagnostic speaker IPs or media URLs. Microphone calibration, clock measurements and rendered audio caches are removed. Whether this release method sounds as well aligned as the old method must be verified on real speakers using the explicit test button.

## Partial playback failures and security

A failed state lookup, volume request or playback request affects only its effective output. Independent successful outputs continue their remaining repetitions. Failed members are excluded for the remainder of that ring, without retry or fallback playback; a new ring can try them again. A failed shared coordinator excludes the selected members represented by that request. Status reports `partial` when some outputs succeeded, with per-member outcomes and accepted-request counts. HA acceptance does not prove audible playback.

All interface and administrative API requests require the actual Supervisor ingress peer, including read-only status and discovery. Loopback and forwarded-address headers cannot authorize access. Speaker media downloads remain intentionally separate from ingress authorization. The enforced AppArmor policy limits file writes and executable paths while retaining the network capabilities needed for virtual chimes. See [security details](../docs/SECURITY.md).

## Chime setup status

With automatic adoption enabled and a local Protect account configured, new chimes show blue **Adopting in Protect**. Once adopted and connected, an unpaired chime shows orange **Configure chime in Protect**. Pair it with a doorbell in Protect and choose its sound, volume and repetitions there. Green **Active as chime** requires a confirmed doorbell pairing and a live chime connection; default sound/volume settings count as configured. This status does not prove acoustic playback.

Pairing is read from Protect for verified installation-owned identities approximately every thirty seconds, with up to another interface refresh interval before the label changes. Missing fields stay unknown. A temporary failed refresh retains the last confirmed pairing for up to five minutes while the live chime connection is monitored separately. A warning requires at least three consecutive failures spanning ninety seconds, or evidence older than five minutes. Failed checks retry after 15, 30 and then 60 seconds; successful checks reset the failure episode. No doorbell identifiers or settings are added to the UI. Automatic adoption disabled keeps the orange **Adopt chime in Protect** instruction. Offline, migration and removal states take precedence.
