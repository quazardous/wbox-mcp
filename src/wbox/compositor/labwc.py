"""
labwc.py — labwc stacking compositor backend.

labwc is a wlroots-based stacking window manager (Openbox-inspired).
When running nested, the window is resizable and movable on the host desktop.
The app is launched separately into the running labwc instance.

Screenshots use grim (wlroots protocol).
Supports hybrid/wayland input backends (wlroots protocols).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import CompositorServer

log = logging.getLogger(__name__)


class LabwcCompositor(CompositorServer):
    """labwc nested compositor — resizable, movable, app launches separately."""

    compositor_name = "labwc"

    def __init__(self, *, screen: str = "1280x800", instance_name: str = "",
                 timeouts: dict | None = None, input_backend: str | dict = "x11"):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts, input_backend=input_backend)
        # labwc doesn't support deterministic socket naming — use diff-based detection
        self.wayland_socket_name = ""
        self._config_dir: Path | None = None
        self._last_app_cmd: list[str] = []
        self._last_app_env: dict[str, str] = {}
        self._log_file: Path | None = None

    def set_log_dir(self, log_dir: Path) -> None:
        """Set directory for labwc stderr log capture."""
        self._log_file = log_dir / "labwc-compositor.log"

    def _write_config(self) -> Path:
        """Write a minimal labwc rc.xml config."""
        state_id = self.instance_name or self.compositor_name
        config_dir = Path(tempfile.gettempdir()) / f"wbox_{state_id}_labwc"
        config_dir.mkdir(parents=True, exist_ok=True)

        rc_xml = """\
<?xml version="1.0"?>
<labwc_config>
  <core>
    <xwayland>yes</xwayland>
  </core>
</labwc_config>
"""
        (config_dir / "rc.xml").write_text(rc_xml)
        log.info("Wrote labwc config: %s", config_dir)
        self._config_dir = config_dir
        return config_dir

    def _start_compositor(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
        wl_before: set[Path],
        x11_before: set[Path],
    ) -> None:
        for tool in ("labwc", "grim", "xdotool"):
            if not shutil.which(tool):
                raise RuntimeError(f"'{tool}' not found in PATH")

        config_dir = self._write_config()

        w, h = self.screen.split("x")
        env = os.environ.copy()
        env["WLR_BACKENDS"] = "wayland"
        env["LABWC_CONFIG_DIR"] = str(config_dir)
        # wlroots: set initial window size
        env["WLR_WL_OUTPUTS"] = "1"

        labwc_cmd = [
            "labwc",
        ]

        log.info("Launching labwc: %s", " ".join(labwc_cmd))

        stderr_target: int | object
        if self._log_file:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            stderr_target = open(self._log_file, "w")
            log.info("labwc stderr → %s", self._log_file)
        else:
            stderr_target = subprocess.PIPE

        self.state.compositor_proc = subprocess.Popen(
            labwc_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_target,
        )

    def _start_app(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
    ) -> None:
        if not app_cmd:
            return

        self._last_app_cmd = list(app_cmd)
        self._last_app_env = dict(app_env)

        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        if self.state.x_display:
            env["DISPLAY"] = self.state.x_display
        env.update(app_env)

        log.info("Launching app in labwc: %s", " ".join(app_cmd))

        self.state.app_proc = subprocess.Popen(
            app_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.state.app_pid = self.state.app_proc.pid

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        # labwc nested window is on the host — query via host xdotool
        wid = self._find_host_window()
        if wid:
            result = subprocess.run(
                ["xdotool", "getwindowgeometry", "--shell", wid],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                info = {}
                for line in result.stdout.strip().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        info[k.strip()] = v.strip()
                return {
                    "width": int(info.get("WIDTH", 0)),
                    "height": int(info.get("HEIGHT", 0)),
                    "x": int(info.get("X", 0)),
                    "y": int(info.get("Y", 0)),
                }
        w, h = self.screen.split("x")
        return {"width": int(w), "height": int(h), "source": "configured"}

    def resize(self, width: int, height: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        # Try resizing the host window directly via xdotool
        wid = self._find_host_window()
        if wid:
            result = subprocess.run(
                ["xdotool", "windowsize", wid, str(width), str(height)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                self.screen = f"{width}x{height}"
                return self.get_size()
        # Fallback: restart
        app_cmd = self._last_app_cmd
        app_env = self._last_app_env
        if not app_cmd:
            return {"error": "no app command stored — cannot restart"}
        self.kill(aggressive=False)
        self.screen = f"{width}x{height}"
        return self.launch(app_cmd, app_env)

    def _find_host_window(self) -> str:
        """Find the labwc window on the host compositor (X11 host)."""
        pid = self.state.compositor_pid or (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        if pid:
            result = subprocess.run(
                ["xdotool", "search", "--pid", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        # Fallback: search by name
        result = subprocess.run(
            ["xdotool", "search", "--name", "labwc"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
        return ""
