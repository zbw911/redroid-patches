"""Build the existing status bar window host with caller-validated SDK/signing inputs."""

import copy
import json
import os
from pathlib import Path
import re
import zipfile

from patch_systemui import digest, run, verify_native_dex


SOURCES = Path(__file__).resolve().parents[1] / "components" / "statusbar-host"


def build_host(output_dir: Path, sdk: Path, java_home: Path, key: Path,
               cert: Path, lock: dict) -> dict:
    """Write the APK, intermediate files, and proof into an independent empty output."""
    if (lock["javaMajor"], lock["javaRelease"], lock["targetAndroidSdk"]) != (17, 11, 35):
        raise ValueError("The window host requires Java 17, release 11 and Android 35")
    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    output = output.resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ValueError("Output directory must be empty")
    else:
        output.mkdir()
    sdk, java_home = Path(sdk).resolve(), Path(java_home).resolve()
    tools = sdk / "build-tools" / lock["buildTools"]["version"]
    android = sdk / lock["androidPlatform"]["androidJar"]
    jdk = java_home / "bin"
    env = dict(os.environ, JAVA_HOME=str(java_home))
    env["PATH"] = str(jdk) + os.pathsep + env.get("PATH", "")
    classes, dex = output / "classes", output / "dex"
    classes.mkdir()
    dex.mkdir()
    sources = sorted((SOURCES / "src").rglob("*.java"))
    manifest = SOURCES / "AndroidManifest.xml"
    run([jdk / "javac", "--release", "11", "-classpath", android,
         "-d", classes, *sources], env=env, cwd=output)
    jar = output / "classes.jar"
    run([jdk / "jar", "cf", jar, "-C", classes, "."], env=env, cwd=output)
    run([tools / "d8", "--min-api", "35", "--lib", android,
         "--output", dex, jar], env=env, cwd=output)
    dex_proof = verify_native_dex(dex / "classes.dex", tools / "dexdump", env=env)
    unsigned, normalized = output / "unsigned.apk", output / "normalized.apk"
    aligned = output / "aligned-unsigned.apk"
    run([tools / "aapt", "package", "-f", "-M", manifest,
         "-I", android, "-F", unsigned], env=env, cwd=output)
    run([tools / "aapt", "add", unsigned, "classes.dex"], env=env, cwd=dex)
    # Stable ZIP order/timestamps keep fixed component inputs reproducible.
    with zipfile.ZipFile(unsigned) as source, zipfile.ZipFile(normalized, "w") as result:
        for entry in sorted(source.infolist(), key=lambda entry: entry.filename):
            fixed = copy.copy(entry)
            fixed.date_time = (1980, 1, 1, 0, 0, 0)
            result.writestr(fixed, source.read(entry))
    run([tools / "zipalign", "-P", "16", "-f", "4", normalized, aligned], env=env, cwd=output)
    signed = output / "statusbar-window-host.apk"
    run([tools / "apksigner", "sign", "--key", Path(key).resolve(),
         "--cert", Path(cert).resolve(), "--out", signed, aligned], env=env, cwd=output)
    signing = run([tools / "apksigner", "verify", "--verbose", "--print-certs", signed],
                  env=env, cwd=output)
    fingerprint = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})", signing)
    if (not fingerprint or fingerprint.group(1) != lock["sourcePlatformCertSha256"]
            or "Number of signers: 1" not in signing):
        raise ValueError("Window host signer differs from the locked platform certificate")
    run([tools / "zipalign", "-c", "-P", "16", "4", signed], env=env, cwd=output)
    badging = run([tools / "aapt", "dump", "badging", signed], env=env, cwd=output)
    package = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", badging, re.MULTILINE)
    minimum = re.search(r"^sdkVersion:'(\d+)'", badging, re.MULTILINE)
    target = re.search(r"^targetSdkVersion:'(\d+)'", badging, re.MULTILINE)
    if (not package or package.group(1) != lock["windowsHostPackage"]
            or not minimum or minimum.group(1) != "35"
            or not target or target.group(1) != "35"):
        raise ValueError("Window host package or min/target SDK differs from the lock")
    (output / "aapt-badging.txt").write_text(badging)
    (output / "apksigner-verification.txt").write_text(signing)
    proof = {"status": "passed", "apk_sha256": digest(signed),
             "certificate_sha256": fingerprint.group(1),
             "package_name": package.group(1), "version_code": int(package.group(2)),
             "compile_sdk": 35, "min_sdk": 35, "target_sdk": 35,
             "java_major": 17, "java_release": 11,
             "sources": {str(path.relative_to(SOURCES)): digest(path)
                         for path in [manifest, *sources]},
             "verification": {"dex": dex_proof, "signature": "passed",
                              "zipalign": "passed", "zipalign_page_size_kib": 16,
                              "package_sdk_badging": "passed"}}
    proof_path = output / "build-proof.json"
    proof_path.write_text(json.dumps(proof, indent=2) + "\n")
    return {"apk": str(signed), "proof": str(proof_path)}
