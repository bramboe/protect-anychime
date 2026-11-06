# Changelog

## 0.4.4

- Allow the canonical `/proc/<pid>/net/` read path in AppArmor so new chimes can discover the network route; retain enforcement and the same capabilities. Report network setup problems in orange and log their reason once per change.
- Install the corrected profile through a versioned HA update; rebuilding alone does not reload the AppArmor profile.

## 0.4.3

- Use orange consistently for warnings and errors, including dialog feedback. Record adoption/configuration problems in app logs before referring users there.

- Show per-chime setup progress: blue “Adopting in Protect”, orange “Configure chime in Protect”, then green “Active as chime” only after Protect confirms a doorbell pairing and the chime is connected.
- Read configuration for verified local chimes every ten seconds without changing adoption, pairing, sound, volume or identities. Missing or failed observations do not imply a configured chime.
- Keep manual adoption instructions when automatic adoption is disabled; show account, retry and offline problems separately.

## 0.4.2

- Continue repetitions on successful outputs when another speaker rejects a state, volume or playback request. Report partial outcomes and do not retry failed members within the same ring.
- Remove the unused preview endpoint and disabled onboarding implementation; replace the prototype request header with an explicit app request marker.
- Restrict read-only ingress endpoints to the actual Supervisor ingress peer, including denial of loopback requests.
- Package an enforced AppArmor policy with bounded writes and executable paths; retain only the existing required network capabilities.
- Add the MIT license, repository metadata, an installable app subfolder and repository installation instructions. Move internal verification/publication notes under docs.
- Include the fixed-position menu and outside-click dismissal from 0.4.1.

# 0.4.1 menu1

- Keep the open chime menu fixed in the viewport while the page scrolls. Prevent polling from replacing its trigger underneath it, and clamp it to the visible viewport after resizing or zooming.
- Close the menu on an outside pointer click/tap. Keep it open for interactions inside, including slider drags that end outside. Retain Escape dismissal and keyboard focus without jumping the page.

# 0.4.0 sets1

- Make each chime a fixed, user-chosen speaker set independent of Home Assistant music groups. Keep existing keys, native identities, adoption, pairing and ringtone storage during migration and member edits.
- Add drag/drop, +, per-chime Add speaker, keyboard pickup/drop and member ×. Adds, non-last removals and moves are atomic registry changes. Moving a last member is refused until its chime is explicitly removed through the confirmed Protect flow; no empty active identity is left behind.
- Replace automatic group splitting/combining, restored group history and layout transition logic with stable sets. Keep unfinished older operations and historical unconfirmed identities held for explicit review; do not replay or discard their removal state.
- Use one per-output playback plan: filter unavailable/muted members, deduplicate consistent HA music groups to coordinators, keep Sonos AudioClip per speaker, and send effective requests concurrently. Retain announcements when supported, checked normal volume/playback otherwise. Do not call HA music-group services.
- Replace rendered PCM silence and the latency audio cache with server-side download release offsets. Use one batch token plus a distinct output sub-token, count by token rather than IP, release at a common epoch, and bound requested waiting to 1.0 s plus 0–500 ms. Single-output delivery remains unheld. Original MP3 bytes and repeat scheduling are unchanged.
- Keep private manual delays by HA device ID, with entity-ID fallback. Timing uses sliders, Save and an explicit Test at 10%. Existing values survive but require listening checks with the different timing method.
- Remove microphone calibration, AudioWorklet/onset code, clock measurements, detailed ring diagnostics and their endpoints/UI. Retain only compact barrier counts, arrival spread and release reason.
- Clean obsolete grouping preferences, microphone profiles and generated caches after completed migration. Exclude these from new exports and ignore them in older imports after checksum validation, while retaining any pending operation as review state.
- Verify with fake HA/Protect transports and an isolated browser fixture only. Whether response holding sounds as well aligned as file silence still requires the user's explicit listening test. No real-speaker test audio is played by the agent.

# 0.3.49 groups3

- Add an explicit Group chimes picker for individually addressable Sonos announcement speakers when HA no longer reports a group and no previous group layout was saved. The user chooses members; never infer or change their music grouping.
- Keep the partial-selection rule: attach new members to the existing selected chime and retain its identity and pairing. When replacing multiple selected chimes, retain the explicit removal/adoption confirmation. Reject unsupported integrations and partial existing HA music groups.
- Retain local manual timing from 0.3.48, with no microphone/HTTPS requirement and explicit test playback at 10% only.

# 0.3.48 timing1

- Add local manual latency compensation under Adjust timing: per-device 0–500 ms delay, explicit Save timing and Test at 10%, no microphone or HTTPS required. Keep optional automatic microphone measurement collapsed under advanced settings.
- Preserve the original playback route until nonzero delays are saved. Apply sample-exact PCM silence, bounded cached WAV variants and parallel individual Sonos announcement requests with a shared cross-URL download barrier. Keep Protect volume and stored ringtones intact; do not implement combined repeats or alternate synchronized modes.
- Remember known Sonos chime sets even if HA later ungroups their music playback, keeping Separate/Restore menus available and preserving the existing partial-group identity rule. Never change the HA group.
- Test using synthetic PCM and fake HA/Protect transports, including exact 100 ms sample offsets, parallel delivery, profile validation, scope, restart and group restoration. No real speaker audio is started by the agent.

# 0.3.47 testvolume1

- Default AnyChime Test chime playback to 10%, with a Test volume slider that resets to 10% whenever the menu is opened. Moving the slider never plays audio.
- Apply the volume only to the explicit test request through Protect. Preserve actual doorbell volume, ringtone/repeat settings and stored Protect configuration. Respect 0% without playback, validate values server-side, and keep the menu open after tests.
- Explain the blocked calibration Start button: HTTP sessions must reopen HA through HTTPS, including inside Companion. Distinguish HTTP from a browser with no microphone API.
- Verify with fake transports only; no real speaker test audio is played.

# 0.3.46 group2

- Make Separate chimes and Restore original group available for unused HA speaker groups, without creating any Protect chimes.
- Restore a partially selected group into its existing chime. Preserve that chime's identity, adoption, doorbell pairing and ringtone storage, including when its speaker is not the HA coordinator. Validate new members are online, and preserve this choice across interrupted transitions and restarts.
- Add grouping menus beside available speakers. Use distinct confirmation text for display preferences, extending an existing chime, and replacing multiple existing chimes. HA grouping remains unchanged.
- Hide Diagnostics & calibration for a lone single-speaker chime. Keep it for multi-speaker groups or multiple chimes, counting adopted native Ubiquiti chimes through a read-only, cached Protect lookup when a local account is configured. Native chimes themselves cannot be calibrated by AnyChime.
- Fix the automatic-adoption status message when no chime is connected yet. No audio tests or live layout conversions are performed during verification.

# 0.3.45 measure1

- Phase 1 only: record request reception, HA send/response and per-IP first GET/server response-write completion in bounded local diagnostics. Existing grouped download tokens correlate rings; singleton path-based attribution is explicitly marked as heuristic. Normal media URLs, gains, repeats, announcements and group routing remain unchanged.
- Add Diagnostics & calibration to the chime menu. HTTPS-capable browser calibration requires an explicit Start click and microphone permission, then sends five sequential, single-member synthetic clicks through the same playback adapter. Store mean, sample standard deviation and five samples per HA device ID privately in app data. Raw microphone audio never leaves the browser.
- Generate only the calibration click on demand; do not modify native identities, adoption, stored Protect ringtones or the slug. No delay compensation, combined-repeat audio, synchronous mode or state-fetch optimisation is implemented.
- Explain secure-context requirements, fixed microphone placement, propagation delay and measurement uncertainty. HTTP sessions cannot start microphone calibration; manual delay controls remain deferred to phase 2.
- Add an opt-in Automatically adopt chimes in Protect configuration option, off by default. Scope adoption to newly discovered app-owned chimes, using a pinned local account session. Finish doorbell, ringtone, volume and repeat setup in Protect yourself.
- Rename Combine chimes to Restore original group, keeping Remove last in the menu.

# 0.3.44 drag1

- Adopt HA automation-style drag handles, blue ghost placeholders, raised drag cards, 150 ms transitions and automatic scrolling using the same pinned SortableJS 1.15.7 dependency, bundled locally with its MIT licence.
- Move speakers from Available speakers into a clear Chimes in Protect drop area. Restore the original DOM position until the server confirms addition; failed and cancelled moves retain the existing selection.
- Support touch handles and keyboard pickup/drop/cancel. Keep the + action available, and prevent unavailable speakers or a pending chime-layout change from being dragged.
- Pause background list rendering while a speaker is being moved so polling cannot interrupt the gesture. Keep Remove last and the existing Separate/Combine workflow in the menu.

# 0.3.43 layout2

- Keep Remove as the last action in the chime menu, after separation/combination and any adoption repair action.

# 0.3.42 layout1

- Add Separate chimes / Combine chimes with icons for an existing HA speaker group. This changes only virtual Protect identities and leaves HA grouping untouched.
- Preflight ownership and account access before pausing, then remove all old app-owned identities associated with the set before creating replacements. New chimes require adoption and doorbell pairing in Protect.
- Persist a bounded transition journal and grouping preference in private app data. Interrupted/partial removal can be resumed from the UI; refresh and restart cannot silently recreate the old layout or delete a replacement that reuses a key.
- Preserve unrelated chimes and keep separate-member selections independent on later HA group refreshes. Carry grouping preferences in the existing backup/transfer mechanism.
- Test chime now explicitly uses the paired doorbell's Protect volume, ringtone and repetition settings. Respect 0% without sending playback. If multiple paired doorbells have conflicting settings, refuse to guess and direct testing to Protect.

# 0.3.41 sync2

- Give Remove the red destructive-action text/icon and subtle red hover/pressed background used by Home Assistant.
- Keep the actions menu open during and after Test chime. Prevent duplicate requests while pending and show confirmation/errors in the open menu.
- Align the complete HTTP response before headers are sent; increase the bounded group-download window from 300 ms to one second. Release immediately when all distinct downloads arrive; skip HEAD, tiny probes and later ranges.
- Expose address-free download timing in the playback status: expected/observed downloads, arrival spread and release reason. Record late arrivals and retain bounded diagnostics for 60 seconds.
- Retain announcements and existing music playback semantics. Download alignment does not provide a shared acoustic playback clock; real speaker synchronisation still needs an audible user test.

# 0.3.40 actions1

- Replace the direct remove button on added chimes with an anchored actions menu: Test chime and Remove, with aligned vector icons. Keep the existing adoption-repair action available only when required.
- Send explicitly chosen tests through the authenticated, certificate-pinned Protect management session. Validate the local identity against Protect and reject foreign, ambiguous, offline or unadopted targets. No playback occurs during rendering, refresh or account checks.
- Keep the existing removal confirmation. Explain when a Protect account or online/adopted chime is needed for testing.
- Use consistent paragraph size and spacing in the combined sidebar panel; retain List as the default left-hand view option.

# 0.3.39 products1

- Read physical HA product metadata as well as media-player metadata. Resolve related device registrations by explicit parent, exact hardware connection/identifier, or a unique same-manufacturer serial match.
- Click a speaker image to choose its physical HA product when no reliable automatic relationship exists. Store the artwork-only association privately in app data and bind it to the source device ID; restore automatic matching from the same dialog.
- Match rich product descriptions to a unique public catalogue brand/model code, marking the result as illustrative. No installation-specific model aliases are included.
- Preserve speaker names, integration labels, grouping, playback targets, adoption and audio settings. Product metadata edits do not configure or play chimes.
- Combine the sidebar information panels and remove the duplicate large chime image.

# 0.3.38 nft1

- Use one dedicated nftables NAT table per chime, with hook priority -110 before standard Docker destination NAT (-100), regardless of Docker's legacy, nft-compatible or native nftables backend.
- Validate and atomically commit each complete ruleset; persist active/pending ownership for crash recovery and reject altered or foreign tables.
- Install the replacement before removing validated legacy AnyChime iptables rules. Never change host alternatives, Docker tables, firewall policies or unrelated rules.
- Include the nftables userspace tool. Require Linux 4.18+, kernel nftables NAT and the existing NET_ADMIN permission; report unsupported hosts rather than guessing a backend.
- Add simulated migration, rollback, ownership and crash-recovery coverage. Debian/Supervised and Docker backend combinations still require independent integration testing before being advertised as verified.

# 0.3.37 ownership1

- Compare owned management-forward rules by validated option values rather than printed option order; iptables may reorder flags when listing them.
- Retain strict checks on owner comment, console source, owned destination, port and target, and reject extra/changed options.
- Delete the exact validated listed rule and preserve all unrelated rules.
- Report actionable network setup errors instead of only the exception class.

# 0.3.36 nat1

- Preserve the owned chime destination IP when translating management port 8080 to the free listener port.
- Migrate and remove earlier owned REDIRECT rules using their persisted format.

# 0.3.35 shutdown1

- Match management forwarding by verified chime address and console source; do not assume the macvlan name is visible at the netfilter ingress hook.
- Handle HA stop/restart signals gracefully so owned management forwarding, DHCP processes and interfaces are cleaned up.
- Allow up to 30 seconds for normal app shutdown; retain private ownership state for recovery after an ungraceful stop.

# 0.3.34 generic1

- Rename visible branding to Protect AnyChime while keeping the installed app slug and device identities stable.
- Restore the header chime logo without depending on the window load event.
- Handle Protect playBuzzer through the same received-track playback pipeline as playSpeaker; do not ship a buzzer recording.
- Restrict certificate inspection and trust changes to the authenticated HA ingress proxy; reject loopback and spoofed forwarded headers.
- Give the primary chime a dedicated DHCP interface too; bind native management to a free port and expose 8080 through owned-address/source-scoped owned forwarding rules.
- Remove installation-specific renderer image aliases and the old per-model bundled photo map. Use shared public product metadata and generic HA capabilities.

# 0.3.33 portable1

- Remove the bundled controller certificate and installation-specific address defaults.
- Use each installation's confirmed certificate and private HA app data; recover legacy pin-only trust only on an exact match.
- Start ringtone storage empty and receive default/custom audio through native Protect uploads; remove all bundled recordings.
- Keep speaker setup silent and play only Protect-requested, Protect-delivered tracks.
- Replace development-network test values with synthetic fixtures and generate test certificates at runtime.
- Add publication boundaries, source hygiene checks and portable documentation.

# 0.3.32 delivery1

- Disable small TCP packet buffering on audio responses to reduce network-dependent delivery skew.
- Do not count tiny byte-zero probes as a speaker ready for the group announcement.
- Keep native announcements, the bounded download-alignment window and existing music playback.
- Add a central PROJECT.md covering scope, architecture, installation, persistence, testing and known limits.

# 0.3.31 align1

- Keep native announcements and the current music volume for Sonos groups.
- Give each group announcement a unique local media URL and align initial audio downloads across requesting speakers.
- Wait at most 300 ms from the first download; release early when all distinct speaker clients have arrived.
- Exclude HEAD, later byte ranges and duplicate clients from the arrival count; isolate repeated rings and fail open on missing clients.
- This improves delivery timing only; independent speaker clocks, buffers and firmware can still cause audible skew. No shared playback clock or guaranteed acoustic synchronisation is claimed.

# 0.3.30 announce1

- Show HA-reported announcement support per speaker/group, including unknown and mixed support.

- Prefer Home Assistant announcements on supported speakers, including groups.
- Pass the Protect gong volume as a separate announcement volume without changing music volume.
- Deliver Sonos group announcements to reachable, unmuted members; preserve coordinator semantics elsewhere.
- Keep standard playback for unsupported players; never retry rejected announcements with ordinary playback.
- Expose the requested playback mode in diagnostics; leave identities, grouping and adoption intact.

# 0.3.29 artwork2

- Resolve alternative product names, including Apple TV 4 and A1625 as Apple TV HD.
- Search for related generations within the same manufacturer before using a generic icon.
- Mark related-model images as illustrative; retain exact metadata matches over alternatives.
- Preserve device names, model labels, availability and playback settings.

# 0.3.28 onboarding1

- Use the original HA device name and physical parent-device model as additional artwork metadata.
- Use additional HA metadata for artwork recognition.
- Keep speaker names, displayed model/manufacturer labels and playback targets unchanged.
- Never identify a product using an unqualified numeric model ID or an unrelated hub name.
- Copy speaker/group names to verified adopted chimes that still use generic Protect names; preserve custom Protect names.
- Offer adoption repair only for rejected existing pairings, not brand-new chimes.
- Respond within a recent authorised Protect scan when a newly added identity becomes ready.

# 0.3.27 tooltips1

- Show offline information in a small hover/focus tooltip, also available by tap.
- Replace the idle Waiting for Protect label with No chimes added and an add-speaker hint.
- Distinguish adoption, preparation, offline, reconnecting and setup errors in the connection badge.

# 0.3.26 availability1

- Match HA manufacturer/model metadata against the UniFi client artwork catalogue.
- Download small official icons on demand and cache them locally in HA app data.
- Prefer exact matches; use explicit related-model substitutes such as One SL → One.
- Explain possible image differences at the bottom of the page and in substitute tooltips.
- Keep reported device names and models unchanged.
- Block adding offline speakers in the UI and API; show an information button instead of +.
- Retain paired offline speakers while disconnecting their virtual chime in Protect; reconnect with the same identity when online.
- Refresh availability automatically and sort offline speakers last within each section.

# 0.3.25 vieworder1

- Place List before Tiles in the view switch.

# 0.3.24 cleanup1

- Purge mappings, device keys, chime tracks and owned network state after confirmed removal.
- Re-adding a removed speaker creates a fresh virtual device identity.
- Authenticate and verify retained inactive history before deleting obsolete owned Protect records and local state.
- Wait for Protect factory-reset acknowledgement before teardown; preserve active identities and failed-removal recovery state.
- Show management menus only when there is an actionable repair.

# 0.3.23 menufix1

- Hide the management menu for available speakers with retained identity history.
- Show the menu only for selected active chimes that may need adoption repair.
- Connected chimes no longer open an empty management menu.

# 0.3.22 icons1

- Centre add/remove symbols using consistent drawn strokes instead of font glyphs.
- Centre action buttons vertically in list rows.
- Default to List while remembering an explicitly chosen view.

# 0.3.21 loading1

- Use a small official 256px UniFi chime icon instead of the 4 MB product render in the web UI.
- Start decorative image requests after the document loads, so HA ingress cannot hide the ready UI behind its loading screen while an image stalls.
- Keep speaker discovery and Protect configuration unchanged.

# 0.3.20 products1

- Discover manufacturer, model and model identifier from HA; show manufacturer and model beneath speakers.
- Match bundled product photos exactly by manufacturer and model, including One SL and Play:1.
- Show neutral TV, receiver or speaker artwork for unknown products; never show album artwork as a product photo.
- Keep photos local and display individual member photos within group tiles.

# 0.3.19 dragdrop1

- Drag available speakers or HA groups into Chimes in Protect to add them immediately.
- Remove with ×; return speakers to Available only after Protect confirms deletion.
- Replace checkboxes and Save with direct actions; + supports touch and keyboard.
- Serialize actions, retain failed changes visibly and preserve one chime per group.

# 0.3.18 uncheckremove1

- Unchecking a speaker or group and saving removes its virtual chime from Protect through the configured local account.
- Verify all requested identities before removal; keep failed removals visible and prevent rediscovery until resolved.
- Save the selection only after confirmed removals. Preserve physical speakers, groups and other chimes.
- Remove separate Remove buttons; previous-chime cleanup uses the same checkbox and Save selection flow.

# 0.3.17 readopt1

- Record controller handshake rejection status without disclosing credentials.
- Repair stale local adoption after a 401/403 only when an authenticated Protect read confirms the saved identity is absent or unadopted and disconnected.
- Preserve selection, identity keys, MAC/UUID and ringtones; advertise for adoption again without deleting or adopting controller records.
- Add a confirmed Make adoptable action to the speaker menu.

# 0.3.16 selectionsections1

- Classify Added as chimes using the saved active selection only.
- Retained inactive identities no longer keep unselected speakers in Added; group tiles return to Available.
- Preserve identity history for explicit reuse and previous-chime management, without reactivating anything.

# 0.3.15 groupchime1

- One virtual Protect chime per HA speaker group, retaining a stable existing identity when grouping or changing coordinator.
- Send media once to the coordinator, set volume for reachable unmuted members, and let HA handle synchronous playback.
- Suppress per-device announce mode for groups; fail before audio writes when reported membership changes.
- Automatically refresh group metadata without playing confirmation sounds.
- Keep obsolete individual identities for confirmed cleanup through Previous chimes.
- Transfer an existing working local account to HA app configuration without asking for another login.
- Include Added/Available sections, Tiles/List views and configuration changes from 0.3.14.

# 0.3.14 speakerviews1

- Separate Added as chimes and Available speakers based on saved configuration; keep HA groups intact.
- Add Tiles/List controls with a remembered browser preference.
- Move local Protect credentials to the HA app Configuration tab; remove login forms from the web interface.
- Read credentials from app options for confirmed removals, keep console trust internal, and disable legacy credential-writing endpoints.

# 0.3.13 management1

- Add independent local UniFi username/password authentication for optional Protect management.
- Use private account storage, pinned HTTPS, session cookies and CSRF; status and logs never return credentials.
- Add a confirmed Remove chime action per individual speaker, including members of grouped tiles.
- Resolve remote IDs only from this installation’s saved identities; no generic device delete or API proxy.
- Pause before deletion and retain identities; failed/ambiguous requests are not automatically retried.
- Keep Protect-to-AnyChime removal synchronisation from 0.3.12.

# 0.3.12 protectremove1

- Explain removal through Protect beside the selection controls; unselecting an existing chime asks for confirmation and explains that saving only pauses it.

- Handle Protect factoryReset as removal of the linked virtual chime: persist deselection, stop advertising and reset adoption credentials while retaining MAC, UUID and keys.
- Acknowledge removal before closing the controller socket; do not reconnect until explicitly selected again.
- Synchronise saved selection in the UI without replacing unsaved user edits.
- Clarify that deselection in AnyChime pauses chimes and does not delete Protect records.
- Offline deletions cannot be detected without an authenticated Protect management connection.

# 0.3.11 grouptiles1

- Display matching HA speaker groups as one tile with one checkbox and member images/names.
- Preserve mixed selections until the checkbox is changed; saving retains one chime per member.
- Conflicting group metadata stays separate; filtered devices are never reintroduced.
- No HA join/unjoin or playback requests occur during discovery or rendering.

# 0.3.10 compactstatus1

- Remove the connection information and troubleshooting block.
- Show the Protect console IP under the connection badge.
- Keep a simple setup prompt only when confirmation or connection repair is needed; fingerprints remain internal.

# 0.3.9 habackup1

- Use Home Assistant app backups for private state, including identities, connection, selections and ringtone data.
- Request cold backups for a consistent snapshot while the app is briefly stopped.
- Remove custom migration/export/import controls from the web interface.

# 0.3.8 connectionui1

- Remove the duplicate per-speaker connection summary from the sidebar; retain bridge-level errors.

- Simplify connection setup to Connect to Protect and Confirm console; certificate pinning stays automatic after confirmation.
- Move the read-only console address to Connection information; editing and fingerprints are under Troubleshooting.
- Change the tagline to Any speaker can be a chime.

# 0.3.7 reconnect1

- Reconnect after temporary transport failures or closed controller sockets with backoff capped at 60 seconds.
- Configuration, TLS and protocol failures still stop after three consecutive failures.
- Reconnection never replays sound requests or regenerates device identities.

# 0.3.6 profile1720

- Advertise compatibility profile 1.7.20 consistently in discovery, device info and controller connection headers.
- Retire saved profile 1.3.7 automatically to match the firmware catalogue observed on Protect 7.3.70.
- No vendor firmware is downloaded or installed; firmware commands remain explicitly rejected.

# 0.3.5 networkparent1

- Recognise same-namespace named parent links from iproute2 JSON, allowing extra chime network interfaces to pass ownership verification.
- Continue rejecting mismatched parents, foreign namespaces, MACs, ownership labels and interface types.
- Report which network check failed.

# 0.3.4 offline1

- Use Offline for speakers that are currently unavailable or powered off, distinct from unsupported audio routes.

# 0.3.3 capabilities1

- Hide entities without advertised media playback or with unknown capability metadata.
- Hide webOS integration entities because their play_media handler only implements channel playback; other audio routes for the same TV remain eligible.
- Use the HA configuration entry domain rather than device names or TV model lists.
- Saved excluded entities do not reappear in selection; save to remove their bridge assignment.

# 0.3.2 thirdparty1

- Exclude native UniFi devices from speaker discovery and new selections using device manufacturer metadata.
- Previously selected UniFi devices are hidden too; saving the selection removes them from the bridge.
- User-defined names never determine device exclusion.

# 0.3.1 diagnostics1

- Startup logs show whether a saved identity, speaker selection and console certificate are present.
- Waiting-for-setup and discovery failures are now visible in the app log without private data.
- Migration export retains a verified cached connection if a store refresh cleared rendered options.

# 0.3.0 rename1

- Installation ID and source directory renamed to unifi_anychime.
- Ingress/media port 48200 allows the transfer interface to coexist with a previous app.
- Explicit migration backup/import preserves private device identities and pairing.
- Import validates file hashes and paths and refuses configured installations.
- Generic manufacturer identifiers and configuration contain no legacy project branding.

# 0.2.1 protect1

- Independent native Protect chime identity per selected HA media player.
- Preserve the existing primary identity; DHCP/macvlan addresses for extra chimes.
- Remove brand eligibility filters and global volume/playback controls.
- Native original/custom MP3 track upload, deletion, metadata and hashed media delivery.
- Protect-selected track, volume and repeat translated to one generic HA player.
- Original success confirmation requested once at 10% for newly selected speakers.
- NET_ADMIN/NET_RAW needed for owned extra network interfaces; main HA networking preserved.
- Local tests and limited real loopback lab pass; HA deployment and LAN operation remain unverified.

# 0.2.1 managed1

- HA configuration now contains only playback enable and maximum volume controls.
- Virtual chime starts with the app; its LAN address is selected automatically.
- Existing console connection, compatibility profile and sound source move to private settings.
- New installations use the gateway as a console candidate, with an editable address under Connection.
- Read-only certificate fingerprint, public-certificate refresh and explicit confirmation of changes.
- Confirmation does not send credentials, play audio or regenerate the virtual identity.
- Original short built-in default chime for new installations; existing sound sources are preserved.
- Custom Protect sound synchronisation is still not implemented.
- Local validation covers migration, automatic addressing, certificate confirmation and routing.

# 0.2.1 tiles1

- English interface, configuration labels, help text, logs and operational errors.
- Generic speaker configuration with automatic migration of old settings.
- Speaker tiles with search, availability, saved selection and exact-model product photos.
- Official Ubiquiti PoE chime artwork used for the app icon and interface.
- Preserved installation ID, adoption identity, sound URL, playback state and volume cap.
- 60 local tests pass; HA runtime verification is still required.

# 0.2.1 speakers1

- Discovery and selection of existing HA media players.
- Multiple supported speakers share the existing Protect chime.
- Persistent, live target selection and duplicate-group checks.

# 0.2.1 firmware1

- Explicit refusal of vendor firmware commands without download or installation.
- Consistent emulated compatibility profiles 1.3.7 and 1.7.20.
- Global Protect update preferences remain unchanged.

# 0.2.1 ucpfix1

- Accepted Protect's missing WebSocket subprotocol echo while rejecting an explicit mismatch.
- Preserved certificate pinning, client identity and request validation.
- The virtual chime was adopted, stayed connected and received a silent chime request.

# 0.2.1

- Moved the ingress listener to port 48199 to avoid a host port conflict.
- Bound the web listener before starting native services.

# 0.2.0

- Initial native discovery and adoption prototype.
