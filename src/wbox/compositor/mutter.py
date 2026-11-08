"""Headless mutter (GNOME's compositor) backend (#4851).

The other Linux backends drive wlroots compositors and weston. A bug seen only
on GNOME needs mutter itself, which offers neither wlroots' virtual pointer nor
wlr-screencopy. This backend gives the same tools over mutter's own D-Bus
interfaces:

- mutter runs `--headless --wayland --no-x11 --virtual-monitor WxH` with a
  private session bus, runtime directory and configuration (GSettings come
  from a key file there: auto-maximize off, the keyboard layout pinned);
- the session's private PipeWire (the audio sandbox of #4702) carries mutter's
  screen cast as well as the app's sound;
- input goes through `org.gnome.Mutter.RemoteDesktop`: one session, absolute
  motion against its ScreenCast stream of the monitor, buttons, discrete
  scroll, and keys as keysyms, so text does not depend on a keycode table;
- frames come from that stream: GStreamer's pipewiresrc writes raw RGBx
  frames to a pipe, a thread keeps the latest one, and a screenshot is the
  first frame after the request, cropped and encoded here. The cursor is not
  drawn in them.

What mutter needs from a client, found with Wayland and mutter traces while
validating a GNOME-only bug (egui-pop), is built in:

1. The virtual pointer and keyboard exist only from their first event, and
   until then the seat has neither: a client started before binds them late,
   its first press goes to no surface, and with no wl_keyboard.enter no
   window is ever focused. One motion and a Shift tap are sent before the
   app starts.
2. mutter picks the surface under the pointer on a motion that moves it, a
   little after it. A press at the pointer's spot after a window appeared
   there goes nowhere. Every press is preceded by a motion that really moves
   the pointer (a pixel aside and back when it is there already), then a wait.
3. The same nudge before a capture, so hover state is current in the frame.

Not available, because bare mutter has no interface for it: the window list
(`list_windows`, `focus_window`), and resizing the virtual monitor while it
runs. They answer with an error saying so.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path

from .base import CompositorServer

log = logging.getLogger(__name__)

RD_BUS = "org.gnome.Mutter.RemoteDesktop"
SC_BUS = "org.gnome.Mutter.ScreenCast"

BINARIES = ("mutter", "dbus-daemon", "gst-launch-1.0")

# mutter gives the pointer focus to the surface under it a little after a
# motion (at its next pick): a press sent at once can go to no surface, and
# the application then sees only the release.
FOCUS_DELAY = 0.3
# A screenshot is the first frame that arrives this long after the request.
# The stream repeats the last frame every KEEPALIVE_MS when nothing changes,
# so the wait is bounded by about SETTLE + KEEPALIVE_MS.
SETTLE = 0.2
KEEPALIVE_MS = 200

# evdev button codes (linux/input-event-codes.h)
_BUTTONS = {1: 0x110, 2: 0x112, 3: 0x111}

# X keysyms (xkbcommon-keysyms.h) of the named keys
_NAMED_KEYSYMS = {
    "BackSpace": 0xFF08, "Tab": 0xFF09, "Return": 0xFF0D, "Escape": 0xFF1B,
    "Delete": 0xFFFF, "Insert": 0xFF63, "Home": 0xFF50, "Left": 0xFF51,
    "Up": 0xFF52, "Right": 0xFF53, "Down": 0xFF54, "Prior": 0xFF55,
    "Next": 0xFF56, "End": 0xFF57, "space": 0x20, "Menu": 0xFF67,
    "Shift_L": 0xFFE1, "Control_L": 0xFFE3, "Alt_L": 0xFFE9,
    "Super_L": 0xFFEB,
}
for _i in range(12):
    _NAMED_KEYSYMS[f"F{_i + 1}"] = 0xFFBE + _i

_MODIFIER_KEYSYMS = {
    "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9,
    "super": 0xFFEB, "meta": 0xFFEB, "logo": 0xFFEB, "win": 0xFFEB,
    "cmd": 0xFFEB,
}


def char_keysym(ch: str) -> int:
    """The keysym that types `ch`."""
    if ch == "\n":
        return _NAMED_KEYSYMS["Return"]
    if ch == "\t":
        return _NAMED_KEYSYMS["Tab"]
    code = ord(ch)
    # Latin-1 keysyms are the code points; the rest are 0x01000000 + it
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code


def key_keysym(name: str) -> int:
    """The keysym of a key name: `a`, `Return`, `enter`, `F5`, `comma`…"""
    from wbox.pointer import _KEY_ALIASES, _KEYSYM_CHARS
    sym = _KEY_ALIASES.get(name.lower(), name)
    if sym in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[sym]
    upper = sym[:1].upper() + sym[1:]
    if upper in _NAMED_KEYSYMS and len(sym) > 1:  # f5, backspace…
        return _NAMED_KEYSYMS[upper]
    if sym in _KEYSYM_CHARS:
        return char_keysym(_KEYSYM_CHARS[sym])
    if len(sym) == 1:
        return char_keysym(sym)
    raise ValueError(f"unknown key {name!r}")


def parse_shortcut(shortcut: str) -> tuple[list[int], int]:
    """`ctrl+shift+a` → (modifier keysyms, key keysym)."""
    parts = shortcut.split("+")
    if shortcut.endswith("++"):  # "ctrl++": the key is "+"
        parts = parts[:-2] + ["+"]
    mods = []
    for m in parts[:-1]:
        try:
            mods.append(_MODIFIER_KEYSYMS[m.strip().lower()])
        except KeyError:
            raise ValueError(f"unknown modifier {m!r} in {shortcut!r}") from None
    key = parts[-1].strip() or parts[-1]
    if key.lower() in _MODIFIER_KEYSYMS:
        return mods, _MODIFIER_KEYSYMS[key.lower()]
    return mods, key_keysym(key)


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """An 8-bit RGB PNG, no filter: fast to make, and identical pixels give
    identical bytes, which `record` and `screen_changed` compare."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[r * stride:(r + 1) * stride] for r in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1))
            + chunk(b"IEND", b""))


def parse_region(region: str, width: int, height: int) -> tuple[int, int, int, int]:
    """grim's "x,y WxH", checked against the output."""
    try:
        pos, size = region.split()
        x, y = (int(v) for v in pos.split(","))
        w, h = (int(v) for v in size.lower().split("x"))
    except ValueError:
        raise ValueError(f"region must be 'x,y WxH', got {region!r}") from None
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
        raise ValueError(f"region {region!r} is not inside the {width}x{height} output")
    return x, y, w, h


def crop_rgbx(frame: bytes, frame_width: int, box: tuple[int, int, int, int],
              step: int = 1) -> tuple[int, int, bytes]:
    """A box of an RGBx frame as RGB, keeping one pixel in `step` each way."""
    x, y, w, h = box
    w -= w % step
    h -= h % step
    stride = frame_width * 4
    rows = b"".join(frame[(y + r) * stride + x * 4:(y + r) * stride + (x + w) * 4]
                    for r in range(0, h, step))
    out_w, out_h = w // step, h // step
    out = bytearray(out_w * out_h * 3)
    for c in range(3):
        out[c::3] = rows[c::4 * step]
    return out_w, out_h, bytes(out)


class MutterCompositor(CompositorServer):
    """A headless mutter session with one app inside."""

    compositor_name = "mutter"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Clipboard tools go through wl-clipboard: there is no X server
        self.input_backends["clipboard"] = "wayland"
        self.width, self.height = (int(v) for v in self.screen.lower().split("x"))
        self._log_dir: Path | None = None
        self._log_file: Path | None = None
        self._dbus_proc: subprocess.Popen | None = None
        self._capture: subprocess.Popen | None = None
        self._bus_address = ""
        self._conn = None
        self._lock = threading.RLock()  # one D-Bus call at a time
        self._rd_session = ""
        self._stream = ""
        self._pointer: tuple[float, float] | None = None
        self._frame: bytes = b""
        self._frame_time = 0.0
        self._frame_seq = 0
        self._frame_cond = threading.Condition()
        self._reader: threading.Thread | None = None

    def set_log_dir(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_file = self._log_dir / "mutter-compositor.log"

    def _log_target(self, name: str):
        if self._log_dir is None:
            return subprocess.DEVNULL
        self._log_dir.mkdir(parents=True, exist_ok=True)
        return open(self._log_dir / f"mutter-{name}.log", "wb")

    # ── audio: the session's PipeWire is also mutter's ─────────────

    def _start_audio(self) -> None:
        """Always a private PipeWire: mutter's screen cast needs one.

        With `audio: host` it still runs, for the screen cast only, and the
        app is not pointed at it: its sound goes to the desktop as asked.
        """
        if self._audio is not None:
            return
        from wbox.audio import AudioSandbox
        mode = "null" if self.audio == "host" else self.audio
        sandbox = AudioSandbox(mode, self.state.screenshot_dir)
        sandbox.start()
        self._audio = sandbox

    def _apply_audio_env(self, env: dict[str, str]) -> None:
        if self.audio != "host":
            super()._apply_audio_env(env)

    # ── lifecycle ─────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._conn is not None and super().is_running()

    def _launch(self, app_cmd: list[str], app_env: dict[str, str] | None = None) -> dict:
        if self.is_running():
            return {"status": "already_running",
                    "pid": self.state.compositor_pid,
                    "wayland_display": self.state.wayland_display}
        try:
            self._start_session()
        except Exception as exc:
            msg = self._died_message() or str(exc)
            self.kill(aggressive=True)
            return {"error": msg}

        self._start_app(app_cmd, app_env or {})
        time.sleep(self.timeouts.get("app_render", 3))
        self.state.compositor_pid = self.state.compositor_proc.pid
        self.state.save(self._state_file)
        # As on the other backends: the pointer at the centre, inside the app
        # in every mode, so the app has its pointer-enter before the first
        # real click.
        self.mouse_move(self.width // 2, self.height // 2)
        return {"status": "running",
                "pid": self.state.compositor_pid,
                "wayland_display": self.state.wayland_display,
                "x_display": ""}

    def _start_session(self) -> None:
        missing = [b for b in BINARIES if not shutil.which(b)]
        try:
            import jeepney  # noqa: F401
        except ImportError:
            missing.append("jeepney (pip install 'wbox-mcp[mutter]')")
        if missing:
            raise RuntimeError("compositor: mutter needs " + ", ".join(missing)
                               + ", not found")
        if not self._gst_has("pipewiresrc"):
            raise RuntimeError("compositor: mutter needs GStreamer's pipewiresrc "
                               "(pipewire-gstreamer on Fedora, gstreamer1.0-pipewire on Ubuntu)")

        rt = self._audio.dir  # private runtime directory, PipeWire inside
        keyfile = rt / "cfg" / "glib-2.0" / "settings" / "keyfile"
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        layout = self.keyboard_layout or "us"
        keyfile.write_text(
            "[org/gnome/mutter]\nauto-maximize=false\n\n"
            f"[org/gnome/desktop/input-sources]\nsources=[('xkb', '{layout}')]\n")

        env = {k: v for k, v in os.environ.items()
               if k not in ("WAYLAND_DISPLAY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
                            "PULSE_SERVER", "PIPEWIRE_REMOTE")}
        env.update(XDG_RUNTIME_DIR=str(rt), PIPEWIRE_RUNTIME_DIR=str(rt),
                   XDG_CONFIG_HOME=str(rt / "cfg"), GSETTINGS_BACKEND="keyfile",
                   NO_AT_BRIDGE="1")
        # mutter's own traces in its log: WBOX_MUTTER_DEBUG=input,…
        if os.environ.get("WBOX_MUTTER_DEBUG"):
            env["MUTTER_DEBUG"] = os.environ["WBOX_MUTTER_DEBUG"]

        bus = rt / "bus"
        self._dbus_proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--nopidfile",
             f"--address=unix:path={bus}"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        if not self._poll_until(bus.exists, 5):
            raise RuntimeError("the private session bus did not start")
        self._bus_address = env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        self._env = env

        if self._log_file:  # made first: a log dir that does not exist yet is not an error
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
        log_target = (open(self._log_file, "wb") if self._log_file
                      else subprocess.DEVNULL)
        try:
            self.state.compositor_proc = subprocess.Popen(
                ["mutter", "--headless", "--wayland", "--no-x11",
                 "--virtual-monitor", f"{self.width}x{self.height}"],
                env=env, stdin=subprocess.DEVNULL, stdout=log_target,
                stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            if log_target is not subprocess.DEVNULL:
                log_target.close()

        from jeepney.io.blocking import open_dbus_connection
        self._conn = open_dbus_connection(bus=self._bus_address)
        timeout = self.timeouts.get("wayland_display", 10)
        if not self._poll_until(lambda: self._has_owner(RD_BUS) and self._has_owner(SC_BUS),
                                timeout, self._compositor_died):
            raise RuntimeError(f"mutter's RemoteDesktop did not appear on its bus "
                               f"(timeout={timeout}s)")
        socket = self._poll_until(lambda: (rt / "wayland-0").exists(), timeout,
                                  self._compositor_died)
        if not socket:
            raise RuntimeError(f"mutter's Wayland socket did not appear (timeout={timeout}s)")
        # Absolute: the app keeps its own XDG_RUNTIME_DIR
        self.state.wayland_display = str(rt / "wayland-0")

        self._start_remote_desktop()
        # Pitfall 1: the virtual pointer exists from its first event only.
        self._move(self.width - 1, self.height - 1)
        # The virtual keyboard too: until its first key the seat has no
        # keyboard, so no client gets wl_keyboard.enter. winit takes window
        # focus from it (an egui title-bar drag was dropped for want of
        # focus), and the first key was swallowed (the "H" of a typed
        # "Hello"). A Shift tap, before any client, as a real session has.
        self._keysym(_NAMED_KEYSYMS["Shift_L"], True)
        self._keysym(_NAMED_KEYSYMS["Shift_L"], False)
        self._start_capture()

    def _start_remote_desktop(self) -> None:
        from jeepney import DBusAddress, MatchRule, Properties, message_bus
        from jeepney.wrappers import unwrap_msg
        self._rd_session = self._call(RD_BUS, "/org/gnome/Mutter/RemoteDesktop",
                                      RD_BUS, "CreateSession")[0]
        with self._lock:
            session_id = unwrap_msg(self._conn.send_and_get_reply(
                Properties(DBusAddress(self._rd_session, RD_BUS, RD_BUS + ".Session"))
                .get("SessionId")))[0][1]
        sc_session = self._call(SC_BUS, "/org/gnome/Mutter/ScreenCast", SC_BUS,
                                "CreateSession", "a{sv}",
                                ({"remote-desktop-session-id": ("s", session_id)},))[0]
        self._stream = self._call(SC_BUS, sc_session, SC_BUS + ".Session",
                                  "RecordMonitor", "sa{sv}",
                                  (self._connector(), {"cursor-mode": ("u", 0)}))[0]
        rule = MatchRule(type="signal", interface=SC_BUS + ".Stream",
                         member="PipeWireStreamAdded", path=self._stream)
        with self._lock:
            unwrap_msg(self._conn.send_and_get_reply(message_bus.AddMatch(rule)))
            with self._conn.filter(rule) as queue:
                self._rd("Start")
                signal_msg = self._conn.recv_until_filtered(
                    queue, timeout=self.timeouts.get("wayland_display", 10))
        self._node = int(signal_msg.body[0])

    def _connector(self) -> str:
        state = self._call("org.gnome.Mutter.DisplayConfig",
                           "/org/gnome/Mutter/DisplayConfig",
                           "org.gnome.Mutter.DisplayConfig", "GetCurrentState")
        if not state[1]:
            raise RuntimeError("mutter reports no monitor")
        return state[1][0][0][0]

    def _start_capture(self) -> None:
        """The stream's frames, raw, into a pipe read by a thread."""
        log_target = self._log_target("capture")
        try:
            self._capture = subprocess.Popen(
                ["gst-launch-1.0", "-q", "pipewiresrc", f"path={self._node}",
                 "always-copy=true", f"keepalive-time={KEEPALIVE_MS}", "!",
                 "videoconvert", "!",
                 f"video/x-raw,format=RGBx,width={self.width},height={self.height}",
                 "!", "fdsink", "fd=1", "sync=false"],
                env=self._env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=log_target, bufsize=0, start_new_session=True)
        finally:
            if log_target is not subprocess.DEVNULL:
                log_target.close()
        self._reader = threading.Thread(target=self._read_frames,
                                        args=(self._capture,), daemon=True)
        self._reader.start()
        if not self._wait_frame(time.monotonic(), self.timeouts.get("wayland_display", 10)):
            raise RuntimeError("no frame from mutter's screen cast"
                               + (f" (see {self._log_dir}/mutter-capture.log)"
                                  if self._log_dir else ""))

    def _read_frames(self, proc: subprocess.Popen) -> None:
        size = self.width * self.height * 4
        pipe = proc.stdout
        while True:
            buf = bytearray(size)
            view = memoryview(buf)
            got = 0
            while got < size:
                n = pipe.readinto(view[got:])
                if not n:
                    with self._frame_cond:
                        self._frame_cond.notify_all()
                    return
                got += n
            with self._frame_cond:
                self._frame = bytes(buf)
                self._frame_time = time.monotonic()
                self._frame_seq += 1
                self._frame_cond.notify_all()

    def _wait_frame(self, after: float, timeout: float = 5.0, after_seq: int = -1):
        """The first frame that arrived at `after` or later (and past
        `after_seq`): (frame, time, seq), or None."""
        deadline = time.monotonic() + timeout
        with self._frame_cond:
            while True:
                if self._frame and self._frame_time >= after and self._frame_seq > after_seq:
                    return self._frame, self._frame_time, self._frame_seq
                left = deadline - time.monotonic()
                if left <= 0 or self._capture is None or self._capture.poll() is not None:
                    return None
                self._frame_cond.wait(min(left, 0.25))

    def _gst_has(self, element: str) -> bool:
        if not shutil.which("gst-inspect-1.0"):
            return True  # gst-launch will say so
        return self._run_cmd(["gst-inspect-1.0", element], timeout=15).returncode == 0

    def _start_app(self, app_cmd: list[str], app_env: dict[str, str]) -> None:
        if not app_cmd:
            return
        self._last_app_cmd = list(app_cmd)
        self._last_app_env = dict(app_env)
        env = os.environ.copy()
        env.pop("DISPLAY", None)  # no X server: toolkits pick Wayland
        env["WAYLAND_DISPLAY"] = self.state.wayland_display
        # The session's bus, not the desktop's: what the app asks of a portal
        # or a settings daemon stays in the box.
        env["DBUS_SESSION_BUS_ADDRESS"] = self._bus_address
        self._apply_audio_env(env)
        env.update(app_env)
        log.info("Launching app in mutter: %s", " ".join(app_cmd))
        target = self._log_target("app")
        try:
            self.state.app_proc = subprocess.Popen(
                app_cmd, env=env, stdin=subprocess.DEVNULL, stdout=target,
                stderr=subprocess.STDOUT)
        finally:
            if target is not subprocess.DEVNULL:
                target.close()
        self.state.app_pid = self.state.app_proc.pid

    def _died_message(self) -> str:
        if not self._compositor_died():
            return ""
        code = self.state.compositor_proc.returncode
        msg = f"mutter exited with code {code} during launch"
        if self._log_file and self._log_file.exists():
            lines = [l for l in self._log_file.read_text(errors="replace").splitlines()
                     if "CRITICAL" in l or "ERROR" in l or "WARNING" in l][-5:]
            if lines:
                msg += ":\n" + "\n".join(lines)
            msg += f"\n(full log: {self._log_file})"
        return msg

    def _teardown(self) -> None:
        if self._capture is not None:
            if self._capture.poll() is None:
                self._capture.send_signal(signal.SIGINT)
                try:
                    self._capture.wait(3)
                except subprocess.TimeoutExpired:
                    self._capture.kill()
            self._capture = None
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        app = self.state.app_proc
        if app is not None and app.poll() is None:
            app.terminate()
            try:
                app.wait(3)
            except subprocess.TimeoutExpired:
                app.kill()
        if self._dbus_proc is not None:
            if self._dbus_proc.poll() is None:
                self._dbus_proc.terminate()
            try:
                self._dbus_proc.wait(3)
            except subprocess.TimeoutExpired:
                self._dbus_proc.kill()
            self._dbus_proc = None
        self._pointer = None
        with self._frame_cond:
            self._frame = b""
        super()._teardown()  # the audio sandbox, its directory with it

    # ── D-Bus ─────────────────────────────────────────────────────

    def _has_owner(self, name: str) -> bool:
        from jeepney import message_bus
        from jeepney.wrappers import unwrap_msg
        with self._lock:
            return bool(unwrap_msg(self._conn.send_and_get_reply(
                message_bus.NameHasOwner(name)))[0])

    def _call(self, bus: str, path: str, iface: str, method: str,
              signature: str | None = None, body: tuple = ()):
        from jeepney import DBusAddress, new_method_call
        from jeepney.wrappers import unwrap_msg
        msg = new_method_call(DBusAddress(path, bus, iface), method, signature, body)
        with self._lock:
            return unwrap_msg(self._conn.send_and_get_reply(msg, timeout=10))

    def _rd(self, method: str, signature: str | None = None, *args):
        return self._call(RD_BUS, self._rd_session, RD_BUS + ".Session", method,
                          signature, tuple(args))

    # ── input ─────────────────────────────────────────────────────

    def _move(self, x: float, y: float) -> None:
        self._rd("NotifyPointerMotionAbsolute", "sdd", self._stream, float(x), float(y))
        self._pointer = (x, y)
        self._last_mouse_x, self._last_mouse_y = int(x), int(y)

    def _nudge(self, x: float, y: float) -> None:
        """A motion that really moves the pointer, ending at (x, y)."""
        if self._pointer == (x, y):
            self._move(x + 1 if x + 1 < self.width else x - 1, y)
        self._move(x, y)

    def _focus(self, x: float, y: float) -> None:
        """Pitfall 2: the pointer at (x, y), and the surface there picked."""
        self._nudge(x, y)
        time.sleep(FOCUS_DELAY)

    def _button(self, button: int, pressed: bool) -> None:
        self._rd("NotifyPointerButton", "ib", _BUTTONS.get(button, button), pressed)

    def _keysym(self, keysym: int, pressed: bool) -> None:
        self._rd("NotifyKeyboardKeysym", "ub", keysym, pressed)

    def _guard(self, action):
        if not self.is_running():
            return {"error": "compositor is not running"}
        try:
            return action()
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # a D-Bus error: mutter refused or went away
            return {"error": f"mutter RemoteDesktop: {exc}"}

    def mouse_move(self, x: int, y: int) -> dict:
        def run():
            self._move(x, y)
            return {"ok": True}
        return self._guard(run)

    def click(self, x: int, y: int, button: int = 1) -> dict:
        def run():
            self._focus(x, y)
            self._button(button, True)
            time.sleep(0.05)
            self._button(button, False)
            return {"ok": True}
        return self._guard(run)

    def double_click(self, x: int, y: int, button: int = 1,
                     interval: float = 0.08) -> dict:
        def run():
            self._focus(x, y)
            for i in range(2):
                self._button(button, True)
                time.sleep(0.02)
                self._button(button, False)
                if i == 0:
                    time.sleep(interval)
            return {"ok": True}
        return self._guard(run)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: int = 1,
             steps: int = 10, seconds: float = 0.3) -> dict:
        def run():
            n = max(1, int(steps))
            self._focus(x1, y1)
            self._button(button, True)
            try:
                for i in range(1, n + 1):
                    time.sleep(max(0.0, seconds) / n)
                    self._move(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n)
                time.sleep(0.05)
            finally:
                self._button(button, False)
            return {"ok": True}
        return self._guard(run)

    def scroll(self, x: int, y: int, notches: int, horizontal: bool = False) -> dict:
        def run():
            self._focus(x, y)
            # axis 0 vertical, 1 horizontal; positive = down / right
            self._rd("NotifyPointerAxisDiscrete", "ui", 1 if horizontal else 0,
                     int(notches))
            return {"ok": True}
        return self._guard(run)

    def _tap(self, shortcut: str) -> None:
        mods, key = parse_shortcut(shortcut)
        for m in mods:
            self._keysym(m, True)
        try:
            self._keysym(key, True)
            time.sleep(0.02)
            self._keysym(key, False)
        finally:
            for m in reversed(mods):
                self._keysym(m, False)

    def key(self, shortcut: str) -> dict:
        def run():
            self._tap(shortcut)
            return {"ok": True}
        return self._guard(run)

    def keys(self, shortcuts: list[str], delay_ms: int = 100) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running", "sent": 0}
        for n, shortcut in enumerate(shortcuts):
            done = self.key(shortcut)
            if "error" in done:
                return {"error": done["error"], "sent": n}
            time.sleep(max(0, delay_ms) / 1000)
        return {"ok": True, "sent": len(shortcuts)}

    def type_text(self, text: str, delay_ms: int = 12) -> dict:
        """Each character as its keysym: mutter finds the key in its keymap,
        with Shift or AltGr as the level needs. A character the keymap lacks
        is dropped by mutter, which reports nothing."""
        def run():
            for ch in text:
                sym = char_keysym(ch)
                self._keysym(sym, True)
                self._keysym(sym, False)
                time.sleep(max(0, delay_ms) / 1000)
            return {"ok": True, "length": len(text)}
        return self._guard(run)

    def hold(self, keys: list[str], actions: list[dict]) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        if not keys:
            return {"error": "hold needs at least one modifier"}
        try:
            mods = [parse_shortcut(k)[1] for k in keys]
        except ValueError as exc:
            return {"error": str(exc)}
        done, shots = [], []
        pressed = []
        try:
            for m in mods:
                self._keysym(m, True)
                pressed.append(m)
            for action in actions:
                kind = action.get("type")
                if kind == "key":
                    self._tap(action["key"])
                elif kind == "click":
                    x, y = int(action["x"]), int(action["y"])
                    self._focus(x, y)
                    b = int(action.get("button", 1))
                    self._button(b, True)
                    time.sleep(0.05)
                    self._button(b, False)
                elif kind == "screenshot":
                    shot = self.screenshot(action.get("name"))
                    if "error" in shot:
                        return {"error": shot["error"], "done": done}
                    shots.append(shot["path"])
                else:
                    return {"error": f"unknown action type: {kind!r}", "done": done}
                done.append(kind)
        except (ValueError, KeyError) as exc:
            return {"error": f"hold failed: {exc}", "done": done}
        except Exception as exc:
            return {"error": f"mutter RemoteDesktop: {exc}", "done": done}
        finally:
            for m in reversed(pressed):
                try:
                    self._keysym(m, False)
                except Exception:
                    pass
        return {"ok": True, "held": keys, "done": done,
                **({"screenshots": shots} if shots else {})}

    # ── capture ───────────────────────────────────────────────────

    def _current_frame(self):
        """Pitfall 3: nudge the pointer so hover is current, then the first
        frame after the request."""
        if self._pointer is not None:
            self._nudge(*self._pointer)
        return self._wait_frame(time.monotonic() + SETTLE,
                                SETTLE + KEEPALIVE_MS / 1000 + 5)

    def _box_and_step(self, region: str | None, scale: float | None):
        box = (parse_region(region, self.width, self.height) if region
               else (0, 0, self.width, self.height))
        step = 1
        if scale:
            if not 0 < scale <= 1:
                raise ValueError("scale must be in (0, 1] on mutter")
            step = max(1, round(1 / scale))
        return box, step

    def screenshot(self, name: str | None = None, scale: float | None = None,
                   region: str | None = None) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        try:
            box, step = self._box_and_step(region, scale)
            got = self._current_frame()
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"mutter RemoteDesktop: {exc}"}
        if got is None:
            return {"error": "no frame from mutter's screen cast"}
        w, h, rgb = crop_rgbx(got[0], self.width, box, step)
        path = self._next_screenshot_path(name)
        path.write_bytes(png_bytes(w, h, rgb))
        return {"path": str(path), "size": path.stat().st_size}

    def record(self, seconds: float, region: str | None = None,
               fps: float | None = None, name: str | None = None,
               during: dict | None = None, assemble: str | None = None) -> dict:
        """Every frame of the stream for `seconds`: mutter sends one when the
        output changes, and the last one again every KEEPALIVE_MS otherwise."""
        if not self.is_running():
            return {"error": "compositor is not running"}
        if seconds <= 0:
            return {"error": "seconds must be positive"}
        try:
            box, _ = self._box_and_step(region, None)
        except ValueError as exc:
            return {"error": str(exc)}
        frames_dir = (self.state.screenshot_dir or Path(".")) / (
            name or f"record_{int(time.time())}")
        try:
            frames_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"error": f"cannot create {frames_dir}: {exc}"}
        if self._pointer is not None:
            self._nudge(*self._pointer)

        gesture_error: list[str] = []
        thread = (threading.Thread(target=self._play_during,
                                   args=(during, gesture_error), daemon=True)
                  if during else None)
        min_interval = (1.0 / fps) if fps and fps > 0 else 0.0
        frames, stamps = [], []
        started = time.monotonic()
        seq = self._frame_seq
        if thread:
            thread.start()
        try:
            while True:
                left = seconds - (time.monotonic() - started)
                if left <= 0:
                    break
                got = self._wait_frame(0, left, after_seq=seq)
                if got is None:
                    if self._capture is None or self._capture.poll() is not None:
                        return {"error": "mutter's screen cast stopped",
                                "frames": len(frames)}
                    continue
                frame, at, seq = got
                w, h, rgb = crop_rgbx(frame, self.width, box)
                path = frames_dir / f"frame_{len(frames):05d}.png"
                path.write_bytes(png_bytes(w, h, rgb))
                frames.append(path)
                stamps.append(at - started)
                if min_interval:
                    wait = min_interval - (time.monotonic() - at)
                    if wait > 0:
                        time.sleep(wait)
        finally:
            if thread:
                thread.join(timeout=max(5.0, seconds))

        result = {
            "frames_dir": str(frames_dir),
            "frames": len(frames),
            **self._record_summary(frames, stamps, time.monotonic() - started),
            **({"during_error": gesture_error[0]} if gesture_error else {}),
        }
        if assemble and frames:
            result.update(self._assemble(frames_dir, assemble, result.get("fps") or 10))
        return result

    # ── what bare mutter does not offer ───────────────────────────

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"width": self.width, "height": self.height}

    def resize(self, width: int, height: int) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"error": "mutter's virtual monitor is sized at launch: set "
                         "screen and launch again"}

    def list_windows(self) -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"error": "list_windows is not available on mutter: bare mutter "
                         "exposes no window list (no foreign-toplevel protocol, "
                         "no gnome-shell)"}

    def focus_window(self, title: str = "", app_id: str = "") -> dict:
        if not self.is_running():
            return {"error": "compositor is not running"}
        return {"error": "focus_window is not available on mutter: bare mutter "
                         "exposes no window list; click the window instead"}

    def debug_input(self, test_key: str = "a", target: str = "xev") -> dict:
        return {"error": "debug_input inspects X11 delivery; mutter runs "
                         "without X11"}
