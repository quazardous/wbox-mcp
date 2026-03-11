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

        self.state.compositor_proc = subprocess.Popen(
            cage_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
