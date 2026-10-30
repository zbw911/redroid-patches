#!/usr/bin/env python3
"""Build the root-only VD creation/capture probe; never connect to a device."""

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.patch_systemui import digest, run, verify_native_dex


def build(output: Path, sdk: Path, java_home: Path) -> dict:
    output, sdk, java_home = output.resolve(), sdk.resolve(), java_home.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("Display probe output must be outside the source repository")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError("Output must be absent or an empty directory")
    else:
        output.mkdir(parents=True)
    classes, dex = output / "classes", output / "dex"
    classes.mkdir()
    dex.mkdir()
    tools = sdk / "build-tools" / "35.0.0"
    android = sdk / "platforms" / "android-35" / "android.jar"
    source = Path(__file__).with_name("StatusBarProbe.java").resolve()
    jdk = java_home / "bin"
    env = dict(os.environ, JAVA_HOME=str(java_home))
    env["PATH"] = str(jdk) + os.pathsep + env.get("PATH", "")
    run([jdk / "javac", "--release", "11", "-classpath", android, "-d", classes, source], env=env)
    jar = output / "classes.jar"
    run([jdk / "jar", "cf", jar, "-C", classes, "."], env=env)
    run([tools / "d8", "--min-api", "30", "--lib", android, "--output", dex, jar], env=env)
    artifact = dex / "classes.dex"
    validation = verify_native_dex(artifact, tools / "dexdump", env=env)
    proof = {"status": "passed", "source_sha256": digest(source),
             "dex": str(artifact), "dex_sha256": digest(artifact),
             "dex_validation": validation, "entry_point": "probe.StatusBarProbe",
             "geometry": {"width": 480, "height": 800, "dpi": 160},
             "protocol": {"transport": "line-delimited JSON on stdin/stdout",
                          "actions": ["create", "snapshot", "status", "release", "resize", "display-info", "host"],
                          "request": "{id,action,key}; create adds runtimeId/generation; snapshot/display-info accept displayId; host accepts request object",
                          "host_rpc": "root-only abstract buildploy-statusbar-host-v1; host-rpc one-shot mode also tests nonroot denial",
                          "response": "{id,ok,result} or {id,ok:false,error,cause}",
                          "snapshot": "result.pngBase64 contains original display PNG",
                          "eof": "original behavior: release all displays and exit"},
             "device_operations_performed": False}
    (output / "build-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=Path.home() / "dev/build/redroid-android15/artifacts/display-probe")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.sdk, args.java_home), indent=2))


if __name__ == "__main__":
    main()
