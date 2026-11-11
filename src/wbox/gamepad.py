"""
gamepad.py — a virtual Xbox controller for the box, over Linux uinput.

Games read controllers through SDL (or evdev directly), not through Wayland:
no compositor protocol can hand them a pad. So the pad is a kernel device,
made with /dev/uinput, that announces itself the way the xpad driver
announces a real one — same vendor/product, same buttons and axes in the same
order. SDL then maps it with its built-in database entry, exactly as it maps
a pad plugged into USB.

A uinput device is global: every process on the machine could see it, and the
app could see the host's real pads. The box hides both directions at launch
(`isolation_wrapper`): the app runs under bubblewrap with a /dev/input of its
own, holding this pad's node and nothing else.

Rumble: a pad that announces force feedback must answer the app's effect
uploads, or the app's ioctl blocks until the kernel's timeout. A thread
answers them and keeps what the app asked for, which `rumble()` reports —
a test can check that a hit made the pad shake.

Pure Python, stdlib only, like pointer.py.
"""

import fcntl
import os
import select
import struct
import threading
import time
from pathlib import Path

# ── ioctl numbers (linux/uinput.h, linux/input.h) ──

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, nr, size, kind=ord("U")):
    return (direction << 30) | (size << 16) | (kind << 8) | nr


def _io(nr):
    return _ioc(0, nr, 0)


def _iow(nr, size):
    return _ioc(_IOC_WRITE, nr, size)


def _iowr(nr, size):
    return _ioc(_IOC_READ | _IOC_WRITE, nr, size)


UI_DEV_CREATE = _io(1)
UI_DEV_DESTROY = _io(2)
_SETUP_FMT = "HHHH80sI"          # struct uinput_setup: input_id, name, ff_effects_max
UI_DEV_SETUP = _iow(3, struct.calcsize(_SETUP_FMT))
_ABS_SETUP_FMT = "H2xiiiiii"     # struct uinput_abs_setup: code, input_absinfo
UI_ABS_SETUP = _iow(4, struct.calcsize(_ABS_SETUP_FMT))
UI_SET_EVBIT = _iow(100, 4)
UI_SET_KEYBIT = _iow(101, 4)
UI_SET_ABSBIT = _iow(103, 4)
UI_SET_FFBIT = _iow(107, 4)


def _ui_get_sysname(length):
    return _ioc(_IOC_READ, 44, length)


# struct ff_effect is 48 bytes on 64-bit: type, id, direction, trigger
# (button, interval), replay (length, delay), then an 8-aligned union at 16
# (ff_rumble_effect: strong_magnitude, weak_magnitude).
_FF_EFFECT_SIZE = 48
_FF_UPLOAD_FMT = f"Ii{_FF_EFFECT_SIZE}s{_FF_EFFECT_SIZE}s"   # struct uinput_ff_upload
_FF_ERASE_FMT = "IiI"                                        # struct uinput_ff_erase
UI_BEGIN_FF_UPLOAD = _iowr(200, struct.calcsize(_FF_UPLOAD_FMT))
UI_END_FF_UPLOAD = _iow(201, struct.calcsize(_FF_UPLOAD_FMT))
UI_BEGIN_FF_ERASE = _iowr(202, struct.calcsize(_FF_ERASE_FMT))
UI_END_FF_ERASE = _iow(203, struct.calcsize(_FF_ERASE_FMT))

EV_SYN, EV_KEY, EV_ABS, EV_FF, EV_UINPUT = 0x00, 0x01, 0x03, 0x15, 0x0101
SYN_REPORT = 0
UI_FF_UPLOAD, UI_FF_ERASE = 1, 2
FF_RUMBLE = 0x50
BUS_USB = 0x03
_EVENT_FMT = "llHHi"             # struct input_event (64-bit timeval)
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

# Buttons, by the names an Xbox pad prints, with the names SDL and Nintendo
# layouts use as aliases. The codes are xpad's.
BUTTONS = {
    "a": 0x130, "b": 0x131, "x": 0x133, "y": 0x134,
    "lb": 0x136, "rb": 0x137,
    "back": 0x13a, "start": 0x13b, "guide": 0x13c,
    "ls": 0x13d, "rs": 0x13e,
}
_BUTTON_ALIASES = {
    "view": "back", "select": "back", "minus": "back",
    "menu": "start", "plus": "start",
    "home": "guide", "xbox": "guide",
    "l": "lb", "r": "rb", "l1": "lb", "r1": "rb",
    "leftshoulder": "lb", "rightshoulder": "rb",
    "l3": "ls", "r3": "rs", "leftstick": "ls", "rightstick": "rs",
}
# The d-pad is a hat on xpad pads: (axis, value)
DPAD = {
    "up": (ABS_HAT0Y, -1), "down": (ABS_HAT0Y, 1),
    "left": (ABS_HAT0X, -1), "right": (ABS_HAT0X, 1),
}
_DPAD_ALIASES = {"dpad_up": "up", "dpad_down": "down",
                 "dpad_left": "left", "dpad_right": "right"}
TRIGGERS = {"lt": ABS_Z, "rt": ABS_RZ}
_TRIGGER_ALIASES = {"l2": "lt", "r2": "rt", "zl": "lt", "zr": "rt",
                    "lefttrigger": "lt", "righttrigger": "rt"}
STICKS = {"left": (ABS_X, ABS_Y), "right": (ABS_RX, ABS_RY)}
# One axis at a time, by the names pad_script uses for analog values:
# sticks -1..1 (y = -1 pushed up), triggers 0..1.
AXES = {"lx": ABS_X, "ly": ABS_Y, "rx": ABS_RX, "ry": ABS_RY,
        "lt": ABS_Z, "rt": ABS_RZ}
_TRIGGER_AXES = ("lt", "rt")

# Profiles: what the pad tells the kernel it is. SDL keys its mapping on
# bus/vendor/product/version, so these are the values xpad gives real pads.
PROFILES = {
    "xbox360": {"name": "Microsoft X-Box 360 pad", "vendor": 0x045e,
                "product": 0x028e, "version": 0x0114, "trigger_max": 255},
    "xboxone": {"name": "Microsoft X-Box One S pad", "vendor": 0x045e,
                "product": 0x02ea, "version": 0x0301, "trigger_max": 1023},
}

STICK_MAX = 32767


def uinput_problem() -> str | None:
    """Why a pad cannot be made here, or None when it can."""
    path = "/dev/uinput"
    if not os.path.exists(path):
        return ("/dev/uinput does not exist: load the module "
                "(sudo modprobe uinput)")
    if not os.access(path, os.R_OK | os.W_OK):
        return ("/dev/uinput is not writable by this user. A udev rule gives "
                "it to the input group: KERNEL==\"uinput\", GROUP=\"input\", "
                "MODE=\"0660\", OPTIONS+=\"static_node=uinput\" in "
                "/etc/udev/rules.d/60-wbox-uinput.rules, then "
                "`sudo usermod -aG input $USER` and log in again")
    return None


def _parse_button(name: str):
    """A button name → ("key", code) or ("hat", axis, value)."""
    n = name.strip().lower()
    n = _BUTTON_ALIASES.get(n, n)
    if n in BUTTONS:
        return ("key", BUTTONS[n])
    n = _DPAD_ALIASES.get(n, n)
    if n in DPAD:
        return ("hat", *DPAD[n])
    raise ValueError(
        f"unknown pad button {name!r}; expected one of "
        f"{sorted(BUTTONS) + sorted(DPAD)} (or an alias such as "
        f"{sorted(_BUTTON_ALIASES)[:6]}...)")


def _parse_trigger(name: str) -> int:
    n = name.strip().lower()
    n = _TRIGGER_ALIASES.get(n, n)
    if n not in TRIGGERS:
        raise ValueError(f"unknown trigger {name!r}; expected lt or rt")
    return TRIGGERS[n]


def _parse_stick(name: str) -> tuple[int, int]:
    n = name.strip().lower()
    n = {"l": "left", "r": "right", "ls": "left", "rs": "right"}.get(n, n)
    if n not in STICKS:
        raise ValueError(f"unknown stick {name!r}; expected left or right")
    return STICKS[n]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class VirtualGamepad:
    """One Xbox-like pad, alive from open() to close()."""

    def __init__(self, profile: str = "xbox360", name: str | None = None):
        if profile not in PROFILES:
            raise ValueError(f"unknown gamepad profile {profile!r}; "
                             f"expected one of {sorted(PROFILES)}")
        self.profile = profile
        self._p = dict(PROFILES[profile])
        if name:
            self._p["name"] = name
        self.fd = -1
        self.event_node = ""
        self.sysname = ""
        self._lock = threading.Lock()
        self._state: dict[tuple[int, int], int] = {}   # (type, code) → value
        self._effects: dict[int, dict] = {}            # effect id → rumble
        self._playing: dict[int, float] = {}           # effect id → start
        self._rumble_log: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listeners: list = []   # fn(monotonic time, [(type, code, value)])

    # ── Lifecycle ──

    def open(self) -> "VirtualGamepad":
        problem = uinput_problem()
        if problem:
            raise RuntimeError(problem)
        fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        try:
            for ev in (EV_KEY, EV_ABS, EV_FF):
                fcntl.ioctl(fd, UI_SET_EVBIT, ev)
            for code in BUTTONS.values():
                fcntl.ioctl(fd, UI_SET_KEYBIT, code)
            fcntl.ioctl(fd, UI_SET_FFBIT, FF_RUMBLE)
            tmax = self._p["trigger_max"]
            axes = [(ABS_X, -32768, STICK_MAX, 16, 128),
                    (ABS_Y, -32768, STICK_MAX, 16, 128),
                    (ABS_RX, -32768, STICK_MAX, 16, 128),
                    (ABS_RY, -32768, STICK_MAX, 16, 128),
                    (ABS_Z, 0, tmax, 0, 0), (ABS_RZ, 0, tmax, 0, 0),
                    (ABS_HAT0X, -1, 1, 0, 0), (ABS_HAT0Y, -1, 1, 0, 0)]
            for code, lo, hi, fuzz, flat in axes:
                fcntl.ioctl(fd, UI_SET_ABSBIT, code)
                fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack(
                    _ABS_SETUP_FMT, code, 0, lo, hi, fuzz, flat, 0))
            fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack(
                _SETUP_FMT, BUS_USB, self._p["vendor"], self._p["product"],
                self._p["version"], self._p["name"].encode()[:79], 16))
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        self.sysname = self._sysname()
        self.event_node = self._wait_event_node()
        self._thread = threading.Thread(target=self._serve_ff, daemon=True,
                                        name="wbox-gamepad-ff")
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self.fd >= 0:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = -1

    def _sysname(self) -> str:
        buf = fcntl.ioctl(self.fd, _ui_get_sysname(64), b"\0" * 64)
        return buf.split(b"\0", 1)[0].decode()

    def _wait_event_node(self, timeout: float = 3.0) -> str:
        """The /dev/input/eventN udev makes for the pad (it takes a moment)."""
        sysdir = Path("/sys/devices/virtual/input") / self.sysname
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for child in sysdir.glob("event*"):
                node = Path("/dev/input") / child.name
                if node.exists():
                    return str(node)
            time.sleep(0.02)
        raise RuntimeError(f"no event node appeared for {sysdir}")

    # ── Output: what the app sees ──

    def _emit(self, events) -> None:
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        data = b"".join(struct.pack(_EVENT_FMT, sec, usec, t, c, v)
                        for t, c, v in events)
        data += struct.pack(_EVENT_FMT, sec, usec, EV_SYN, SYN_REPORT, 0)
        with self._lock:
            os.write(self.fd, data)
            for t, c, v in events:
                self._state[(t, c)] = v
            # Under the lock: a recorder sees events in the order the app did
            if self._listeners:
                at = time.monotonic()
                for fn in list(self._listeners):
                    fn(at, events)

    def add_listener(self, fn) -> None:
        """fn(monotonic time, [(type, code, value)]) after each emit:
        whatever drives the pad — a script, a tool, a proxied real pad."""
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    @property
    def trigger_max(self) -> int:
        return self._p["trigger_max"]

    def set_buttons(self, names, down: bool) -> None:
        """Press or release buttons (d-pad directions included) at once."""
        events = []
        for name in names:
            kind, *rest = _parse_button(name)
            if kind == "key":
                events.append((EV_KEY, rest[0], 1 if down else 0))
            else:
                axis, value = rest
                events.append((EV_ABS, axis, value if down else 0))
        if events:
            self._emit(events)

    def set_stick(self, stick: str, x: float, y: float) -> None:
        """Tilt a stick: x, y in -1..1, y = -1 pushed up (away from you)."""
        ax, ay = _parse_stick(stick)
        self._emit([(EV_ABS, ax, round(_clamp(x, -1, 1) * STICK_MAX)),
                    (EV_ABS, ay, round(_clamp(y, -1, 1) * STICK_MAX))])

    def set_trigger(self, trigger: str, value: float) -> None:
        """Pull a trigger: 0 (released) to 1 (fully)."""
        axis = _parse_trigger(trigger)
        self._emit([(EV_ABS, axis,
                     round(_clamp(value, 0, 1) * self._p["trigger_max"]))])

    def set_axis(self, name: str, value: float) -> None:
        """One axis: lx ly rx ry in -1..1 (ly = -1 pushed up), lt rt in 0..1."""
        n = name.strip().lower()
        if n not in AXES:
            raise ValueError(f"unknown axis {name!r}; expected one of {sorted(AXES)}")
        if n in _TRIGGER_AXES:
            raw = round(_clamp(value, 0, 1) * self._p["trigger_max"])
        else:
            raw = round(_clamp(value, -1, 1) * STICK_MAX)
        self._emit([(EV_ABS, AXES[n], raw)])

    def neutral(self) -> None:
        """Every button up, sticks centred, triggers released."""
        events = [(EV_KEY, c, 0) for c in BUTTONS.values()]
        events += [(EV_ABS, a, 0) for a in
                   (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ,
                    ABS_HAT0X, ABS_HAT0Y)]
        self._emit(events)

    def state(self) -> dict:
        """What the pad shows now: buttons down, sticks, triggers."""
        with self._lock:
            st = dict(self._state)
        down = sorted(n for n, c in BUTTONS.items() if st.get((EV_KEY, c)))
        for n, (axis, value) in DPAD.items():
            if st.get((EV_ABS, axis)) == value:
                down.append(n)
        tmax = self._p["trigger_max"]
        return {
            "buttons": down,
            "left": [round(st.get((EV_ABS, ABS_X), 0) / STICK_MAX, 3),
                     round(st.get((EV_ABS, ABS_Y), 0) / STICK_MAX, 3)],
            "right": [round(st.get((EV_ABS, ABS_RX), 0) / STICK_MAX, 3),
                      round(st.get((EV_ABS, ABS_RY), 0) / STICK_MAX, 3)],
            "lt": round(st.get((EV_ABS, ABS_Z), 0) / tmax, 3),
            "rt": round(st.get((EV_ABS, ABS_RZ), 0) / tmax, 3),
        }

    # ── Input: rumble the app asks for ──

    def _serve_ff(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self.fd, _EVENT_SIZE * 32)
            except BlockingIOError:
                continue
            except OSError:
                return
            for off in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                _, _, etype, code, value = struct.unpack_from(
                    _EVENT_FMT, data, off)
                try:
                    if etype == EV_UINPUT and code == UI_FF_UPLOAD:
                        self._ff_upload(value)
                    elif etype == EV_UINPUT and code == UI_FF_ERASE:
                        self._ff_erase(value)
                    elif etype == EV_FF:
                        self._ff_play(code, value)
                except OSError:
                    pass  # the app gives up on its own after the timeout

    def _ff_upload(self, request_id: int) -> None:
        buf = bytearray(struct.pack(_FF_UPLOAD_FMT, request_id, 0,
                                    b"\0" * _FF_EFFECT_SIZE,
                                    b"\0" * _FF_EFFECT_SIZE))
        fcntl.ioctl(self.fd, UI_BEGIN_FF_UPLOAD, buf)
        req, _, effect, old = struct.unpack(_FF_UPLOAD_FMT, buf)
        ftype, fid = struct.unpack_from("Hh", effect, 0)
        length, _delay = struct.unpack_from("HH", effect, 10)
        if ftype == FF_RUMBLE:
            strong, weak = struct.unpack_from("HH", effect, 16)
            with self._lock:
                self._effects[fid] = {"strong": strong, "weak": weak,
                                      "length_ms": length}
        fcntl.ioctl(self.fd, UI_END_FF_UPLOAD,
                    struct.pack(_FF_UPLOAD_FMT, req, 0, effect, old))

    def _ff_erase(self, request_id: int) -> None:
        buf = bytearray(struct.pack(_FF_ERASE_FMT, request_id, 0, 0))
        fcntl.ioctl(self.fd, UI_BEGIN_FF_ERASE, buf)
        req, _, fid = struct.unpack(_FF_ERASE_FMT, buf)
        with self._lock:
            self._effects.pop(fid, None)
            self._playing.pop(fid, None)
        fcntl.ioctl(self.fd, UI_END_FF_ERASE,
                    struct.pack(_FF_ERASE_FMT, req, 0, fid))

    def _ff_play(self, fid: int, value: int) -> None:
        with self._lock:
            if value:
                self._playing[fid] = time.monotonic()
                eff = self._effects.get(fid)
                if eff and (eff["strong"] or eff["weak"]):
                    self._rumble_log.append(
                        {"t": round(time.time(), 3), **eff})
                    del self._rumble_log[:-200]
            else:
                self._playing.pop(fid, None)

    def rumble(self, clear: bool = False) -> dict:
        """The rumble the app asked for: playing now, and the recent log."""
        now = time.monotonic()
        with self._lock:
            playing = []
            for fid, started in self._playing.items():
                eff = self._effects.get(fid)
                # SDL stops the motors by uploading the same effect again
                # with both magnitudes at 0: still "playing", shaking nothing.
                if not eff or not (eff["strong"] or eff["weak"]):
                    continue
                if eff["length_ms"] and now - started > eff["length_ms"] / 1000:
                    continue
                playing.append(eff)
            log = list(self._rumble_log)
            if clear:
                self._rumble_log.clear()
        return {
            "playing": bool(playing),
            "strong": max((e["strong"] for e in playing), default=0) / 65535,
            "weak": max((e["weak"] for e in playing), default=0) / 65535,
            "events": log,
        }


def isolation_wrapper(event_node: str) -> tuple[list[str], dict[str, str]]:
    """The command prefix and env that give the app this pad and no other.

    bubblewrap keeps the whole filesystem, but mounts an empty /dev/input
    holding only the pad's node: the app cannot open the host's pads,
    keyboards or mice. With udev's enumeration off, SDL scans /dev/input
    itself, and finds only that node; with HIDAPI off it does not reach the
    host's pads through /dev/hidraw either.
    """
    import shutil
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("gamepad isolation needs bubblewrap (bwrap); "
                           "install it, or set gamepad.isolate: false")
    prefix = [bwrap, "--dev-bind", "/", "/",
              "--tmpfs", "/dev/input",
              "--dev-bind", event_node, event_node,
              "--die-with-parent", "--"]
    env = {"SDL_JOYSTICK_DISABLE_UDEV": "1",
           "SDL_JOYSTICK_HIDAPI": "0"}
    return prefix, env


# ── Timed scripts ──
#
# `<seconds>:<input>[+<input>...][/<ms>]`, comma-separated: the form game
# input scripts commonly take, so one written for a game's own scripted
# input can often be played here as it is.

_SCRIPT_STICKS = {
    "lup": ("left", 0, -1), "ldown": ("left", 0, 1),
    "lleft": ("left", -1, 0), "lright": ("left", 1, 0),
    "rup": ("right", 0, -1), "rdown": ("right", 0, 1),
    "rleft": ("right", -1, 0), "rright": ("right", 1, 0),
}


def parse_script_input(token: str) -> tuple:
    """One input of a script → ("button", name) | ("trigger", name) |
    ("stick", stick, x, y)."""
    t = token.strip().lower()
    if "=" in t:
        name, _, raw = t.partition("=")
        name = name.strip()
        if name not in AXES:
            raise ValueError(f"script input {token!r}: unknown axis {name!r}; "
                             f"expected one of {sorted(AXES)}")
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"script input {token!r}: {raw!r} is not a number") from None
        lo = 0.0 if name in _TRIGGER_AXES else -1.0
        if not lo <= value <= 1.0:
            raise ValueError(f"script input {token!r}: {name} goes from {lo:g} to 1")
        return ("axis", name, value)
    if t in _SCRIPT_STICKS:
        return ("stick", *_SCRIPT_STICKS[t])
    if _TRIGGER_ALIASES.get(t, t) in TRIGGERS:
        return ("trigger", t)
    _parse_button(t)  # raises with the list of names
    return ("button", t)


def parse_pad_script(script: str, default_ms: int = 200):
    """→ [(start_seconds, [input, ...], hold_ms)], in script order."""
    steps = []
    for raw in script.split(","):
        raw = raw.strip()
        if not raw:
            continue
        when, sep, rest = raw.partition(":")
        if not sep:
            raise ValueError(f"script step {raw!r}: expected <seconds>:<input>")
        try:
            start = float(when)
        except ValueError:
            raise ValueError(f"script step {raw!r}: {when!r} is not a time") from None
        names, _, ms = rest.partition("/")
        try:
            hold = int(ms) if ms else default_ms
        except ValueError:
            raise ValueError(f"script step {raw!r}: {ms!r} is not a duration in ms") from None
        inputs = [parse_script_input(n) for n in names.split("+") if n.strip()]
        if not inputs:
            raise ValueError(f"script step {raw!r}: no input")
        if ms and any(i[0] == "axis" for i in inputs):
            raise ValueError(f"script step {raw!r}: an axis value holds until "
                             "the next one for that axis; /<ms> is for presses")
        if start < 0 or hold < 0:
            raise ValueError(f"script step {raw!r}: negative time")
        steps.append((start, inputs, hold))
    if not steps:
        raise ValueError("empty script")
    return steps


def script_actions(pad: "VirtualGamepad", kind: str, args) -> tuple:
    """(press, release) callables for one parsed script input; release is
    None for an axis value, which stays until the next one."""
    if kind == "button":
        return (lambda: pad.set_buttons([args[0]], True),
                lambda: pad.set_buttons([args[0]], False))
    if kind == "trigger":
        return (lambda: pad.set_trigger(args[0], 1.0),
                lambda: pad.set_trigger(args[0], 0.0))
    if kind == "axis":           # a value: set, and left as it is
        name, value = args
        return (lambda: pad.set_axis(name, value), None)
    stick, x, y = args
    return (lambda: pad.set_stick(stick, x, y),
            lambda: pad.set_stick(stick, 0, 0))


def play_script(pad: "VirtualGamepad", steps) -> tuple[float, float]:
    """Play parsed steps on the pad, on its own clock: (seconds, worst late)."""
    timeline = []   # (time, order, fn): releases before presses at a tie
    for start, inputs, ms in steps:
        for kind, *args in inputs:
            on, off = script_actions(pad, kind, args)
            timeline.append((start, 1, on))
            if off is not None:
                timeline.append((start + ms / 1000.0, 0, off))
    timeline.sort(key=lambda e: (e[0], e[1]))
    t0 = time.monotonic()
    late = 0.0
    for when, _, fn in timeline:
        delay = when - (time.monotonic() - t0)
        if delay > 0:
            time.sleep(delay)
        else:
            late = max(late, -delay)
        fn()
    return time.monotonic() - t0, late
