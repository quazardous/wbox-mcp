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
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # Check for zombie — kill -0 succeeds but process is defunct
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        if "\nState:\tZ" in status:
            return False
    except OSError:
        pass
    return True


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
    # Subclasses can set this to get deterministic wayland socket naming
    wayland_socket_name: str = ""

    def __init__(self, *, screen: str = "1280x800", instance_name: str = "",
                 timeouts: dict | None = None, input_backend: str | dict = "x11",
                 undecorate: bool = True, keyboard_layout: str = "",
                 headless: bool = False):
        from wbox.config import resolve_input_backend
        self.screen = screen
        self.instance_name = instance_name
        self.timeouts = timeouts or {}
        self.input_backends = resolve_input_backend(input_backend)
        self.undecorate = undecorate
        self.keyboard_layout = keyboard_layout
        self.headless = headless
        # Use instance name for state file if available, else compositor name
        state_id = instance_name or self.compositor_name
        self._state_file = Path(tempfile.gettempdir()) / f"wbox_{state_id}_state.json"
        self.state = CompositorState.load(self._state_file) or CompositorState()
        self._last_mouse_x: int = 0
        self._last_mouse_y: int = 0
        # Persistent wbox-pointer Wayland connection (lazy, see _vptr_client)
        self._vptr = None
        # Last app command/env, kept for restart (separate-app backends)
        self._last_app_cmd: list[str] = []
        self._last_app_env: dict[str, str] = {}

    def _compositor_env(self) -> dict[str, str]:
        """Environment for the compositor process.

        When keyboard_layout is set, force the XKB layout (and drop any host
        variant/options) so the nested seat keymap is deterministic — input
        injection and the app must agree on a keymap regardless of the host
        layout (e.g. AZERTY hosts).
        """
        env = os.environ.copy()
        if self.keyboard_layout:
            env["XKB_DEFAULT_LAYOUT"] = self.keyboard_layout
            env.pop("XKB_DEFAULT_VARIANT", None)
            env.pop("XKB_DEFAULT_OPTIONS", None)
        return env

    def _wlroots_env(self) -> dict[str, str]:
        """Compositor environment for wlroots backends (labwc, cage).

        In headless mode wlroots renders to an offscreen output: no window
        appears on the host desktop, while screenshots, pointer and keyboard
        injection keep working against the nested compositor.
        """
        env = self._compositor_env()
        if self.headless:
            env["WLR_BACKENDS"] = "headless"
            env["WLR_HEADLESS_OUTPUTS"] = "1"
        else:
            env["WLR_BACKENDS"] = "wayland"
        return env

    @staticmethod
    def _run_cmd(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        """subprocess.run wrapper: missing binaries and timeouts become a
        nonzero returncode instead of an exception escaping the MCP handler."""
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("timeout", 10)
        try:
            return subprocess.run(cmd, **kwargs)
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                cmd, 127, "", f"{cmd[0]} not found — install it")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                cmd, 124, "", f"{cmd[0]} timed out")

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

    def _post_compositor_start(self) -> None:
        """Hook called after compositor and Xwayland are ready, before app launch."""
        pass

    def _wlr_output_name(self, env: dict[str, str]) -> str:
        """First output name reported by wlr-randr.

        The name depends on the wlroots backend ("WL-1" nested on Wayland,
        "HEADLESS-1" offscreen), so it cannot be hardcoded.
        """
        result = self._run_cmd(["wlr-randr"], env=env, timeout=5)
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            # Output blocks start at column 0; their properties are indented
            if line and not line[0].isspace():
                return line.split()[0]
        return ""

    def _apply_screen_size(self) -> None:
        """Set the nested output resolution via wlr-randr (wlroots only).

        Some compositors ignore the requested size at startup (cage has no
        size option at all) — force the output mode to match self.screen so
        coordinates, screenshots, and get_size agree with the config.
        """
        if not shutil.which("wlr-randr"):
            log.warning("wlr-randr not found — cannot set screen size")
            return
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        output = self._wlr_output_name(env)
        if not output:
            log.warning("wlr-randr reported no output — cannot set screen size")
            return
        result = self._run_cmd(
            ["wlr-randr", "--output", output, "--custom-mode", self.screen],
            env=env, timeout=5,
        )
        if result.returncode == 0:
            log.info("Set %s output %s to %s via wlr-randr",
                     self.compositor_name, output, self.screen)
        else:
            log.warning("wlr-randr failed: %s", result.stderr.strip())

    def _post_app_start(self) -> None:
        """Hook called after app has rendered (after app_render wait)."""
        pass

    def send_post_launch_keys(self, keys: list[str], delay: float = 0.5) -> None:
        """Send key shortcuts after app launch (e.g. F11 for fullscreen).

        Args:
            keys: list of key shortcuts (e.g. ["F11", "ctrl+l"])
            delay: seconds to wait before first key and between each key
        """
        time.sleep(delay)
        for shortcut in keys:
            result = self.key(shortcut)
            if "error" in result:
                log.warning("post_launch_keys: %s failed: %s", shortcut, result["error"])
            else:
                log.info("post_launch_keys: sent %s", shortcut)
            time.sleep(delay)

    def _start_app(
        self,
        app_cmd: list[str],
        app_env: dict[str, str],
    ) -> None:
        """Launch the app into the running compositor.

        Override for compositors where app is launched separately (e.g. weston).
        No-op for compositors where app starts with compositor (e.g. cage).
        """

    def _spawn_app(self, app_cmd: list[str], app_env: dict[str, str]) -> None:
        """Shared _start_app body for separate-app backends (weston, labwc)."""
        if not app_cmd:
            return

        self._last_app_cmd = list(app_cmd)
        self._last_app_env = dict(app_env)

        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        if self.state.x_display:
            env["DISPLAY"] = self.state.x_display
        env.update(app_env)

        log.info("Launching app in %s: %s", self.compositor_name, " ".join(app_cmd))

        self.state.app_proc = subprocess.Popen(
            app_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.state.app_pid = self.state.app_proc.pid

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

        # Snapshot existing X11 displays before launch (can't control X display
        # number). Only LIVE sockets: a stale file left by a dead compositor
        # gets reused by the new one and a raw diff would never see it appear.
        x11_dir = Path("/tmp/.X11-unix")
        x11_before = self._live_sockets(x11_dir, "X*")

        # For wayland: snapshot only needed if no deterministic socket name
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        wl_before = set() if self.wayland_socket_name else self._live_sockets(
            Path(runtime_dir), "wayland-*")

        # Start compositor
        self._start_compositor(app_cmd, app_env, wl_before, x11_before)

        # Wait for compositor's Wayland display
        wl_timeout = self.timeouts.get("wayland_display", 10)
        if self.wayland_socket_name:
            wayland_display = self._wait_for_named_socket(
                Path(runtime_dir) / self.wayland_socket_name, timeout=wl_timeout)
        else:
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
        self._force_xwayland_layout()

        # Hook for post-compositor setup (e.g. resize nested window)
        self._post_compositor_start()

        # Launch app into compositor (no-op for cage)
        self._start_app(app_cmd, app_env)

        # Wait for app to render
        render_wait = self.timeouts.get("app_render", 3)
        time.sleep(render_wait)

        # Hook for post-app setup (e.g. fullscreen)
        self._post_app_start()

        # Remove window decorations on X11 windows (labwc SSD)
        if self.undecorate:
            self._undecorate_x11_windows()

        self.state.compositor_pid = (
            self.state.compositor_proc.pid if self.state.compositor_proc else 0
        )
        self.state.save(self._state_file)

        # Events sent to a freshly created device are swallowed while the
        # compositor still propagates it to clients — create the virtual
        # pointer and keyboard now, so the first real click or keystroke
        # isn't the one that gets lost.
        if "wbox-pointer" in self.input_backends.values() or \
                "wbox-keyboard" in self.input_backends.values():
            self._vptr_op("warm_up")

        # Likewise the first pointer event after launch lands at an arbitrary
        # position: the nested compositor has no pointer position established
        # yet, so the warp is swallowed and the button press is delivered
        # wherever the cursor happened to be. Aim at the center: it is inside
        # the app surface in every app mode, so the app also gets its
        # pointer-enter before the first real click.
        try:
            w, h = (int(v) for v in self.screen.split("x"))
            self.mouse_move(w // 2, h // 2)
        except ValueError:
            self.mouse_move(0, 0)

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

        timeout = self.timeouts.get("stop", 10)
        force_killed = False

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already dead
        else:
            # Wait up to timeout for graceful shutdown
            if self.state.compositor_proc:
                try:
                    self.state.compositor_proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
            else:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if not _pid_alive(pid):
                        break
                    time.sleep(0.5)

            # Escalate to SIGKILL if still alive
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                    force_killed = True
                except ProcessLookupError:
                    pass

        self._teardown()
        self._clean_stale_sockets()
        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.app_proc = None
        self.state.app_pid = 0
        self.state.wayland_display = ""
        self.state.x_display = ""
        self.state.clear(self._state_file)
        status = "force_killed" if force_killed else "stopped"
        return {"status": status, "pid": pid}

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
            # Only kill orphan compositors that belong to this instance
            # (matched by state file PID), not other wbox instances
            stale_pid = self.state.compositor_pid
            if stale_pid and stale_pid != pid and _pid_alive(stale_pid):
                try:
                    os.kill(stale_pid, signal.SIGKILL)
                    killed.append(f"orphan-{self.compositor_name}(pid={stale_pid})")
                except ProcessLookupError:
                    pass

        # Reap our own children so SIGKILLed processes don't linger as zombies,
        # then wait (briefly) for externally-tracked pids to vanish
        for proc in (self.state.compositor_proc, self.state.app_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._poll_until(
            lambda: not any(_pid_alive(p) for p in (pid, app_pid) if p),
            timeout=1)
        self._teardown()
        self._clean_stale_sockets()
        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.app_proc = None
        self.state.app_pid = 0
        self.state.wayland_display = ""
        self.state.x_display = ""
        self.state.clear(self._state_file)
        return {"status": "killed", "killed": killed}

    def _teardown(self) -> None:
        """Extra cleanup shared by stop() and kill(). Subclasses extend this."""
        self._vptr_close()

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

    def _next_screenshot_path(self, name: str | None) -> Path:
        """Allocate the output path for a screenshot (shared naming scheme)."""
        self.state.screenshot_seq += 1
        if not name:
            name = f"{self.compositor_name}_{self.state.screenshot_seq:04d}.png"
        elif not name.endswith(".png"):
            name += ".png"
        return self.state.screenshot_dir / name

    def screenshot(self, name: str | None = None, scale: float | None = None,
                   region: str | None = None) -> dict:
        """Capture the compositor display. Returns the image path.

        scale: grim -s factor (e.g. 0.5 halves the image, cheaper for the model)
        region: grim -g geometry "x,y WxH" to capture a sub-rectangle
        """
        if not self.is_running():
            return {"error": "compositor is not running"}

        out_path = self._next_screenshot_path(name)
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display

        cmd = ["grim"]
        if scale:
            cmd += ["-s", str(scale)]
        if region:
            cmd += ["-g", region]
        cmd.append(str(out_path))

        result = self._run_cmd(cmd, env=env)
        if result.returncode != 0:
            return {"error": f"grim failed: {result.stderr.strip()}"}
        return {"path": str(out_path), "size": out_path.stat().st_size}

    # ── Input injection ─────────────────────────────────────────────

    def click(self, x: int, y: int, button: int = 1) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self._last_mouse_x, self._last_mouse_y = x, y
        if self.input_backends["mouse"] == "wbox-pointer":
            return self._vptr_click(x, y, button)
        if self.input_backends["mouse"] == "ydotool":
            return self._wl_click(x, y, button)
        # Chain mousemove + focus into one xdotool spawn. The click stays a
        # separate spawn: with a window on xdotool's stack, `click` would use
        # XSendEvent, which GTK/LO ignore — bare `click` uses XTEST.
        self._xdotool("mousemove", str(x), str(y), "getactivewindow",
                      "windowactivate", "--sync", "windowfocus", "--sync")
        return self._xdotool("click", str(button))

    def type_text(self, text: str, delay_ms: int = 12) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        if self.input_backends["keyboard"] == "wbox-keyboard":
            return self._vptr_op("type_text", text, delay_ms)
        if self.input_backends["keyboard"] == "wtype":
            return self._wl_type(text, delay_ms)
        self._focus_active_window()
        return self._xdotool("type", "--delay", str(delay_ms), "--", text)

    def key(self, shortcut: str) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        if self.input_backends["keyboard"] == "wbox-keyboard":
            return self._vptr_op("key_combo", shortcut)
        if self.input_backends["keyboard"] == "wtype":
            return self._wl_key(shortcut)
        self._focus_active_window()
        return self._xdotool("key", "--", shortcut)

    def keys(self, shortcuts: list[str], delay_ms: int = 100) -> dict:
        """Send a sequence of shortcuts, batched into one process when possible."""
        if not self.is_running():
            return {"error": "compositor is not running", "sent": 0}
        if not shortcuts:
            return {"ok": True, "sent": 0}
        delay_ms = max(delay_ms, 0)
        if self.input_backends["keyboard"] == "wbox-keyboard":
            result = self._vptr_op("key_combos", shortcuts, delay_ms)
            if "error" in result:
                return {"error": result["error"], "sent": 0}
            return {"ok": True, "sent": len(shortcuts)}
        if self.input_backends["keyboard"] == "wtype":
            return self._wl_keys(shortcuts, delay_ms)
        # x11: focus once, then one xdotool spawn for the whole sequence
        self._focus_active_window()
        total_delay = delay_ms * len(shortcuts) / 1000.0
        result = self._xdotool("key", "--delay", str(delay_ms), "--", *shortcuts,
                               timeout=10 + total_delay)
        if "error" in result:
            return {"error": result["error"], "sent": 0}
        return {"ok": True, "sent": len(shortcuts)}

    def mouse_move(self, x: int, y: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        self._last_mouse_x, self._last_mouse_y = x, y
        if self.input_backends["mouse"] == "wbox-pointer":
            return self._vptr_move(x, y)
        if self.input_backends["mouse"] == "ydotool":
            return self._wl_mouse_move(x, y)
        return self._xdotool("mousemove", str(x), str(y))

    def get_mouse_position(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        # Return last known position tracked by click/mouse_move.
        # xdotool getmouselocation is unreliable on Xwayland.
        return {"ok": True, "x": self._last_mouse_x, "y": self._last_mouse_y}

    # ── Clipboard ────────────────────────────────────────────────────

    def _clipboard_env(self) -> dict | None:
        """Build env dict for clipboard operations, or None if not available."""
        env = os.environ.copy()
        if self.input_backends["clipboard"] == "wayland":
            if not self.state.wayland_display:
                self.reload_state()
            if not self.state.wayland_display:
                return None
            env["WAYLAND_DISPLAY"] = self.state.wayland_display
            env["XDG_RUNTIME_DIR"] = os.environ.get(
                "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
            )
        else:
            if not self.state.x_display:
                self.reload_state()
            if not self.state.x_display:
                return None
            env["DISPLAY"] = self.state.x_display
        return env

    def clipboard_read(self) -> dict:
        """Read text from the clipboard."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        env = self._clipboard_env()
        if not env:
            return {"error": "no display available"}

        if self.input_backends["clipboard"] == "wayland":
            if shutil.which("wl-paste"):
                cmd = ["wl-paste", "--no-newline"]
            else:
                return {"error": "wl-paste not found — install wl-clipboard"}
        else:
            if shutil.which("xsel"):
                cmd = ["xsel", "--clipboard", "--output"]
            elif shutil.which("xclip"):
                cmd = ["xclip", "-selection", "clipboard", "-o"]
            else:
                return {"error": "no clipboard tool found — install xclip or xsel"}

        result = self._run_cmd(cmd, env=env, timeout=5)
        if result.returncode != 0:
            return {"error": f"clipboard read failed: {result.stderr.strip()}"}
        return {"text": result.stdout}

    def clipboard_write(self, text: str) -> dict:
        """Write text to the clipboard."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        env = self._clipboard_env()
        if not env:
            return {"error": "no display available"}

        if self.input_backends["clipboard"] == "wayland":
            if shutil.which("wl-copy"):
                cmd = ["wl-copy", "--paste-once", "--"]
                result = self._run_cmd(cmd + [text], env=env, timeout=5)
                if result.returncode != 0:
                    return {"error": f"wl-copy failed: {result.stderr.strip()}"}
            else:
                return {"error": "wl-copy not found — install wl-clipboard"}
        else:
            if shutil.which("xsel"):
                cmd = ["xsel", "--clipboard", "--input"]
                result = self._run_cmd(cmd, input=text, env=env, timeout=5)
                if result.returncode != 0:
                    return {"error": f"xsel write failed: {result.stderr.strip()}"}
            elif shutil.which("xclip"):
                proc = subprocess.Popen(
                    ["xclip", "-selection", "clipboard"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    env=env,
                )
                proc.stdin.write(text.encode())
                proc.stdin.close()
            else:
                return {"error": "no clipboard tool found — install xclip or xsel"}

        return {"ok": True, "length": len(text)}

    # ── Window management ──────────────────────────────────────────

    def list_windows(self) -> dict:
        """List windows/toplevels in the compositor via wlrctl."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        if not shutil.which("wlrctl"):
            return {"error": "wlrctl not found — install wlrctl"}
        env = self._wl_env()
        result = self._run_cmd(["wlrctl", "toplevel", "list"], env=env, timeout=5)
        if result.returncode != 0:
            return {"error": f"wlrctl failed: {result.stderr.strip()}"}
        windows = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "app_id: title"
            if ": " in line:
                app_id, title = line.split(": ", 1)
                windows.append({"app_id": app_id, "title": title})
            else:
                windows.append({"app_id": "", "title": line})
        return {"windows": windows}

    def focus_window(self, title: str = "", app_id: str = "") -> dict:
        """Focus/raise a window by title or app_id via wlrctl."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        if not shutil.which("wlrctl"):
            return {"error": "wlrctl not found — install wlrctl"}
        env = self._wl_env()
        cmd = ["wlrctl", "toplevel", "focus"]
        if app_id:
            cmd.extend(["app_id:" + app_id])
        elif title:
            cmd.extend(["title:" + title])
        else:
            return {"error": "provide title or app_id"}
        result = self._run_cmd(cmd, env=env, timeout=5)
        if result.returncode != 0:
            return {"error": f"wlrctl focus failed: {result.stderr.strip()}"}
        return {"ok": True}

    # ── Internal helpers ────────────────────────────────────────────

    def _undecorate_x11_windows(self) -> None:
        """Remove SSD from all X11 windows via _MOTIF_WM_HINTS.

        labwc serverDecoration="no" in rc.xml doesn't always work for
        Xwayland windows. Setting _MOTIF_WM_HINTS with decorations=0
        tells the WM to remove its server-side decorations.
        """
        if not self.state.x_display:
            return
        if not shutil.which("xprop"):
            return
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        # List all X11 windows
        result = self._run_cmd(
            ["xdotool", "search", "--onlyvisible", "--name", ""],
            env=env, timeout=5,
        )
        if result.returncode != 0:
            return
        w, h = self.screen.split("x")
        for wid in result.stdout.strip().splitlines():
            wid = wid.strip()
            if not wid:
                continue
            # _MOTIF_WM_HINTS: flags=2 (decorations), decorations=0
            self._run_cmd(
                ["xprop", "-id", wid, "-f", "_MOTIF_WM_HINTS", "32c",
                 "-set", "_MOTIF_WM_HINTS", "2, 0, 0, 0, 0"],
                env=env, timeout=5, text=False,
            )
            # labwc drops the SSD but leaves the window at its decorated
            # placement (titlebar offset, shrunken size) — snap it back to
            # the origin at full screen size. Only touch managed toplevels
            # (WM_STATE set): the search above also yields app-internal
            # subwindows (tk widgets), and resizing those wrecks the app
            # layout. cage sets no WM_STATE and needs no snapping (kiosk).
            state = self._run_cmd(["xprop", "-id", wid, "WM_STATE"],
                                  env=env, timeout=5)
            if "window state:" not in state.stdout:
                continue
            self._run_cmd(
                ["xdotool", "windowmove", wid, "0", "0",
                 "windowsize", wid, w, h],
                env=env, timeout=5,
            )
        log.info("Undecorated X11 windows on %s", self.state.x_display)

    def _force_xwayland_layout(self) -> None:
        """Align the nested Xwayland core keymap with keyboard_layout.

        Xwayland starts with the system default layout and only adopts the
        compositor's seat keymap when an X client gains keyboard focus — which
        never happens when the app is a Wayland client. xdotool computes
        keycodes from the Xwayland keymap while the compositor delivers them
        through the seat keymap, so a mismatch garbles typed text. Best-effort.
        """
        if not (self.keyboard_layout and self.state.x_display):
            return
        if not shutil.which("setxkbmap"):
            log.warning("setxkbmap not found — cannot pin Xwayland layout %r",
                        self.keyboard_layout)
            return
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        result = self._run_cmd(
            ["setxkbmap", "-display", self.state.x_display, self.keyboard_layout],
            env=env, timeout=5,
        )
        if result.returncode != 0:
            log.warning("setxkbmap %s failed on %s: %s", self.keyboard_layout,
                        self.state.x_display, result.stderr.strip())

    def _focus_active_window(self) -> None:
        """Force X11 focus on the active window.

        xdotool --window uses XSendEvent which GTK/LO on Xwayland ignores.
        windowactivate + windowfocus sets real X11 input focus instead.
        """
        # One chained spawn: getactivewindow pushes the window onto xdotool's
        # stack, windowactivate/windowfocus consume it. Best-effort: the chain
        # fails harmlessly when there is no active window (same as before).
        self._xdotool("getactivewindow",
                      "windowactivate", "--sync", "windowfocus", "--sync")

    def _get_active_window(self) -> str:
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        result = self._run_cmd(["xdotool", "getactivewindow"], env=env, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _xdotool(self, *args: str, timeout: float = 10) -> dict:
        if not self.state.x_display:
            self.reload_state()
        if not self.state.x_display:
            return {"error": "no x_display available — is compositor running?"}
        env = os.environ.copy()
        env["DISPLAY"] = self.state.x_display
        cmd = ["xdotool", *args]
        log.debug("xdotool DISPLAY=%s cmd=%s", self.state.x_display, cmd)
        result = self._run_cmd(cmd, env=env, timeout=timeout)
        if result.returncode != 0:
            return {"error": f"xdotool failed (DISPLAY={self.state.x_display}): {result.stderr.strip()}"}
        return {"ok": True, "stdout": result.stdout.strip()}

    # ── Wayland input helpers ─────────────────────────────────────

    def _wl_env(self) -> dict:
        env = os.environ.copy()
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        return env

    # wtype uses -M for modifier down, -m for modifier up, -k for key
    _WTYPE_MOD_MAP = {"super": "logo", "Super_L": "logo", "Super_R": "logo"}

    @classmethod
    def _wtype_combo_args(cls, shortcut: str) -> list[str]:
        """Convert xdotool "ctrl+shift+a" → -M ctrl -M shift -k a -m shift -m ctrl."""
        parts = shortcut.split("+")
        key = parts[-1]
        modifiers = [cls._WTYPE_MOD_MAP.get(m, m) for m in parts[:-1]]
        args = []
        for m in modifiers:
            args.extend(["-M", m])
        args.extend(["-k", key])
        for m in reversed(modifiers):
            args.extend(["-m", m])
        return args

    def _wl_key(self, shortcut: str) -> dict:
        """Send key via wtype. Translates xdotool-style shortcuts to wtype format."""
        if not self.state.wayland_display:
            self.reload_state()
        if not self.state.wayland_display:
            return {"error": "no wayland_display available"}
        env = self._wl_env()
        cmd = ["wtype"] + self._wtype_combo_args(shortcut)
        log.debug("wtype cmd=%s", cmd)
        result = self._run_cmd(cmd, env=env)
        if result.returncode != 0:
            return {"error": f"wtype failed: {result.stderr.strip()}"}
        return {"ok": True}

    def _wl_keys(self, shortcuts: list[str], delay_ms: int = 100) -> dict:
        """Send a shortcut sequence in one wtype spawn (-s = sleep between combos)."""
        if not self.state.wayland_display:
            self.reload_state()
        if not self.state.wayland_display:
            return {"error": "no wayland_display available", "sent": 0}
        env = self._wl_env()
        cmd = ["wtype"]
        for i, shortcut in enumerate(shortcuts):
            if i and delay_ms > 0:
                cmd.extend(["-s", str(delay_ms)])
            cmd.extend(self._wtype_combo_args(shortcut))
        log.debug("wtype cmd=%s", cmd)
        total_delay = delay_ms * len(shortcuts) / 1000.0
        result = self._run_cmd(cmd, env=env, timeout=10 + total_delay)
        if result.returncode != 0:
            return {"error": f"wtype failed: {result.stderr.strip()}", "sent": 0}
        return {"ok": True, "sent": len(shortcuts)}

    def _wl_type(self, text: str, delay_ms: int = 12) -> dict:
        """Type text via wtype."""
        if not self.state.wayland_display:
            self.reload_state()
        if not self.state.wayland_display:
            return {"error": "no wayland_display available"}
        env = self._wl_env()
        cmd = ["wtype", "-d", str(delay_ms), "--", text]
        result = self._run_cmd(cmd, env=env, timeout=30)
        if result.returncode != 0:
            return {"error": f"wtype failed: {result.stderr.strip()}"}
        return {"ok": True}

    def _wl_click(self, x: int, y: int, button: int = 1) -> dict:
        """Click via ydotool (works on Wayland via /dev/uinput)."""
        self._wl_mouse_move(x, y)
        time.sleep(0.05)
        # ydotool button codes: 0x00=left, 0x01=right, 0x02=middle
        btn_map = {1: "0x00", 2: "0x02", 3: "0x01"}
        btn_code = btn_map.get(button, "0x00")
        cmd = ["ydotool", "click", btn_code]
        log.debug("ydotool click cmd=%s", cmd)
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            return {"error": f"ydotool click failed: {result.stderr.strip()}"}
        return {"ok": True}

    def _wl_mouse_move(self, x: int, y: int) -> dict:
        """Move mouse via ydotool."""
        cmd = ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)]
        log.debug("ydotool mousemove cmd=%s", cmd)
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            return {"error": f"ydotool mousemove failed: {result.stderr.strip()}"}
        return {"ok": True}

    # ── wbox-pointer (Wayland virtual pointer) ─────────────────────

    def _vptr_client(self):
        """Get (or lazily create) the persistent virtual-input connection."""
        if self._vptr is None:
            from wbox.pointer import WaylandClient
            w, h = self.screen.split("x")
            # fallback only: the real output size (wl_output.mode) is
            # authoritative — compositors may not honor the configured size
            wl = WaylandClient(fallback_size=(int(w), int(h)))
            wl.setup(self.state.wayland_display)
            self._vptr = wl
        return self._vptr

    def _vptr_close(self) -> None:
        if self._vptr is not None:
            try:
                self._vptr.disconnect()
            except OSError:
                pass
            self._vptr = None

    def _vptr_op(self, op: str, *args) -> dict:
        """Run a wbox-pointer operation, reconnecting once on a dead connection."""
        if not self.state.wayland_display:
            self.reload_state()
        if not self.state.wayland_display:
            return {"error": "no wayland_display available"}
        for attempt in (1, 2):
            try:
                getattr(self._vptr_client(), op)(*args)
                return {"ok": True}
            except (OSError, EOFError, RuntimeError, ValueError) as exc:
                self._vptr_close()
                if attempt == 2:
                    return {"error": f"wbox-pointer {op} failed: {exc}"}
        return {"error": f"wbox-pointer {op} failed"}

    def _vptr_move(self, x: int, y: int) -> dict:
        """Move mouse via wbox-pointer (Wayland virtual pointer)."""
        return self._vptr_op("move", x, y)

    def _vptr_click(self, x: int, y: int, button: int = 1) -> dict:
        """Click via wbox-pointer (Wayland virtual pointer)."""
        return self._vptr_op("click", x, y, button)

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
            r = self._run_cmd(["xdotool", "getwindowname", wid], env=env, timeout=5)
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
        state_id = self.instance_name or self.compositor_name
        logfile = Path(tempfile.gettempdir()) / f"wbox_{state_id}_xev.log"
        with open(logfile, "w") as xev_out:
            xev_proc = subprocess.Popen(
                ["xev", "-event", "keyboard"],
                stdout=xev_out,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        time.sleep(0.5)

        self._run_cmd(["xdotool", "key", "--", test_key], env=env, timeout=5)
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

    def _poll_until(self, check, timeout: float, fail_fast=None):
        """Poll check() until truthy, with 20ms→200ms backoff on a monotonic clock.

        Returns check()'s first truthy result, or None on timeout / when
        fail_fast() turns truthy (e.g. the compositor died).
        """
        deadline = time.monotonic() + timeout
        delay = 0.02
        while time.monotonic() < deadline:
            if fail_fast is not None and fail_fast():
                return None
            result = check()
            if result:
                return result
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        return None

    def _compositor_died(self) -> bool:
        proc = self.state.compositor_proc
        return proc is not None and proc.poll() is not None

    def _wait_for_named_socket(self, sock_path: Path, timeout: float = 10) -> str:
        """Wait for a specific socket file to appear."""
        return self._poll_until(
            lambda: sock_path.name if sock_path.exists() else "",
            timeout, fail_fast=self._compositor_died) or ""

    def _wait_for_wayland_display(
        self, before: set[Path], timeout: float = 10,
    ) -> str:
        """Fallback: wait for a new wayland-N socket to appear."""
        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )

        def check():
            new = self._live_sockets(runtime_dir, "wayland-*") - before
            return sorted(new)[0].name if new else ""

        return self._poll_until(check, timeout, fail_fast=self._compositor_died) or ""

    def _wait_for_xwayland(self, before: set[Path], timeout: float = 15) -> str:
        x11_dir = Path("/tmp/.X11-unix")

        def check():
            new = self._live_sockets(x11_dir, "X*") - before
            if new:
                m = re.search(r"X(\d+)$", sorted(new)[0].name)
                if m:
                    return f":{m.group(1)}"
            return ""

        result = self._poll_until(check, timeout, fail_fast=self._compositor_died)
        if result is None and self._compositor_died():
            log.error("%s exited early (code=%s)", self.compositor_name,
                      self.state.compositor_proc.returncode)
        return result or ""

    @classmethod
    def _live_sockets(cls, directory: Path, pattern: str) -> set[Path]:
        """Sockets under directory matching pattern that accept connections
        (skips stale socket files and .lock files)."""
        if not directory.exists():
            return set()
        return {p for p in directory.glob(pattern) if cls._socket_alive(p)}

    @staticmethod
    def _socket_alive(path: Path) -> bool:
        """True if the Unix socket at path still accepts connections."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _clean_stale_sockets(self):
        """Clean sockets/locks that belonged to this instance.

        Uses state file (x_display, wayland_display) and deterministic
        wayland_socket_name. Never touches sockets from other instances.
        """
        wl_display = self.state.wayland_display
        x_display = self.state.x_display

        runtime_dir = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        )

        # Clean wayland socket: both from state and deterministic name
        wl_names = set()
        if wl_display:
            wl_names.add(wl_display)
        if self.wayland_socket_name:
            wl_names.add(self.wayland_socket_name)

        for wl_name in wl_names:
            sock = runtime_dir / wl_name
            lock = runtime_dir / f"{wl_name}.lock"
            # Safety: never remove a socket whose owner still accepts
            # connections (e.g. another server process for the same instance)
            if sock.exists() and self._socket_alive(sock):
                log.debug("Wayland socket %s is live — skipping clean", wl_name)
                continue
            removed = False
            for f in (sock, lock):
                if f.exists():
                    try:
                        f.unlink(missing_ok=True)
                        removed = True
                    except OSError:
                        pass
            if removed:
                log.info("Cleaned stale Wayland socket: %s", wl_name)

        # Clean X11 socket + lock (can't control display number, use state).
        # Safety: check the lock file PID — only clean if the process is dead.
        if x_display:
            num = x_display.lstrip(":")
            x11_sock = Path("/tmp/.X11-unix") / f"X{num}"
            x_lock = Path(f"/tmp/.X{num}-lock")
            # Check lock PID before cleaning — never remove a live process's socket
            if x_lock.exists():
                try:
                    lock_pid = int(x_lock.read_text().strip())
                    if _pid_alive(lock_pid):
                        log.debug("X11 lock %s held by live pid %d — skipping", x_display, lock_pid)
                        return
                except (ValueError, OSError):
                    pass  # corrupt/unreadable lock — safe to clean
            cleaned = False
            for f in (x11_sock, x_lock):
                if f.exists():
                    try:
                        f.unlink(missing_ok=True)
                        cleaned = True
                    except OSError:
                        pass
            if cleaned:
                log.info("Cleaned stale X11 socket/lock: %s", x_display)
