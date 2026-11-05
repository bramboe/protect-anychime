# Release 0.4.2 verification

## Automated boundaries

315 Python tests passed with fake HA/Protect transports and synthetic media fixtures. New cases cover rejected state reads, volume writes and playback requests; independent successful repetitions; coordinator failures affecting only represented members; a fresh ring attempting a previously failed member; and later total failure retaining earlier accepted calls. Upstream response text is not exposed.

Ingress tests reject IPv4/IPv6 loopback and forged forwarding headers, verify the removed preview route and reject the obsolete request header. Repository checks verify the installable subfolder, license and enforcement-mode AppArmor configuration. JavaScript syntax checks and the source release hygiene check passed.

The isolated browser fixture passed 20 membership, menu, timing, list/tile and confirmation checks plus 7 fixed-menu/outside-click checks. All transports were mocked; no real HA playback service was called.

## HA OS check

The existing installation reported rating 5 and the default AppArmor profile before updating. After installing 0.4.2, Supervisor reported rating 6, `apparmor: profile` and `protected: true`. Read-only checks confirmed the existing virtual chime connected to Protect, identical saved identity/key/member lists, and no migration review required. The removed preview route returned 404.

No real speaker audio was played. Acoustic behavior, every permitted subprocess path and other HA/container hosts are not established by this silent verification. Native chime startup and connection were exercised under the custom profile. The full remaining permission set and rating calculation are described in [SECURITY.md](SECURITY.md).

## Publication boundary

The public repository URL is still an owner-supplied publication setting; no repository was created or pushed. The root README explicitly marks the URL placeholder. Source archive generation excludes Git internals, credentials, runtime state, cached media and local build artifacts. Third-party artwork rights remain separate from the MIT code license.
