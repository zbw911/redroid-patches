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
import uuid


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
        self.probe = self.streams = self.helper = None
        self.installed, self.displays = [], []
        self.helper_installed = False
        self.helper_package = "com.buildploy.preview.helper"
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

    def probe_request(self, action, **fields):
        self.sequence += 1
        response = self.request(self.probe, {"id": self.sequence, "action": action, **fields})
        if not response["ok"]:
            raise RuntimeError(response)
        return response["result"]

    def helper_request(self, action, app=None, **fields):
        self.sequence += 1
        request = {"id": self.sequence, "action": action, **fields}
        if app is not None:
            request.update({key: app[key] for key in ("runtimeId", "generation", "packageName")})
            if action != "create":
                request.update({key: app[key] for key in ("displayId", "displayUniqueId")})
        response = self.request(self.helper, request)
        if not response["ok"]:
            raise RuntimeError(response)
        return response["result"]

    def host(self, action="status", binding=None, check=True, **fields):
        response = self.probe_request("host", request={"action": action, **(binding or {}), **fields})
        if check and (not response.get("ok") or response.get("protocolVersion") != 1):
            raise RuntimeError(response)
        return response

    @staticmethod
    def binding(app):
        return {key: app[key] for key in ("runtimeId", "generation", "packageName", "displayId", "displayUniqueId")}

    def wait_host(self, predicate, name, timeout=12):
        end = time.monotonic() + timeout
        while True:
            status = self.host()
            if predicate(status):
                write(self.output / (name + ".json"), status)
                return status
            if time.monotonic() >= end:
                self.check(False, name, status)
            time.sleep(.15)

    def package_path(self, package):
        result = subprocess.run(self.base + ["shell", "pm", "path", package], env=self.env,
                                capture_output=True, text=True, timeout=40)
        if result.returncode in (0, 1) and not result.stdout.strip() and not result.stderr.strip():
            return None
        if result.returncode == 0 and result.stdout.startswith("package:"):
            return result.stdout.splitlines()[0].removeprefix("package:").strip()
        raise RuntimeError("Could not establish package installation ownership: " + package)

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
                width, height = app.get("width", 480), app.get("height", 800)
                bar_height = round(28 * app.get("dpi", 160) / 160)
                good &= (report.get("displayId") == app["displayId"]
                         and report.get("decorWidth") == width and report.get("rootWidth") == width
                         and report.get("decorHeight") == height and report.get("rootHeight") == height
                         and report.get("windowBounds") == f"[0,0][{width},{height}]"
                         and report.get("statusBars", {}).get("top") == bar_height
                         and report.get("rootScreenY") == 0
                         and report.get("requestedDarkIcons") == dark
                         and bar.get("darkIcons") == dark
                         and bar.get("appearanceReady") and bar.get("attached")
                         and all(bar.get("binding", {}).get(k) == v for k, v in self.binding(app).items())
                         and bar.get("widthPx") == width and bar.get("heightPx") == bar_height
                         and bar.get("requestedHeightPx") == bar_height
                         and bar.get("providedTopInsetPx") == bar_height
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
        snapshot = self.helper_request("snapshot", self.apps[index])
        (self.output / (name + ".png")).write_bytes(base64.b64decode(snapshot.pop("pngBase64")))

    def create_runtime(self, app):
        app["runtimeId"] = self.device["owner"] + "-" + app["key"]
        app["generation"] = "generation-" + uuid.uuid4().hex
        screen = self.helper_request("create", app, width=480, height=800, dpi=160)
        self.check(screen.get("statusBarHost") is True and bool(screen.get("displayUniqueId")),
                   "platform-helper-auto-binds-" + app["key"], screen)
        app.update(screen)
        self.displays.append(dict(app))
        real = self.probe_request("display-info", displayId=app["displayId"])
        self.check(real["displayUniqueId"] == app["displayUniqueId"]
                   and real["displayName"].startswith("buildploy-preview-" + app["runtimeId"] + "-" + app["generation"] + "-"),
                   "real-display-identity-" + app["key"], real)
        self.wait_host(lambda state: any(all(bar["binding"].get(k) == v for k, v in self.binding(app).items())
                                        for bar in state["bars"]), "auto-binding-" + app["key"])

    def release_runtime(self, app):
        self.helper_request("release", app)
        self.displays = [item for item in self.displays if item["runtimeId"] != app["runtimeId"]]
        self.wait_host(lambda state: not any(bar["displayId"] == app["displayId"] for bar in state["bars"]),
                       "helper-release-" + app["key"])

    def add_session(self, index):
        app = self.apps[index]
        if self.package_path(app["packageName"]) is not None:
            raise ValueError("Refusing to replace a preexisting fixture package")
        if hashlib.sha256(Path(app["apk"]).read_bytes()).hexdigest() != app["sha256"]:
            raise ValueError("Fixture APK changed")
        self.adb("install", "--user", "0", app["apk"])
        self.installed.append(app["packageName"])
        app["key"] = f"session{index + 1}"
        self.create_runtime(app)
        self.launch(index, darkIcons=index % 2 == 0, prefsWrite=f"private-{index + 1}")

    def recover_process(self, package, name, expected):
        before = self.host()
        old = self.shell("pidof", package).strip()
        self.check(bool(re.fullmatch(r"[1-9][0-9]*", old)), name + "-single-owned-pid", old)
        command = self.shell("su", "0", "cat", "/proc/" + old + "/cmdline").replace("\0", " ").strip()
        self.check(command == package, name + "-exact-process", command)
        self.shell("su", "0", "kill", "-9", old)
        started = time.monotonic()
        end = started + 15
        observed = None
        while time.monotonic() < end:
            new = self.adb("shell", "pidof", package, check=False).strip()
            windows = self.shell("dumpsys", "window", "windows")
            owned_windows = windows.split("Window #")
            # No RPC/broadcast/start call occurs until both the new process and its windows exist.
            if re.fullmatch(r"[1-9][0-9]*", new) and new != old and all(
                    any(re.search(r"BuildployStatusBarHost-" + str(self.apps[i]["displayId"]) + r"(?:[}\s])", block)
                        and (package != HOST or re.search(r"mSession=Session\{[^}]*\b" + new + r":", block))
                        for block in owned_windows)
                    for i in expected):
                observed = {"oldPid": int(old), "newPid": int(new), "elapsedSeconds": time.monotonic() - started,
                            "windowTitlesObservedBeforeHostRpc": True}
                break
            time.sleep(.15)
        (self.output / (name + "-windows.txt")).write_text(windows)
        self.check(observed is not None, name + "-automatic-process-and-window-recovery", observed)
        previous = {bar["displayId"]: bar for bar in before["bars"]}
        def replay_complete(status):
            bars = {bar["displayId"]: bar for bar in status["bars"]}
            for index in expected:
                app = self.apps[index]
                bar = bars.get(app["displayId"], {})
                event = bar.get("lastAppliedEvent") or {}
                old_bar = previous[app["displayId"]]
                if (not bar.get("appearanceReady") or not bar.get("requestId")
                        or bar["requestId"] == old_bar["requestId"]
                        or not all(bar.get("binding", {}).get(k) == v for k, v in self.binding(app).items())):
                    return False
                if package == "com.android.systemui" and (
                        not event.get("bridgeEpoch")
                        or event["bridgeEpoch"] == old_bar["lastAppliedEvent"]["bridgeEpoch"]
                        or event.get("source") != "SystemUI.StatusBarManagerService.registerStatusBarForAllDisplays"):
                    return False
            return True
        self.wait_host(replay_complete, name + "-replay-complete")
        restored = self.state(name, expected)
        self.check(all(bar["requestId"] != previous[bar["displayId"]]["requestId"] for bar in restored["host"]["bars"]),
                   name + "-fresh-nonces")
        if package == "com.android.systemui":
            self.check(all(bar["lastAppliedEvent"]["bridgeEpoch"] != previous[bar["displayId"]]["lastAppliedEvent"]["bridgeEpoch"]
                           and bar["lastAppliedEvent"]["source"] == "SystemUI.StatusBarManagerService.registerStatusBarForAllDisplays"
                           for bar in restored["host"]["bars"]), name + "-fresh-registration-snapshot")
        self.result["lifecycle"].append({"name": name, "passed": True, **observed})

    def identity_fences(self):
        app = self.apps[0]
        before = self.host()
        for field, value in (("generation", "expired-generation"), ("displayUniqueId", "expired-unique-id"),
                             ("packageName", self.apps[1]["packageName"])):
            for action in ("bind", "release"):
                response = self.host(action, {**self.binding(app), field: value}, check=False)
                self.check(response.get("ok") is False and response.get("error") == "BINDING_CONFLICT",
                           "host-rejects-" + action + "-" + field, response)
        after = self.host()
        self.check([(bar["binding"], bar["requestId"]) for bar in before["bars"]]
                   == [(bar["binding"], bar["requestId"]) for bar in after["bars"]], "identity-rejections-preserve-live-bindings")
        result = subprocess.run(self.base + ["shell", "CLASSPATH=" + self.remote_dex, "su", "2000", "app_process", "/",
                                            "probe.StatusBarProbe", "host-rpc"], env=self.env,
                                input='{"action":"status"}\n', capture_output=True, text=True, timeout=15)
        denied = json.loads(result.stdout.strip())
        self.check(denied.get("ok") is False and bool(denied.get("transportError")), "host-socket-denies-nonroot-peer", denied)

    def tap(self, index):
        app = self.apps[index]
        before = self.reports()
        box = before[app["packageName"]]["topButtonBounds"]
        point = {"x": ((box["left"] + box["right"]) // 2) / (app["width"] - 1),
                 "y": ((box["top"] + box["bottom"]) // 2) / (app["height"] - 1),
                 "pointerId": self.sequence + 1}
        receipts, input_error = [], None
        try:
            for phase in ("down", "up"):
                receipt = self.helper_request("pointer", app, input={**point, "phase": phase})
                receipts.append(receipt)
                if receipt.get("applied") is not True:
                    raise RuntimeError("Platform helper did not acknowledge pointer " + phase)
        except Exception as error:
            input_error = repr(error)
        end = time.monotonic() + 8
        good = False
        after = self.reports()
        while input_error is None and time.monotonic() < end:
            after = self.reports()
            good = all(p in after and after[p]["topButtonTaps"] == value["topButtonTaps"] + (p == app["packageName"])
                       for p, value in before.items())
            if good:
                break
            time.sleep(0.15)
        detail = {"displayId": app["displayId"], "bounds": box, "inputPath": "platform-helper-pointer"}
        if not good:
            prefix = "tap-failed-" + app["key"] + "-" + str(self.sequence)
            evidence = {"binding": self.binding(app), "point": point, "before": before, "after": after,
                        "pointerReceipts": receipts, "inputError": input_error, "captureErrors": []}
            for name, command in (("window", ("dumpsys", "window", "windows")),
                                  ("input", ("dumpsys", "input")), ("display", ("dumpsys", "display"))):
                try:
                    (self.output / (prefix + "-" + name + ".txt")).write_text(self.shell(*command))
                except Exception as error:
                    evidence["captureErrors"].append({"part": name, "error": repr(error)})
            try:
                snapshot = self.probe_request("snapshot", displayId=app["displayId"])
                (self.output / (prefix + ".png")).write_bytes(base64.b64decode(snapshot.pop("pngBase64")))
                evidence["snapshot"] = snapshot
            except Exception as error:
                evidence["captureErrors"].append({"part": "snapshot", "error": repr(error)})
            write(self.output / (prefix + ".json"), evidence)
            detail["evidence"] = prefix
        self.check(good, "targeted-tap-" + app["key"], detail)

    def run(self):
        self.phase("boot-baseline")
        host_pid = self.shell("pidof", HOST).strip()
        self.check(bool(re.fullmatch(r"[1-9][0-9]*", host_pid)), "persistent-host-started-without-activity", host_pid)
        self.remote_dex = "/data/local/tmp/" + self.device["owner"] + ".dex"
        self.adb("push", self.args.probe_dex, self.remote_dex)
        self.probe_log = (self.output / "display-probe-stderr.txt").open("w")
        self.probe = subprocess.Popen(self.base + ["shell", "CLASSPATH=" + self.remote_dex, "su", "0",
                                      "app_process", "/", "probe.StatusBarProbe"], env=self.env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.probe_log,
                                      text=True, bufsize=1)
        hello = self.host("hello")
        self.check(hello.get("hostPackage") == HOST and hello.get("heightDp") == 28, "host-protocol-v1", hello)
        self.check(not self.host()["bars"], "empty-host-before-test")
        if self.package_path(self.helper_package) is not None:
            raise ValueError("Refusing to replace a preexisting platform helper")
        self.adb("install", "--user", "0", self.args.helper_apk)
        self.helper_installed = True
        helper_path = self.package_path(self.helper_package)
        self.check(bool(helper_path), "platform-helper-installed")
        self.helper_log = (self.output / "platform-helper-stderr.txt").open("w")
        self.helper = subprocess.Popen(self.base + ["shell", "CLASSPATH=" + helper_path, "su", "0", "app_process", "/",
                                       self.args.helper_main_class], env=self.env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=self.helper_log, text=True, bufsize=1)
        helper_hello = self.helper_request("hello")
        self.check(helper_hello.get("protocolVersion") == 1 and helper_hello.get("statusBarHost") is True,
                   "platform-helper-detects-persistent-host", helper_hello)
        self.result["helper"] = {"apk": str(self.args.helper_apk), "sha256": hashlib.sha256(self.args.helper_apk.read_bytes()).hexdigest(),
                                 "mainClass": self.args.helper_main_class, "hello": helper_hello}
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
        self.identity_fences()
        self.helper_request("resize", self.apps[0], width=640, height=960, dpi=240)
        self.apps[0].update(width=640, height=960, dpi=240)
        resized = self.state("resize-density-42px", {0: True, 1: False})
        bar = next(bar for bar in resized["host"]["bars"] if bar["displayId"] == self.apps[0]["displayId"])
        self.check(bar["heightPx"] == 42 and bar["widthPx"] == 640, "28dp-resizes-with-target-density", bar)
        self.helper_request("resize", self.apps[0], width=480, height=800, dpi=160)
        self.apps[0].update(width=480, height=800, dpi=160)
        self.state("resize-restored", {0: True, 1: False})
        self.recover_process("com.android.systemui", "systemui-kill-replay", {0: True, 1: False})
        for index in range(2):
            self.tap(index)
            self.capture(index, "systemui-recovered-" + str(index + 1))
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
                  "scrcpyServer": str(self.args.scrcpy), "fps": self.args.fps, "maxSize": 800, "bitrateKbps": 1200}
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
        previous_packets = {item["key"]: item["packets"] for item in ready["streams"]}
        sample_at = time.monotonic()
        while time.monotonic() < end:
            for index in range(10):
                self.tap(index)
            rounds += 1
            if rounds == 1:
                self.recover_process(HOST, "host-kill-recovery-with-ten-streams", expected)
            if time.monotonic() >= sample_at:
                sample = self.request(self.streams, {"action": "status"})
                with (self.output / "stream-samples.jsonl").open("a") as output:
                    output.write(json.dumps(sample) + "\n")
                self.check(len(sample["streams"]) == 10 and all(not item["errors"] and not item["closed"]
                           and item["packets"] > previous_packets[item["key"]] for item in sample["streams"]),
                           "ten-streams-progress-round-" + str(rounds))
                previous_packets = {item["key"]: item["packets"] for item in sample["streams"]}
                sample_at = time.monotonic() + 10
            time.sleep(1)
        live = self.request(self.streams, {"action": "status"})
        write(self.output / "streams-live.json", live)
        self.check(rounds > 0 and len(live["streams"]) == 10 and
                   all(item["packets"] > 2 and not item["errors"] for item in live["streams"]),
                   "ten-streams-survive-concurrent-interaction", live)
        self.result["stability"] = {"requestedFps": self.args.fps, "requestedSeconds": self.args.duration,
                                    "interactionRounds": rounds, "elapsedSeconds": time.monotonic() - (end - self.args.duration)}
        for index in range(10):
            self.capture(index, "ten-final-" + str(index + 1))
        self.close_streams()
        self.phase("data-lifecycle")
        self.shell("am", "force-stop", "--user", "0", self.apps[0]["packageName"])
        restarted = self.launch(0, darkIcons=True)
        self.check(restarted["privateValue"] == "private-1", "app-restart-retains-own-data")
        old_binding = self.binding(self.apps[0])
        self.adb("uninstall", self.apps[0]["packageName"])
        self.wait_host(lambda status: all(bar["binding"]["packageName"] != self.apps[0]["packageName"] for bar in status["bars"]),
                       "package-uninstall-removes-registration")
        self.release_runtime(self.apps[0])
        rejected = self.host("bind", old_binding, check=False)
        self.check(rejected.get("ok") is False and rejected.get("error") == "DISPLAY_IDENTITY_EXPIRED", "released-display-binding-rejected", rejected)
        self.adb("install", "--user", "0", self.apps[0]["apk"])
        self.create_runtime(self.apps[0])
        stale = self.host("bind", {**self.binding(self.apps[0]), "generation": old_binding["generation"]}, check=False)
        self.check(stale.get("ok") is False and stale.get("error") == "BINDING_CONFLICT", "old-generation-cannot-bind-new-display", stale)
        fresh = self.launch(0, darkIcons=True)
        others = [self.launch(i, darkIcons=i % 2 == 0) for i in range(1, 10)]
        self.check(fresh["privateValue"] is None and
                   all(r["privateValue"] == f"private-{i + 2}" for i, r in enumerate(others)),
                   "uninstall-clears-only-own-data")
        self.phase("host-lifecycle-observations")
        self.release_runtime(self.apps[9])
        auxiliary_runtime = "aux-" + uuid.uuid4().hex
        auxiliary = self.probe_request("create", key="removed-display", runtimeId=auxiliary_runtime,
                                       generation="aux-generation")
        binding = {"runtimeId": auxiliary_runtime,
                   "generation": "aux-generation", "packageName": self.apps[9]["packageName"],
                   "displayId": auxiliary["displayId"], "displayUniqueId": auxiliary["displayUniqueId"]}
        self.host("bind", binding)
        self.probe_request("release", key="removed-display")
        absent = self.wait_host(lambda status: all(bar["displayId"] != binding["displayId"] for bar in status["bars"]),
                                "display-removal-automatically-prunes-binding")
        self.check(len(absent["bars"]) == 9, "display-removal-preserves-other-nine")
        rejected = self.host("bind", binding, check=False)
        self.check(rejected.get("ok") is False and rejected.get("error") == "DISPLAY_IDENTITY_EXPIRED", "removed-auxiliary-identity-rejected", rejected)
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
        for display in list(self.displays):
            try:
                self.release_runtime(display)
            except Exception as error:
                errors.append(str(error))
        if self.helper is not None:
            self.helper.stdin.close()
            try:
                self.helper.wait(timeout=20)
            except subprocess.TimeoutExpired:
                errors.append("Platform helper did not close after EOF")
                self.helper.terminate()
                self.helper.wait(timeout=5)
            self.helper_log.close()
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
        if self.helper_installed:
            try:
                self.adb("uninstall", self.helper_package)
            except Exception as error:
                errors.append(str(error))
        if hasattr(self, "remote_dex"):
            self.shell("rm", "-f", self.remote_dex)
        self.result["cleanupErrors"] = errors
        self.result["passed"] = self.result["passed"] and not errors
        write(self.output / "result.json", self.result)
        self.phase("finished")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--probe-dex", type=Path, required=True)
    parser.add_argument("--helper-apk", type=Path, required=True)
    parser.add_argument("--helper-main-class", default="com.buildploy.preview.Helper")
    parser.add_argument("--providers", type=Path, required=True)
    parser.add_argument("--scrcpy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--fps", type=int, default=15)
    acceptance = Acceptance(parser.parse_args())
    try:
        acceptance.run()
    except Exception as error:
        acceptance.result["error"] = repr(error)
        raise
    finally:
        acceptance.cleanup()
    if acceptance.result.get("cleanupErrors"):
        raise RuntimeError("Device cleanup did not complete")


if __name__ == "__main__":
    main()
