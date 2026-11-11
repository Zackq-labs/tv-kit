#!/usr/bin/env python3
"""tvkit: a small, reproducible set-up kit for Android / Google TVs, driven over adb. Python 3.8+, standard library only.

Runs on any computer that has adb. Rules:
  - Every TV gets its own unique ID and its own record. Never reuse one TV's ID for another.
  - Nothing for an off-site TV may be tied to a home network address.
  - Nothing runs on a TV without its owner present, saying go.

Every step that changes a TV prints exactly what it will do and asks for "yes". Read-only steps never ask.
Screenshots are never taken: a TV may be showing someone's program.

Subcommands: init, new, check, baseline, wifi-check, install, after, open-requests, undo, and for an off-site TV
offsite-sheet (prints the owner's on-TV steps) and usb-pack (the checked APKs + those steps for a USB stick, no computer
at the other house). Nothing an off-site TV gets may name a private address, a .local/.lan name or a machine name
listed in kit.json (lan_leaks).
Exit codes: 0 ok, 1 refused or usage, 2 after-check failed, 3 wifi-check says stop, 4 artifact mismatch.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import string
import struct
import subprocess
import sys
import uuid

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PKG_DIR, "data")          # the bundled kit.json, schema.json and profiles/
PLACEHOLDER_SHA = "0" * 64                        # a pin the user has not filled in yet


def data_home():
    """Where records, APKs and snapshots live by default: $TVKIT_HOME, else $XDG_DATA_HOME/tvkit, else ~/.local/share/tvkit."""
    if os.environ.get("TVKIT_HOME"):
        return os.environ["TVKIT_HOME"]
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "tvkit")


def config_home():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "tvkit")


def default_kit_path():
    """--kit, else $TVKIT_KIT, else <config>/kit.json (made by `tvkit init`), else the bundled example kit."""
    if os.environ.get("TVKIT_KIT"):
        return os.environ["TVKIT_KIT"]
    user = os.path.join(config_home(), "kit.json")
    return user if os.path.isfile(user) else os.path.join(DATA_DIR, "kit.json")

SIG_V2_ID = 0x7109871A
SIG_V3_ID = 0xF05368C0
SIG_MAGIC = b"APK Sig Block 42"

SECURE_SETTINGS = ["screensaver_components", "screensaver_default_component", "screensaver_enabled",
                   "enabled_accessibility_services", "accessibility_enabled"]
PROPS = ["ro.product.manufacturer", "ro.product.model", "ro.product.device", "ro.build.version.release",
         "ro.build.version.sdk", "ro.build.fingerprint", "ro.build.version.security_patch"]
HOME_RESOLVE = "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.HOME"
PLAY_STORE = "com.android.vending"
MUTATING_SHELL = ("pm enable", "pm disable", "pm uninstall", "settings put", "settings delete", "cmd package set-home",
                  "am start", "screencap", "screenrecord", "input ", "reboot", "svc ")


class Refused(Exception):
    """A rule said no. The message is for the person at the keyboard; the exit code is the second argument."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------------------------- small helpers

def now_stamp():
    return _now().strftime("%Y%m%d-%H%M%S")


def _tz():
    """The computer's local zone unless TVKIT_TZ names an IANA zone (for example Europe/Berlin)."""
    name = os.environ.get("TVKIT_TZ")
    if not name:
        return None
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:   # no tz database or an unknown name: the computer's local zone
        return None


def _now():
    return datetime.datetime.now(_tz()).astimezone()


def now_text():
    """12-hour time with the zone."""
    return _now().strftime("%a %m-%d %I:%M %p %Z").replace(" 0", " ")


def say(*parts):
    print(*parts, flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CONSENTS = [0]   # how many times a person typed yes in this run; Adb refuses a changing command while it is 0


def _ask(prompt):
    """The word must come from a person at a terminal: a piped or scripted yes is refused (no yes flag, no env, no pipe)."""
    if not sys.stdin.isatty():
        raise Refused("stdin is not a terminal: the TV's owner must type yes here; a piped yes does not count")
    try:
        return input(prompt)
    except EOFError:
        return ""


def confirm(title, lines):
    """Print what is about to change on the TV and ask for the word yes. Anything else skips the step."""
    say("")
    say("ABOUT TO CHANGE THE TV: " + title)
    for line in lines:
        say("    " + line)
    say("The TV's owner must be present and say go.")
    answer = _ask('Type "yes" to do this, anything else to skip: ')
    if answer.strip() != "yes":
        say("Skipped (no \"yes\").")
        return False
    CONSENTS[0] += 1
    return True


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------------------- the kit manifest

def load_kit(path):
    kit = load_json(path)
    kit["_path"] = path
    for key in ("version", "artifacts", "expect", "home_networks", "requests_url"):   # requests_url may be ""
        if key not in kit:
            raise Refused("kit.json has no \"%s\"" % key)
    for name, art in kit["artifacts"].items():
        for key in ("file", "package", "sha256", "signer_cert_sha256"):
            if key not in art:
                raise Refused("kit.json artifact %s has no \"%s\"" % (name, key))
    for net in kit["home_networks"]:
        try:
            ipaddress.ip_network(net)
        except ValueError:
            raise Refused("kit.json home_networks has %r, which is not a network like 192.0.2.0/24" % net)
    return kit


def requests_url_for(args, kit, rec=None):
    """The page the TV is pointed at: --requests-url, else the TV record's requests_url, else kit.json requests_url.
    There is no built-in default host."""
    url = getattr(args, "requests_url", None) or (rec or {}).get("requests_url") or kit.get("requests_url") or ""
    if not url:
        raise Refused("no requests page URL: set \"requests_url\" in the TV record (or kit.json), or pass --requests-url https://...")
    if not re.match(r"^https?://[^\s/]+(/\S*)?$", url):
        raise Refused("requests page URL %r is not an http(s) address" % url)
    return url


def home_networks(kit):
    return [ipaddress.ip_network(n) for n in kit["home_networks"]]


def is_home_address(text, networks):
    """True when text names an address inside one of the home networks (host, host:port, or an IP in a sentence)."""
    for found in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", text):
        try:
            addr = ipaddress.ip_address(".".join(str(int(o)) for o in found.split(".")))   # a leading zero does not change the address
        except ValueError:
            continue
        if any(addr in n for n in networks):
            return True
    return False


LOCAL_SUFFIXES = ("local", "lan", "home", "internal", "localdomain", "home.arpa", "intranet", "corp", "localhost")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.]*\d)")
_IPV6_LOCAL = re.compile(r"(?<![0-9a-f:])(?:fe80|f[cd][0-9a-f]{2}):[0-9a-f:]*[0-9a-f]", re.I)
_LOCAL_NAME = re.compile(r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.(?:%s)(?![\w-])"
                         % "|".join(re.escape(s) for s in LOCAL_SUFFIXES), re.I)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def lan_leaks(text, kit):
    """Everything in text that ties a TV to a home network: a private, loopback, link-local, CGNAT or home-network IP
    (leading zeros too), a fe80::/fc00::/7 IPv6, a .local/.lan/.home.arpa/... name or localhost, or a machine
    name from kit.json lan_hostnames. Empty list = clean. Used for everything tvkit writes or prints for an OFFSITE TV."""
    found = []
    networks = home_networks(kit)
    for raw in _IPV4.findall(text):
        try:
            addr = ipaddress.ip_address(".".join(str(int(o)) for o in raw.split(".")))
        except ValueError:
            continue
        if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr in _CGNAT
                or any(addr in n for n in networks)):
            found.append(raw)
    found += _IPV6_LOCAL.findall(text)
    found += [m.group(0) for m in _LOCAL_NAME.finditer(text)]
    if re.search(r"(?<![\w.-])localhost(?![\w-])", text, re.I):
        found.append("localhost")
    for name in kit.get("lan_hostnames", []):
        if re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(name), text, re.I):
            found.append(name)
    return sorted(set(found))


def refuse_lan_leaks(what, text, kit):
    leaks = lan_leaks(text, kit)
    if leaks:
        raise Refused("%s would tie an off-site TV to a home network: %s. Nothing off-site may name a home IP, a private "
                      "address, a .local/.lan name or a listed machine name." % (what, ", ".join(leaks)))


def load_profile(kit, site="OFFSITE"):
    rel = kit.get("profiles", {}).get(site)
    if not rel:
        raise Refused("kit.json has no %s profile" % site)
    profile = load_json(find_kit_file(kit, rel))
    refuse_lan_leaks("profile %s" % rel, json.dumps(profile), kit)
    return profile


def find_kit_file(kit, rel):
    """A file named in kit.json (profiles/...): next to the kit file in use, else the bundled copy."""
    for base in (os.path.dirname(os.path.abspath(kit.get("_path", ""))) if kit.get("_path") else None, DATA_DIR):
        if base and os.path.isfile(os.path.join(base, rel)):
            return os.path.join(base, rel)
    raise Refused("kit file %s not found (looked next to the kit file and in the bundled data)" % rel)


# ----------------------------------------------------------------------------------------------- APK signer certs

def _lp(buf, pos):
    n = struct.unpack("<I", buf[pos:pos + 4])[0]
    return buf[pos + 4:pos + 4 + n], pos + 4 + n


def _signer_certs(block_value):
    """The X.509 DER certificates in an APK Signature Scheme v2 or v3 block (both start: digests, certificates)."""
    certs = []
    signers, _ = _lp(block_value, 0)
    pos = 0
    while pos < len(signers):
        signer, pos = _lp(signers, pos)
        signed, _ = _lp(signer, 0)
        _digests, p = _lp(signed, 0)
        cert_seq, p = _lp(signed, p)
        q = 0
        while q < len(cert_seq):
            cert, q = _lp(cert_seq, q)
            certs.append(cert)
    return certs


def apk_signer_cert_sha256(path):
    """sha256 of every signer certificate in the APK's signing block (what `apksigner verify --print-certs` prints).

    The file's own sha256 already pins the exact bytes; this reads the identity of the key that signed them.
    """
    try:
        return _apk_signer_cert_sha256(path)
    except (struct.error, IndexError, ValueError) as e:
        raise Refused("%s: unreadable APK signing block (%s)" % (path, e), 4)


def _apk_signer_cert_sha256(path):
    with open(path, "rb") as f:
        data = f.read()
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise Refused("%s: not a zip/APK (no end-of-central-directory)" % path, 4)
    cd_off = struct.unpack("<I", data[eocd + 16:eocd + 20])[0]
    if data[cd_off - 16:cd_off] != SIG_MAGIC:
        raise Refused("%s: no APK signing block (unsigned, or v1-only signature)" % path, 4)
    size = struct.unpack("<Q", data[cd_off - 24:cd_off - 16])[0]
    start = cd_off - size - 8
    if start < 0 or struct.unpack("<Q", data[start:start + 8])[0] != size:
        raise Refused("%s: damaged APK signing block" % path, 4)
    pos, end = start + 8, cd_off - 24
    certs = []
    while pos < end:
        pair_len = struct.unpack("<Q", data[pos:pos + 8])[0]
        pos += 8
        pair_id = struct.unpack("<I", data[pos:pos + 4])[0]
        value = data[pos + 4:pos + pair_len]
        pos += pair_len
        if pair_id in (SIG_V2_ID, SIG_V3_ID):
            certs.extend(_signer_certs(value))
    if not certs:
        raise Refused("%s: signing block has no v2/v3 signer" % path, 4)
    return sorted(set(hashlib.sha256(c).hexdigest() for c in certs))


def apksigner_cert_sha256(apksigner, path):
    """The same digests from the real apksigner (optional: --apksigner or TVKIT_APKSIGNER)."""
    env = dict(os.environ)
    java_home = env.get("JAVA_HOME")
    if java_home:
        env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")
    out = subprocess.run([apksigner, "verify", "--print-certs", path], capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise Refused("apksigner refused %s: %s" % (path, (out.stderr or out.stdout).strip()), 4)
    return sorted(set(re.findall(r"certificate SHA-256 digest: ([0-9a-f]{64})", out.stdout)))


def check_artifacts(kit, artifacts_dir, apksigner=None):
    """Every artifact's sha256 and signer against kit.json. Returns {name: path}. Raises Refused(…, 4) on any mismatch."""
    paths = {}
    problems = []
    for name, art in kit["artifacts"].items():
        path = os.path.join(artifacts_dir, art["file"])
        if not os.path.isfile(path):
            problems.append("%s: missing: %s" % (name, path))
            continue
        if art["sha256"] == PLACEHOLDER_SHA or art["signer_cert_sha256"] == PLACEHOLDER_SHA:
            problems.append("%s: kit.json still has the placeholder pin. Fill in sha256 and signer_cert_sha256 for your own "
                            "copy of %s (the build script prints them), see README \"Pinning\"" % (name, art["file"]))
            continue
        actual = sha256_file(path)
        if actual != art["sha256"]:
            problems.append("%s: sha256 MISMATCH\n      expected %s\n      actual   %s" % (name, art["sha256"], actual))
            continue
        signers = apk_signer_cert_sha256(path)
        if signers != [art["signer_cert_sha256"]]:
            problems.append("%s: signer cert MISMATCH\n      expected %s\n      actual   %s"
                            % (name, art["signer_cert_sha256"], ", ".join(signers)))
            continue
        if apksigner:
            tool = apksigner_cert_sha256(apksigner, path)
            if tool != [art["signer_cert_sha256"]]:
                problems.append("%s: apksigner says signer %s, kit.json says %s"
                                % (name, ", ".join(tool), art["signer_cert_sha256"]))
                continue
        paths[name] = path
        say("  OK  %-12s %s  sha256 %s…  signer %s…%s" % (name, art["file"], actual[:12], art["signer_cert_sha256"][:12],
                                                          "  (apksigner agrees)" if apksigner else ""))
    if problems:
        raise Refused("artifact check FAILED:\n  " + "\n  ".join(problems) + "\n  Nothing goes on a TV until kit.json and the files agree.", 4)
    return paths


# ----------------------------------------------------------------------------------------------- per-TV records

UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
# What `adb devices` lists for a TV on a USB cable: letters and digits only (no dot, no colon), so it can never be a
# host name that adb would look up. adb -s <serial> only matches a device already attached.
USB_SERIAL = re.compile(r"^[A-Za-z0-9]{4,64}$")


def record_path(tvs_dir, tv):
    if os.path.isfile(tv):
        return tv
    candidate = os.path.join(tvs_dir, tv + ".json")
    if os.path.isfile(candidate):
        return candidate
    raise Refused("no TV record for %r (looked in %s). Mint one with: tvkit new --name ... --site ..." % (tv, tvs_dir))


def all_tv_ids(tvs_dir):
    ids = {}
    if not os.path.isdir(tvs_dir):
        return ids
    for fn in sorted(os.listdir(tvs_dir)):
        if fn.endswith(".json") and fn != "schema.json":
            try:
                ids[load_json(os.path.join(tvs_dir, fn)).get("tv_id")] = fn
            except (ValueError, OSError):
                ids[None] = fn
    return ids


def validate_record(rec, kit, path, target=None):
    """The schema in tvs/schema.json, plus the off-site rules. Returns the adb target to use (may be None)."""
    problems = []
    allowed = set(load_json(os.path.join(DATA_DIR, "schema.json"))["properties"])
    for key in sorted(set(rec) - allowed):
        problems.append("unknown field %r (records hold only what tvs/schema.json lists; never an SSID, MAC, code or key)" % key)
    for key in ("tv_id", "name", "site", "kit_version"):
        if not rec.get(key):
            problems.append("missing %s" % key)
    if rec.get("requests_url") and not re.match(r"^https?://[^\s/]+(/\S*)?$", str(rec["requests_url"])):
        problems.append("requests_url must be an http(s) address")
    tv_id = str(rec.get("tv_id", ""))
    if not UUID4.match(tv_id):
        problems.append("tv_id must be a lower-case UUID v4 (mint it with tvkit new), got %r" % tv_id)
    site = rec.get("site")
    if site not in ("HOME", "OFFSITE"):
        problems.append("site must be HOME or OFFSITE, got %r" % site)
    if rec.get("screensaver", "keep") not in ("keep", "app"):
        problems.append("screensaver must be keep or app")
    if rec.get("status", "active") not in ("active", "revoked"):
        problems.append("status must be active or revoked")
    for secret_word in ("pairing_code", "pairing", "code", "cookie", "token", "key", "password", "adbkey"):
        if secret_word in rec:
            problems.append("records never hold secrets: remove %r" % secret_word)
    networks = home_networks(kit)
    chosen = target or rec.get("adb_target")
    if site == "OFFSITE":
        if "adb_target" in rec:
            problems.append("an OFFSITE record must not store adb_target (pass --target on the laptop there)")
        if "ha" in rec:
            problems.append("an OFFSITE TV never joins Home Assistant: remove \"ha\"")
        leaks = lan_leaks(json.dumps(rec), kit)
        if is_home_address(json.dumps(rec), networks) or leaks:
            problems.append("an OFFSITE record must not mention a home IP (%s), a private address, a .local/.lan name or a "
                            "machine name from kit.json lan_hostnames: %s" % (", ".join(kit["home_networks"]), ", ".join(leaks) or "home IP"))
        if chosen and is_home_address(chosen, networks):
            problems.append("OFFSITE TV, but the adb target %s is a home address (%s). If the other network also uses that range, "
                            "use the USB path (tvkit usb-pack)." % (chosen, ", ".join(kit["home_networks"])))
        elif chosen and not (re.match(r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$", chosen) or USB_SERIAL.match(chosen)):
            problems.append("OFFSITE TV: the adb target must be an IP literal (host:port) or a USB serial, not a name (%r)" % chosen)
    if problems:
        raise Refused("%s: record refused:\n  - " % path + "\n  - ".join(problems))
    if rec.get("status") == "revoked":
        raise Refused("%s: this TV is revoked. A re-add gets a NEW tv_id (tvkit new); the old one is never reused." % path)
    return chosen


# ----------------------------------------------------------------------------------------------- adb

class Adb:
    def __init__(self, path, target=None):
        self.path = path
        self.target = target
        self.calls = []

    def run(self, *args, check=True, timeout=120):
        if args and (args[0] in ("install", "uninstall", "reboot", "root", "remount", "push", "sideload")
                     or (args[0] == "shell" and any(" ".join(args[1:]).startswith(m) for m in MUTATING_SHELL))) and not CONSENTS[0]:
            raise Refused("internal guard: adb %s would change the TV and nobody has typed yes" % " ".join(args))
        cmd = [self.path]
        if self.target:
            cmd += ["-s", self.target]
        cmd += list(args)
        self.calls.append(args)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and out.returncode != 0:
            raise Refused("adb %s failed (%s): %s" % (" ".join(args), out.returncode, (out.stderr or out.stdout).strip()))
        return out

    def shell(self, command, check=True):
        return self.run("shell", command, check=check).stdout

    def connect(self):
        """TCP targets get `adb connect`; a USB serial is used as is. Refuses an unauthorized or offline TV."""
        if not self.target:
            raise Refused("no adb target: put adb_target in a HOME record, or pass --target host:port")
        if ":" in self.target:
            out = self.run("connect", self.target, check=False)
            text = (out.stdout + out.stderr).strip()
            if "connected" not in text:
                raise Refused("adb connect %s: %s" % (self.target, text or "no answer"))
        state = self.run("get-state", check=False)
        value = (state.stdout + state.stderr).strip()
        if value != "device":
            raise Refused("TV %s is %r, not ready. If it says unauthorized, the TV is showing \"Allow USB debugging?\": only its "
                          "owner, present, ticks Always allow. Then run this again." % (self.target, value or "absent"))
        return True

    def disconnect(self):
        if self.target and ":" in self.target:
            self.run("disconnect", self.target, check=False)


def find_adb(explicit):
    for candidate in (explicit, os.environ.get("TVKIT_ADB"), shutil.which("adb")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise Refused("adb not found: pass --adb /path/to/adb or set TVKIT_ADB (platform-tools from developer.android.com, or put adb on PATH)")


# ----------------------------------------------------------------------------------------------- read-only snapshot

def _packages(adb, flag):
    out = adb.shell("pm list packages" + (" " + flag if flag else ""))
    return sorted(line[len("package:"):].strip() for line in out.splitlines() if line.startswith("package:"))


def _resolve(adb, command):
    out = adb.shell(command, check=False)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if "/" in line and not line.startswith("priority="):
            return line
    return None


def wifi_state(adb):
    """Connected? (`cmd wifi status`, Android 11+; `dumpsys wifi` on older TVs) and which packages saved networks
    (the cname= owners in `dumpsys wifi`). Sanitized: no SSID, no MAC."""
    status = adb.shell("cmd wifi status", check=False)
    dump = adb.shell("dumpsys wifi", check=False)
    connected = "Wifi is connected to" in status or "Wifi is connected to" in dump
    owners = sorted(set(re.findall(r"cname=([A-Za-z0-9_.]+)", dump)))
    return {"connected": connected, "saved_by": owners}


def snapshot(adb, kit, tv_id=None):
    """Everything the kit needs to know about a TV, read-only and sanitized (no SSID, no MAC, no account)."""
    snap = {"taken_at": now_text(), "tv_id": tv_id, "props": {}, "settings": {}}
    for prop in PROPS:
        snap["props"][prop] = adb.shell("getprop " + prop).strip()
    snap["home"] = _resolve(adb, HOME_RESOLVE)
    all_pkgs = _packages(adb, "")
    disabled = _packages(adb, "-d")
    snap["packages"] = {"all_count": len(all_pkgs), "disabled": disabled, "system": _packages(adb, "-s"),
                        "third_party": _packages(adb, "-3")}
    watched = list(kit["expect"]["disabled_by_switch"]) + [a["package"] for a in kit["artifacts"].values()] + [PLAY_STORE]
    snap["present"] = {}
    for pkg in watched:
        snap["present"][pkg] = {"installed": pkg in all_pkgs, "enabled": pkg in all_pkgs and pkg not in disabled}
    for key in SECURE_SETTINGS:
        snap["settings"][key] = adb.shell("settings get secure " + key).strip()
    snap["wifi"] = wifi_state(adb)
    launch = _resolve(adb, "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                           "-c android.intent.category.LEANBACK_LAUNCHER " + PLAY_STORE)
    snap["play_store"] = {"installed": PLAY_STORE in all_pkgs, "enabled": PLAY_STORE in all_pkgs and PLAY_STORE not in disabled,
                          "launchable": bool(launch and launch.startswith(PLAY_STORE + "/"))}
    return snap


def print_snapshot(snap):
    p = snap["props"]
    say("  model      %s %s (%s), Android %s (sdk %s), patch %s" % (p.get("ro.product.manufacturer"), p.get("ro.product.model"),
                                                                     p.get("ro.product.device"), p.get("ro.build.version.release"),
                                                                     p.get("ro.build.version.sdk"), p.get("ro.build.version.security_patch")))
    say("  home       %s" % snap["home"])
    say("  disabled   %d package(s): %s" % (len(snap["packages"]["disabled"]), ", ".join(snap["packages"]["disabled"]) or "-"))
    for pkg, st in snap["present"].items():
        say("  %-10s %s" % ("package", "%s: %s" % (pkg, "absent" if not st["installed"] else ("enabled" if st["enabled"] else "DISABLED"))))
    for key, value in snap["settings"].items():
        say("  setting    %s = %s" % (key, value))
    say("  wifi       %s; saved by %s" % ("connected" if snap["wifi"]["connected"] else "NOT CONNECTED",
                                          ", ".join(snap["wifi"]["saved_by"]) or "(unknown)"))
    ps = snap["play_store"]
    say("  play store %s" % ("installed, %s, %s" % ("enabled" if ps["enabled"] else "DISABLED",
                                                     "launchable" if ps["launchable"] else "NOT launchable") if ps["installed"] else "ABSENT"))


def load_baseline(base_dir, tv_id):
    baseline = load_json(os.path.join(base_dir, "snapshot.json"))
    if baseline.get("tv_id") not in (None, tv_id):
        raise Refused("%s is a baseline of another TV (%s), not %s" % (base_dir, baseline.get("tv_id"), tv_id))
    return baseline


def newest_baseline(work, tv_id):
    base = os.path.join(work, tv_id, "baseline")
    if not os.path.isdir(base):
        return None
    runs = sorted(d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, "snapshot.json")))
    return os.path.join(base, runs[-1]) if runs else None


def after_diff(baseline, after, rec, kit):
    """The after-check. Returns (ok, lines). Anything unexpected is a FAIL line; the caller fails loudly."""
    expect = kit["expect"]
    lines = []
    fails = []

    def check(condition, ok_text, fail_text):
        if condition:
            lines.append("  ok    " + ok_text)
        else:
            fails.append(fail_text)
            lines.append("  FAIL  " + fail_text)

    before_disabled = set(baseline["packages"]["disabled"])
    now_disabled = set(after["packages"]["disabled"])
    newly = now_disabled - before_disabled
    expected = set(expect["disabled_by_switch"])
    already = sorted(expected & before_disabled)
    check(newly == expected,
          "newly disabled = exactly %s" % ", ".join(sorted(expected)),
          "newly disabled packages differ: missing %s; extra %s%s" % (sorted(expected - newly) or "-", sorted(newly - expected) or "-",
                                                                       "; note: %s were already disabled in the baseline" % already if already else ""))
    re_enabled = before_disabled - now_disabled
    check(not re_enabled, "nothing that was disabled before is enabled now", "packages re-enabled by the switch: %s" % sorted(re_enabled))
    check(after["home"] == expect["home_component"], "home = %s" % expect["home_component"],
          "home is %s, expected %s" % (after["home"], expect["home_component"]))
    ps = after["play_store"]
    check(ps["installed"] and ps["enabled"] and ps["launchable"], "Play Store installed, enabled and launchable",
          "Play Store: installed=%s enabled=%s launchable=%s" % (ps["installed"], ps["enabled"], ps["launchable"]))
    saver_before = baseline["settings"].get("screensaver_components")
    saver_after = after["settings"].get("screensaver_components")
    if rec.get("screensaver", "keep") == "keep":
        check(saver_after == saver_before and after["settings"].get("screensaver_enabled") == baseline["settings"].get("screensaver_enabled"),
              "screensaver unchanged (%s)" % saver_before, "screensaver changed: %s -> %s" % (saver_before, saver_after))
    else:
        check(bool(saver_after) and saver_after.startswith(kit["artifacts"]["free-my-tv"]["package"] + "/"),
              "screensaver is the app's (%s)" % saver_after, "screensaver is %s, expected the app's" % saver_after)
    check(after["wifi"]["connected"], "Wi-Fi still connected", "Wi-Fi is NOT connected (was it erased by the switch?)")
    acc_before = baseline["settings"].get("enabled_accessibility_services")
    acc_after = after["settings"].get("enabled_accessibility_services")
    fmtv = kit["artifacts"]["free-my-tv"]["package"]
    check(acc_after == acc_before and fmtv not in (acc_after or ""),
          "accessibility services unchanged (%s)" % acc_before, "accessibility services changed: %s -> %s" % (acc_before, acc_after))
    for name, art in kit["artifacts"].items():
        st = after["present"].get(art["package"], {})
        check(st.get("installed") and st.get("enabled"), "%s installed and enabled" % art["package"],
              "%s is %s" % (art["package"], "absent" if not st.get("installed") else "DISABLED"))
    return not fails, lines


# ----------------------------------------------------------------------------------------------- the owner's steps (printed)

def switch_steps(kit):
    return [
        "On the TV: open Free My TV -> Home Screen -> Free My TV.",
        "The TV asks \"Allow USB debugging?\" for Free My TV: tick Always allow, then Allow.",
        "The app disables exactly: %s, sets itself as HOME, and shows what it did." % ", ".join(kit["expect"]["disabled_by_switch"]),
        "Press Home: the new home appears. Then run: tvkit after --tv <tv-id> --target <host:port>",
    ]


def wifi_resave_steps():
    return [
        "On the TV: Settings -> Network & Internet -> the connected network -> Forget.",
        "Then add it again (same network, type the Wi-Fi password). Settings saves it, not the setup app.",
        "Then run tvkit wifi-check again. Do NOT install or switch before it passes: disabling the setup app",
        "would erase the networks it saved, and \"Original\" cannot bring them back.",
    ]


def original_steps():
    return [
        "On the TV: open Free My TV -> Home Screen -> Original.",
        "It re-enables what it disabled and restores the old home and screensaver.",
        "Then Settings -> Apps -> Free My TV -> Uninstall (and TV Bro, if it goes too).",
        "If the app is broken, tvkit undo does the same with adb from the baseline (it asks before each step).",
    ]


# ----------------------------------------------------------------------------------------------- subcommands

def cmd_new(args, kit):
    tvs_dir = args.tvs
    os.makedirs(tvs_dir, exist_ok=True)
    existing = all_tv_ids(tvs_dir)
    tv_id = str(uuid.uuid4())
    while tv_id in existing:   # never reuse an id, even the one-in-2^122 case
        tv_id = str(uuid.uuid4())
    rec = {"tv_id": tv_id, "name": args.name, "site": args.site, "model": args.model or "",
           "set_up_on": None, "kit_version": kit["version"], "screensaver": "keep", "apps": [], "status": "active",
           "city": args.city or "", "tz": args.tz or "", "notes": ""}
    if getattr(args, "requests_url", None):
        requests_url_for(args, kit)   # refuses a malformed address
        rec["requests_url"] = args.requests_url
    if args.site == "HOME" and args.target:
        rec["adb_target"] = args.target
    elif args.target:
        if is_home_address(args.target, home_networks(kit)):
            raise Refused("OFFSITE TV, but %s is a home address" % args.target)
        say("OFFSITE: the adb target is not stored; pass --target on the laptop there.")
    validate_record(rec, kit, "(new)")
    path = os.path.join(tvs_dir, tv_id + ".json")
    write_json(path, rec)
    say("Minted %s" % path)
    say("  tv_id %s  name %r  site %s  kit %s" % (tv_id, args.name, args.site, kit["version"]))
    if args.site == "OFFSITE":
        for line in offsite_next_steps(rec):
            say(line)
        return 0
    say("Next: enable USB/network debugging on the TV (RUNBOOK step 2), then tvkit check --tv %s" % tv_id)
    return 0


def offsite_next_steps(rec):
    tv_id = rec["tv_id"]
    return [
        "Next, off-site (another house / another network):",
        "  1. Decide the requests page URL for this TV (record field requests_url, or --requests-url).",
        "  2. No computer at that address: tvkit usb-pack --tv %s --out <empty folder on the USB stick>" % tv_id,
        "     With a laptop there instead: the runbook with --target <the TV's address or USB serial> (never stored).",
        "  3. The owner's on-TV steps: tvkit offsite-sheet --tv %s" % tv_id,
    ]


def cmd_check(args, kit):
    say("tv-kit kit %s  kit.json %s" % (kit["version"], args.kit))
    say("Artifacts in %s:" % args.artifacts)
    check_artifacts(kit, args.artifacts, args.apksigner)
    if args.tv:
        path = record_path(args.tvs, args.tv)
        rec = load_json(path)
        target = validate_record(rec, kit, path, args.target)
        ids = all_tv_ids(args.tvs)
        dupes = [fn for fn in os.listdir(args.tvs) if fn.endswith(".json") and fn != "schema.json"
                 and fn != os.path.basename(path) and load_json(os.path.join(args.tvs, fn)).get("tv_id") == rec["tv_id"]]
        if dupes:
            raise Refused("tv_id %s appears in more than one record: %s" % (rec["tv_id"], ", ".join(dupes)))
        say("Record OK: %s  (%d record(s) in %s)" % (path, len(ids), args.tvs))
        say("  tv_id %s  name %r  site %s  screensaver %s  adb target %s"
            % (rec["tv_id"], rec["name"], rec["site"], rec.get("screensaver", "keep"), target or "(none yet: pass --target)"))
    say("CHECK PASS at %s" % now_text())
    return 0


def _load_tv(args, kit):
    """The record and the adb target, after every rule (OFFSITE, secrets, revoked). Nothing has touched adb yet."""
    path = record_path(args.tvs, args.tv)
    rec = load_json(path)
    return rec, validate_record(rec, kit, path, args.target)


def _connect(args, rec, target):
    adb = Adb(find_adb(args.adb), target)
    adb.connect()
    say("Connected to %s (%s, %s)" % (target, rec["name"], rec["site"]))
    return adb


def _open_tv(args, kit):
    rec, target = _load_tv(args, kit)
    return rec, _connect(args, rec, target)


def cmd_baseline(args, kit):
    rec, adb = _open_tv(args, kit)
    try:
        say("Reading the TV (read-only; nothing changes, no screenshot)…")
        snap = snapshot(adb, kit, rec["tv_id"])
    finally:
        adb.disconnect()
    out = os.path.join(args.work, rec["tv_id"], "baseline", now_stamp())
    write_json(os.path.join(out, "snapshot.json"), snap)
    print_snapshot(snap)
    say("Baseline written: %s" % out)
    if snap["present"].get(kit["artifacts"]["free-my-tv"]["package"], {}).get("installed"):
        say("NOTE: Free My TV is already on this TV; this baseline is not a factory one.")
    say("Next: tvkit wifi-check --tv %s" % rec["tv_id"])
    return 0


def cmd_wifi_check(args, kit):
    rec, adb = _open_tv(args, kit)
    try:
        wifi = wifi_state(adb)
    finally:
        adb.disconnect()
    owners, connected = wifi["saved_by"], wifi["connected"]
    say("Wi-Fi: %s; saved by: %s" % ("connected" if connected else "not connected", ", ".join(owners) or "(no saved network found)"))
    risky = [p for p in owners if p in set(kit["expect"]["disabled_by_switch"])]
    if risky:
        say("STOP: the Wi-Fi was saved by %s, which the switch disables. Android would erase those networks." % ", ".join(risky))
        for line in wifi_resave_steps():
            say("    " + line)
        return 3
    if not owners:
        say("STOP: could not tell which app saved the Wi-Fi (no cname= in dumpsys wifi). Free My TV itself refuses to switch in that "
            "case. If the TV is on Ethernet with no saved Wi-Fi, say so in the record's notes and re-run with Wi-Fi off; otherwise "
            "forget and re-add the network in Settings and try again.")
        return 3
    say("WIFI-CHECK PASS at %s. Next: tvkit install --tv %s" % (now_text(), rec["tv_id"]))
    return 0


def cmd_install(args, kit):
    say("Artifacts in %s:" % args.artifacts)
    paths = check_artifacts(kit, args.artifacts, args.apksigner)   # refuses before anything touches the TV
    rec, adb = _open_tv(args, kit)
    try:
        lines = ["adb -s %s install -r %s   (%s %s)" % (adb.target, paths[name], kit["artifacts"][name]["package"],
                                                        kit["artifacts"][name].get("version", ""))
                 for name in paths]
        lines.append("then adb disconnect (the project says: disconnect before the switch on Android 8-10)")
        if not confirm("install %d APK(s) on %s (%s)" % (len(paths), rec["name"], adb.target), lines):
            return 1
        for name, path in paths.items():
            out = adb.run("install", "-r", path, timeout=600)
            text = (out.stdout + out.stderr).strip()
            if "Success" not in text:
                raise Refused("install of %s did not say Success: %s" % (name, text))
            say("  installed %s (%s)" % (name, kit["artifacts"][name]["package"]))
        present = _packages(adb, "")
        for name in paths:
            pkg = kit["artifacts"][name]["package"]
            if pkg not in present:
                raise Refused("%s not in pm list packages after install" % pkg)
    finally:
        adb.disconnect()
    say("Disconnected. Now the owner switches (the kit never does this):")
    for line in switch_steps(kit):
        say("    " + line)
    return 0


def cmd_after(args, kit):
    rec, target = _load_tv(args, kit)
    base_dir = args.baseline or newest_baseline(args.work, rec["tv_id"])
    if not base_dir:
        raise Refused("no baseline for this TV under %s: run tvkit baseline first (before the switch)" % args.work)
    baseline = load_baseline(base_dir, rec["tv_id"])
    adb = _connect(args, rec, target)
    try:
        say("Reading the TV after the switch (read-only)…")
        after = snapshot(adb, kit, rec["tv_id"])
    finally:
        adb.disconnect()
    out = os.path.join(args.work, rec["tv_id"], "after", now_stamp())
    write_json(os.path.join(out, "snapshot.json"), after)
    ok, lines = after_diff(baseline, after, rec, kit)
    say("After-check against %s:" % base_dir)
    for line in lines:
        say(line)
    write_json(os.path.join(out, "diff.json"), {"baseline": base_dir, "ok": ok, "lines": lines})
    if not ok:
        say("AFTER-CHECK FAILED at %s: the TV is not in the expected state. Do not use it yet; look at the FAIL lines, "
            "and use Free My TV -> Original (or tvkit undo) if in doubt." % now_text())
        return 2
    say("AFTER-CHECK PASS at %s. Written: %s" % (now_text(), out))
    say("Next (optional): tvkit open-requests --tv %s opens your requests page in TV Bro." % rec["tv_id"])
    return 0


def cmd_open_requests(args, kit):
    rec, target = _load_tv(args, kit)
    url = requests_url_for(args, kit, rec)
    adb = _connect(args, rec, target)
    try:
        browser = kit["artifacts"]["tv-bro"]["package"]
        if not confirm("open the requests page on %s" % rec["name"],
                       ["adb shell am start -a android.intent.action.VIEW -d %s %s" % (url, browser),
                        "The TV's screen changes to TV Bro showing that page."]):
            return 1
        out = adb.shell("am start -a android.intent.action.VIEW -d %s %s" % (url, browser), check=False)
        if "Error" in out or "does not exist" in out:
            raise Refused("am start failed: %s" % out.strip())
    finally:
        adb.disconnect()
    say("Opened %s in TV Bro." % url)
    return 0


def cmd_undo(args, kit):
    rec, target = _load_tv(args, kit)
    base_dir = args.baseline or newest_baseline(args.work, rec["tv_id"])
    if not base_dir:
        raise Refused("no baseline for this TV under %s; without it tvkit cannot know what to restore. Use Free My TV -> Original on the TV." % args.work)
    baseline = load_baseline(base_dir, rec["tv_id"])
    say("Undo, first choice (the owner, on the TV):")
    for line in original_steps():
        say("    " + line)
    adb = _connect(args, rec, target)
    try:
        now = snapshot(adb, kit, rec["tv_id"])
        expected = kit["expect"]["disabled_by_switch"]
        before_disabled = set(baseline["packages"]["disabled"])
        now_disabled = set(now["packages"]["disabled"])
        to_enable = [p for p in expected if p in now_disabled and p not in before_disabled]
        if to_enable:
            if confirm("re-enable %d package(s) the switch disabled (from the baseline %s)" % (len(to_enable), base_dir),
                       ["adb shell pm enable %s" % p for p in to_enable]):
                for p in to_enable:
                    adb.shell("pm enable " + p)
                    say("  enabled " + p)
        else:
            say("  nothing to re-enable (no package disabled since the baseline)")
        if baseline["home"] and now["home"] != baseline["home"]:
            if confirm("restore the home app", ["adb shell cmd package set-home-activity %s" % baseline["home"]]):
                out = adb.shell("cmd package set-home-activity " + baseline["home"], check=False)
                say("  " + (out.strip() or "done"))
        else:
            say("  home already %s" % now["home"])
        saver_b = baseline["settings"].get("screensaver_components")
        saver_n = now["settings"].get("screensaver_components")
        if saver_n != saver_b:
            cmd = ("settings delete secure screensaver_components" if saver_b in (None, "", "null")
                   else "settings put secure screensaver_components %s" % shlex.quote(saver_b))
            if confirm("restore the screensaver setting", ["adb shell " + cmd]):
                adb.shell(cmd)
                say("  screensaver restored")
        else:
            say("  screensaver already %s" % saver_b)
        mid = snapshot(adb, kit, rec["tv_id"])
        launcher_back = (all(mid["present"].get(p, {}).get("enabled") for p in expected if p not in before_disabled)
                         and (not baseline["home"] or mid["home"] == baseline["home"]))
        for name, art in kit["artifacts"].items():
            pkg = art["package"]
            if not mid["present"].get(pkg, {}).get("installed"):
                continue
            if baseline["present"].get(pkg, {}).get("installed"):
                say("  keeping %s: the baseline already had it (not the kit's to remove)" % pkg)
                continue
            if pkg == kit["artifacts"]["free-my-tv"]["package"] and not launcher_back:
                say("  NOT offering to uninstall %s: the TV's own launcher is not back (packages or HOME differ from the baseline); "
                    "a TV without a home app is worse than one with Free My TV" % pkg)
                continue
            if confirm("uninstall %s (%s)" % (name, pkg), ["adb uninstall %s" % pkg]):
                out = adb.run("uninstall", pkg, check=False)
                say("  " + (out.stdout + out.stderr).strip())
        final = snapshot(adb, kit, rec["tv_id"])
    finally:
        adb.disconnect()
    say("After undo:")
    print_snapshot(final)
    say("UNDO DONE at %s. A re-add of this TV gets a NEW tv_id; mark this record status \"revoked\" and keep it." % now_text())
    return 0


# ----------------------------------------------------------------------------------------------- off-site (no home network)

def _offsite_record(args, kit):
    """An active OFFSITE record that passes every rule. These steps never touch adb."""
    path = record_path(args.tvs, args.tv)
    rec = load_json(path)
    validate_record(rec, kit, path)
    if rec["site"] != "OFFSITE":
        raise Refused("%s is a %s TV. The off-site sheet and USB pack are for OFFSITE TVs; a HOME TV follows the runbook over adb."
                      % (path, rec["site"]))
    return rec


def render_sheet(kit, profile, rec, requests_url, made_at=None):
    """The owner's on-TV steps for one off-site TV (profiles/offsite-sheet.txt). Refused if it names anything at home."""
    with open(find_kit_file(kit, "profiles/offsite-sheet.txt"), "r", encoding="utf-8") as f:
        template = string.Template(f.read())
    text = template.substitute(
        tv_name=rec["name"], tv_short=rec["tv_id"][:8], kit_version=kit["version"], made_at=made_at or now_text(),
        requests_url=requests_url, fmtv_file=kit["artifacts"]["free-my-tv"]["file"],
        tvbro_file=kit["artifacts"]["tv-bro"]["file"],
        checks="\n".join("   - " + line for line in profile["checks_without_adb"]))
    refuse_lan_leaks("the owner sheet", text, kit)
    return text


def cmd_offsite_sheet(args, kit):
    rec = _offsite_record(args, kit)
    profile = load_profile(kit)
    say(render_sheet(kit, profile, rec, requests_url_for(args, kit, rec)).rstrip("\n"))
    return 0


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def cmd_usb_pack(args, kit):
    """The two checked APKs plus the owner's steps, for a USB stick (no computer, no home network at that address).
    Never touches a TV and never writes a code, key, address or adb target."""
    rec = _offsite_record(args, kit)
    profile = load_profile(kit)
    requests_url = requests_url_for(args, kit, rec)
    out = os.path.join(args.out, "tv-kit")
    repo_root = os.path.dirname(os.path.dirname(PKG_DIR))   # the source checkout when run from one (src/tvkit/..)
    if os.path.isfile(os.path.join(repo_root, "pyproject.toml")) and _inside(args.out, repo_root):
        raise Refused("--out %s is inside the repository; APKs never go in git. Use the USB stick or a scratch folder." % args.out)
    if os.path.exists(out) and os.listdir(out):
        raise Refused("%s is not empty; tvkit never overwrites a pack. Use an empty folder." % out)
    say("Artifacts in %s:" % args.artifacts)
    paths = check_artifacts(kit, args.artifacts, args.apksigner)   # refuses before anything is written
    made_at = now_text()
    sheet = render_sheet(kit, profile, rec, requests_url, made_at)
    manifest = {
        "tv_id": rec["tv_id"], "name": rec["name"], "site": rec["site"], "kit_version": kit["version"], "made_at": made_at,
        "requests_url": requests_url, "requests_path": profile["requests"]["recommended"],
        "rules": profile["rules"], "tv_never_uses": profile["tv_never_uses"],
        "artifacts": {name: {k: kit["artifacts"][name][k] for k in ("file", "package", "version", "sha256", "signer_cert_sha256")
                             if k in kit["artifacts"][name]} for name in paths},
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    sums = "".join("%s  %s\n" % (kit["artifacts"][name]["sha256"], kit["artifacts"][name]["file"]) for name in paths)
    for what, text in (("READ-ME-ON-THE-TV.txt", sheet), ("tv-kit-offsite.json", manifest_text), ("SHA256SUMS", sums)):
        refuse_lan_leaks(what, text, kit)
    os.makedirs(out, exist_ok=True)
    for name, src in paths.items():
        dst = os.path.join(out, kit["artifacts"][name]["file"])
        shutil.copyfile(src, dst)
        if sha256_file(dst) != kit["artifacts"][name]["sha256"]:
            raise Refused("%s: the copy on the stick does not match kit.json (bad stick?). Use another stick." % dst, 4)
        say("  copied %s (sha256 verified on the stick)" % dst)
    for fn, text in (("READ-ME-ON-THE-TV.txt", sheet), ("tv-kit-offsite.json", manifest_text), ("SHA256SUMS", sums)):
        with open(os.path.join(out, fn), "w", encoding="utf-8") as f:
            f.write(text)
        say("  wrote  %s" % os.path.join(out, fn))
    say("USB-PACK DONE at %s: %s (no codes, keys or addresses on it)." % (made_at, out))
    say("Next: give the owner the stick and go through READ-ME-ON-THE-TV.txt together (a phone call works).")
    return 0


# ----------------------------------------------------------------------------------------------- main

def cmd_init(args, kit):
    """Copy the bundled kit.json and profiles/ into a folder you can edit (default: the config folder)."""
    dest = args.dir or config_home()
    files = ["kit.json", "profiles/offsite.json", "profiles/offsite-sheet.txt"]
    clash = [f for f in files if os.path.exists(os.path.join(dest, f))]
    if clash:
        raise Refused("%s already has %s; tvkit init never overwrites" % (dest, ", ".join(clash)))
    for f in files:
        target = os.path.join(dest, f)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(DATA_DIR, f), target)
        say("  wrote %s" % target)
    say("Edit %s (the pins for your own Free My TV build, requests_url, home_networks, lan_hostnames)." % os.path.join(dest, "kit.json"))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="tvkit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kit", default=None, help="kit.json (default: $TVKIT_KIT, then <config>/tvkit/kit.json from `tvkit init`, "
                   "then the bundled example)")
    p.add_argument("--tvs", default=os.environ.get("TVKIT_TVS") or os.path.join(data_home(), "tvs"), help="per-TV records directory")
    p.add_argument("--artifacts", default=os.environ.get("TVKIT_ARTIFACTS", os.path.join(data_home(), "artifacts")),
                   help="where the APKs are (never in git)")
    p.add_argument("--work", default=os.environ.get("TVKIT_WORK", os.path.join(data_home(), "work")),
                   help="baseline/after snapshots go here (never in git)")
    p.add_argument("--requests-url", default=None, help="the page open-requests / offsite-sheet / usb-pack point the TV at "
                   "(default: the TV record's requests_url, then kit.json; no built-in host)")
    p.add_argument("--adb", default=None, help="adb binary (default: TVKIT_ADB, then PATH)")
    p.add_argument("--apksigner", default=os.environ.get("TVKIT_APKSIGNER"), help="optional apksigner for a second signer check")
    p.add_argument("--target", default=None, help="adb host:port (required for OFFSITE; overrides a HOME record's adb_target); also accepted after the subcommand")
    p.add_argument("--baseline", default=None, help="a specific baseline directory for after/undo (default: the newest)")
    sub = p.add_subparsers(dest="command", required=True)
    i = sub.add_parser("init", help="copy the bundled kit.json and profiles into an editable folder")
    i.add_argument("--dir", default=None, help="destination (default: the tvkit config folder)")
    n = sub.add_parser("new", help="mint a per-TV record with a new tv_id (local file only)")
    n.add_argument("--name", required=True)
    n.add_argument("--site", required=True, choices=["HOME", "OFFSITE"])
    n.add_argument("--model", default="")
    n.add_argument("--city", default="")
    n.add_argument("--tz", default="")
    n.add_argument("--requests-url", dest="requests_url", default=argparse.SUPPRESS, help="stored in the record")
    n.add_argument("--target", default=argparse.SUPPRESS, help="HOME only: stored as adb_target; refused for OFFSITE home addresses")
    for name, help_text in (("check", "verify every artifact's sha256 and signer, and the TV record (no TV needed)"),
                            ("baseline", "read-only snapshot of the TV before anything changes"),
                            ("wifi-check", "read-only: stop if the setup app saved the Wi-Fi"),
                            ("install", "adb install the APKs after the checks (asks yes)"),
                            ("after", "read-only diff against the baseline; fails loudly"),
                            ("open-requests", "open your requests page URL in TV Bro (asks yes)"),
                            ("undo", "Original steps, then manual restore from the baseline (asks yes per step)")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--tv", required=(name != "check"), help="tv_id or path of the record")
        # The same options are accepted after the subcommand (SUPPRESS: only set when given, so the global value survives).
        s.add_argument("--target", default=argparse.SUPPRESS, help="adb host:port")
        s.add_argument("--baseline", default=argparse.SUPPRESS, help="a specific baseline directory")
    o = sub.add_parser("offsite-sheet", help="OFFSITE: print the owner's on-TV steps (no TV, no network)")
    o.add_argument("--tv", required=True, help="tv_id or path of the record")
    u = sub.add_parser("usb-pack", help="OFFSITE: checked APKs + the owner's steps into <out>/tv-kit for a USB stick")
    u.add_argument("--tv", required=True, help="tv_id or path of the record")
    u.add_argument("--out", required=True, help="the USB stick (or an empty folder outside the repo)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.kit is None:
        args.kit = default_kit_path()
    try:
        if args.command == "init":
            return cmd_init(args, None)
        kit = load_kit(args.kit)
        handler = {"new": cmd_new, "check": cmd_check, "baseline": cmd_baseline, "wifi-check": cmd_wifi_check,
                   "install": cmd_install, "after": cmd_after, "open-requests": cmd_open_requests, "undo": cmd_undo,
                   "offsite-sheet": cmd_offsite_sheet, "usb-pack": cmd_usb_pack}[args.command]
        return handler(args, kit)
    except Refused as e:
        say("REFUSED: %s" % e)
        return e.code
    except subprocess.TimeoutExpired as e:
        say("REFUSED: adb timed out: %s" % e)
        return 1


def run():
    sys.exit(main())


if __name__ == "__main__":
    run()
