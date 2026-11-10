"""gamepad_proxy.py — mirror a real controller onto the box's virtual pad.

The virtual pad is driven by scripted events. Reaching the place in a game
where a bug shows is slow that way, and hard to repeat. This reads a real pad
on the host and replays it on the virtual one, so the box can be played by
hand — and, later, so a session played by hand can be recorded as a script.

Only `VirtualGamepad`'s public setters are used, so nothing here depends on
how that pad talks to uinput.

The app keeps seeing the virtual pad alone: the real device is read on this
side of the sandbox and never enters it.
"""

from __future__ import annotations

import array
import ctypes
import fcntl
import glob
import logging
import os
import struct
import threading
from pathlib import Path

from wbox.gamepad import BUTTONS, DPAD

log = logging.getLogger(__name__)

# struct input_event on 64-bit Linux: two longs of timeval, then type, code, value
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_GAMEPAD = 0x130

ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

_IOC_READ, _IOC_WRITE = 2, 1


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (ord("E") << 8) | (nr << 0) | (size << 16)


def _eviocgname(length: int) -> int:
    return _ioc(_IOC_READ, 0x06, length)


def _eviocgbit(ev: int, length: int) -> int:
    return _ioc(_IOC_READ, 0x20 + ev, length)


def _eviocgabs(axis: int) -> int:
    return _ioc(_IOC_READ, 0x40 + axis, 24)          # struct input_absinfo: 6 ints


_EVIOCGRAB = _ioc(_IOC_WRITE, 0x90, 4)

# Real-pad event codes to the names VirtualGamepad's setters take.
_BY_CODE = {code: name for name, code in BUTTONS.items()}
_HAT = {axis: {value: name for name, (a, value) in DPAD.items() if a == axis}
        for axis in (ABS_HAT0X, ABS_HAT0Y)}
_STICK_AXIS = {ABS_X: ("left", 0), ABS_Y: ("left", 1),
               ABS_RX: ("right", 0), ABS_RY: ("right", 1)}
_TRIGGER_AXIS = {ABS_Z: "lt", ABS_RZ: "rt"}


def device_name(fd: int) -> str:
    buf = array.array("B", b"\0" * 128)
    try:
        fcntl.ioctl(fd, _eviocgname(len(buf)), buf)
    except OSError:
        return ""
    return bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")


def _has_gamepad_button(fd: int) -> bool:
    buf = array.array("B", b"\0" * 96)               # KEY_MAX/8 + 1 is enough here
    try:
        fcntl.ioctl(fd, _eviocgbit(EV_KEY, len(buf)), buf)
    except OSError:
        return False
    byte, bit = divmod(BTN_GAMEPAD, 8)
    return byte < len(buf) and bool(buf[byte] & (1 << bit))


def _absinfo(fd: int, axis: int) -> tuple[int, int] | None:
    buf = array.array("i", [0] * 6)
    try:
        fcntl.ioctl(fd, _eviocgabs(axis), buf)
    except OSError:
        return None
    _value, lo, hi = buf[0], buf[1], buf[2]
    return (lo, hi) if hi > lo else None


def find_pad(exclude: str | None = None) -> str | None:
    """The first real pad that is not `exclude` (the box's own virtual one)."""
    for node in sorted(glob.glob("/dev/input/event*"),
                       key=lambda p: int(p.rsplit("event", 1)[1])):
        if exclude and os.path.realpath(node) == os.path.realpath(exclude):
            continue
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            if _has_gamepad_button(fd):
                return node
        finally:
            os.close(fd)
    return None


class GamepadProxy:
    """Replays a real pad's events on a VirtualGamepad, in a thread."""

    def __init__(self, pad, device: str, grab: bool = False):
        self.pad = pad
        self.device = device
        self.grab = grab
        self.name = ""
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ranges: dict[int, tuple[int, int]] = {}
        self._sticks = {"left": [0.0, 0.0], "right": [0.0, 0.0]}
        self._pending = False
        self.events = 0

    # ---------------------------------------------------------------- start
    def start(self) -> dict:
        self._fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        self.name = device_name(self._fd)
        for axis in (*_STICK_AXIS, *_TRIGGER_AXIS):
            rng = _absinfo(self._fd, axis)
            if rng:
                self._ranges[axis] = rng
        if self.grab:
            try:
                fcntl.ioctl(self._fd, _EVIOCGRAB, 1)
            except OSError as exc:
                os.close(self._fd)
                self._fd = None
                raise RuntimeError(
                    f"cannot grab {self.device}: {exc}. Without grab the real "
                    "pad drives the host too; with it, it is taken for the "
                    "session."
                ) from exc
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="gamepad-proxy")
        self._thread.start()
        return self.status()

    def status(self) -> dict:
        return {"device": self.device, "name": self.name, "grab": self.grab,
                "running": bool(self._thread and self._thread.is_alive()),
                "events": self.events}

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._fd is not None:
            if self.grab:
                try:
                    fcntl.ioctl(self._fd, _EVIOCGRAB, 0)
                except OSError:
                    pass
            os.close(self._fd)
            self._fd = None
        try:
            self.pad.neutral()
        except Exception:                            # the pad may be gone
            pass
        return {"stopped": True, "events": self.events}

    # ----------------------------------------------------------------- loop
    def _scale(self, axis: int, value: int, signed: bool) -> float:
        lo, hi = self._ranges.get(axis, (0, 255))
        if hi == lo:
            return 0.0
        if signed:                                   # -1..1 around the middle
            mid = (hi + lo) / 2
            half = (hi - lo) / 2
            return max(-1.0, min(1.0, (value - mid) / half))
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    def _apply(self, etype: int, code: int, value: int) -> None:
        if etype == EV_KEY:
            name = _BY_CODE.get(code)
            if name and value in (0, 1):             # 2 is autorepeat
                self.pad.set_buttons([name], bool(value))
        elif etype == EV_ABS:
            if code in _STICK_AXIS:
                stick, index = _STICK_AXIS[code]
                self._sticks[stick][index] = self._scale(code, value, True)
                self._pending = True
            elif code in _TRIGGER_AXIS:
                self.pad.set_trigger(_TRIGGER_AXIS[code],
                                     self._scale(code, value, False))
            elif code in _HAT:
                # Both directions of a hat share one axis, so pressing one and
                # releasing the other would zero what was just pressed: act on
                # the matching direction only, and centre with a single release.
                names = _HAT[code]
                if value in names:
                    self.pad.set_buttons([names[value]], True)
                else:
                    self.pad.set_buttons([next(iter(names.values()))], False)

    def _flush(self) -> None:
        if not self._pending:
            return
        self._pending = False
        for stick, (x, y) in self._sticks.items():
            self.pad.set_stick(stick, x, y)

    def _run(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, TypeError):
                break
            if not ready:
                continue
            try:
                data = os.read(self._fd, _EVENT_SIZE * 64)
            except BlockingIOError:
                continue
            except OSError as exc:
                log.warning("gamepad proxy: %s went away (%s)", self.device, exc)
                break
            for off in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                _s, _us, etype, code, value = struct.unpack_from(
                    _EVENT_FMT, data, off)
                self.events += 1
                if etype == EV_SYN and code == SYN_REPORT:
                    self._flush()
                else:
                    try:
                        self._apply(etype, code, value)
                    except Exception as exc:         # one bad event is not fatal
                        log.debug("gamepad proxy: %s", exc)
            self._flush()
