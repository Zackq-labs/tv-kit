#!/usr/bin/env bash
# Builds a Free My TV APK from the commit pinned in kit.json, signed with YOUR key. No sudo.
#
#   TVKIT_KEYSTORE=/path/to/your.keystore TVKIT_KEYSTORE_PASS=... \
#     build/build-free-my-tv.sh [work-dir]
#
# - Clones the upstream repo named in kit.json into <work-dir>/src (default
#   ${XDG_DATA_HOME:-$HOME/.local/share}/tvkit/build) and checks out exactly the pinned commit. A different commit is
#   a kit change: update kit.json, review the diff, bump the version.
# - Uses config.properties.template next to this script (no secrets in it). The weather city is NOT baked per TV: every TV
#   sets its own in Home Settings -> City. The build's latitude/longitude is only the default before that.
# - Refuses to sign with upstream build.sh's auto-generated throwaway key: set TVKIT_KEYSTORE (+ TVKIT_KEYSTORE_PASS) to
#   your own keystore, which must live outside any git checkout. Android needs the same key for every later update.
# - Prints the APK's sha256 and signer cert sha256 to paste into kit.json. It never edits kit.json or installs anything.
# - Env: TVKIT_KIT (kit.json to read), TVKIT_TOOLCHAIN (folder holding jdk/ and sdk/), or JAVA_HOME / ANDROID_HOME.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KIT_JSON="${TVKIT_KIT:-$HERE/../src/tvkit/data/kit.json}"
WORK="${1:-${XDG_DATA_HOME:-$HOME/.local/share}/tvkit/build}"
TOOLS="${TVKIT_TOOLCHAIN:-${XDG_DATA_HOME:-$HOME/.local/share}/tvkit/toolchain}"

COMMIT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifacts"]["free-my-tv"]["source"]["commit"])' "$KIT_JSON")
REPO=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifacts"]["free-my-tv"]["source"]["repo"])' "$KIT_JSON")

[ -n "${TVKIT_KEYSTORE:-}" ] && [ -f "$TVKIT_KEYSTORE" ] || {
  echo "REFUSED: TVKIT_KEYSTORE is not set or not a file. Use your own signing key, kept outside git; see README.md 'Building Free My TV yourself'." >&2
  echo "         Without it, upstream build.sh would sign with its throwaway default key, which the kit does not accept." >&2
  exit 1
}
[ -n "${TVKIT_KEYSTORE_PASS:-}" ] || { echo "REFUSED: TVKIT_KEYSTORE_PASS is empty (the keystore and key password)." >&2; exit 1; }
KS_DIR="$(cd "$(dirname "$TVKIT_KEYSTORE")" && pwd)"
if git -C "$KS_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "REFUSED: the keystore must not live inside a git checkout." >&2
  exit 1
fi

export JAVA_HOME="${JAVA_HOME:-$TOOLS/jdk}"
export ANDROID_HOME="${ANDROID_HOME:-$TOOLS/sdk}"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$PATH"
[ -x "$JAVA_HOME/bin/javac" ] || { echo "JDK not found at $JAVA_HOME (set JAVA_HOME or TVKIT_TOOLCHAIN)." >&2; exit 1; }
[ -d "$ANDROID_HOME/build-tools" ] || { echo "Android build-tools not found under $ANDROID_HOME." >&2; exit 1; }
BT="$ANDROID_HOME/build-tools/$(ls "$ANDROID_HOME/build-tools" | sort -V | tail -1)"

mkdir -p "$WORK"
SRC="$WORK/src"
if [ ! -d "$SRC/.git" ]; then
  git clone -q "$REPO" "$SRC"
fi
git -C "$SRC" fetch -q origin
git -C "$SRC" checkout -q --detach "$COMMIT"
git -C "$SRC" clean -fdxq -e config.properties
ACTUAL=$(git -C "$SRC" rev-parse HEAD)
[ "$ACTUAL" = "$COMMIT" ] || { echo "REFUSED: checkout is $ACTUAL, kit.json pins $COMMIT" >&2; exit 1; }

cp "$HERE/config.properties.template" "$SRC/config.properties"
# Upstream build.sh signs with FREEMYTV_KEYSTORE when set (env password), and only otherwise makes its throwaway key.
export FREEMYTV_KEYSTORE="$TVKIT_KEYSTORE" FREEMYTV_KEYSTORE_PASS="$TVKIT_KEYSTORE_PASS"
( cd "$SRC" && bash ./build.sh )

OUT="$WORK/FreeMyTV-${COMMIT:0:8}-tvkit.apk"   # -tvkit: signed with your key, not upstream's throwaway key
cp "$SRC/FreeMyTV.apk" "$OUT"
SHA=$(sha256sum "$OUT" | cut -d' ' -f1)
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"
echo
echo "Built $OUT"
echo "  commit             $COMMIT"
echo "  sha256             $SHA"
"$BT/apksigner" verify --print-certs "$OUT" | sed -n 's/^Signer #1 certificate SHA-256 digest: /  signer_cert_sha256 /p; s/^Signer #1 certificate DN: /  signer_dn          /p'
echo
echo "Next: paste file/sha256/signer_cert_sha256 into your kit.json (see 'tvkit init'), copy the APK to your artifacts folder,"
echo "      then run tvkit check. Never commit the APK or the keystore."
