#!/usr/bin/env python3
"""Exercise an explicitly provisioned disposable redroid; never select a device implicitly."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time


HOST = "com.buildploy.statusbarprobe.host"


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        repository = Path(__file__).resolve().parents[1]
        if self.output == repository or repository in self.output.parents:
            raise ValueError("Device evidence must be outside the source repository")
        self.output.mkdir(parents=True, exist_ok=False)
        self.device = json.loads(args.device.read_text())
        if not self.device.get("owner", "").startswith("statusbar-image-test-"):
            raise ValueError("An explicitly owned disposable test device is required")
        self.env = dict(os.environ, ADB_SERVER_SOCKET=self.device["adbServerSocket"])
        self.base = [self.device["adb"], "-s", self.device["serial"]]
        fixture = json.loads(args.fixture.read_text())
        self.apps = [next(p for p in fixture["packages"]
                          if p["packageName"] == f"com.buildploy.statusbarprobe.session{i}")
                     for i in range(1, 11)]
        self.result = {"device": self.device, "fixture": fixture, "checks": [], "lifecycle": [],
                       "displayModel": "10 independent package UIDs in Android user 0", "passed": False}
        self.probe = self.streams = None
        self.installed, self.displays = [], []
        self.sequence = 0

    def adb(self, *args, check=True):
        result = subprocess.run(self.base + list(map(str, args)), env=self.env,
                                capture_output=True, text=True, timeout=40)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout if check else result.stdout + result.stderr

    def shell(self, *args):
        return self.adb("shell", *args)

    def phase(self, name):
        value = {"name": name, "time": time.time()}
        write(self.output / "phase.json", value)
        print(json.dumps(value), flush=True)

    def check(self, condition, name, detail=None):
        self.result["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        write(self.output / "result.json", self.result)
        if not condition:
            raise AssertionError(name + ": " + repr(detail))

    def request(self, child, value, timeout=25):
        child.stdin.write(json.dumps(value) + "\n")
        child.stdin.flush()
        return self.reply(child, timeout)

    @staticmethod
    def reply(child, timeout):
        if not select.select([child.stdout], [], [], timeout)[0]:
            raise TimeoutError("Probe response timeout")
        line = child.stdout.readline()
        if not line:
            raise RuntimeError(f"Probe exited: {child.poll()}")
        return json.loads(line)

    def display(self, action, key=None):
        self.sequence += 1
        request = {"id": self.sequence, "action": action}
        if key is not None:
            request["key"] = key
        response = self.request(self.probe, request)
        if not response["ok"]:
            raise RuntimeError(response)
        return response["result"]

    def host(self, action="status", display=None):
        args = ["su", "0", "am", "broadcast", "--user", "0", "-n", HOST + "/.Commands", "--es", "action", action]
        if display is not None:
            args += ["--ei", "displayId", str(display)]
        raw = self.shell(*args)
        match = re.search(r'data="(\{.*\})"', raw)
        if not match:
            raise RuntimeError(raw)
        response = json.loads(match[1])
        if not response["ok"]:
            raise RuntimeError(response)
        return response["result"]

    def reports(self):
        raw = self.adb("logcat", "-d", "-s", "BuildployStatusBarProbe:I", "*:S")
        reports = {}
        for line in raw.splitlines():
            prefix = "BUILDPLOY_STATUSBAR_PROBE "
            if prefix in line:
                value = json.loads(line.split(prefix, 1)[1])
                reports[value["packageName"]] = value
        return reports

    def launch(self, index, **options):
        app = self.apps[index]
        self.sequence += 1
        request = "request-" + str(self.sequence)
        args = ["am", "start", "--user", "0", "--display", str(app["displayId"]),
                "-n", app["component"], "--es", "requestId", request]
        for key, value in options.items():
            args += ["--ez" if isinstance(value, bool) else "--es", key,
                     str(value).lower() if isinstance(value, bool) else str(value)]
        self.shell(*args)
        end = time.monotonic() + 12
        while time.monotonic() < end:
            report = self.reports().get(app["packageName"], {})
            if report.get("requestId") == request:
                return report
            time.sleep(0.15)
        raise TimeoutError(f"App did not render {request}: {app['packageName']}")

    def state(self, name, expected):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            host = self.host()
            reports = self.reports()
            bars = {b["displayId"]: b for b in host["bars"]}
            good = True
            for index, dark in expected.items():
                app = self.apps[index]
                report = reports.get(app["packageName"], {})
                bar = bars.get(app["displayId"], {})
                event = bar.get("lastAppliedEvent") or {}
                good &= (report.get("displayId") == app["displayId"]
                         and report.get("statusBars", {}).get("top") == 28
                         and report.get("rootScreenY") == 0
                         and report.get("requestedDarkIcons") == dark
                         and bar.get("darkIcons") == dark
                         and event.get("packageName") == app["packageName"]
                         and event.get("senderPackage") == "com.android.systemui"
                         and bar.get("lastDrawnSequence") == event.get("seq"))
            if good:
                break
            time.sleep(0.2)
        value = {"host": host, "apps": reports}
        write(self.output / (name + ".json"), value)
        self.check(good, name, {"expected": expected, "evidence": name + ".json"})
        return value

    def capture(self, index, name):
        snapshot = self.display("snapshot", self.apps[index]["key"])
        (self.output / (name + ".png")).write_bytes(base64.b64decode(snapshot.pop("pngBase64")))

    def add_session(self, index):
        app = self.apps[index]
        if self.adb("shell", "pm", "path", app["packageName"], check=False).strip():
            raise ValueError("Refusing to replace a preexisting fixture package")
        if hashlib.sha256(Path(app["apk"]).read_bytes()).hexdigest() != app["sha256"]:
            raise ValueError("Fixture APK changed")
        self.adb("install", "--user", "0", app["apk"])
        self.installed.append(app["packageName"])
        app["key"] = f"session{index + 1}"
        screen = self.display("create", app["key"])
        app["displayId"] = screen["displayId"]
        self.displays.append(screen)
        self.host("add", app["displayId"])
        self.launch(index, darkIcons=index % 2 == 0, prefsWrite=f"private-{index + 1}")

    def tap(self, index):
        app = self.apps[index]
        before = self.reports()
        box = before[app["packageName"]]["topButtonBounds"]
        self.shell("input", "-d", str(app["displayId"]), "tap", str((box["left"] + box["right"]) // 2),
                   str((box["top"] + box["bottom"]) // 2))
        end = time.monotonic() + 8
        good = False
        while time.monotonic() < end:
            after = self.reports()
            good = all(after[p]["topButtonTaps"] == value["topButtonTaps"] + (p == app["packageName"])
                       for p, value in before.items())
            if good:
                break
            time.sleep(0.15)
        self.check(good, "targeted-tap-" + app["key"], {"displayId": app["displayId"], "bounds": box})

    def run(self):
        self.phase("boot-baseline")
        systemui = self.shell("pidof", "com.android.systemui").strip()
        self.shell("am", "start", "--user", "0", "--display", "0", "-n", HOST + "/.HostActivity")
        self.check(not self.host()["bars"], "empty-host-before-test")
        self.remote_dex = "/data/local/tmp/" + self.device["owner"] + ".dex"
        self.adb("push", self.args.probe_dex, self.remote_dex)
        self.probe_log = (self.output / "display-probe-stderr.txt").open("w")
        self.probe = subprocess.Popen(self.base + ["shell", "CLASSPATH=" + self.remote_dex, "su", "0",
                                      "app_process", "/", "probe.StatusBarProbe"], env=self.env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.probe_log,
                                      text=True, bufsize=1)
        for index in range(2):
            self.add_session(index)
        self.phase("two-displays")
        first = self.state("dual-initial", {0: True, 1: False})
        for index in range(2):
            self.capture(index, f"dual-initial-{index + 1}")
        self.launch(0, darkIcons=False)
        second = self.state("dual-first-changed", {0: False, 1: False})
        self.check(first["host"]["bars"][1]["lastAppliedEvent"]["seq"] ==
                   second["host"]["bars"][1]["lastAppliedEvent"]["seq"], "non-target-event-unchanged")
        self.launch(1, darkIcons=True)
        swapped = self.state("dual-swapped", {0: False, 1: True})
        forged = self.adb("shell", "su", "2000", "am", "broadcast", "--user", "0",
                          "-n", HOST + "/.AppearanceEvents", "-a", "com.buildploy.statusbarprobe.APPEARANCE",
                          "--ei", "displayId", str(self.apps[0]["displayId"]),
                          "--ei", "appearance", "8", check=False)
        after_forged = self.host()
        self.check(("SecurityException" in forged or "Permission Denial" in forged)
                   and [b["lastAppliedEvent"]["seq"] for b in after_forged["bars"]] ==
                   [b["lastAppliedEvent"]["seq"] for b in swapped["host"]["bars"]],
                   "unprivileged-appearance-event-rejected", forged)
        for index in range(2):
            self.capture(index, f"dual-swapped-{index + 1}")
        self.launch(0, showStatus=False)
        time.sleep(0.5)
        hidden = self.reports()
        self.check(hidden[self.apps[0]["packageName"]]["statusBars"]["top"] == 0
                   and hidden[self.apps[1]["packageName"]]["statusBars"]["top"] == 28,
                   "hide-status-only-target")
        self.capture(0, "dual-hidden-1")
        self.launch(0, showStatus=True, darkIcons=True)
        self.launch(1, darkIcons=False)
        self.state("dual-restored", {0: True, 1: False})
        for index in range(2, 10):
            self.add_session(index)
        expected = {i: i % 2 == 0 for i in range(10)}
        ten = self.state("ten-apps", expected)
        reports = [ten["apps"][app["packageName"]] for app in self.apps]
        self.check(len({r["uid"] for r in reports}) == 10
                   and len({r["displayId"] for r in reports}) == 10
                   and all(r["privateValue"] == f"private-{i + 1}" for i, r in enumerate(reports)),
                   "ten-independent-uids-displays-private-values", reports)
        other_uid = str(reports[1]["uid"])
        self.check(self.shell("su", other_uid, "id", "-u").strip() == other_uid,
                   "private-read-probe-uses-other-app-uid")
        denial = self.adb("shell", "su", other_uid, "cat",
                          "/data/user/0/" + self.apps[0]["packageName"] + "/shared_prefs/device-fixture.xml", check=False)
        self.check("Permission denied" in denial, "other-uid-cannot-read-private-prefs", denial)
        self.phase("ten-apps-without-encoders")
        time.sleep(10)
        config = {**{k: self.device[k] for k in ("adb", "serial", "adbServerSocket")},
                  "providerModule": str(self.args.providers.resolve()), "outputDirectory": str(self.output / "streams"),
                  "displays": [{k: app[k] for k in ("key", "displayId", "packageName")} for app in self.apps],
                  "scrcpyServer": str(self.args.scrcpy), "fps": 5, "maxSize": 800, "bitrateKbps": 1200}
        write(self.output / "streams-config.json", config)
        self.stream_log = (self.output / "streams-stderr.txt").open("w")
        self.streams = subprocess.Popen(["node", str(Path(__file__).with_name("stream_probe.mjs")),
                                        str(self.output / "streams-config.json")], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=self.stream_log, text=True, bufsize=1)
        ready = self.reply(self.streams, 180)
        write(self.output / "streams-ready.json", ready)
        self.check(ready.get("event") == "ready", "ten-streams-ready", ready)
        self.phase("ten-simultaneous-streams")
        end = time.monotonic() + self.args.duration
        rounds = 0
        while time.monotonic() < end:
            for index in range(10):
                self.tap(index)
            rounds += 1
            time.sleep(1)
        live = self.request(self.streams, {"action": "status"})
        write(self.output / "streams-live.json", live)
        self.check(rounds > 0 and len(live["streams"]) == 10 and
                   all(item["packets"] > 2 and not item["errors"] for item in live["streams"]),
                   "ten-streams-survive-concurrent-interaction", live)
        for index in range(10):
            self.capture(index, "ten-final-" + str(index + 1))
        self.close_streams()
        self.phase("data-lifecycle")
        self.shell("am", "force-stop", "--user", "0", self.apps[0]["packageName"])
        restarted = self.launch(0, darkIcons=True)
        self.check(restarted["privateValue"] == "private-1", "app-restart-retains-own-data")
        self.adb("uninstall", self.apps[0]["packageName"])
        self.adb("install", "--user", "0", self.apps[0]["apk"])
        fresh = self.launch(0, darkIcons=True)
        others = [self.launch(i, darkIcons=i % 2 == 0) for i in range(1, 10)]
        self.check(fresh["privateValue"] is None and
                   all(r["privateValue"] == f"private-{i + 2}" for i, r in enumerate(others)),
                   "uninstall-clears-only-own-data")
        self.phase("host-lifecycle-observations")
        self.display("release", self.apps[9]["key"])
        self.displays = [d for d in self.displays if d["key"] != self.apps[9]["key"]]
        after_removal = self.host()
        write(self.output / "host-after-display-removal.json", after_removal)
        self.result["lifecycle"].append({"name": "automatic-display-removal-cleanup", "supported":
            not any(b["displayId"] == self.apps[9]["displayId"] for b in after_removal["bars"])})
        self.shell("am", "force-stop", "--user", "0", HOST)
        self.shell("am", "start", "--user", "0", "--display", "0", "-n", HOST + "/.HostActivity")
        restarted_host = self.host()
        write(self.output / "host-after-restart.json", restarted_host)
        self.result["lifecycle"].append({"name": "automatic-host-restart-recovery",
                                         "supported": len(restarted_host["bars"]) == 9,
                                         "actualBars": len(restarted_host["bars"])})
        self.check(self.shell("pidof", "com.android.systemui").strip() == systemui, "systemui-process-stable")
        crashes = self.adb("logcat", "-d", "-b", "crash")
        (self.output / "crashes.txt").write_text(crashes)
        self.check("FATAL EXCEPTION" not in crashes and "Fatal signal" not in crashes, "no-android-crashes")
        self.result["passed"] = True

    def close_streams(self):
        if self.streams is not None:
            self.streams.stdin.close()
            self.streams.stdin = None
            output, _ = self.streams.communicate(timeout=30)
            (self.output / "streams-close.txt").write_text(output)
            code = self.streams.returncode
            self.streams = None
            self.stream_log.close()
            if code:
                raise RuntimeError(f"Stream probe failed while closing: {code}")

    def cleanup(self):
        errors = []
        try:
            self.close_streams()
        except Exception as error:
            errors.append(str(error))
        for display in self.displays:
            try:
                self.host("remove", display["displayId"])
            except Exception as error:
                errors.append(str(error))
        if self.probe is not None:
            self.probe.stdin.close()
            try:
                self.probe.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.probe.terminate()
                self.probe.wait(timeout=5)
            self.probe_log.close()
        for package in self.installed:
            try:
                self.adb("uninstall", package)
            except Exception as error:
                errors.append(str(error))
        if hasattr(self, "remote_dex"):
            self.shell("rm", "-f", self.remote_dex)
        self.result["cleanupErrors"] = errors
        write(self.output / "result.json", self.result)
        self.phase("finished")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--probe-dex", type=Path, required=True)
    parser.add_argument("--providers", type=Path, required=True)
    parser.add_argument("--scrcpy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=60)
    acceptance = Acceptance(parser.parse_args())
    try:
        acceptance.run()
    except Exception as error:
        acceptance.result["error"] = repr(error)
        raise
    finally:
        acceptance.cleanup()


if __name__ == "__main__":
    main()
