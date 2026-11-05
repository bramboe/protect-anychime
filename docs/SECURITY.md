# Security model

The app runs in HA's protected mode. It does not request Docker control, host PID access, full access, Supervisor administration or kernel module loading. Home Assistant API access is needed for speaker discovery and the bounded media-player service adapter. It does require host networking, NET_ADMIN for app-owned virtual interfaces and forwarding rules, and NET_RAW for DHCP. Removing those permissions would disable native chime networking.

The custom `unifi_anychime/apparmor.txt` runs in enforcement mode. It permits writes only to private app data, temporary files and the packet-filter lock, with a fixed executable allowlist. It keeps application code and host system files read-only. The profile retains the existing network capabilities rather than requesting new ones. Container startup, DHCP renewal, nftables and graceful shutdown must be checked on each supported HA host; a source-only check cannot establish that a host loaded the profile.

According to HA's security rating formula, base 5 plus ingress 2 minus privileged network capabilities 1 minus host networking 1 yields 5. A successfully loaded custom AppArmor profile adds 1, yielding **6**. An unsupported or failed profile load does not earn that point. Release 0.4.2 was checked on HA OS: Supervisor reported `rating: 6`, `apparmor: profile` and `protected: true`, with the existing chime connected. This does not establish compatibility on every supported host. A score above 6 needs a different network architecture, not a manifest-only change. HA's score is a permissions heuristic, not a security audit.

The HTTP interface trusts only the real Supervisor ingress peer (`172.30.32.2`), never loopback or client-provided forwarding headers. Mutations also require `X-AnyChime-Request: 1`, an intent marker rather than a password or replacement for ingress authentication. Static UI and read-only API access use the same peer restriction. Uploaded and cached speaker media are separately served to speakers; their bounded paths do not grant access to administration or arbitrary files.

There is no development preview endpoint or disabled onboarding playback flow. Protect certificate trust and management sessions remain private and pinned per installation. Status never includes passwords, tokens or arbitrary upstream response text. Failed independent outputs do not trigger retry or a less restrictive playback fallback.

Report issues privately to the repository maintainer once the public project URL and contact are configured. Do not attach HA backups, credentials, certificates or identity directories to a public report.

References: [HA security rating](https://contributors.home-assistant.io/apps/reference/security-rating/) and [AppArmor guidance](https://contributors.home-assistant.io/apps/guides/security/).
