# Protect AnyChime

[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/bramboe?frequency=one-time) [![Donate via bunq.me](https://img.shields.io/badge/Donate-bunq.me-2E9AD0)](https://bunq.me/bramboe)

Any speaker can be a chime. Home Assistant app for forwarding UniFi Protect chime requests to Home Assistant media players. Independent project; not an official Home Assistant or Ubiquiti product.

## Install in Home Assistant

1. Open **Settings → Apps → Install app** (called **Add-ons → Add-on store** in older HA versions).
2. Open the **⋮** menu → **Repositories**.
3. Add `https://github.com/bramboe/protect-anychime`.
4. Find **Protect AnyChime**, install it, start it and open its web interface.
5. Connect your Protect console, add HA speakers to a chime, then adopt and configure that chime in Protect. Optional automatic adoption does not configure doorbell pairing, ringtone or volume.

The repository root contains `repository.yaml`; the installable app lives in [`protect_anychime/`](protect_anychime/). The internal slug is `protect_anychime`. Existing installations using the previous app ID must transfer their saved data before starting the renamed app. Switching from a local app to a separately hosted repository can produce a different HA app ID: use the [app ID migration guide](protect_anychime/DOCS.md#moving-from-the-previous-app-id) instead of running both bridges with the same identities.

See the [app guide](protect_anychime/README.md), [configuration and behavior](protect_anychime/DOCS.md), [project architecture](protect_anychime/PROJECT.md), [release notes](protect_anychime/CHANGELOG.md) and [security model](docs/SECURITY.md).

## License

Project code is licensed under the [MIT License](LICENSE). Bundled SortableJS retains its own MIT notice. Manufacturer artwork and catalogue data have separate ownership and terms; see [artwork sources](protect_anychime/ASSETS.md). MIT does not grant rights to third-party artwork or trademarks.
