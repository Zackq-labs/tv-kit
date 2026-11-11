# Changelog

## 0.1.0

First public release.

- `tvkit` command line tool (Python 3.8+, standard library only), installable with `pipx`.
- Per-TV record with a fresh UUID v4, validated against `schema.json`.
- Pinned APKs in `kit.json`, verified by sha256 and signer certificate before anything is installed.
- Read-only baseline, Wi-Fi check, install (asks "yes" per step), after-check diff, undo.
- Off-site support: `offsite-sheet` and `usb-pack` for a TV at another address that reaches only a requests page URL you choose.
- The requests page URL is a per-TV / per-kit setting with no built-in default.
- Build script for Free My TV (pinned upstream commit, your own signing key). No APKs or keys are included.
- Tests run against a fake adb; no TV or network needed.
