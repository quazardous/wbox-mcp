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
                 timeouts: dict | None = None, input_backend: str | dict = "x11",
                 undecorate: bool = True, keyboard_layout: str = ""):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts, input_backend=input_backend, undecorate=undecorate)
        self.keyboard_layout = keyboard_layout
        # labwc doesn't support deterministic socket naming — use diff-based detection
        self.wayland_socket_name = ""
        self._config_dir: Path | None = None
        self._last_app_cmd: list[str] = []
        self._last_app_env: dict[str, str] = {}
        self._log_file: Path | None = None
        self._clip_procs: list[subprocess.Popen] = []
        self._clip_guard: Path | None = None

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
        # Keyboard layout (XKB)
        if self.keyboard_layout:
            env["XKB_DEFAULT_LAYOUT"] = self.keyboard_layout

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

    def _post_compositor_start(self) -> None:
        """Resize the nested labwc window and start clipboard bridge."""
        self._apply_screen_size()
        self._start_clipboard_bridge()

    def _start_clipboard_bridge(self) -> None:
        """Start bidirectional clipboard sync between nested labwc and host."""
        host_wl = os.environ.get("WAYLAND_DISPLAY", "")
        nested_wl = self.state.wayland_display
        if not host_wl or not nested_wl:
            log.info("Clipboard bridge: skipped (no host WAYLAND_DISPLAY)")
            return
        if not shutil.which("wl-paste") or not shutil.which("wl-copy"):
            log.info("Clipboard bridge: skipped (wl-clipboard not found)")
            return

        guard = Path(tempfile.gettempdir()) / f"wbox_clip_guard_{os.getpid()}"
        self._clip_guard = guard

        # Shell script: read clipboard to temp file, check hash guard, sync to dest
        script = (
            'tmpf=$(mktemp); '
            'wl-paste --no-newline > "$tmpf" 2>/dev/null || { rm -f "$tmpf"; exit 0; }; '
            '[ -s "$tmpf" ] || { rm -f "$tmpf"; exit 0; }; '
            'hash=$(md5sum < "$tmpf" | cut -d" " -f1); '
            '[ -f "$_WBOX_GUARD" ] && [ "$(cat "$_WBOX_GUARD")" = "$hash" ] '
            '&& { rm -f "$tmpf"; exit 0; }; '
            'printf "%s" "$hash" > "$_WBOX_GUARD"; '
            'WAYLAND_DISPLAY="$_WBOX_DST" wl-copy < "$tmpf"; '
            'rm -f "$tmpf"'
        )

        def start_watcher(src_wl: str, dst_wl: str, name: str) -> subprocess.Popen:
            env = os.environ.copy()
            env["WAYLAND_DISPLAY"] = src_wl
            env["_WBOX_GUARD"] = str(guard)
            env["_WBOX_DST"] = dst_wl
            proc = subprocess.Popen(
                ["wl-paste", "--watch", "sh", "-c", script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("Clipboard bridge %s: pid=%d (%s → %s)", name, proc.pid, src_wl, dst_wl)
            return proc

        self._clip_procs.append(start_watcher(nested_wl, host_wl, "nested→host"))
        self._clip_procs.append(start_watcher(host_wl, nested_wl, "host→nested"))

    def _stop_clipboard_bridge(self) -> None:
        """Stop clipboard bridge processes."""
        for proc in self._clip_procs:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._clip_procs.clear()
        if self._clip_guard and self._clip_guard.exists():
            try:
                self._clip_guard.unlink()
            except OSError:
                pass


    def _apply_screen_size(self) -> None:
        """Set the labwc output resolution via wlr-randr."""
        if not shutil.which("wlr-randr"):
            log.warning("wlr-randr not found — cannot set screen size")
            return
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        result = subprocess.run(
            ["wlr-randr", "--output", "WL-1", "--custom-mode", self.screen],
            env=env, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log.info("Set labwc output to %s via wlr-randr", self.screen)
        else:
            log.warning("wlr-randr failed: %s", result.stderr.strip())

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        if shutil.which("wlr-randr"):
            env = os.environ.copy()
            env["WAYLAND_DISPLAY"] = self.state.wayland_display
            result = subprocess.run(
                ["wlr-randr"], env=env,
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                import re
                m = re.search(r"(\d+)x(\d+)\s+px\s+\(current\)", result.stdout)
                if m:
                    return {"width": int(m.group(1)), "height": int(m.group(2))}
        w, h = self.screen.split("x")
        return {"width": int(w), "height": int(h), "source": "configured"}

    def resize(self, width: int, height: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self.screen = f"{width}x{height}"
        self._apply_screen_size()
        return self.get_size()

    def stop(self) -> dict:
        self._stop_clipboard_bridge()
        return super().stop()

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
