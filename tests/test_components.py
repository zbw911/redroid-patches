import contextlib
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import components


class ComponentsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        self.workspace = self.root / "workspace"
        self.original = b"small pinned source apk fixture"
        self.lock = {
            "schemaVersion": 1,
            "baseImage": "redroid/redroid@sha256:" + "a" * 64,
            "sourceSystemUiPath": "/system/system_ext/priv-app/SystemUI/SystemUI.apk",
            "sourceSystemUiSha256": hashlib.sha256(self.original).hexdigest(),
            "sourcePlatformCertSha256": "b" * 64,
            "architecture": "x86_64", "targetAndroidSdk": 35,
            "dexFormat": "039", "javaMajor": 17,
            "androidPlatform": {"androidJarSha256": "c" * 64},
            "buildTools": {"files": {}},
            "mavenArtifacts": [{"url": "https://example.invalid/library.jar",
                                "sha256": "d" * 64}],
        }
        self.lock_file = self.repository / "configs/components.lock.json"
        self.lock_file.parent.mkdir(parents=True)
        self.lock_file.write_text(json.dumps(self.lock))
        (self.repository / "build.sh").write_text("#!/bin/sh\n")
        (self.repository / "scripts").mkdir()
        (self.repository / "scripts/build.py").write_text("print('build')\n")
        self.component_source = self.repository / "components/host/Host.java"
        self.component_source.parent.mkdir(parents=True)
        self.component_source.write_text("final class Host {}\n")
        self.patch(components, "ROOT", self.repository)
        self.patch(components, "LOCK", self.lock_file)
        self.patch(components.subprocess, "run", mock.Mock(
            side_effect=AssertionError("Tests must not execute Docker or other tools")))
        self.patch(components.urllib.request, "urlopen", mock.Mock(
            side_effect=AssertionError("Tests must not download dependencies")))

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def image_build(self, workspace):
        components.prepare(workspace)
        destination = workspace / "artifacts/candidate"
        context = destination / "image"
        context.mkdir(parents=True)
        (context / "Dockerfile").write_text(components.dockerfile(self.lock))
        (context / "SystemUI.apk").write_bytes(b"patched systemui fixture")
        (context / "statusbar-window-host.apk").write_bytes(b"host fixture")
        (context / ".dockerignore").write_text("*\n!Dockerfile\n!*.apk\n")
        proof = {
            "lockSha256": components.sha(self.lock_file),
            "sources": components.source_hashes(),
            "imageFiles": {p.name: components.sha(p) for p in context.iterdir()},
        }
        (destination / "build.json").write_text(json.dumps(proof))
        return context

    def test_prepare_is_idempotent_and_preserves_outputs(self):
        self.assertTrue(components.prepare(self.workspace)["created"])
        marker = (self.workspace / components.MARKER).read_bytes()
        output = self.workspace / "artifacts/retained.txt"
        output.write_text("keep this result")
        self.assertFalse(components.prepare(self.workspace)["created"])
        self.assertEqual((self.workspace / components.MARKER).read_bytes(), marker)
        self.assertEqual(output.read_text(), "keep this result")
        for name in ("inputs", "cache", "artifacts"):
            self.assertTrue((self.workspace / name).is_dir())

    def test_prepare_does_not_adopt_nonempty_unmarked_directory(self):
        self.workspace.mkdir()
        existing = self.workspace / "existing.txt"
        existing.write_text("unrelated data")
        with self.assertRaisesRegex(ValueError, "nonempty unowned"):
            components.prepare(self.workspace)
        self.assertEqual(list(self.workspace.iterdir()), [existing])
        self.assertEqual(existing.read_text(), "unrelated data")

    def test_prepare_rejects_workspace_from_another_lock(self):
        components.prepare(self.workspace)
        marker = (self.workspace / components.MARKER).read_bytes()
        self.lock_file.write_text(json.dumps({**self.lock, "sourcePlatformCertSha256": "e" * 64}))
        with self.assertRaisesRegex(ValueError, "another lock"):
            components.prepare(self.workspace)
        self.assertEqual((self.workspace / components.MARKER).read_bytes(), marker)

    def test_prepare_rejects_redirected_input_cache_or_artifact_directories(self):
        external = self.root / "external"
        external.mkdir()
        sentinel = external / "retained.txt"
        sentinel.write_text("outside workspace")
        for name in ("inputs", "cache", "artifacts"):
            with self.subTest(directory=name):
                workspace = self.root / ("redirected-" + name)
                components.prepare(workspace)
                (workspace / name).rmdir()
                (workspace / name).symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "redirected"):
                    components.prepare(workspace)
                self.assertTrue((workspace / name).is_symlink())
                self.assertEqual(list(external.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_text(), "outside workspace")

    def test_extract_local_source_is_pinned_and_reusable_without_docker(self):
        components.prepare(self.workspace)
        source = self.root / "original.apk"
        source.write_bytes(self.original)
        self.assertFalse(components.extract(self.workspace, source, self.lock)["reused"])
        self.assertTrue(components.extract(self.workspace, None, self.lock)["reused"])
        self.assertEqual((self.workspace / "inputs/SystemUI.apk").read_bytes(), self.original)
        self.assertEqual(source.read_bytes(), self.original)

    def test_extract_never_overwrites_existing_input_on_hash_mismatch(self):
        for name, existing, incoming in (
                ("bad-existing", b"unexpected cached apk", self.original),
                ("bad-incoming", self.original, b"unexpected source apk")):
            with self.subTest(case=name):
                workspace = self.root / name
                components.prepare(workspace)
                target = workspace / "inputs/SystemUI.apk"
                target.write_bytes(existing)
                source = self.root / (name + ".apk")
                source.write_bytes(incoming)
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    components.extract(workspace, source, self.lock)
                self.assertEqual(target.read_bytes(), existing)
                self.assertEqual(source.read_bytes(), incoming)
                self.assertEqual(list((workspace / "inputs").iterdir()), [target])

    def test_dry_runs_do_not_create_workspaces_or_run_tools_or_downloads(self):
        for command in ("prepare", "deps", "extract", "components"):
            with self.subTest(command=command):
                workspace = self.root / ("dry-" + command)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    components.main([command, "--workspace", str(workspace), "--dry-run"])
                result = json.loads(output.getvalue())
                self.assertTrue(result["dryRun"])
                self.assertEqual(result["command"], command)
                self.assertFalse(workspace.exists())

    def test_image_dry_run_validates_existing_build_without_executing_docker(self):
        context = self.image_build(self.workspace)
        before = {p.name: p.read_bytes() for p in context.iterdir()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            components.main(["image", "--workspace", str(self.workspace), "--name", "candidate",
                             "--tag", "buildploy/redroid:components-test", "--dry-run"])
        result = json.loads(output.getvalue())
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["command"][:4], ["docker", "build", "--platform", "linux/amd64"])
        self.assertEqual(result["command"][-1], str(context))
        self.assertEqual({p.name: p.read_bytes() for p in context.iterdir()}, before)
        self.assertFalse((context.parent / "image-build.json").exists())

    def test_image_command_rejects_modified_component_source_or_output_apk(self):
        for name in ("source", "apk"):
            with self.subTest(modified=name):
                workspace = self.root / ("modified-" + name)
                context = self.image_build(workspace)
                self.assertEqual(components.image_command(
                    workspace, "candidate", "buildploy/redroid:components-test", self.lock)[-1],
                    str(context))
                changed = self.component_source if name == "source" else context / "SystemUI.apk"
                original = changed.read_bytes()
                try:
                    changed.write_bytes(original + b"modified")
                    message = "Source or lock changed" if name == "source" else "SHA256 mismatch"
                    with self.assertRaisesRegex(ValueError, message):
                        components.image_command(workspace, "candidate",
                                                 "buildploy/redroid:components-test", self.lock)
                    self.assertEqual(changed.read_bytes(), original + b"modified")
                finally:
                    changed.write_bytes(original)

    def test_image_rejects_changed_apk_permissions(self):
        context = self.image_build(self.workspace)
        (context / "SystemUI.apk").chmod(0o600)
        with self.assertRaisesRegex(ValueError, "permissions must be 0644"):
            components.image_command(self.workspace, "candidate", "redroid:test", self.lock)

    def test_capacity_rejects_unknown_or_low_physical_storage_despite_large_virtual_disk(self):
        for physical, error, message in (
                (None, "Windows physical host storage is unknown", "host storage is unknown"),
                (components.GIB, None, "Insufficient measured workspace capacity")):
            with self.subTest(physical=physical, error=error):
                report = {"wsl_distribution": "Ubuntu-24.04", "storage": {
                    "filesystem_available_bytes": 567 * components.GIB,
                    "host_available_bytes": physical, "host_error": error}}
                with mock.patch.object(components, "inspect_environment", return_value=report):
                    with self.assertRaisesRegex(ValueError, message):
                        components.capacity(self.workspace)
                self.assertFalse(self.workspace.exists())

    def test_capacity_uses_the_smaller_measurement_and_accepts_exact_headroom(self):
        report = {"storage": {"filesystem_available_bytes": 567 * components.GIB,
                              "host_available_bytes": 2 * components.GIB, "host_error": None}}
        with mock.patch.object(components, "inspect_environment", return_value=report):
            self.assertIs(components.capacity(self.workspace), report)
        report["storage"].update(filesystem_available_bytes=components.GIB,
                                 host_available_bytes=567 * components.GIB)
        with mock.patch.object(components, "inspect_environment", return_value=report):
            with self.assertRaisesRegex(ValueError, "Insufficient measured workspace capacity"):
                components.capacity(self.workspace)

    def test_signing_identity_rejects_a_different_platform_certificate(self):
        cert = self.root / "platform.pem"
        cert.write_text("-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"test cert").decode()
                        + "\n-----END CERTIFICATE-----\n")
        lock = {"sourcePlatformCertSha256": hashlib.sha256(b"test cert").hexdigest()}
        components.signing_identity(cert, lock)
        with self.assertRaisesRegex(ValueError, "Signing certificate does not match"):
            components.signing_identity(cert, self.lock)

    def test_toolchain_rejects_jdk_patch_version_drift(self):
        sdk, jdk = self.root / "sdk", self.root / "jdk"
        sdk.mkdir()
        jdk.mkdir()
        android = sdk / "android.jar"
        android.write_bytes(b"pinned android sdk")
        lock = {"androidPlatform": {"androidJar": "android.jar", "androidJarSha256": components.sha(android)},
                "buildTools": {"files": {}}, "javaVersion": "17.0.17+10"}
        (jdk / "release").write_text('JAVA_RUNTIME_VERSION="17.0.17+10"\n')
        with mock.patch.object(components, "run", return_value="javac 17.0.17\n"):
            env, version = components.toolchain(sdk, jdk, lock)
            self.assertEqual(env["JAVA_HOME"], str(jdk))
            self.assertEqual(version, "javac 17.0.17")
            (jdk / "release").write_text('JAVA_RUNTIME_VERSION="17.0.17+9"\n')
            with self.assertRaisesRegex(ValueError, "Pinned JDK"):
                components.toolchain(sdk, jdk, lock)


if __name__ == "__main__":
    unittest.main()
