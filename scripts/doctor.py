"""Read-only measurements for the component build workspace."""

import base64
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess


TOOLS = ("git", "python3", "docker", "java", "javac", "adb")
MEMINFO_PATH = Path("/proc/meminfo")
POWERSHELL_PATHS = (
    Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
)


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _memory(issues: list[str]) -> dict:
    memory = {"total_bytes": None, "available_bytes": None}
    try:
        for line in MEMINFO_PATH.read_text().splitlines():
            fields = line.split()
            if fields and fields[0] in ("MemTotal:", "MemAvailable:"):
                if len(fields) != 3 or fields[2] != "kB":
                    raise ValueError("Unexpected memory units")
                key = "total_bytes" if fields[0] == "MemTotal:" else "available_bytes"
                memory[key] = int(fields[1]) * 1024
    except (OSError, ValueError):
        issues.append("Linux memory measurements are unavailable")
        return memory
    if any(value is None for value in memory.values()):
        issues.append("Linux memory measurements are incomplete")
    return memory


def _powershell_path(explicit: str | Path | None) -> str | None:
    if explicit is not None:
        path = Path(explicit)
        return str(path) if path.is_absolute() and path.is_file() else None
    found = shutil.which("powershell.exe")
    if found:
        return found
    return next((str(path) for path in POWERSHELL_PATHS if path.is_file()), None)


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_storage(executable: str | None, distribution: str | None,
                     drive: str | None) -> tuple[str | None, int | None, str | None]:
    unknown = "Windows physical host storage is unknown: "
    if executable is None:
        return drive, None, unknown + "powershell.exe is unavailable"
    if drive is None and not distribution:
        return None, None, unknown + "WSL_DISTRO_NAME is unavailable"
    script = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
"""
    if drive is not None:
        script += "$drive = " + _ps_literal(drive) + "\n"
    else:
        script += "$distribution = " + _ps_literal(distribution) + "\n"
        script += r"""
$distros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' |
    ForEach-Object { Get-ItemProperty -LiteralPath $_.PSPath } |
    Where-Object { $_.DistributionName -eq $distribution })
if ($distros.Count -ne 1) { throw 'Selected WSL distribution mapping is unavailable' }
$base = [Environment]::ExpandEnvironmentVariables([string]$distros[0].BasePath)
$match = [regex]::Match($base, '^(?:\\\\\?\\)?([A-Za-z]):[\\/]')
if (-not $match.Success) { throw 'Selected WSL storage drive is unavailable' }
$drive = $match.Groups[1].Value.ToUpperInvariant() + ':'
"""
    script += r"""
$disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'")
if ($disks.Count -ne 1 -or $null -eq $disks[0].FreeSpace) {
    throw 'Windows drive free space is unavailable'
}
[pscustomobject]@{
    host_drive = $drive
    host_available_bytes = [long]$disks[0].FreeSpace
} | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except subprocess.TimeoutExpired:
        return drive, None, unknown + "PowerShell timed out"
    except OSError:
        return drive, None, unknown + "PowerShell could not be executed"
    if result.returncode:
        # PowerShell errors may contain the private registry BasePath; never expose them.
        return drive, None, unknown + "PowerShell query failed"
    try:
        data = json.loads(result.stdout.lstrip("\ufeff").strip())
        host_drive = data["host_drive"]
        available = data["host_available_bytes"]
        if not isinstance(host_drive, str) or not re.fullmatch(r"[A-Za-z]:", host_drive):
            raise ValueError("Invalid drive")
        if isinstance(available, bool) or not isinstance(available, int) or available < 0:
            raise ValueError("Invalid free space")
        host_drive = host_drive.upper()
        if drive is not None and host_drive != drive:
            raise ValueError("Unexpected workspace drive")
    except (ValueError, TypeError, KeyError):
        return drive, None, unknown + "PowerShell returned invalid storage data"
    return host_drive, available, None


def inspect_environment(workspace: Path, *,
                        powershell_executable: str | Path | None = None) -> dict:
    """Observe resources without creating the workspace or changing any services.

    The optional absolute PowerShell path supports WSL installations without a
    Windows PATH entry. Resource requirements belong to the operation's caller.
    """
    issues = []
    storage = {"filesystem_available_bytes": None, "filesystem_path": None,
               "host_drive": None, "host_available_bytes": None, "host_error": None}
    resolved = None
    try:
        resolved = Path(workspace).expanduser().resolve()
        existing = _existing_parent(resolved)
        storage["filesystem_path"] = str(existing)
        stats = os.statvfs(existing)
        storage["filesystem_available_bytes"] = stats.f_bavail * stats.f_frsize
    except (OSError, ValueError, AttributeError):
        issues.append("Workspace filesystem space is unavailable")
    distribution = os.environ.get("WSL_DISTRO_NAME") or None
    is_wsl = distribution is not None or "microsoft" in platform.release().lower()
    if is_wsl:
        match = re.match(r"^/mnt/([A-Za-z])(?:/|$)", str(resolved)) if resolved else None
        drive = match.group(1).upper() + ":" if match else None
        host_drive, available, error = _windows_storage(
            _powershell_path(powershell_executable), distribution, drive,
        )
        storage.update(host_drive=host_drive, host_available_bytes=available, host_error=error)
        if error:
            issues.append(error)
    cpu_count = os.cpu_count()
    if cpu_count is None:
        issues.append("CPU count is unavailable")
    return {"tools": {name: shutil.which(name) for name in TOOLS},
            "storage": storage, "memory": _memory(issues), "cpu_count": cpu_count,
            "wsl_distribution": distribution, "issues": issues}
