#!/usr/bin/env python3
"""A fake adb for the tv-kit tests. No TV anywhere.

State lives in the JSON file named by FAKE_ADB_STATE; every call is appended to FAKE_ADB_LOG (one JSON list per line).
Mutating commands change the state file, so a test can see exactly what "the TV" would have become.
A screenshot (screencap/screenrecord) exits 99: the kit must never ask for one.
"""
import json
import os
import re
import sys


def load():
    with open(os.environ["FAKE_ADB_STATE"], "r", encoding="utf-8") as f:
        return json.load(f)


def save(state):
    tmp = os.environ["FAKE_ADB_STATE"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, os.environ["FAKE_ADB_STATE"])


def log(argv):
    with open(os.environ["FAKE_ADB_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\n")


def packages(state, flag):
    pk = state["packages"]   # {name: {"system": bool, "enabled": bool}}
    names = sorted(pk)
    if flag == "-d":
        names = [n for n in names if not pk[n]["enabled"]]
    elif flag == "-s":
        names = [n for n in names if pk[n]["system"]]
    elif flag == "-3":
        names = [n for n in names if not pk[n]["system"]]
    elif flag:
        names = [n for n in names if flag in n]
    return "".join("package:%s\n" % n for n in names)


def shell(state, cmd):
    words = cmd.split()
    if not words:
        return "", 0
    if words[0] in ("screencap", "screenrecord"):
        sys.stderr.write("fake adb: SCREENSHOT REQUESTED: the kit must never do this\n")
        return "", 99
    if words[0] == "getprop":
        return state["props"].get(words[1], "") + "\n", 0
    if cmd.startswith("cmd package resolve-activity"):
        if "android.intent.category.HOME" in cmd:
            return "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n%s\n" % state["home"], 0
        if "LEANBACK_LAUNCHER" in cmd:
            pkg = words[-1]
            p = state["packages"].get(pkg)
            if p and p["enabled"] and state.get("launchable", {}).get(pkg):
                return "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n%s\n" % state["launchable"][pkg], 0
            return "No activity found\n", 0
    if cmd.startswith("cmd package set-home-activity"):
        state["home"] = words[-1]
        save(state)
        return "Success\n", 0
    if cmd.startswith("pm list packages"):
        return packages(state, words[3] if len(words) > 3 else ""), 0
    if words[0] == "pm" and words[1] in ("enable", "disable", "disable-user"):
        pkg = words[-1]
        if pkg not in state["packages"]:
            return "Error: package %s not found\n" % pkg, 1
        state["packages"][pkg]["enabled"] = words[1] == "enable"
        save(state)
        return "Package %s new state: %s\n" % (pkg, "enabled" if words[1] == "enable" else "disabled"), 0
    if cmd.startswith("settings get secure"):
        return str(state["settings"].get(words[3], "null")) + "\n", 0
    if cmd.startswith("settings put secure"):
        state["settings"][words[3]] = " ".join(words[4:])
        save(state)
        return "", 0
    if cmd.startswith("settings delete secure"):
        state["settings"].pop(words[3], None)
        save(state)
        return "Deleted 1 rows\n", 0
    if cmd == "cmd wifi status":   # Android 11+: the connection lives here, not in dumpsys wifi (seen on Android 12 TVs)
        out = "Wifi is enabled\n==== Primary ClientModeManager instance ====\n"
        if state["wifi"]["connected"]:
            out += 'Wifi is connected to "FakeNet"\nWifiInfo: SSID: "FakeNet", BSSID: 00:11:22:33:44:55, MAC: 02:00:00:00:00:00\n'
        else:
            out += "Wifi is not connected\n"
        return out, 0
    if cmd == "dumpsys wifi":
        out = "Wifi is enabled\n"
        for owner in state["wifi"]["saved_by"]:
            out += "  ID: 0 SSID: \"FakeNet\" ... cname=%s ...\n" % owner
        return out, 0
    if words[0] == "am" and words[1] == "start":
        state.setdefault("started", []).append(cmd)
        save(state)
        return "Starting: Intent { act=android.intent.action.VIEW }\n", 0
    return "fake adb: unknown shell command: %s\n" % cmd, 1


def main(argv):
    log(argv)
    args = list(argv)
    target = None
    if args and args[0] == "-s":
        target = args[1]
        args = args[2:]
    state = load()
    if not args:
        return 1
    cmd = args[0]
    if cmd == "connect":
        if state.get("adb_state", "device") == "unreachable":
            print("cannot connect to %s: Connection refused" % args[1])
            return 1
        print("connected to %s" % args[1])
        return 0
    if cmd == "get-state":
        st = state.get("adb_state", "device")
        if st == "unauthorized":
            sys.stderr.write("error: device unauthorized.\n")
            return 1
        print(st)
        return 0
    if cmd == "disconnect":
        print("disconnected %s" % (args[1] if len(args) > 1 else ""))
        return 0
    if cmd == "shell":
        out, code = shell(state, " ".join(args[1:]))
        sys.stdout.write(out)
        return code
    if cmd == "install":
        path = args[-1]
        pkg = state["apk_packages"].get(os.path.basename(path))
        if not pkg:
            print("Failure [INSTALL_FAILED_INVALID_APK: fake adb knows no package for %s]" % path)
            return 1
        state["packages"][pkg] = {"system": False, "enabled": True}
        save(state)
        print("Performing Streamed Install\nSuccess")
        return 0
    if cmd == "uninstall":
        pkg = args[-1]
        if state["packages"].pop(pkg, None) is None:
            print("Failure [DELETE_FAILED_INTERNAL_ERROR]")
            return 1
        save(state)
        print("Success")
        return 0
    if cmd == "version":
        print("Android Debug Bridge version 0.0.0 (fake)")
        return 0
    sys.stderr.write("fake adb: unknown command %r\n" % argv)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
