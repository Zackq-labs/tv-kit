"""tv-kit tests: every subcommand against a fake adb (tests/fake_adb.py). No TV, no network, no real adb.

Run from the project root: python3 -m unittest discover -s tests -v
"""
import copy
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
from tvkit import cli as tvkit  # noqa: E402

KIT_DIR = tvkit.DATA_DIR                       # the bundled kit.json, schema.json, profiles/
TVKIT = os.path.join(SRC, "tvkit", "cli.py")
FAKE_ADB = os.path.join(HERE, "fake_adb.py")
# Optional: a folder holding the real upstream APKs, to check the real pins (skipped when unset).
REAL_ARTIFACTS = os.environ.get("TVKIT_TEST_ARTIFACTS")


def ip(*octets):
    """Private addresses are built, not written, so no private address literal sits in this repository."""
    return ".".join(str(o) for o in octets)


PRIVATE_ADDR = r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d])"
HOME_TV = ip(192, 0, 2, 41) + ":5555"            # inside the kit's example home network 192.0.2.0/24
HER_TARGET = ip(198, 51, 100, 50) + ":5555"     # a TV on another network (not inside the kit's home network)

FMTV = "io.freemytv"
TVBRO = "com.phlox.tvwebbrowser"
VENDING = "com.android.vending"
LAUNCHERX = "com.google.android.apps.tv.launcherx"
SETUP = "com.google.android.tungsten.setupwraith"
GAMES = "com.google.android.play.games"
TVMUSIC = "com.google.android.youtube.tvmusic"
VENDOR_DISABLED = "com.vendor.tv.softap"


def fake_apk(path, cert_bytes, payload=b"fake"):
    """A zip with a v2 APK signing block holding one signer whose certificate is cert_bytes."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("classes.dex", payload)
        z.writestr("AndroidManifest.xml", b"<manifest/>")
    data = open(path, "rb").read()
    eocd = data.rfind(b"PK\x05\x06")
    cd_off = struct.unpack("<I", data[eocd + 16:eocd + 20])[0]

    def lp(b):
        return struct.pack("<I", len(b)) + b

    signed_data = lp(lp(b"")) + lp(lp(cert_bytes)) + lp(b"")          # digests, certificates, attributes
    signer = lp(signed_data) + lp(b"") + lp(b"")                        # signed data, signatures, public key
    value = lp(lp(signer))                                              # length-prefixed sequence of length-prefixed signers
    pair = struct.pack("<I", tvkit.SIG_V2_ID) + value
    pairs = struct.pack("<Q", len(pair)) + pair
    size = len(pairs) + 8 + 16
    block = struct.pack("<Q", size) + pairs + struct.pack("<Q", size) + tvkit.SIG_MAGIC
    new = data[:cd_off] + block + data[cd_off:]
    eocd_new = len(new) - (len(data) - eocd)
    new = new[:eocd_new + 16] + struct.pack("<I", cd_off + len(block)) + new[eocd_new + 20:]
    with open(path, "wb") as f:
        f.write(new)
    return hashlib.sha256(new).hexdigest(), hashlib.sha256(cert_bytes).hexdigest()


def factory_state():
    pk = {}
    for name in [LAUNCHERX, SETUP, GAMES, TVMUSIC, VENDING, "com.google.android.katniss", "com.android.tv.settings",
                 "com.google.android.apps.tv.dreamx", VENDOR_DISABLED]:
        pk[name] = {"system": True, "enabled": True}
    pk[VENDOR_DISABLED]["enabled"] = False
    for name in ["com.plexapp.android", "com.tubitv"]:
        pk[name] = {"system": False, "enabled": True}
    return {
        "adb_state": "device",
        "props": {"ro.product.manufacturer": "ExampleCo", "ro.product.model": "Example TV 4K", "ro.product.device": "example_tv",
                  "ro.build.version.release": "12", "ro.build.version.sdk": "31",
                  "ro.build.fingerprint": "ExampleCo/example_tv/example_tv:12/x/y:user/release-keys",
                  "ro.build.version.security_patch": "2026-05-01"},
        "home": LAUNCHERX + "/.home.HomeActivity",
        "packages": pk,
        "launchable": {VENDING: VENDING + "/com.google.android.finsky.tvmainactivity.TvMainActivity"},
        "settings": {"screensaver_components": "com.google.android.apps.tv.dreamx/.service.Backdrop",
                     "screensaver_default_component": "com.google.android.apps.tv.dreamx/.service.Backdrop",
                     "screensaver_enabled": "1", "enabled_accessibility_services": "null", "accessibility_enabled": "0"},
        "wifi": {"connected": True, "saved_by": ["com.android.tv.settings"]},
        "apk_packages": {"FreeMyTV-test.apk": FMTV, "tvbro-test.apk": TVBRO},
    }


def switched(state):
    """What Free My TV's switch does on the TV."""
    s = copy.deepcopy(state)
    for p in (LAUNCHERX, SETUP, GAMES, TVMUSIC):
        s["packages"][p]["enabled"] = False
    s["home"] = FMTV + "/.HomeActivity"
    return s


class KitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tvkit-test-")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        self.work = os.path.join(self.tmp, "work")
        self.tvs = os.path.join(self.tmp, "tvs")
        os.makedirs(self.artifacts)
        os.makedirs(self.tvs)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.log_path = os.path.join(self.tmp, "adb.log")
        os.environ["FAKE_ADB_STATE"] = self.state_path
        os.environ["FAKE_ADB_LOG"] = self.log_path
        os.environ["TVKIT_ADB"] = FAKE_ADB
        os.environ.pop("TVKIT_APKSIGNER", None)
        self.set_state(factory_state())
        kit = json.load(open(os.path.join(KIT_DIR, "kit.json")))
        kit["requests_url"] = "https://requests.example.com/r/"
        sha1, cert1 = fake_apk(os.path.join(self.artifacts, "FreeMyTV-test.apk"), b"CERT-FREEMYTV-KIT-KEY", b"fmtv")
        sha2, cert2 = fake_apk(os.path.join(self.artifacts, "tvbro-test.apk"), b"CERT-PHLOX", b"tvbro")
        kit["artifacts"]["free-my-tv"].update({"file": "FreeMyTV-test.apk", "sha256": sha1, "signer_cert_sha256": cert1})
        kit["artifacts"]["tv-bro"].update({"file": "tvbro-test.apk", "sha256": sha2, "signer_cert_sha256": cert2})
        self.kit = kit
        self.kit_path = os.path.join(self.tmp, "kit.json")
        self.write_kit()

    def write_kit(self):
        with open(self.kit_path, "w") as f:
            json.dump(self.kit, f, indent=2)

    def set_state(self, state):
        with open(self.state_path, "w") as f:
            json.dump(state, f)

    def state(self):
        return json.load(open(self.state_path))

    def adb_log(self):
        if not os.path.exists(self.log_path):
            return []
        return [json.loads(line) for line in open(self.log_path) if line.strip()]

    def mutating_calls(self):
        out = []
        for call in self.adb_log():
            args = call[2:] if call[:1] == ["-s"] else call
            if not args:
                continue
            if args[0] in ("install", "uninstall"):
                out.append(call)
            elif args[0] == "shell" and any(" ".join(args[1:]).startswith(m) for m in tvkit.MUTATING_SHELL):
                out.append(call)
        return out

    def run_kit(self, *argv, stdin=""):
        """stdin = the words a person would type at the prompts, one per line. Injected through tvkit._ask (the real
        _ask refuses a non-terminal stdin, see test_piped_yes_is_refused); an empty line or EOF is "no"."""
        base = ["--kit", self.kit_path, "--tvs", self.tvs, "--artifacts", self.artifacts, "--work", self.work]
        answers = stdin.split("\n")
        old_ask = tvkit._ask
        tvkit._ask = lambda prompt: answers.pop(0) if answers else ""
        tvkit.CONSENTS[0] = 0
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = tvkit.main(base + list(argv))
        finally:
            tvkit._ask = old_ask
        return code, buf.getvalue()

    def new_tv(self, site="HOME", target=HOME_TV):
        argv = ["new", "--name", "Test TV", "--site", site]
        if target and site == "HOME":
            argv += ["--target", target]
        code, out = self.run_kit(*argv)
        self.assertEqual(code, 0, out)
        return re.search(r"tv_id ([0-9a-f-]{36})", out).group(1)

    # ---- check

    def test_check_passes_with_matching_artifacts(self):
        code, out = self.run_kit("check")
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK PASS", out)

    def test_check_refuses_sha_mismatch(self):
        with open(os.path.join(self.artifacts, "tvbro-test.apk"), "ab") as f:
            f.write(b"tampered")
        code, out = self.run_kit("check")
        self.assertEqual(code, 4)
        self.assertIn("sha256 MISMATCH", out)

    def test_check_refuses_signer_mismatch(self):
        sha, _cert = fake_apk(os.path.join(self.artifacts, "FreeMyTV-test.apk"), b"CERT-SOMEONE-ELSE", b"fmtv")
        self.kit["artifacts"]["free-my-tv"]["sha256"] = sha     # bytes "match", the key does not
        self.write_kit()
        code, out = self.run_kit("check")
        self.assertEqual(code, 4)
        self.assertIn("signer cert MISMATCH", out)

    def test_check_refuses_truncated_signing_block(self):
        path = os.path.join(self.artifacts, "tvbro-test.apk")
        data = open(path, "rb").read()
        eocd = data.rfind(b"PK\x05\x06")
        cd_off = struct.unpack("<I", data[eocd + 16:eocd + 20])[0]
        broken = data[:cd_off - 60] + b"\xff" * 20 + data[cd_off - 40:]       # garbage inside the block, same length
        open(path, "wb").write(broken)
        self.kit["artifacts"]["tv-bro"]["sha256"] = hashlib.sha256(broken).hexdigest()
        self.write_kit()
        code, out = self.run_kit("check")
        self.assertEqual(code, 4, out)
        self.assertIn("REFUSED", out)
        self.assertNotIn("Traceback", out)

    def test_install_refuses_mismatch_before_touching_tv(self):
        tv = self.new_tv()
        with open(os.path.join(self.artifacts, "FreeMyTV-test.apk"), "ab") as f:
            f.write(b"x")
        code, out = self.run_kit("install", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 4)
        self.assertEqual(self.adb_log(), [], "adb must not be called when an artifact fails the check")

    @unittest.skipUnless(REAL_ARTIFACTS and os.path.isdir(REAL_ARTIFACTS), "set TVKIT_TEST_ARTIFACTS to a folder with real APKs")
    def test_real_artifacts_match_kit_json(self):
        kit = json.load(open(os.path.join(KIT_DIR, "kit.json")))
        for name, art in kit["artifacts"].items():
            path = os.path.join(REAL_ARTIFACTS, art["file"])
            if art["sha256"] == tvkit.PLACEHOLDER_SHA or not os.path.isfile(path):
                continue   # your own Free My TV build has no shipped pin
            self.assertEqual(tvkit.sha256_file(path), art["sha256"], name)
            self.assertEqual(tvkit.apk_signer_cert_sha256(path), [art["signer_cert_sha256"]], name)

    # ---- records and the off-site rules

    def test_new_mints_unique_uuid4_and_check_accepts_it(self):
        a = self.new_tv()
        b = self.new_tv()
        self.assertNotEqual(a, b)
        self.assertRegex(a, tvkit.UUID4)
        code, out = self.run_kit("check", "--tv", a)
        self.assertEqual(code, 0, out)
        self.assertIn("Record OK", out)

    def test_offsite_new_never_stores_target(self):
        code, out = self.run_kit("new", "--name", "Cabin TV", "--site", "OFFSITE", "--target", HER_TARGET)
        self.assertEqual(code, 0, out)
        rec = json.load(open(os.path.join(self.tvs, re.search(r"tv_id ([0-9a-f-]{36})", out).group(1) + ".json")))
        self.assertNotIn("adb_target", rec)

    def test_offsite_refuses_home_ip_target(self):
        tv = self.new_tv(site="OFFSITE")
        for cmd in ("baseline", "wifi-check", "install", "after", "open-requests", "undo"):
            code, out = self.run_kit(cmd, "--tv", tv, "--target", HOME_TV, stdin="yes\n")
            self.assertEqual(code, 1, cmd + out)
            self.assertIn("home address", out)
        self.assertEqual(self.adb_log(), [], "no adb call may happen for a refused OFFSITE target")
        code, out = self.run_kit("new", "--name", "X", "--site", "OFFSITE", "--target", ip(192, 0, 2, 9) + ":5555")
        self.assertEqual(code, 1)

    def test_offsite_refuses_leading_zero_ip_and_hostnames(self):
        tv = self.new_tv(site="OFFSITE")
        for target in (ip(192, 0, 2, "041") + ":5555", ip("192", "000", "002", "041") + ":5555", "tv.lan:5555", "friends-tv:5555"):
            code, out = self.run_kit("baseline", "--tv", tv, "--target", target)
            self.assertEqual(code, 1, target + out)
        self.assertEqual(self.adb_log(), [])
        code, out = self.run_kit("baseline", "--tv", tv, "--target", HER_TARGET)
        self.assertEqual(code, 0, out)

    def test_record_refuses_unknown_fields(self):
        tv = self.new_tv()
        path = os.path.join(self.tvs, tv + ".json")
        rec = json.load(open(path))
        rec["ssid"] = "FakeNet"
        json.dump(rec, open(path, "w"))
        code, out = self.run_kit("check", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("unknown field 'ssid'", out)

    def test_offsite_record_refuses_home_ip_and_ha(self):
        tv = self.new_tv(site="OFFSITE")
        path = os.path.join(self.tvs, tv + ".json")
        rec = json.load(open(path))
        rec["notes"] = "reach it at " + ip(192, 0, 2, 77)
        rec["ha"] = {"tv": "media_player.other_house"}
        json.dump(rec, open(path, "w"))
        code, out = self.run_kit("check", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("home IP", out)
        self.assertIn("Home Assistant", out)

    def test_record_refuses_secrets_and_revoked(self):
        tv = self.new_tv()
        path = os.path.join(self.tvs, tv + ".json")
        rec = json.load(open(path))
        rec["pairing_code"] = "ABCD1234"
        json.dump(rec, open(path, "w"))
        code, out = self.run_kit("check", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("never hold secrets", out)
        rec.pop("pairing_code")
        rec["status"] = "revoked"
        json.dump(rec, open(path, "w"))
        code, out = self.run_kit("baseline", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("revoked", out)
        self.assertEqual(self.adb_log(), [])

    # ---- baseline (read-only)

    def test_baseline_is_read_only_and_sanitized(self):
        tv = self.new_tv()
        code, out = self.run_kit("baseline", "--tv", tv)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.mutating_calls(), [])
        base = tvkit.newest_baseline(self.work, tv)
        snap = json.load(open(os.path.join(base, "snapshot.json")))
        self.assertEqual(snap["home"], LAUNCHERX + "/.home.HomeActivity")
        self.assertEqual(snap["packages"]["disabled"], [VENDOR_DISABLED])
        self.assertEqual(snap["wifi"], {"connected": True, "saved_by": ["com.android.tv.settings"]})
        self.assertTrue(snap["play_store"]["launchable"])
        self.assertNotIn("FakeNet", json.dumps(snap), "no SSID in a snapshot")
        self.assertEqual(self.state(), factory_state(), "the TV must be unchanged by a baseline")

    def test_unauthorized_tv_is_refused(self):
        tv = self.new_tv()
        s = factory_state()
        s["adb_state"] = "unauthorized"
        self.set_state(s)
        code, out = self.run_kit("baseline", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("owner", out)

    # ---- wifi-check

    def test_wifi_check_stops_on_setupwraith_then_passes_after_resave(self):
        tv = self.new_tv()
        s = factory_state()
        s["wifi"]["saved_by"] = [SETUP]
        self.set_state(s)
        code, out = self.run_kit("wifi-check", "--tv", tv)
        self.assertEqual(code, 3)
        self.assertIn("STOP", out)
        self.assertIn("Forget", out)
        s["wifi"]["saved_by"] = ["com.android.tv.settings"]
        self.set_state(s)
        code, out = self.run_kit("wifi-check", "--tv", tv)
        self.assertEqual(code, 0, out)
        self.assertIn("WIFI-CHECK PASS", out)
        self.assertEqual(self.mutating_calls(), [])

    def test_wifi_check_stops_when_no_owner_is_known(self):
        tv = self.new_tv()
        s = factory_state()
        s["wifi"]["saved_by"] = []
        self.set_state(s)
        code, out = self.run_kit("wifi-check", "--tv", tv)
        self.assertEqual(code, 3)
        self.assertIn("STOP", out)

    # ---- install

    def test_piped_yes_is_refused(self):
        """The real prompt refuses a non-terminal stdin: `echo yes | tvkit install` must not count as the owner."""
        tv = self.new_tv()
        base = ["--kit", self.kit_path, "--tvs", self.tvs, "--artifacts", self.artifacts, "--work", self.work]
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("yes\n")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = tvkit.main(base + ["install", "--tv", tv])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 1, buf.getvalue())
        self.assertIn("not a terminal", buf.getvalue())
        self.assertEqual(self.mutating_calls(), [])

    def test_adb_guard_blocks_a_change_without_consent(self):
        adb = tvkit.Adb(FAKE_ADB, "fake:1")
        tvkit.CONSENTS[0] = 0
        with self.assertRaises(tvkit.Refused):
            adb.shell("pm enable " + GAMES)
        with self.assertRaises(tvkit.Refused):
            adb.run("install", "-r", "x.apk")
        self.assertEqual(self.adb_log(), [])

    def test_install_does_nothing_without_yes(self):
        tv = self.new_tv()
        code, out = self.run_kit("install", "--tv", tv, stdin="no\n")
        self.assertEqual(code, 1)
        self.assertIn("ABOUT TO CHANGE THE TV", out)
        self.assertEqual(self.mutating_calls(), [])
        self.assertNotIn(FMTV, self.state()["packages"])
        code, out = self.run_kit("install", "--tv", tv, stdin="")     # EOF is not a yes either
        self.assertEqual(code, 1)
        self.assertEqual(self.mutating_calls(), [])

    def test_install_with_yes_installs_both_and_disconnects(self):
        tv = self.new_tv()
        code, out = self.run_kit("install", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 0, out)
        pk = self.state()["packages"]
        self.assertTrue(pk[FMTV]["enabled"] and pk[TVBRO]["enabled"])
        installs = [c for c in self.adb_log() if "install" in c]
        self.assertEqual(len(installs), 2)
        self.assertTrue(any("disconnect" in c for c in self.adb_log()))
        self.assertIn("Home Screen -> Free My TV", out)
        self.assertEqual(self.state()["home"], LAUNCHERX + "/.home.HomeActivity", "the kit never does the switch itself")

    # ---- after

    def _baseline_then_switch(self):
        tv = self.new_tv()
        code, out = self.run_kit("baseline", "--tv", tv)
        self.assertEqual(code, 0, out)
        code, out = self.run_kit("install", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 0, out)
        self.set_state(switched(self.state()))
        return tv

    def test_after_passes_on_the_expected_switch(self):
        tv = self._baseline_then_switch()
        code, out = self.run_kit("after", "--tv", tv)
        self.assertEqual(code, 0, out)
        self.assertIn("AFTER-CHECK PASS", out)
        self.assertEqual(self.mutating_calls()[:], [c for c in self.mutating_calls() if "install" in c], "after is read-only")

    def test_after_fails_loudly_on_every_deviation(self):
        tv = self._baseline_then_switch()
        good = self.state()
        cases = {
            "home not switched": lambda s: s.update(home=LAUNCHERX + "/.home.HomeActivity"),
            "extra package disabled": lambda s: s["packages"][VENDING].update(enabled=False),
            "a target still enabled": lambda s: s["packages"][GAMES].update(enabled=True),
            "vendor package re-enabled": lambda s: s["packages"][VENDOR_DISABLED].update(enabled=True),
            "screensaver changed": lambda s: s["settings"].update(screensaver_components=FMTV + "/.Screensaver"),
            "wifi gone": lambda s: s["wifi"].update(connected=False),
            "accessibility service": lambda s: s["settings"].update(enabled_accessibility_services=FMTV + "/.RemoteKeyService"),
            "play store not launchable": lambda s: s["launchable"].pop(VENDING),
            "tv bro missing": lambda s: s["packages"].pop(TVBRO),
        }
        for name, mutate in cases.items():
            s = copy.deepcopy(good)
            mutate(s)
            self.set_state(s)
            code, out = self.run_kit("after", "--tv", tv)
            self.assertEqual(code, 2, "%s should fail: %s" % (name, out))
            self.assertIn("AFTER-CHECK FAILED", out, name)
            self.assertIn("FAIL", out, name)

    def test_after_with_screensaver_app_expects_the_apps_saver(self):
        tv = self._baseline_then_switch()
        path = os.path.join(self.tvs, tv + ".json")
        rec = json.load(open(path))
        rec["screensaver"] = "app"
        json.dump(rec, open(path, "w"))
        code, out = self.run_kit("after", "--tv", tv)
        self.assertEqual(code, 2, out)
        s = self.state()
        s["settings"]["screensaver_components"] = FMTV + "/io.freemytv.ScreensaverAlone"
        self.set_state(s)
        code, out = self.run_kit("after", "--tv", tv)
        self.assertEqual(code, 0, out)

    def test_after_without_baseline_is_refused(self):
        tv = self.new_tv()
        code, out = self.run_kit("after", "--tv", tv)
        self.assertEqual(code, 1)
        self.assertIn("baseline", out)
        self.assertEqual(self.adb_log(), [])

    # ---- open-requests

    def test_open_requests_asks_and_opens_r_in_tv_bro(self):
        tv = self._baseline_then_switch()
        code, out = self.run_kit("open-requests", "--tv", tv, stdin="no\n")
        self.assertEqual(code, 1)
        self.assertNotIn("started", self.state())
        code, out = self.run_kit("open-requests", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 0, out)
        started = self.state()["started"]
        self.assertEqual(len(started), 1)
        self.assertIn(self.kit["requests_url"], started[0])
        self.assertIn(TVBRO, started[0])
        self.assertIn("android.intent.action.VIEW", started[0])

    # ---- undo

    def test_undo_restores_from_baseline_with_yes_per_step(self):
        tv = self._baseline_then_switch()
        s = self.state()
        s["settings"]["screensaver_components"] = FMTV + "/io.freemytv.Screensaver"
        self.set_state(s)
        code, out = self.run_kit("undo", "--tv", tv, stdin="yes\nyes\nyes\nyes\nyes\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Home Screen -> Original", out)
        after = self.state()
        for p in (LAUNCHERX, SETUP, GAMES, TVMUSIC):
            self.assertTrue(after["packages"][p]["enabled"], p)
        self.assertFalse(after["packages"][VENDOR_DISABLED]["enabled"], "a package disabled in the baseline stays disabled")
        self.assertEqual(after["home"], LAUNCHERX + "/.home.HomeActivity")
        self.assertEqual(after["settings"]["screensaver_components"], "com.google.android.apps.tv.dreamx/.service.Backdrop")
        self.assertNotIn(FMTV, after["packages"])
        self.assertNotIn(TVBRO, after["packages"])
        self.assertIn("UNDO DONE", out)

    def test_undo_keeps_what_the_baseline_had_and_refuses_to_strand_the_tv(self):
        # TV Bro was on the TV before the kit, and one target package was already disabled by the owner.
        s = factory_state()
        s["packages"][TVBRO] = {"system": False, "enabled": True}
        s["packages"][GAMES]["enabled"] = False
        self.set_state(s)
        tv = self.new_tv()
        code, out = self.run_kit("baseline", "--tv", tv)
        self.assertEqual(code, 0, out)
        code, out = self.run_kit("install", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 0, out)
        self.set_state(switched(self.state()))
        # Owner says no to re-enabling and no to the home restore, yes to everything else: Free My TV must stay.
        code, out = self.run_kit("undo", "--tv", tv, stdin="no\nno\nyes\nyes\nyes\n")
        self.assertEqual(code, 0, out)
        after = self.state()
        self.assertIn(FMTV, after["packages"], "must not strand a TV whose launcher is still disabled")
        self.assertIn("NOT offering to uninstall", out)
        self.assertIn(TVBRO, after["packages"], "TV Bro was there before the kit")
        self.assertIn("keeping " + TVBRO, out)
        # Now the owner says yes to everything: launcher back, Free My TV goes, TV Bro still kept, Games stays disabled.
        code, out = self.run_kit("undo", "--tv", tv, stdin="yes\nyes\nyes\nyes\nyes\n")
        self.assertEqual(code, 0, out)
        after = self.state()
        self.assertNotIn(FMTV, after["packages"])
        self.assertIn(TVBRO, after["packages"])
        self.assertFalse(after["packages"][GAMES]["enabled"], "disabled before the kit: stays disabled")
        self.assertTrue(after["packages"][LAUNCHERX]["enabled"])
        self.assertEqual(after["home"], LAUNCHERX + "/.home.HomeActivity")

    def test_baseline_of_another_tv_is_refused(self):
        tv = self._baseline_then_switch()
        other = self.new_tv()
        base = tvkit.newest_baseline(self.work, tv)
        code, out = self.run_kit("after", "--tv", other, "--baseline", base)
        self.assertEqual(code, 1, out)
        self.assertIn("another TV", out)

    def test_undo_without_yes_changes_nothing(self):
        tv = self._baseline_then_switch()
        before = self.state()
        code, out = self.run_kit("undo", "--tv", tv, stdin="no\nno\nno\nno\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state(), before)
        self.assertEqual([c for c in self.mutating_calls() if "install" not in c], [])

    # ---- global rules

    def test_no_screenshot_anywhere(self):
        import ast
        tree = ast.parse(open(TVKIT).read())
        allowed = set()     # the MUTATING_SHELL list names screencap so the fake adb can reject it; that is the one legal mention
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "MUTATING_SHELL" for t in node.targets):
                allowed.update(id(n) for n in ast.walk(node))
        self.assertTrue(allowed, "MUTATING_SHELL not found; update this test")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in allowed:
                for word in ("screencap", "screenrecord", "exec-out"):
                    self.assertNotIn(word, node.value, "line %d" % node.lineno)
        out = subprocess.run([sys.executable, FAKE_ADB, "shell", "screencap", "-p"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 99, "the fake adb must reject screenshots so any attempt fails a test")

    def test_no_yes_flag(self):
        self.assertNotIn("--yes", open(TVKIT).read())

    # ---- off-site profile (a TV at another address)

    HER_TARGET = HER_TARGET     # the TV on its owner's own Wi-Fi: a runtime argument only, never written anywhere

    def new_offsite(self, name="Cabin TV"):
        code, out = self.run_kit("new", "--name", name, "--site", "OFFSITE")
        self.assertEqual(code, 0, out)
        self.assertIn("Next, off-site", out)
        return re.search(r"tv_id ([0-9a-f-]{36})", out).group(1)

    def files_under(self, root):
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                yield os.path.join(dirpath, fn)

    def test_lan_leaks_finds_every_kind_of_home_reference(self):
        kit = self.kit
        for bad in (ip(10, 0, 0, 41), "adb " + ip(10, 0, 0, 41) + ":5555", ip("010", "000", "000", "041"), ip(192, 168, 1, 5),
                    ip(172, 16, 4, 2), "127.0.0.1", "169.254.10.1", "100.64.3.3", "0.0.0.0", "fe80::1", "fd12:3456::1",
                    "nas.local", "tv.lan:5555", "router.home.arpa", "printer.home", "box.internal",
                    "http://localhost:32400", "my-server", "ssh my-nas", "MY-PIHOLE", "plex on my-nas"):
            self.assertTrue(tvkit.lan_leaks(bad, kit), "should be flagged: %r" % bad)
        for good in ("https://requests.example.com/r/", "plex.tv/link", "8.8.8.8", "kit 2026.11.11", "v1.2.0",
                     "io.freemytv/.HomeActivity", "Home Screen -> Free My TV", "a home Pi-hole", "Home Assistant",
                     "sha256 d8634edfe8d94b4fb9a52005d68a090a7f1e85b7f39c2cf01a25dc9dd60942b2", "Android 12", "1.2.3.4.5",
                     "com.google.android.apps.tv.launcherx", "localhosting"):
            self.assertEqual(tvkit.lan_leaks(good, kit), [], "should be clean: %r" % good)

    def test_offsite_config_in_repo_has_no_home_reference(self):
        """The off-site profile, the owner-sheet template and the one APK build config (it serves every TV, off-site too)."""
        kit = tvkit.load_kit(os.path.join(KIT_DIR, "kit.json"))
        for rel in ("profiles/offsite.json", "profiles/offsite-sheet.txt", "../../../build/config.properties.template"):
            text = open(os.path.join(KIT_DIR, rel), encoding="utf-8").read()
            self.assertEqual(tvkit.lan_leaks(text, kit), [], rel)
            self.assertNotRegex(text, PRIVATE_ADDR, rel)
        props = dict(line.split("=", 1) for line in open(os.path.join(ROOT, "build", "config.properties.template"))
                     if "=" in line and not line.startswith("#"))
        for key in ("plex_url", "plex_token", "tmdb_key", "watchmode_key"):
            self.assertEqual(props[key].strip(), "", "%s must stay empty: no local Plex address, no key in the APK" % key)

    def test_offsite_profile_is_consistent(self):
        kit = tvkit.load_kit(os.path.join(KIT_DIR, "kit.json"))
        profile = tvkit.load_profile(kit)
        req = profile["requests"]
        self.assertNotIn("url", req, "the profile has no built-in host: the URL is a per-TV setting")
        self.assertIn(req["recommended"], req["paths"])
        self.assertEqual(set(req["paths"]), {"r-browser", "own-app"})
        self.assertEqual(set(profile["install_paths"]), {"usb", "laptop"})
        self.assertEqual(profile["rules"], kit["rules"])
        never = " ".join(profile["tv_never_uses"]).lower()
        for word in ("private", "dns", "media-server address", "dashboard", "home assistant"):
            self.assertIn(word, never)
        self.assertGreaterEqual(len(profile["checks_without_adb"]), 4)

    def test_offsite_outputs_never_name_a_home_address(self):
        """Every file tvkit writes and everything it prints for an OFFSITE TV, over the whole flow, scanned for private IPs,
        .local/.lan names and listed machine names. The TV's address is typed at run time and must not be written anywhere."""
        tv = self.new_offsite()
        outputs = []
        pack = os.path.join(self.tmp, "stick")
        for argv, stdin in ((("check", "--tv", tv), ""), (("offsite-sheet", "--tv", tv), ""), (("usb-pack", "--tv", tv, "--out", pack), ""),
                            (("baseline", "--tv", tv, "--target", self.HER_TARGET), ""),
                            (("wifi-check", "--tv", tv, "--target", self.HER_TARGET), ""),
                            (("install", "--tv", tv, "--target", self.HER_TARGET), "yes\n")):
            code, out = self.run_kit(*argv, stdin=stdin)
            self.assertEqual(code, 0, "%s: %s" % (argv[0], out))
            outputs.append((argv[0], out))
        self.set_state(switched(self.state()))
        for argv, stdin in ((("after", "--tv", tv, "--target", self.HER_TARGET), ""),
                            (("open-requests", "--tv", tv, "--target", self.HER_TARGET), "yes\n")):
            code, out = self.run_kit(*argv, stdin=stdin)
            self.assertEqual(code, 0, "%s: %s" % (argv[0], out))
            outputs.append((argv[0], out))
        written = [os.path.join(self.tvs, tv + ".json")] + list(self.files_under(os.path.join(self.work, tv))) + \
            [p for p in self.files_under(pack) if not p.endswith(".apk")]
        self.assertGreaterEqual(len(written), 7, written)
        for path in written:
            text = open(path, encoding="utf-8").read()
            self.assertEqual(tvkit.lan_leaks(text, self.kit), [], path)
            self.assertNotIn(self.HER_TARGET.split(":")[0], text, "%s stores the TV's address" % path)
        for name, out in outputs:
            self.assertEqual(tvkit.lan_leaks(out.replace(self.HER_TARGET, "<tv>"), self.kit), [], name)

    def test_usb_pack_writes_checked_apks_and_the_sheet(self):
        tv = self.new_offsite()
        pack = os.path.join(self.tmp, "stick")
        code, out = self.run_kit("usb-pack", "--tv", tv, "--out", pack)
        self.assertEqual(code, 0, out)
        self.assertIn("USB-PACK DONE", out)
        folder = os.path.join(pack, "tv-kit")
        self.assertEqual(sorted(os.listdir(folder)), sorted(["FreeMyTV-test.apk", "tvbro-test.apk", "READ-ME-ON-THE-TV.txt",
                                                            "SHA256SUMS", "tv-kit-offsite.json"]))
        for line in open(os.path.join(folder, "SHA256SUMS")):
            sha, fn = line.split()
            self.assertEqual(tvkit.sha256_file(os.path.join(folder, fn)), sha)
        manifest = json.load(open(os.path.join(folder, "tv-kit-offsite.json")))
        self.assertEqual(manifest["tv_id"], tv)
        self.assertEqual(manifest["requests_url"], self.kit["requests_url"])
        self.assertEqual(set(manifest), {"tv_id", "name", "site", "kit_version", "made_at", "requests_url", "requests_path",
                                         "rules", "tv_never_uses", "artifacts"})
        sheet = open(os.path.join(folder, "READ-ME-ON-THE-TV.txt")).read()
        self.assertIn("Cabin TV", sheet)
        self.assertIn(self.kit["requests_url"], sheet)
        self.assertIn("Forget", sheet, "the Wi-Fi re-save comes before the switch (no wifi-check without adb)")
        self.assertIn("Original", sheet)
        self.assertNotIn("$", sheet, "every template field filled")
        self.assertEqual(self.adb_log(), [], "usb-pack never touches a TV")

    def test_usb_pack_refusals(self):
        home = self.new_tv()
        code, out = self.run_kit("usb-pack", "--tv", home, "--out", os.path.join(self.tmp, "a"))
        self.assertEqual(code, 1, out)
        self.assertIn("OFFSITE", out)
        tv = self.new_offsite()
        inside = os.path.join(ROOT, "pack-must-not-exist")
        code, out = self.run_kit("usb-pack", "--tv", tv, "--out", inside)
        self.assertEqual(code, 1, out)
        self.assertIn("inside the repository", out)
        self.assertFalse(os.path.exists(inside))
        busy = os.path.join(self.tmp, "busy", "tv-kit")
        os.makedirs(busy)
        open(os.path.join(busy, "old.txt"), "w").write("x")
        code, out = self.run_kit("usb-pack", "--tv", tv, "--out", os.path.dirname(busy))
        self.assertEqual(code, 1, out)
        self.assertIn("not empty", out)
        with open(os.path.join(self.artifacts, "FreeMyTV-test.apk"), "ab") as f:
            f.write(b"tampered")
        stick = os.path.join(self.tmp, "stick")
        code, out = self.run_kit("usb-pack", "--tv", tv, "--out", stick)
        self.assertEqual(code, 4, out)
        self.assertFalse(os.path.exists(stick), "nothing is written when an APK fails its check")

    def test_offsite_record_refuses_any_home_network_reference(self):
        tv = self.new_offsite()
        path = os.path.join(self.tvs, tv + ".json")
        clean = json.load(open(path))
        for note in ("DNS via my-pihole", "Plex at nas.local:32400", "their router " + ip(192, 168, 1, 1), "set up from my-server"):
            rec = dict(clean, notes=note)
            json.dump(rec, open(path, "w"))
            code, out = self.run_kit("check", "--tv", tv)
            self.assertEqual(code, 1, note + out)
            self.assertIn("must not mention a home IP", out)
            code, out = self.run_kit("usb-pack", "--tv", tv, "--out", os.path.join(self.tmp, "s"))
            self.assertEqual(code, 1, note + out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "s")))

    def test_offsite_usb_serial_target_needs_no_network(self):
        tv = self.new_offsite()
        code, out = self.run_kit("baseline", "--tv", tv, "--target", "ABC123XYZ")
        self.assertEqual(code, 0, out)
        self.assertFalse(any("connect" in c for c in self.adb_log()), "a USB serial is never adb-connected over a network")
        self.assertTrue(all(c[:2] == ["-s", "ABC123XYZ"] for c in self.adb_log()))
        for bad in ("cabin.tv", "tv-1", "ABC123XYZ:5555"):
            code, out = self.run_kit("baseline", "--tv", tv, "--target", bad)
            self.assertEqual(code, 1, bad + out)

    def test_each_offsite_tv_gets_its_own_identity(self):
        a, b = self.new_offsite("Cabin TV"), self.new_offsite("Cabin Bedroom TV")
        self.assertNotEqual(a, b)
        manifests = []
        for tv, stick in ((a, "s1"), (b, "s2")):
            code, out = self.run_kit("usb-pack", "--tv", tv, "--out", os.path.join(self.tmp, stick))
            self.assertEqual(code, 0, out)
            manifests.append(json.load(open(os.path.join(self.tmp, stick, "tv-kit", "tv-kit-offsite.json"))))
        self.assertEqual([m["tv_id"] for m in manifests], [a, b])
        self.assertNotEqual(manifests[0]["name"], manifests[1]["name"])
        for m in manifests:
            text = json.dumps(m).lower()
            for word in ("pairing_code", "cookie", "token", "password", "adb_target", "secret"):
                self.assertNotIn(word, text)

    def test_docs_have_no_private_address_or_local_name(self):
        kit = tvkit.load_kit(os.path.join(KIT_DIR, "kit.json"))
        for rel in ("README.md", "RUNBOOK.md", "CHANGELOG.md"):
            path = os.path.join(ROOT, rel)
            self.assertTrue(os.path.isfile(path), rel)
            doc = open(path, encoding="utf-8").read()
            self.assertNotRegex(doc, PRIVATE_ADDR, rel)
            leaks = tvkit.lan_leaks(doc, dict(kit, lan_hostnames=[], home_networks=[]))
            # 192.0.2.0/24 is the reserved documentation range, used for the example target in the README
            leaks = [x for x in leaks if not x.startswith("192.0.2.")]
            self.assertEqual(leaks, [], rel)

    def test_kit_json_in_repo_is_consistent(self):
        kit = tvkit.load_kit(os.path.join(KIT_DIR, "kit.json"))
        self.assertRegex(kit["version"], r"^\d{4}\.\d{2}\.\d{2}$")
        for name, art in kit["artifacts"].items():
            self.assertRegex(art["sha256"], r"^[0-9a-f]{64}$", name)
            self.assertRegex(art["signer_cert_sha256"], r"^[0-9a-f]{64}$", name)
            self.assertTrue(art["file"].endswith(".apk"))
        self.assertEqual(len(kit["expect"]["disabled_by_switch"]), 4)
        self.assertIn("192.0.2.0/24", kit["home_networks"])
        self.assertEqual(kit["requests_url"], "", "no built-in requests host")
        self.assertEqual(kit["artifacts"]["free-my-tv"]["sha256"], tvkit.PLACEHOLDER_SHA, "no shipped hash for a build you make yourself")
        examples = os.path.join(ROOT, "examples", "tvs")
        for fn in os.listdir(examples):
            if fn.endswith(".json"):
                rec = json.load(open(os.path.join(examples, fn)))
                self.assertEqual(fn, rec["tv_id"] + ".json")
                tvkit.validate_record(rec, kit, fn)

    # ---- the public package: no built-in host, placeholder pin, init, defaults

    def test_placeholder_pin_is_refused_with_a_clear_message(self):
        self.kit["artifacts"]["free-my-tv"]["sha256"] = tvkit.PLACEHOLDER_SHA
        self.write_kit()
        code, out = self.run_kit("check")
        self.assertEqual(code, 4, out)
        self.assertIn("placeholder pin", out)

    def test_no_requests_url_means_refusal_not_a_default_host(self):
        self.kit["requests_url"] = ""
        self.write_kit()
        tv = self.new_offsite()
        code, out = self.run_kit("offsite-sheet", "--tv", tv)
        self.assertEqual(code, 1, out)
        self.assertIn("no requests page URL", out)
        code, out = self.run_kit("--requests-url", "https://other.example.org/x", "offsite-sheet", "--tv", tv)
        self.assertEqual(code, 0, out)
        self.assertIn("https://other.example.org/x", out)

    def test_per_tv_requests_url_wins_over_kit_default(self):
        code, out = self.run_kit("new", "--name", "Den TV", "--site", "OFFSITE", "--requests-url", "https://den.example.net/r/")
        self.assertEqual(code, 0, out)
        tv = re.search(r"tv_id ([0-9a-f-]{36})", out).group(1)
        code, out = self.run_kit("offsite-sheet", "--tv", tv)
        self.assertEqual(code, 0, out)
        self.assertIn("https://den.example.net/r/", out)
        self.assertNotIn("requests.example.com", out)
        code, out = self.run_kit("new", "--name", "Bad", "--site", "OFFSITE", "--requests-url", "not-a-url")
        self.assertEqual(code, 1, out)

    def test_open_requests_uses_the_url_setting(self):
        tv = self.new_tv()
        self.set_state(switched(self.state()))
        code, out = self.run_kit("open-requests", "--tv", tv, stdin="yes\n")
        self.assertEqual(code, 0, out)
        self.assertTrue(any("https://requests.example.com/r/" in " ".join(c) for c in self.adb_log()))

    def test_init_copies_editable_kit_and_never_overwrites(self):
        dest = os.path.join(self.tmp, "cfg")
        code, out = self.run_kit("init", "--dir", dest)
        self.assertEqual(code, 0, out)
        for rel in ("kit.json", "profiles/offsite.json", "profiles/offsite-sheet.txt"):
            self.assertTrue(os.path.isfile(os.path.join(dest, rel)), rel)
        code, out = self.run_kit("init", "--dir", dest)
        self.assertEqual(code, 1, out)
        self.assertIn("never overwrites", out)

    def test_defaults_follow_xdg_and_overrides(self):
        old = {k: os.environ.get(k) for k in ("TVKIT_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "TVKIT_KIT")}
        try:
            os.environ.pop("TVKIT_HOME", None)
            os.environ.pop("TVKIT_KIT", None)
            os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp, "data")
            os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
            self.assertEqual(tvkit.data_home(), os.path.join(self.tmp, "data", "tvkit"))
            self.assertEqual(tvkit.default_kit_path(), os.path.join(tvkit.DATA_DIR, "kit.json"))
            os.makedirs(os.path.join(self.tmp, "config", "tvkit"))
            open(os.path.join(self.tmp, "config", "tvkit", "kit.json"), "w").write("{}")
            self.assertEqual(tvkit.default_kit_path(), os.path.join(self.tmp, "config", "tvkit", "kit.json"))
            os.environ["TVKIT_HOME"] = os.path.join(self.tmp, "home")
            self.assertEqual(tvkit.data_home(), os.path.join(self.tmp, "home"))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_module_help_runs_from_the_source_tree(self):
        env = dict(os.environ, PYTHONPATH=SRC)
        out = subprocess.run([sys.executable, "-m", "tvkit", "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("usb-pack", out.stdout)


if __name__ == "__main__":
    unittest.main()
