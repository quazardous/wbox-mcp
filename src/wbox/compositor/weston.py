"""
weston.py — Weston desktop compositor backend.

Weston runs as a nested Wayland client with window decorations from the host
compositor. The app is launched separately into the running weston instance.
Supports resizing and moving the window on the host desktop.

Screenshots use weston-screenshooter (requires --debug flag) to capture the
full compositor framebuffer including popups and menus.
"""

from __future__ import annotations

import glob as globmod
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import CompositorServer

log = logging.getLogger(__name__)


class WestonCompositor(CompositorServer):
    """Weston nested compositor — app launches separately after compositor starts."""

    compositor_name = "weston"

    def __init__(self, *, screen: str = "1280x800", shell: str = "kiosk",
                 backend: str = "wayland", instance_name: str = "",
                 timeouts: dict | None = None):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts)
        self.shell = shell
        self.backend = backend
        self._ini_path: Path | None = None
        self._last_app_cmd: list[str] = []
        self._last_app_env: dict[str, str] = {}

    def _write_ini(self) -> Path:
        shell_so = {
            "kiosk": "kiosk-shell.so",
            "desktop": "desktop-shell.so",
        }.get(self.shell, "kiosk-shell.so")

        lines = [
            "[core]",
            "xwayland=true",
            f"shell={shell_so}",
            "",
        ]

        if self.shell == "desktop":
            lines.extend([
                "[shell]",
                "panel-position=none",
                "",
            ])

        state_id = self.instance_name or self.compositor_name
        ini_path = Path(tempfile.gettempdir()) / f"wbox_{state_id}_weston.ini"
        ini_path.write_text("\n".join(lines) + "\n")
        log.info("Wrote weston config: %s", ini_path)
        return ini_path

    def _start_compositor(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
        wl_before: set[Path],
        x11_before: set[Path],
    ) -> None:
        for tool in ("weston", "xdotool"):
            if not shutil.which(tool):
                raise RuntimeError(f"'{tool}' not found in PATH")
        if not shutil.which("weston-screenshooter"):
            log.warning("weston-screenshooter not found — screenshots will not work")

        self._ini_path = self._write_ini()

        w, h = self.screen.split("x")
        weston_cmd = [
            "weston",
            "-B", self.backend,
            f"--config={self._ini_path}",
            f"--width={w}",
            f"--height={h}",
            "--debug",
        ]

        log.info("Launching weston: %s", " ".join(weston_cmd))

        self.state.compositor_proc = subprocess.Popen(
            weston_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        env["DISPLAY"] = self.state.x_display
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        env.update(app_env)

        log.info("Launching app in weston: %s", " ".join(app_cmd))

        self.state.app_proc = subprocess.Popen(
            app_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.state.app_pid = self.state.app_proc.pid

    def _find_host_window(self) -> str:
        if self.backend != "x11":
            return ""
        pid = self.state.compositor_pid or (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        if pid:
            # Search by PID to avoid matching other weston instances
            result = subprocess.run(
                ["xdotool", "search", "--pid", str(pid), "--name", "Weston"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        # Fallback to name-only search
        result = subprocess.run(
            ["xdotool", "search", "--name", "Weston"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
        return ""

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}

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

        app_cmd = self._last_app_cmd
        app_env = self._last_app_env

        if not app_cmd:
            return {"error": "no app command stored — cannot restart"}

        self.kill(aggressive=False)

        self.screen = f"{width}x{height}"
        return self.launch(app_cmd, app_env)

    def screenshot(self, name: str | None = None) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}

        self.state.screenshot_seq += 1
        if not name:
            name = f"{self.compositor_name}_{self.state.screenshot_seq:04d}.png"
        elif not name.endswith(".png"):
            name += ".png"

        out_path = self.state.screenshot_dir / name
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display

        tmpdir = str(self.state.screenshot_dir)
        before = set(globmod.glob(os.path.join(tmpdir, "wayland-screenshot-*.png")))

        result = subprocess.run(
            ["weston-screenshooter"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=tmpdir,
        )
        if result.returncode != 0:
            return {"error": f"weston-screenshooter failed: {result.stderr.strip()}"}

        after = set(globmod.glob(os.path.join(tmpdir, "wayland-screenshot-*.png")))
        new_files = after - before
        if not new_files:
            return {"error": "weston-screenshooter produced no output"}

        src = sorted(new_files)[0]
        Path(src).rename(out_path)

        return {"path": str(out_path), "size": out_path.stat().st_size}
