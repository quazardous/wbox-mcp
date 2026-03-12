"""
cage.py — Cage kiosk compositor backend.

Cage runs a single app fullscreen. The app command is passed directly to cage.
No window decorations, no resizing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .base import CompositorServer

log = logging.getLogger(__name__)


class CageCompositor(CompositorServer):
    """Cage kiosk compositor — app starts with the compositor."""

    compositor_name = "cage"

    def __init__(self, *, screen: str = "1280x800", instance_name: str = "",
                 timeouts: dict | None = None, input_backend: str | dict = "x11"):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts, input_backend=input_backend)
        self._log_file: Path | None = None

    def set_log_dir(self, log_dir: Path) -> None:
        """Set directory for cage stderr log capture."""
        self._log_file = log_dir / "cage-compositor.log"

    def _start_compositor(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
        wl_before: set[Path],
        x11_before: set[Path],
    ) -> None:
        for tool in ("cage", "grim", "xdotool"):
            if not shutil.which(tool):
                raise RuntimeError(f"'{tool}' not found in PATH")

        cage_cmd = ["cage", "--"]
        inner: list[str] = []
        if app_env:
            inner.append("env")
            for k, v in app_env.items():
                inner.append(f"{k}={v}")
        inner.extend(app_cmd)
        cage_cmd.extend(inner)

        log.info("Launching cage: %s", " ".join(cage_cmd))

        # Capture stderr to log file for debugging
        stderr_target: int | object
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            stderr_target = open(self._log_file, "w")
            log.info("Cage stderr → %s", self._log_file)
        else:
            stderr_target = subprocess.PIPE

        self.state.compositor_proc = subprocess.Popen(
            cage_cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
        )
