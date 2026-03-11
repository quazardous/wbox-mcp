"""
base.py — Generic nested Wayland compositor management.

Manages a nested compositor (cage, weston, etc.) running an arbitrary program.
Provides screenshot capture (grim) and input injection (xdotool) via the
Xwayland display inside the compositor.

Subclass CompositorServer and override _start_compositor() / _start_app().
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@dataclass
class CompositorState:
    """Runtime state of a compositor session."""

    compositor_proc: subprocess.Popen | None = None
    compositor_pid: int = 0
    app_proc: subprocess.Popen | None = None
    app_pid: int = 0
    wayland_display: str = ""
    x_display: str = ""
    screenshot_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    screenshot_seq: int = 0

    def save(self, state_file: Path):
        data = {
            "compositor_pid": self.compositor_pid
            or (self.compositor_proc.pid if self.compositor_proc else 0),
            "app_pid": self.app_pid
            or (self.app_proc.pid if self.app_proc else 0),
            "wayland_display": self.wayland_display,
            "x_display": self.x_display,
            "screenshot_seq": self.screenshot_seq,
        }
        state_file.write_text(json.dumps(data))

    @classmethod
    def load(cls, state_file: Path) -> CompositorState | None:
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text())
            comp_pid = data.get("compositor_pid", 0)
            if comp_pid and _pid_alive(comp_pid):
                state = cls()
                state.compositor_pid = comp_pid
                state.app_pid = data.get("app_pid", 0)
                state.wayland_display = data.get("wayland_display", "")
                state.x_display = data.get("x_display", "")
                state.screenshot_seq = data.get("screenshot_seq", 0)
                return state
        except Exception:
            pass
        return None

    def clear(self, state_file: Path):
        if state_file.exists():
            state_file.unlink(missing_ok=True)


class CompositorServer:
    """
    Base class for a nested Wayland compositor session.

    Subclass and override:
      - _start_compositor(app_cmd, app_env)  : launch the compositor process
      - _start_app(app_cmd, app_env)         : launch app into running compositor
                                                (no-op if app starts with compositor)
      - compositor_name                      : str for state file naming
    """

    compositor_name: str = "compositor"

    def __init__(self, *, screen: str = "1280x800", instance_name: str = "",
                 timeouts: dict | None = None):
        self.screen = screen
        self.instance_name = instance_name
        self.timeouts = timeouts or {}
        # Use instance name for state file if available, else compositor name
        state_id = instance_name or self.compositor_name
        self._state_file = Path(tempfile.gettempdir()) / f"wbox_{state_id}_state.json"
        self.state = CompositorState.load(self._state_file) or CompositorState()

    def reload_state(self) -> None:
        """Reload state from disk (useful after /mcp reload when compositor is still running)."""
        loaded = CompositorState.load(self._state_file)
        if loaded:
            loaded.screenshot_dir = self.state.screenshot_dir
            self.state = loaded

    # ── Subclass hooks ──────────────────────────────────────────────

    def _start_compositor(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
        wl_before: set[Path],
        x11_before: set[Path],
    ) -> None:
        """Launch the compositor process. Must set self.state.compositor_proc."""
        raise NotImplementedError

    def _start_app(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
    ) -> None:
        """Launch the app into the running compositor.

        Override for compositors where app is launched separately (e.g. weston).
        No-op for compositors where app starts with compositor (e.g. cage).
        """

    # ── Lifecycle ───────────────────────────────────────────────────

    def launch(
        self,
        app_cmd: list[str],
        app_env: dict[str, str] | None = None,
    ) -> dict:
        """Start the compositor with the app inside."""
        if self.is_running():
            pid = self.state.compositor_pid or (
                self.state.compositor_proc.pid if self.state.compositor_proc else 0
            )
            return {
                "status": "already_running",
                "pid": pid,
                "wayland_display": self.state.wayland_display,
                "x_display": self.state.x_display,
            }

        app_env = app_env or {}

        self._clean_stale_sockets()

        # Snapshot existing displays before launch
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        wl_before = set(Path(runtime_dir).glob("wayland-[0-9]"))
        x11_dir = Path("/tmp/.X11-unix")
        x11_before = set(x11_dir.glob("X*")) if x11_dir.exists() else set()

        # Start compositor
        self._start_compositor(app_cmd, app_env, wl_before, x11_before)

        # Wait for compositor's Wayland display
        wl_timeout = self.timeouts.get("wayland_display", 10)
        wayland_display = self._wait_for_wayland_display(wl_before, timeout=wl_timeout)
        if not wayland_display:
            return {
                "error": f"{self.compositor_name} Wayland display did not appear in time (timeout={wl_timeout}s)",
                "pid": self.state.compositor_proc.pid if self.state.compositor_proc else 0,
            }
        self.state.wayland_display = wayland_display

        # Wait for Xwayland display
        xwl_timeout = self.timeouts.get("xwayland_display", 15)
        x_display = self._wait_for_xwayland(x11_before, timeout=xwl_timeout)
        if not x_display:
            return {
                "error": f"Xwayland display did not appear in time (timeout={xwl_timeout}s)",
                "pid": self.state.compositor_proc.pid if self.state.compositor_proc else 0,
            }
        self.state.x_display = x_display

        # Launch app into compositor (no-op for cage)
        self._start_app(app_cmd, app_env)

        # Wait for app to render
        render_wait = self.timeouts.get("app_render", 3)
        time.sleep(render_wait)

        self.state.compositor_pid = (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        self.state.save(self._state_file)

        return {
            "status": "running",
            "pid": self.state.compositor_pid,
            "wayland_display": self.state.wayland_display,
            "x_display": self.state.x_display,
        }

    def restart_app(
        self,
        app_cmd: list[str],
        app_env: dict[str, str] | None = None,
    ) -> dict:
        """Restart just the app inside the running compositor."""
        if not self.is_running():
            return {"error": "compositor is not running"}

        if self.state.app_pid and _pid_alive(self.state.app_pid):
            try:
                os.kill(self.state.app_pid, signal.SIGTERM)
                for _ in range(20):
                    if not _pid_alive(self.state.app_pid):
                        break
                    time.sleep(0.5)
            except ProcessLookupError:
                pass

        self.state.app_proc = None
        self.state.app_pid = 0

        self._start_app(app_cmd, app_env or {})
        render_wait = self.timeouts.get("app_render", 3)
        time.sleep(render_wait)

        self.state.save(self._state_file)
        return {
            "status": "app_restarted",
            "app_pid": self.state.app_pid,
        }

    def stop(self) -> dict:
        """Stop the compositor (and app with it)."""
        pid = self.state.compositor_pid or (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        if not pid:
            return {"status": "not_running"}

        try:
            os.kill(pid, signal.SIGTERM)
            if self.state.compositor_proc:
                self.state.compositor_proc.wait(timeout=10)
            else:
                for _ in range(20):
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.app_proc = None
        self.state.app_pid = 0
        self.state.wayland_display = ""
        self.state.x_display = ""
        self.state.clear(self._state_file)
        return {"status": "stopped", "pid": pid}

    def kill(self, aggressive: bool = True) -> dict:
        """Force-kill compositor by PID and clean state."""
        killed = []
        pid = self.state.compositor_pid or (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        if pid and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(f"{self.compositor_name}(pid={pid})")
            except ProcessLookupError:
                pass
        app_pid = self.state.app_pid
        if app_pid and app_pid != pid and _pid_alive(app_pid):
            try:
                os.kill(app_pid, signal.SIGKILL)
                killed.append(f"app(pid={app_pid})")
            except ProcessLookupError:
                pass

        if aggressive:
            result = subprocess.run(
                ["pgrep", "-x", self.compositor_name],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    orphan_pid = int(line.strip())
                    if orphan_pid != pid and _pid_alive(orphan_pid):
                        try:
                            os.kill(orphan_pid, signal.SIGKILL)
                            killed.append(f"orphan-{self.compositor_name}(pid={orphan_pid})")
                        except ProcessLookupError:
                            pass

        time.sleep(1)
        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.app_proc = None
        self.state.app_pid = 0
        self.state.wayland_display = ""
        self.state.x_display = ""
        self.state.clear(self._state_file)
        return {"status": "killed", "killed": killed}

    def is_running(self) -> bool:
        if self.state.compositor_proc is not None:
            return self.state.compositor_proc.poll() is None
        if self.state.compositor_pid:
            return _pid_alive(self.state.compositor_pid)
        return False

    # ── Window geometry ──────────────────────────────────────────────

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"error": "get_size not supported by this compositor backend"}

    def resize(self, width: int, height: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"error": "resize not supported by this compositor backend"}

    # ── Screenshot ──────────────────────────────────────────────────

    def screenshot(self, name: str | None = None) -> dict:
        """Capture the compositor display. Returns the image path."""
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

        result = subprocess.run(
            ["grim", str(out_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {"error": f"grim failed: {result.stderr.strip()}"}
        return {"path": str(out_path), "size": out_path.stat().st_size}

    # ── Input injection ─────────────────────────────────────────────

    def click(self, x: int, y: int, button: int = 1) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self._xdotool("mousemove", str(x), str(y))
        time.sleep(0.05)
        return self._xdotool("click", str(button))

    def type_text(self, text: str, delay_ms: int = 12) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self._focus_active_window()
        return self._xdotool("type", "--delay", str(delay_ms), "--", text)

    def key(self, shortcut: str) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self._focus_active_window()
        return self._xdotool("key", "--", shortcut)

    def mouse_move(self, x: int, y: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return self._xdotool("mousemove", str(x), str(y))

    # ── Clipboard ────────────────────────────────────────────────────

    def _clipboard_env(self) -> dict | None:
        """Build env dict for clipboard operations, or None if not available."""
        if not self.state.x_display:
            self.reload_state()
        if not self.state.x_display:
            return None
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        return env

    def clipboard_read(self) -> dict:
        """Read text from the X11 clipboard."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        env = self._clipboard_env()
        if not env:
            return {"error": "no x_display available"}

        import shutil
        # Try xsel first (doesn't block), then xclip
        if shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--output"]
        elif shutil.which("xclip"):
            cmd = ["xclip", "-selection", "clipboard", "-o"]
        else:
            return {"error": "no clipboard tool found — install xclip or xsel"}

        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {"error": f"clipboard read failed: {result.stderr.strip()}"}
        return {"text": result.stdout}

    def clipboard_write(self, text: str) -> dict:
        """Write text to the X11 clipboard."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        env = self._clipboard_env()
        if not env:
            return {"error": "no x_display available"}

        import shutil
        if shutil.which("xsel"):
            cmd = ["xsel", "--clipboard", "--input"]
            result = subprocess.run(cmd, input=text, env=env, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return {"error": f"xsel write failed: {result.stderr.strip()}"}
        elif shutil.which("xclip"):
            # xclip forks a daemon to own the clipboard — run it detached
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=env,
            )
            proc.stdin.write(text.encode())
            proc.stdin.close()
            # Don't wait — xclip stays alive as clipboard owner until
            # another process claims the clipboard. This is expected.
        else:
            return {"error": "no clipboard tool found — install xclip or xsel"}

        return {"ok": True, "length": len(text)}

    # ── Internal helpers ────────────────────────────────────────────

    def _focus_active_window(self) -> None:
        """Force X11 focus on the active window.

        xdotool --window uses XSendEvent which GTK/LO on Xwayland ignores.
        windowactivate + windowfocus sets real X11 input focus instead.
        """
        wid = self._get_active_window()
        if wid:
            self._xdotool("windowactivate", "--sync", wid)
            self._xdotool("windowfocus", "--sync", wid)
            time.sleep(0.05)

    def _get_active_window(self) -> str:
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        result = subprocess.run(
            ["xdotool", "getactivewindow"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _xdotool(self, *args: str) -> dict:
        if not self.state.x_display:
            self.reload_state()
        if not self.state.x_display:
            return {"error": "no x_display available — is compositor running?"}
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        cmd = ["xdotool", *args]
        log.debug("xdotool DISPLAY=%s cmd=%s", self.state.x_display, cmd)
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {"error": f"xdotool failed (DISPLAY={self.state.x_display}): {result.stderr.strip()}"}
        return {"ok": True}

    # ── Input debugging ────────────────────────────────────────────

    def debug_input(self, test_key: str = "a", target: str = "xev") -> dict:
        """Test keyboard input delivery."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        if not self.state.x_display:
            self.reload_state()
        if not self.state.x_display:
            return {"error": "no x_display available"}

        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display

        if target == "xev":
            return self._debug_input_xev(test_key, env)

        wid = self._get_active_window()
        wid_name = ""
        if wid:
            r = subprocess.run(
                ["xdotool", "getwindowname", wid],
                env=env, capture_output=True, text=True, timeout=5,
            )
            wid_name = r.stdout.strip() if r.returncode == 0 else ""

        if target == "active":
            self._focus_active_window()
            result = self._xdotool("key", "--", test_key)
            return {
                "test_key": test_key,
                "target": "active (focus method)",
                "window_id": wid,
                "window_name": wid_name,
                "result": result,
            }

        if target == "window":
            if wid:
                result = self._xdotool("key", "--window", wid, "--", test_key)
            else:
                result = self._xdotool("key", "--", test_key)
            return {
                "test_key": test_key,
                "target": "window (XSendEvent)",
                "window_id": wid,
                "window_name": wid_name,
                "result": result,
            }

        return {"error": f"unknown target: {target!r} (use 'xev', 'active', or 'window')"}

    def _debug_input_xev(self, test_key: str, env: dict) -> dict:
        logfile = Path("/tmp/compositor_xev.log")
        xev_proc = subprocess.Popen(
            ["xev", "-event", "keyboard"],
            stdout=open(logfile, "w"),
            stderr=subprocess.DEVNULL,
            env=env,
        )
        time.sleep(0.5)

        subprocess.run(
            ["xdotool", "key", "--", test_key],
            env=env, capture_output=True, timeout=5,
        )
        time.sleep(0.3)

        xev_proc.terminate()
        try:
            xev_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            xev_proc.kill()

        output = logfile.read_text() if logfile.exists() else "(no output)"
        has_keypress = "KeyPress" in output
        return {
            "test_key": test_key,
            "target": "xev (baseline)",
            "display": self.state.x_display,
            "key_received": has_keypress,
            "xev_output": output,
        }

    def _wait_for_wayland_display(
        self, before: set[Path], timeout: float = 10,
    ) -> str:
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state.compositor_proc and self.state.compositor_proc.poll() is not None:
                return ""
            current = set(runtime_dir.glob("wayland-[0-9]"))
            new = current - before
            if new:
                return sorted(new)[0].name
            time.sleep(0.3)
        return ""

    def _wait_for_xwayland(self, before: set[Path], timeout: float = 15) -> str:
        x11_dir = Path("/tmp/.X11-unix")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state.compositor_proc and self.state.compositor_proc.poll() is not None:
                log.error("%s exited early (code=%s)", self.compositor_name,
                          self.state.compositor_proc.returncode)
                return ""
            current = set(x11_dir.glob("X*")) if x11_dir.exists() else set()
            new = current - before
            if new:
                sock = sorted(new)[0]
                m = re.search(r"X(\d+)$", sock.name)
                if m:
                    return f":{m.group(1)}"
            time.sleep(0.3)
        return ""

    def _clean_stale_sockets(self):
        result = subprocess.run(
            ["pgrep", "-x", self.compositor_name],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            log.info("Skipping socket cleanup: %s process(es) still running", self.compositor_name)
            return

        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )
        host_display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        x11_dir = Path("/tmp/.X11-unix")

        for sock in runtime_dir.glob("wayland-[0-9]"):
            if sock.name == host_display:
                continue
            lock = sock.parent / f"{sock.name}.lock"
            try:
                sock.unlink(missing_ok=True)
                lock.unlink(missing_ok=True)
                log.info("Cleaned stale Wayland socket: %s", sock.name)
            except OSError:
                pass

        if x11_dir.exists():
            for sock in x11_dir.glob("X*"):
                num = re.search(r"X(\d+)$", sock.name)
                if num and int(num.group(1)) >= 2:
                    try:
                        sock.unlink(missing_ok=True)
                        log.info("Cleaned stale X11 socket: %s", sock.name)
                    except OSError:
                        pass
