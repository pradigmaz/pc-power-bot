"""Hard-coded Windows power adapters."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger(__name__)

SLEEP_COMMAND = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.Application]::SetSuspendState("
    "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"
)
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32"
SHUTDOWN_EXE = SYSTEM32 / "shutdown.exe"
POWERSHELL_EXE = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"


class PowerController:
    def __init__(self, dry_run: bool = True, runner: Callable[..., object] | None = None):
        self.dry_run = dry_run
        self.runner = runner or subprocess.run

    def execute(self, action: str) -> bool:
        if action not in {"sleep", "shutdown"}:
            raise ValueError("Unsupported power action")
        if self.dry_run:
            LOGGER.info("Dry-run power action: %s", action)
            return False
        if action == "shutdown":
            command = [str(SHUTDOWN_EXE), "/s", "/t", "0"]
        else:
            command = [
                str(POWERSHELL_EXE),
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                SLEEP_COMMAND,
            ]
        try:
            result = self.runner(command, check=False, capture_output=True, text=True, shell=False)
        except OSError:
            LOGGER.warning("Power command could not start")
            return False
        if getattr(result, "returncode", 0) != 0:
            LOGGER.warning("Power command exited with an error")
            return False
        return True
