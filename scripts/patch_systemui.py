"""Build and verify a local, pinned SystemUI appearance bridge without installing it."""

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import zipfile


SOURCES = Path(__file__).resolve().parents[1] / "components" / "systemui-bridge"


def digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command: list[str | Path], *, env: dict | None = None,
        cwd: Path | None = None) -> str:
    return subprocess.run(list(map(str, command)), check=True, capture_output=True,
                          text=True, env=env, cwd=cwd).stdout


def validate_dex_header(data: bytes) -> dict:
    """Keep the original DEX039 layout, including the historical DEX041 failure fence."""
    if len(data) < 112 or data[:8] != b"dex\n039\0":
        raise ValueError("Expected DEX039 with a complete 112-byte header")
    file_size, header_size, endian = struct.unpack_from("<III", data, 32)
    if header_size != 112 or file_size != len(data) or endian != 0x12345678:
        raise ValueError("Invalid DEX039 file size, header size, or byte order")
    return {"version": "039", "header_size": header_size, "file_size": file_size}


def verify_native_dex(path: Path, dexdump: Path, *, env: dict | None = None) -> dict:
    header = validate_dex_header(Path(path).read_bytes())
    native = subprocess.run([str(dexdump), "-f", str(path)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, env=env, check=True)
    if native.stderr.strip():
        raise ValueError("Native dexdump emitted diagnostics: " + native.stderr.strip())
    return {**header, "native_parse": "passed"}


def _signature_entry(name: str) -> bool:
    return name.upper() == "META-INF/MANIFEST.MF" or re.fullmatch(
        r"META-INF/[^/]+\.(SF|RSA|DSA|EC)", name, re.IGNORECASE) is not None


def _entries(archive: zipfile.ZipFile) -> dict:
    entries = {entry.filename: entry for entry in archive.infolist()}
    if len(entries) != len(archive.infolist()):
        raise ValueError("Duplicate APK ZIP entry")
    return entries


def verify_apk_payload(source_apk: Path, candidate_apk: Path, target_dex: str,
                       bridge_entry: str, bridge_bytes: bytes) -> dict:
    """Only the target DEX, a new bridge DEX, and APK signatures may change."""
    with zipfile.ZipFile(source_apk) as source, zipfile.ZipFile(candidate_apk) as candidate:
        original = {name for name in _entries(source) if not _signature_entry(name)}
        actual = {name for name in _entries(candidate) if not _signature_entry(name)}
        if target_dex not in original or bridge_entry in original or actual != original | {bridge_entry}:
            raise ValueError("Unexpected added or missing APK payload entry")
        changed = sorted(name for name in original if source.read(name) != candidate.read(name))
        if changed != [target_dex]:
            raise ValueError(f"Unexpected changed original APK payload: {changed}")
        if candidate.read(bridge_entry) != bridge_bytes:
            raise ValueError("Added bridge DEX differs from compiled bridge")
    return {"changed_original_entries": changed, "added_bridge_dex": bridge_entry,
            "original_payload_entries": len(original), "manifest_and_resources_identical": True}


def _prepare_output(source: Path, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    output_dir = output_dir.resolve()
    if output_dir == source or output_dir in source.parents:
        raise ValueError("Source APK must be outside the output directory")
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise ValueError("Output directory must be empty")
    else:
        output_dir.mkdir()
    return output_dir


def patch_systemui(source_apk: Path, output_dir: Path, sdk: Path, java_home: Path,
                   jars: list[Path], key: Path, cert: Path, lock: dict) -> dict:
    """Consume caller-validated tools/signing inputs; write only inside an empty output."""
    source_apk = Path(source_apk).resolve(strict=True)
    output = _prepare_output(source_apk, output_dir)
    original_sha = digest(source_apk)
    if original_sha != lock["sourceSystemUiSha256"]:
        raise ValueError("Source SystemUI APK SHA-256 differs from lock")
    target = lock["targetDex"]
    if (target != "classes2.dex" or lock["dexFormat"] != "039"
            or lock["targetAndroidSdk"] != 35):
        raise ValueError("Unsupported pinned Android/SystemUI DEX contract")
    sdk, java_home = Path(sdk).resolve(), Path(java_home).resolve()
    tools = sdk / "build-tools" / lock["buildTools"]["version"]
    android = sdk / lock["androidPlatform"]["androidJar"]
    jdk = java_home / "bin"
    env = dict(os.environ, JAVA_HOME=str(java_home))
    env["PATH"] = str(jdk) + os.pathsep + env.get("PATH", "")
    cp = os.pathsep.join(str(Path(jar).resolve()) for jar in jars)
    tool_classes, bridge_classes, bridge_dex = [output / name for name in
                                               ("tool-classes", "bridge-classes", "bridge-dex")]
    for directory in (tool_classes, bridge_classes, bridge_dex):
        directory.mkdir()
    with zipfile.ZipFile(source_apk) as source:
        entries = _entries(source)
        source_header = validate_dex_header(source.read(target))
        dex_ids = [1 if name == "classes.dex" else int(match.group(1))
                   for name in entries if (match := re.fullmatch(r"classes(\d*)\.dex", name))]
        added = f"classes{max(dex_ids) + 1}.dex"
    release = str(lock["javaRelease"])
    run([jdk / "javac", "--release", release, "-cp", cp, "-d", tool_classes,
         SOURCES / "DexPatch.java", SOURCES / "VerifyDex.java"], env=env)
    # Only the wrapper's return descriptor needs this hidden framework type. Its real
    # implementation remains in the frozen device framework, never in our added DEX.
    stub_source = output / "compile-only/com/android/internal/statusbar/RegisterStatusBarResult.java"
    stub_source.parent.mkdir(parents=True)
    stub_source.write_text("package com.android.internal.statusbar; public final class RegisterStatusBarResult {}\n")
    stub_classes = output / "compile-only-classes"
    stub_classes.mkdir()
    run([jdk / "javac", "--release", release, "-d", stub_classes, stub_source], env=env)
    stub_jar = output / "compile-only.jar"
    run([jdk / "jar", "cf", stub_jar, "-C", stub_classes, "."], env=env)
    run([jdk / "javac", "--release", release, "-cp", str(android) + os.pathsep + str(stub_jar), "-d", bridge_classes,
         SOURCES / "AppearanceBridge.java"], env=env)
    run([tools / "d8", "--min-api", "35", "--lib", android, "--classpath", stub_jar, "--output", bridge_dex,
         *sorted(bridge_classes.rglob("*.class"))], env=env)
    patched_dex = output / "classes2-patched.dex"
    tools_cp = str(tool_classes) + os.pathsep + cp
    patch_log = run([jdk / "java", "-Xmx768m", "-cp", tools_cp, "DexPatch",
                     source_apk, patched_dex], env=env)
    (output / "dex-patch.log").write_text(patch_log)
    actual = dict(line.split("=", 1) for line in patch_log.splitlines() if "=" in line)
    expected = {"entry": target, "method": lock["commandQueueMethod"],
                "registers": str(lock["targetRegisters"]),
                "instructions": str(lock["targetInstructions"]),
                "registration_method": lock["registrationMethod"],
                "registration_registers": str(lock["registrationRegisters"]),
                "registration_code_units": str(lock["registrationCodeUnits"]),
                "registration_instruction_address": str(lock["registrationInstructionAddress"]),
                "task_restore_method": lock["taskRestoreMethod"],
                "task_restore_registers": str(lock["taskRestoreRegisters"]),
                "task_restore_code_units": str(lock["taskRestoreCodeUnits"]),
                "task_restore_instruction_address": str(lock["taskRestoreInstructionAddress"])}
    if any(actual.get(name) != value for name, value in expected.items()):
        raise ValueError("Original CommandQueue method differs from locked contract")
    patched_bytes = patched_dex.read_bytes()
    patched_header = verify_native_dex(patched_dex, tools / "dexdump", env=env)
    bridge_bytes = (bridge_dex / "classes.dex").read_bytes()
    bridge_header = verify_native_dex(bridge_dex / "classes.dex", tools / "dexdump", env=env)
    # Native ART tooling complements dexlib's structural/semantic comparison.
    (output / "dexdump-verification.txt").write_text("passed: patched and bridge DEX exit=0, stderr empty\n")
    unsigned = output / "SystemUI-unsigned.apk"
    removed_signatures = []
    with zipfile.ZipFile(source_apk) as source, zipfile.ZipFile(unsigned, "w") as result:
        for info in source.infolist():
            if _signature_entry(info.filename):
                removed_signatures.append(info.filename)
                continue
            result.writestr(info, patched_bytes if info.filename == target else source.read(info))
        bridge_info = zipfile.ZipInfo(added)
        bridge_info.compress_type = zipfile.ZIP_DEFLATED
        result.writestr(bridge_info, bridge_bytes)
    aligned, signed = output / "SystemUI-aligned.apk", output / "SystemUI.apk"
    run([tools / "zipalign", "-P", "16", "-f", "4", unsigned, aligned], env=env)
    run([tools / "apksigner", "sign", "--key", Path(key).resolve(), "--cert", Path(cert).resolve(),
         "--out", signed, aligned], env=env)
    signing = run([tools / "apksigner", "verify", "--verbose", "--print-certs", signed], env=env)
    fingerprint = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})", signing)
    if (not fingerprint or fingerprint.group(1) != lock["sourcePlatformCertSha256"]
            or "Number of signers: 1" not in signing):
        raise ValueError("Signed SystemUI certificate differs from locked platform certificate")
    (output / "apksigner-verification.txt").write_text(signing)
    run([tools / "zipalign", "-c", "-P", "16", "4", signed], env=env)
    payload = verify_apk_payload(source_apk, signed, target, added, bridge_bytes)
    semantic = run([jdk / "java", "-Xmx768m", "-cp", tools_cp, "VerifyDex", source_apk, signed], env=env)
    (output / "semantic-verification.txt").write_text(semantic)
    if not semantic.startswith("PASS classes2.dex "):
        raise ValueError("DEX semantic verifier did not confirm the complete comparison")
    proof = {"status": "passed", "original_sha256": original_sha,
             "patched_sha256": digest(signed), "certificate_sha256": fingerprint.group(1),
             "target_dex": target, "added_bridge_dex": added, **payload,
             "removed_old_signatures": removed_signatures,
             "sources": {name: digest(SOURCES / name) for name in
                         ("AppearanceBridge.java", "DexPatch.java", "VerifyDex.java")},
             "registration": {name: actual[name] for name in
                              ("registration_method", "registration_registers", "registration_code_units",
                               "registration_instruction_address")},
             "task_restore": {name: actual[name] for name in
                              ("task_restore_method", "task_restore_registers", "task_restore_code_units",
                               "task_restore_instruction_address")},
             "verification": {"dex_header": {"source": source_header, "patched": patched_header,
                                             "bridge": bridge_header},
                              "dexdump": "passed", "semantic": semantic.splitlines()[0],
                              "signature": "passed", "zipalign": "passed", "payload": "passed"}}
    proof_path = output / "proof.json"
    proof_path.write_text(json.dumps(proof, indent=2) + "\n")
    return {**proof, "systemui_apk": str(signed), "proof_path": str(proof_path)}
