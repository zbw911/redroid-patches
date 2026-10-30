import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from scripts import doctor


GIB = 1024 ** 3


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "not-created" / "source"
        meminfo = self.root / "meminfo"
        meminfo.write_text("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\n")
        self.patch(doctor, "MEMINFO_PATH", meminfo)
        self.patch(doctor.os, "cpu_count", mock.Mock(return_value=4))
        self.statvfs = self.patch(doctor.os, "statvfs", mock.Mock(
            return_value=types.SimpleNamespace(f_bavail=567 * GIB // 4096, f_frsize=4096)))
        self.patch(doctor.platform, "release", mock.Mock(return_value="6.6.87.2-microsoft-standard-WSL2"))
        self.patch(doctor.shutil, "which", mock.Mock(side_effect=lambda name: None if name == "powershell.exe" else "/tools/" + name))
        env = mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-24.04"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.powershell = self.root / "powershell.exe"
        self.powershell.write_text("#!/usr/bin/python3\nprint(" + repr(json.dumps(
            {"host_drive": "E:", "host_available_bytes": 37 * GIB})) + ")\n")
        self.powershell.chmod(0o755)

    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def inspect(self, workspace=None):
        return doctor.inspect_environment(workspace or self.workspace,
                                          powershell_executable=self.powershell)

    def test_virtual_capacity_is_not_physical_capacity_and_workspace_is_not_created(self):
        before = sorted(path.name for path in self.root.iterdir())
        result = self.inspect()
        self.assertEqual(result["storage"], {
            "filesystem_available_bytes": 567 * GIB, "filesystem_path": str(self.root),
            "host_drive": "E:", "host_available_bytes": 37 * GIB, "host_error": None})
        self.statvfs.assert_called_once_with(self.root)
        self.assertEqual(result["memory"], {"total_bytes": 8 * GIB, "available_bytes": 4 * GIB})
        self.assertEqual(result["cpu_count"], 4)
        self.assertEqual(result["wsl_distribution"], "Ubuntu-24.04")
        self.assertEqual(set(result["tools"]), {"git", "python3", "docker", "java", "javac", "adb"})
        self.assertEqual(result["issues"], [])
        self.assertNotIn("ready", result)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))

    def test_absolute_powershell_fallback_does_not_require_windows_path(self):
        self.patch(doctor, "POWERSHELL_PATHS", (self.powershell,))
        result = doctor.inspect_environment(self.workspace)
        self.assertEqual(result["storage"]["host_available_bytes"], 37 * GIB)

    def test_failed_queries_remain_unknown_without_disclosing_private_output(self):
        private = r"E:\private-user\Ubuntu\ext4.vhdx"
        outcomes = [
            subprocess.TimeoutExpired("powershell.exe", 10),
            subprocess.CompletedProcess([], 1, "", private),
            subprocess.CompletedProcess([], 0, "invalid " + private, ""),
            subprocess.CompletedProcess([], 0, json.dumps({"host_drive": "E:", "host_available_bytes": True}), ""),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                action = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(doctor.subprocess, "run", **action):
                    result = self.inspect()
                self.assertIsNone(result["storage"]["host_available_bytes"])
                self.assertIsNone(result["storage"]["host_drive"])
                self.assertIn("unknown", result["storage"]["host_error"])
                self.assertEqual(result["storage"]["filesystem_available_bytes"], 567 * GIB)
                self.assertNotIn(private, json.dumps(result))
                self.assertTrue(result["issues"])

    def test_mounted_drive_wins_over_distribution_storage(self):
        # Resolve an actual symlink without creating anything on the mounted drive.
        link = self.root / "drive-link"
        link.symlink_to("/mnt/d")
        storage = {"host_drive": "D:", "host_available_bytes": 5 * GIB}
        with mock.patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(storage), "")):
            result = self.inspect(link / "not-created")
        self.assertEqual(result["storage"]["host_drive"], "D:")
        self.assertEqual(result["storage"]["host_available_bytes"], 5 * GIB)
        self.assertFalse((link / "not-created").exists())
        with mock.patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({**storage, "host_drive": "E:"}), "")):
            mismatch = self.inspect(link / "not-created")
        self.assertEqual(mismatch["storage"]["host_drive"], "D:")
        self.assertIsNone(mismatch["storage"]["host_available_bytes"])
        self.assertIn("unknown", mismatch["storage"]["host_error"])

    def test_native_linux_never_queries_windows(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                doctor.platform, "release", return_value="6.8.0-generic"), mock.patch.object(
                doctor.subprocess, "run", side_effect=AssertionError("Unexpected Windows query")):
            result = self.inspect()
        self.assertIsNone(result["wsl_distribution"])
        self.assertIsNone(result["storage"]["host_drive"])
        self.assertIsNone(result["storage"]["host_available_bytes"])
        self.assertIsNone(result["storage"]["host_error"])

    def test_wsl_without_distribution_or_powershell_is_unknown(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                doctor.subprocess, "run", side_effect=AssertionError("No distribution selected")):
            result = self.inspect()
        self.assertIn("WSL_DISTRO_NAME", result["storage"]["host_error"])
        unavailable = doctor.inspect_environment(self.workspace, powershell_executable=self.root / "missing.exe")
        self.assertIsNone(unavailable["storage"]["host_available_bytes"])
        self.assertIn("powershell.exe", unavailable["storage"]["host_error"])

    def test_measurement_failures_are_reported_without_resource_policy(self):
        with mock.patch.object(doctor.os, "statvfs", side_effect=OSError("failed")), mock.patch.object(
                doctor, "MEMINFO_PATH", self.root / "missing-meminfo"), mock.patch.object(
                doctor.os, "cpu_count", return_value=None):
            result = self.inspect()
        self.assertIsNone(result["storage"]["filesystem_available_bytes"])
        self.assertEqual(result["memory"], {"total_bytes": None, "available_bytes": None})
        self.assertIsNone(result["cpu_count"])
        self.assertEqual(len(result["issues"]), 3)
        self.assertEqual(result["storage"]["host_available_bytes"], 37 * GIB)


if __name__ == "__main__":
    unittest.main()
