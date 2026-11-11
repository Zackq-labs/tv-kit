# tvkit

One standard-library-only Python CLI to set up an Android / Google TV over `adb`, the same way every time.

- a **per-TV record** (a JSON file with its own UUID)
- **pinned APKs**, verified by sha256 **and** signer certificate before anything is installed
- a **read-only baseline** of the TV before any change
- **install**, then an **after-check** that diffs the TV against the baseline
- **undo**
- a **USB pack** for a TV in another house, where nobody can run adb

Python 3.8+, no runtime dependencies. Needs `adb` on the machine that runs it.

## Who it is for

People who set up TVs for themselves, family or friends and want a repeatable, reviewable procedure instead of
a pile of one-off adb commands: swap the stock launcher for a lighter home screen (Free My TV) and add a
TV-friendly browser (TV Bro), without losing track of what was changed.

## Safety model

1. **Read-only first.** `check`, `baseline` and `wifi-check` change nothing on the TV.
2. **Every change asks.** Any step that changes a TV prints exactly what it will do and waits for you to type `yes`.
3. **Pins, not trust.** `install` refuses any APK whose sha256 or signer certificate differs from `kit.json`.
4. **Undo.** `tvkit undo` walks back through the changes using the baseline, one confirmed step at a time. It
   needs a baseline taken before `install`, and `install` can leave a TV without a working launcher if it goes
   wrong (see Limitations).
5. **No screenshots.** The TV may be showing someone's programme; tvkit never captures the screen.
6. **Off-site TVs stay off your network.** An OFFSITE record, profile and sheet may not mention private
   addresses, `.local` / `.lan` names or the machine names you list in `kit.json`; tvkit refuses them.
7. **The owner is present** and says go. tvkit is not a remote-administration tool.

## Install

```
pipx install .          # from a checkout
tvkit --help
```

From the source tree without installing: `PYTHONPATH=src python3 -m tvkit --help`.

## Quick start (no real TV needed)

The tests drive every command against `tests/fake_adb.py`. To look around by hand:

```
tvkit init                                   # copy kit.json + profiles to ~/.config/tvkit to edit
tvkit new --name "Living Room TV" --site HOME --target 192.0.2.41:5555
tvkit check --tv <tv-id>                     # verifies the artifact pins and the record (no TV needed)
```

`check` verifies the APKs first, so on a fresh install it exits 4 until you have put the APKs in your artifacts
folder and your own Free My TV hash and signer in `kit.json` (see "kit.json and pinning" and "Building Free My TV
yourself"). `new` and the tests do not need them.

`examples/tvs/` holds a fictional record that validates against `schema.json`. With a real TV:

```
tvkit baseline  --tv <tv-id>   # read-only
tvkit wifi-check --tv <tv-id>  # read-only
tvkit install   --tv <tv-id>   # asks "yes"
tvkit after     --tv <tv-id>   # read-only diff, exit 2 if something is off
tvkit undo      --tv <tv-id>   # asks "yes" per step
```

Follow [RUNBOOK.md](RUNBOOK.md) for the full order.

## Commands

| Command | What it does |
|---|---|
| `init` | Copy the bundled `kit.json` and `profiles/` into an editable folder. |
| `new` | Mint a per-TV record with a new UUID (local file only). |
| `check` | Verify every artifact's sha256 and signer (needs the APKs and your Free My TV pin), then the TV record if `--tv` is given. No TV needed. |
| `baseline` | Read-only snapshot of the TV before anything changes. |
| `wifi-check` | Read-only: stop if the TV's setup app saved the Wi-Fi (disabling it would forget it). |
| `install` | `adb install` the APKs after the checks. Asks `yes`. |
| `after` | Read-only diff against the baseline. Exit 2 if it fails. |
| `open-requests` | Open the TV's requests page URL in TV Bro. Asks `yes`. |
| `undo` | Re-enable switched-off packages, restore home app and screensaver from the baseline (asks `yes` per step), offer to uninstall the kit's apps. |
| `offsite-sheet` | Print the owner's on-TV steps for an OFFSITE TV. |
| `usb-pack` | Checked APKs plus the owner's steps into `<out>/tv-kit` for a USB stick. |

Exit codes: 0 ok, 1 refused or usage, 2 after-check failed, 3 wifi-check says stop, 4 artifact mismatch.

Folders (override with the flags or env vars): records `--tvs` / `TVKIT_TVS`, APKs `--artifacts` / `TVKIT_ARTIFACTS`,
snapshots `--work` / `TVKIT_WORK`; all default under `${XDG_DATA_HOME:-~/.local/share}/tvkit` (or `TVKIT_HOME`).
Also `TVKIT_KIT`, `TVKIT_ADB`, `TVKIT_APKSIGNER`, `TVKIT_TZ`.

## The requests page URL

`open-requests`, `offsite-sheet` and `usb-pack` point a TV at one web page of your choosing, for example a
request form you host. There is **no built-in default**. Set `requests_url` in the TV's record, in `kit.json`,
or pass `--requests-url https://...`. A TV at another address reaches only that URL. If you have no such page,
you do not need `open-requests`.

## kit.json and pinning

`kit.json` lists the artifacts (`free-my-tv`, `tv-bro`) with `file`, `package`, `sha256` and
`signer_cert_sha256`, plus your `home_networks` and `lan_hostnames` (documentation examples ship by default;
replace them with yours via `tvkit init`).

- **TV Bro**: the kit pins version 2.1.6 and its hashes. It does **not** include or mirror the APK.
  Download the official release APK yourself from the upstream releases page
  (<https://github.com/truefedex/tv-bro/releases>), put it in your artifacts folder and run `tvkit check`.
  A file that does not match the pin is refused.
- **Free My TV**: no hash can be shipped, because you build it yourself (next section). The shipped pin is an
  all-zero placeholder that `tvkit check` rejects until you fill in your own. Because every user signs with their
  own key, every user builds a different APK with a different hash; there is no shared hash to compare against and
  the build is not claimed to be reproducible.

APKs and keystores are never part of this repository (`.gitignore` blocks them).

## Building Free My TV yourself

The kit holds only a build script and a pinned upstream commit, not upstream source.

```
export TVKIT_KEYSTORE=/path/to/your.keystore     # your own signing key, outside any git checkout
export TVKIT_KEYSTORE_PASS=...
build/build-free-my-tv.sh [work-dir]
```

It clones the upstream repository at the commit pinned in `kit.json`, applies `build/config.properties.template`
(no keys or tokens baked in), builds with a JDK and the Android SDK build-tools (point `TVKIT_TOOLCHAIN`, or
`JAVA_HOME` and `ANDROID_HOME`, at yours), signs with **your** key, and prints the sha256 and signer certificate
sha256. Paste those into your `kit.json`. It refuses to sign with upstream's throwaway key and refuses a keystore
inside a git checkout. Android needs the same key for every later update, so keep it safe and backed up.

## Legal and credits

tvkit is MIT licensed ([LICENSE](LICENSE)). Third-party credits are in [NOTICE](NOTICE):

- **Free My TV** (simbasjungles/free-my-tv), MIT. Only a build script and a pinned commit live here.
- **TV Bro** (truefedex/tv-bro), copyright Fedir Tsapana, under its own licence terms. This kit ships
  none of it: it pins a version and sha256, and you download the official release yourself. `usb-pack` only copies
  that verified, unmodified file you downloaded onto your own stick.

Not affiliated with, endorsed by or sponsored by Google, Free My TV or TV Bro. Android, Google TV and Google Play
are trademarks of Google LLC.

## Limitations

- You need adb access to the TV (developer options; network debugging for a laptop run). Off-site TVs use the USB pack.
- The automated tests use a fake adb, not a real TV; vendor skins differ. Always read the baseline first.
- `undo` needs a baseline taken before the switch. With it, and one `yes` per step, it re-enables the packages the
  switch disabled, restores the home app and the screensaver setting, and offers to uninstall the kit's apps (not
  ones the baseline already had). It prints the on-TV "Original" steps first. Anything else is restored by hand.
- **Risk:** `install` disables system packages and sets a new home app. If that goes wrong, a TV can be left
  without a working launcher. Take and read the baseline first, keep the TV's "Original" route in mind, and do not
  run `install` on a TV you cannot reach again.
- Disabling the TV's stock launcher / setup app can make some TVs forget a Wi-Fi network the setup app saved; that is
  what `wifi-check` is for.
- Free My TV needs a JDK and Android build-tools to build; the kit does not provide them.
- Software is provided as is, with no warranty. You change your own devices at your own risk.

## Development

```
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```
