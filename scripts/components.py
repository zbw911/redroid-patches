#!/usr/bin/env python3
"""Pinned redroid component builds; never edit or install into a running device."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

from doctor import inspect_environment
from patch_systemui import digest as sha, patch_systemui, run

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "configs/android15-components.lock.json"
MARKER = ".redroid-components.json"
GIB = 1024 ** 3


def check_hash(path, expected):
    if sha(path) != expected:
        raise ValueError(f"SHA256 mismatch: {path}")


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_lock():
    lock = json.loads(LOCK.read_text())
    if lock["schemaVersion"] != 1 or not re.fullmatch(r"redroid/redroid@sha256:[0-9a-f]{64}", lock["baseImage"]):
        raise ValueError("Unsupported lock schema or unpinned base image")
    hashes = [lock["sourceSystemUiSha256"], lock["sourcePlatformCertSha256"],
              lock["androidPlatform"]["androidJarSha256"],
              *lock["buildTools"]["files"].values(),
              *(item["sha256"] for item in lock["mavenArtifacts"])]
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise ValueError("Invalid SHA256 in component lock")
    if (lock["architecture"], lock["targetAndroidSdk"], lock["dexFormat"], lock["javaMajor"]) != ("x86_64", 35, "039", 17):
        raise ValueError("Unsupported component target; port and verify the patch before changing versions")
    return lock


def identity():
    return {"schemaVersion": 1, "purpose": "redroid-components", "lockSha256": sha(LOCK)}


def owned(workspace):
    marker = workspace / MARKER
    if not marker.is_file() or json.loads(marker.read_text()) != identity():
        raise ValueError("Workspace is unowned or uses another lock; prepare a new empty workspace")
    # Reject redirected output directories before any write or cleanup.
    for name in ("inputs", "cache", "artifacts"):
        path = workspace / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"Workspace directory missing or redirected: {path}")


def prepare(workspace):
    if workspace == ROOT or ROOT in workspace.parents:
        raise ValueError("Build workspace must be outside the source repository")
    if (workspace / MARKER).exists():
        owned(workspace)
        return {"workspace": str(workspace), "created": False}
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("Refusing to adopt a nonempty unowned workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("inputs", "cache", "artifacts"):
        (workspace / name).mkdir()
    write_json(workspace / MARKER, identity())
    return {"workspace": str(workspace), "created": True}


def capacity(workspace, minimum=2 * GIB):
    report = inspect_environment(workspace)
    storage = report["storage"]
    if storage["host_error"]:
        raise ValueError(storage["host_error"])
    available = storage["filesystem_available_bytes"]
    physical = storage["host_available_bytes"]
    if available is None or min(available, physical if physical is not None else available) < minimum:
        raise ValueError(f"Insufficient measured workspace capacity; need {minimum // GIB} GiB headroom")
    return report


def disk_usage(path):
    logical = allocated = 0
    for file in Path(path).rglob("*"):
        if file.is_file() and not file.is_symlink():
            stat = file.stat()
            logical += stat.st_size
            allocated += stat.st_blocks * 512
    return {"fileBytes": logical, "allocatedFileBytes": allocated}


def dependencies(workspace, sdk, lock):
    results = []
    for item in lock["mavenArtifacts"]:
        name = item["url"].rsplit("/", 1)[1]
        target = workspace / "cache" / name
        if target.exists():
            check_hash(target, item["sha256"])
        else:
            local = next((p for p in (sdk / "cmdline-tools").glob(f"*/lib/external/**/{name}")
                          if sha(p) == item["sha256"]), None)
            with tempfile.TemporaryDirectory(dir=workspace / "cache") as temporary:
                staged = Path(temporary) / name
                if local:
                    shutil.copyfile(local, staged)
                else:
                    with urllib.request.urlopen(item["url"], timeout=60) as response, staged.open("wb") as output:
                        shutil.copyfileobj(response, output)
                check_hash(staged, item["sha256"])
                staged.replace(target)
        results.append(target)
    return results


def extract(workspace, source, lock):
    target = workspace / "inputs/SystemUI.apk"
    if target.exists():
        check_hash(target, lock["sourceSystemUiSha256"])
        if source:
            check_hash(source, lock["sourceSystemUiSha256"])
        return {"sourceApk": str(target), "reused": True}
    with tempfile.TemporaryDirectory(dir=workspace / "inputs") as temporary:
        staged = Path(temporary) / "SystemUI.apk"
        if source:
            check_hash(source, lock["sourceSystemUiSha256"])
            shutil.copyfile(source, staged)
        else:
            run(["docker", "pull", "--platform", "linux/amd64", lock["baseImage"]])
            name = "redroid-component-extract-" + uuid.uuid4().hex
            container = run(["docker", "create", "--platform", "linux/amd64", "--name", name,
                             lock["baseImage"]]).strip()
            try:
                run(["docker", "cp", f"{container}:{lock['sourceSystemUiPath']}", staged])
            finally:
                run(["docker", "rm", container])
        check_hash(staged, lock["sourceSystemUiSha256"])
        staged.replace(target)
    return {"sourceApk": str(target), "reused": False}


def toolchain(sdk, java_home, lock):
    check_hash(sdk / lock["androidPlatform"]["androidJar"], lock["androidPlatform"]["androidJarSha256"])
    for path, expected in lock["buildTools"]["files"].items():
        check_hash(sdk / path, expected)
    env = dict(os.environ, JAVA_HOME=str(java_home))
    env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
    version = run([java_home / "bin/javac", "-version"], env=env).strip()
    expected = lock["javaVersion"]
    release = (java_home / "release").read_text()
    runtime = re.search(r'^JAVA_RUNTIME_VERSION="([^"]+)"$', release, re.MULTILINE)
    if version != "javac " + expected.split("+", 1)[0] or not runtime or runtime[1] != expected:
        raise ValueError(f"Pinned JDK {expected} required, got {version}")
    return env, version


def signing_identity(cert, lock):
    pem = cert.read_text()
    match = re.fullmatch(r"\s*-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+)-----END CERTIFICATE-----\s*", pem)
    if not match:
        raise ValueError("One PEM platform certificate is required")
    digest = hashlib.sha256(base64.b64decode(re.sub(r"\s", "", match[1]), validate=True)).hexdigest()
    if digest != lock["sourcePlatformCertSha256"]:
        raise ValueError("Signing certificate does not match the pinned SystemUI platform identity")


def source_hashes():
    files = [ROOT / "build.sh", LOCK, *sorted((ROOT / "scripts").glob("*.py")),
             *(p for p in sorted((ROOT / "components").rglob("*")) if p.is_file())]
    return {str(p.relative_to(ROOT)): sha(p) for p in files}


def dockerfile(lock):
    return (f"FROM --platform=linux/amd64 {lock['baseImage']}\n"
            "LABEL org.opencontainers.image.title=\"redroid status bar component prototype\"\n"
            f"LABEL dev.redroid.components.lock=\"{sha(LOCK)}\"\n"
            f"COPY --chown=0:0 --chmod=0644 SystemUI.apk {lock['sourceSystemUiPath']}\n"
            "COPY --chown=0:0 --chmod=0644 statusbar-window-host.apk "
            "/system/system_ext/priv-app/BuildployStatusBarHost/BuildployStatusBarHost.apk\n")


def build_components(args, workspace, lock):
    from build_host import build_host
    source = workspace / "inputs/SystemUI.apk"
    check_hash(source, lock["sourceSystemUiSha256"])
    env, compiler = toolchain(args.sdk, args.java_home, lock)
    if not args.key or not args.cert or not args.key.is_file():
        raise ValueError("Supply external --key platform.pk8 and --cert platform.x509.pem")
    signing_identity(args.cert, lock)
    tools = args.sdk / "build-tools" / lock["buildTools"]["version"]
    verification = run([tools / "apksigner", "verify", "--print-certs", source], env=env)
    if "certificate SHA-256 digest: " + lock["sourcePlatformCertSha256"] not in verification:
        raise ValueError("Original SystemUI signing identity mismatch")
    jars = []
    for item in lock["mavenArtifacts"]:
        jar = workspace / "cache" / item["url"].rsplit("/", 1)[1]
        check_hash(jar, item["sha256"])
        jars.append(jar)
    destination = workspace / "artifacts" / args.name
    destination.mkdir(exist_ok=False)
    sources = source_hashes()
    start = time.monotonic()
    before = disk_usage(workspace)
    systemui = patch_systemui(source, destination / "systemui", args.sdk, args.java_home,
                             jars, args.key, args.cert, lock)
    host = build_host(destination / "host", args.sdk, args.java_home, args.key, args.cert, lock)
    context = destination / "image"
    context.mkdir()
    shutil.copyfile(systemui["systemui_apk"], context / "SystemUI.apk")
    shutil.copyfile(host["apk"], context / "statusbar-window-host.apk")
    (context / "Dockerfile").write_text(dockerfile(lock))
    (context / ".dockerignore").write_text("*\n!Dockerfile\n!SystemUI.apk\n!statusbar-window-host.apk\n")
    if source_hashes() != sources:
        raise ValueError("Sources changed during component build; retry with a new build name")
    result = {"schemaVersion": 1, "lockSha256": sha(LOCK), "baseImage": lock["baseImage"],
              "sources": sources, "compiler": compiler, "systemUi": systemui, "host": host,
              "imageFiles": {p.name: sha(p) for p in sorted(context.iterdir())},
              "elapsedSeconds": round(time.monotonic() - start, 3),
              "workspaceBefore": before, "workspaceAfter": disk_usage(workspace),
              "artifacts": str(destination), "imageBuilt": False, "deviceTested": False}
    write_json(destination / "build.json", result)
    return result


def image_command(workspace, name, tag, lock):
    destination = workspace / "artifacts" / name
    proof = json.loads((destination / "build.json").read_text())
    if proof["lockSha256"] != sha(LOCK) or proof["sources"] != source_hashes():
        raise ValueError("Source or lock changed since component build; rebuild components")
    context = destination / "image"
    if (context / "Dockerfile").read_text() != dockerfile(lock):
        raise ValueError("Dockerfile differs from pinned recipe")
    for name, expected in proof["imageFiles"].items():
        check_hash(context / name, expected)
    return ["docker", "build", "--platform", "linux/amd64", "--pull", "--tag", tag, str(context)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "prepare", "deps", "extract", "components", "image"])
    parser.add_argument("--workspace", type=Path, default=Path.home() / "dev/build/redroid-android15")
    parser.add_argument("--sdk", type=Path, default=Path(os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "/opt/buildploy/android-sdk"))))
    parser.add_argument("--java-home", type=Path, default=Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")))
    parser.add_argument("--source-apk", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--name", default="prototype")
    parser.add_argument("--tag")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", args.name):
        raise ValueError("Invalid build name")
    lock = load_lock()
    if args.command == "doctor":
        result = inspect_environment(workspace)
        result["workspaceUsage"] = disk_usage(workspace)
    elif args.dry_run and args.command != "image":
        result = {"dryRun": True, "command": args.command, "workspace": str(workspace),
                  "baseImage": lock["baseImage"], "sourceApkSha256": lock["sourceSystemUiSha256"],
                  "downloads": [x["url"] for x in lock["mavenArtifacts"]] if args.command == "deps" else [],
                  "willPullBaseImage": args.command == "extract" and args.source_apk is None,
                  "buildName": args.name, "signingRequired": args.command == "components"}
    elif args.command == "prepare":
        result = prepare(workspace)
    else:
        owned(workspace)
        if args.command == "image":
            if not args.tag or args.tag.endswith(":latest") or ":" not in args.tag.rsplit("/", 1)[-1]:
                raise ValueError("Supply an explicit image --tag, excluding latest")
            command = image_command(workspace, args.name, args.tag, lock)
            result = {"dryRun": args.dry_run, "command": command}
            if not args.dry_run:
                capacity(workspace)
                subprocess.run(command, check=True)
                result["imageId"] = run(["docker", "image", "inspect", "--format", "{{.Id}}", args.tag]).strip()
                write_json(workspace / "artifacts" / args.name / "image-build.json", result)
        else:
            capacity(workspace)
            if args.command == "deps":
                result = {"jars": list(map(str, dependencies(workspace, args.sdk, lock)))}
            elif args.command == "extract":
                result = extract(workspace, args.source_apk, lock)
            else:
                result = build_components(args, workspace, lock)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as failure:
        print(f"ERROR: {failure}\n{(failure.stderr or failure.stdout or '')[-4000:]}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, KeyError) as failure:
        print(f"ERROR: {failure}", file=sys.stderr)
        sys.exit(1)
