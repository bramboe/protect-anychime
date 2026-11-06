# Protect AnyChime

Any speaker can be a chime. Home Assistant app for forwarding UniFi Protect chime requests to Home Assistant media players. Independent project; not an official Home Assistant or Ubiquiti product.

## Install in Home Assistant

1. Open **Settings → Apps → Install app** (called **Add-ons → Add-on store** in older HA versions).
2. Open the **⋮** menu → **Repositories**.
3. Add `https://github.com/bramboe/protect-anychime`.
4. Find **Protect AnyChime**, install it, start it and open its web interface.
5. Connect your Protect console, add HA speakers to a chime, then adopt and configure that chime in Protect. Optional automatic adoption does not configure doorbell pairing, ringtone or volume.

The repository root contains `repository.yaml`; the installable app lives in [`unifi_anychime/`](unifi_anychime/). The internal slug remains `unifi_anychime`. Moving the source into this layout does not require resetting existing chimes. Switching from a local app to a separately hosted repository can produce a different HA app ID: use the documented backup/export migration instead of running both bridges with the same identities.

See the [app guide](unifi_anychime/README.md), [configuration and behavior](unifi_anychime/DOCS.md), [project architecture](unifi_anychime/PROJECT.md), [release notes](unifi_anychime/CHANGELOG.md) and [security model](docs/SECURITY.md).

## Development

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s unifi_anychime/app -p 'test_*.py'
python3 tools/check_release.py
node tools/test_chime_status.js
```

Build containers with `unifi_anychime/` as their build context. Never commit HA runtime data, credentials, certificates, device identities or cached recordings. [Publication notes](docs/PUBLICATION.md) describe the release boundary.

## Support

If this project is useful to you, consider sponsoring my work 💙

[![Sponsor on GitHub](https://img.shields.io/badge/Sponsor-GitHub-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/bramboe) [![Donate via bunq.me](https://img.shields.io/badge/Donate-bunq.me-2E9AD0)](https://bunq.me/bramboe)

## License

Project code is licensed under the [MIT License](LICENSE). Bundled SortableJS retains its own MIT notice. Manufacturer artwork and catalogue data have separate ownership and terms; see [artwork sources](unifi_anychime/ASSETS.md). MIT does not grant rights to third-party artwork or trademarks.
