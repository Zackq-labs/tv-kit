# Runbook: setting up a new TV

Generic order of work for one TV. Commands are `tvkit ...`; add `--tvs`, `--artifacts`, `--work` if you
keep those folders somewhere other than the default `~/.local/share/tvkit`.

Rules: every TV gets its own record and UUID; the owner is present and says go; every change asks `yes`.

## 0. One-time kit set-up

1. `tvkit init`, then edit `~/.config/tvkit/kit.json`: set `home_networks`, `lan_hostnames`, and optionally
   `requests_url`.
2. Download the TV Bro release APK named in `kit.json` from its upstream releases page into your artifacts folder.
3. Build Free My TV with your own key (README, "Building Free My TV yourself"), copy the APK to the artifacts folder,
   and paste its sha256 and signer_cert_sha256 into `kit.json`.
4. `tvkit check` (no TV needed). Do not continue until it passes. Exit 4 means an APK differs from its pin.

## 1. Make the TV's record

`tvkit new --name "Living Room TV" --site HOME --target <tv-address>:5555`
(for another address: `--site OFFSITE`, no `--target`, optionally `--requests-url https://...`).
Note the printed `tv_id`. Never reuse a record for a different TV; a replaced TV gets a new one.

## 2. Enable debugging on the TV (owner present)

Settings -> System -> About -> press the build entry 7 times. Developer options -> USB debugging On
(and Network debugging for a laptop run). Accept the "Allow USB debugging?" prompt on the TV.

## 3. Read-only checks

1. `tvkit baseline --tv <tv-id>` (a snapshot is written under your work folder; nothing changes on the TV).
2. `tvkit wifi-check --tv <tv-id>`. Exit 3 means stop: the TV's setup app saved the Wi-Fi, so re-save the Wi-Fi on
   the TV first, then repeat.

## 4. Install

`tvkit install --tv <tv-id>` prints each step and asks `yes`. Then on the TV: open Free My TV and choose it as the
home screen when asked.

## 5. After-check

`tvkit after --tv <tv-id>`. Exit 2 means something differs from what is expected. Read the diff; use `tvkit undo`
if in doubt.

## 6. Optional: requests page

Set `requests_url` in the record, then `tvkit open-requests --tv <tv-id>`.

## 7. Undo

`tvkit undo --tv <tv-id>`: puts the original home screen back, then walks you through restoring anything else from
the baseline, asking `yes` at each step.

## A TV at another address (no adb there)

1. `tvkit new --name "Friend's TV" --site OFFSITE --requests-url https://requests.example.com/`
2. `tvkit usb-pack --tv <tv-id> --out /path/to/usb-stick`. It re-checks every APK first, then writes the APKs,
   `SHA256SUMS`, and the owner's step sheet.
3. Stay on the phone while the owner follows the sheet (`tvkit offsite-sheet --tv <tv-id>` prints it).
4. Alternatively, go there with a laptop and run steps 3 to 5 with `--target <the TV's address>`; the target is
   never stored.
