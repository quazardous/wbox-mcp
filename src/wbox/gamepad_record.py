"""Record what drives the box's pad, as a pad_script (GitHub #17).

Play to the scene of a bug once, by hand through pad_proxy, and get a
sequence that replays the same way afterwards: across versions, drivers,
compositors. The recorder listens to the virtual pad itself, so it records
whatever drives it — a proxied real controller, a script, the pad tools.

The script it writes is the one pad_script plays:

- buttons and d-pad directions as presses with their measured hold,
  `12.034:a/180`;
- sticks and triggers as values, which hold until the next one for that axis,
  `12.100:lx=-0.42+ly=0.31`. A real stick sends hundreds of events a second:
  they are sampled (at most `rate` a second per axis, changes below `step`
  dropped), and a return to rest is always kept exactly.
"""

from __future__ import annotations

import threading
import time

from wbox.gamepad import (ABS_HAT0X, ABS_HAT0Y, AXES, BUTTONS, EV_ABS, EV_KEY,
                          STICK_MAX, _TRIGGER_AXES)

_BUTTON_BY_CODE = {code: name for name, code in BUTTONS.items()}
_AXIS_BY_CODE = {code: name for name, code in AXES.items()}
_HAT_NAMES = {ABS_HAT0X: {-1: "left", 1: "right"}, ABS_HAT0Y: {-1: "up", 1: "down"}}


class PadRecorder:
    """Listens to a VirtualGamepad from start() to stop()."""

    def __init__(self, pad):
        self.pad = pad
        self.events: list[tuple[float, int, int, int]] = []   # (t, type, code, value)
        self._t0 = 0.0
        self._lock = threading.Lock()
        self.stopped_at: float | None = None

    def start(self) -> "PadRecorder":
        self._t0 = time.monotonic()
        self.pad.add_listener(self._on_emit)
        return self

    def _on_emit(self, at: float, events) -> None:
        with self._lock:
            for etype, code, value in events:
                self.events.append((at - self._t0, etype, code, value))

    def stop(self) -> float:
        """Stop listening; the recording's length in seconds."""
        self.pad.remove_listener(self._on_emit)
        self.stopped_at = time.monotonic() - self._t0
        return self.stopped_at

    def script(self, rate: float = 30.0, step: float = 0.02) -> tuple[str, dict]:
        with self._lock:
            events = list(self.events)
        end = self.stopped_at if self.stopped_at is not None else time.monotonic() - self._t0
        return events_to_script(events, end, self.pad.trigger_max, rate, step)


def _fmt_t(t: float) -> str:
    return f"{t:.3f}"


def _fmt_v(v: float) -> str:
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def events_to_script(events, end: float, trigger_max: int,
                     rate: float = 30.0, step: float = 0.02) -> tuple[str, dict]:
    """[(t, type, code, value)] → (pad_script, summary).

    Pure: no pad needed, so the conversion is tested on its own.
    """
    period = 1.0 / rate if rate > 0 else 0.0
    presses: list[tuple[float, str, int]] = []    # (start, name, hold ms)
    values: list[tuple[float, str, float]] = []   # (t, axis, value)
    down: dict[str, float] = {}                   # name → pressed at
    hat_dir: dict[int, str] = {}                  # hat axis → direction down
    # per axis: last value written, when, and a value not written yet
    last: dict[str, tuple[float, float]] = {}
    pending: dict[str, tuple[float, float]] = {}

    def press(name: str, t: float) -> None:
        if name not in down:
            down[name] = t

    def release(name: str, t: float) -> None:
        if name in down:
            start = down.pop(name)
            presses.append((start, name, max(1, round((t - start) * 1000))))

    def write(axis: str, t: float, v: float) -> None:
        values.append((t, axis, v))
        last[axis] = (t, v)
        pending.pop(axis, None)

    def on_axis(axis: str, t: float, v: float) -> None:
        lt, lv = last.get(axis, (float("-inf"), 0.0))
        held = pending.get(axis)
        if held and t - held[0] >= period and abs(held[1] - lv) >= step:
            # the stick rested on that value for a while before this move:
            # it is part of the run, at the time it was reached
            write(axis, *held)
            lt, lv = held
        if v == 0.0 and lv != 0.0:                # back to rest: always, exactly
            write(axis, t, 0.0)
        elif abs(v - lv) >= step and t - lt >= period:
            write(axis, t, v)
        elif v != lv:
            pending[axis] = (t, v)
        else:
            pending.pop(axis, None)

    for t, etype, code, value in sorted(events, key=lambda e: e[0]):
        if etype == EV_KEY and code in _BUTTON_BY_CODE:
            name = _BUTTON_BY_CODE[code]
            if value == 1:
                press(name, t)
            elif value == 0:
                release(name, t)
        elif etype == EV_ABS and code in _HAT_NAMES:
            current = hat_dir.pop(code, None)
            if current:
                release(current, t)
            name = _HAT_NAMES[code].get(value)
            if name:
                hat_dir[code] = name
                press(name, t)
        elif etype == EV_ABS and code in _AXIS_BY_CODE:
            axis = _AXIS_BY_CODE[code]
            scale = trigger_max if axis in _TRIGGER_AXES else STICK_MAX
            on_axis(axis, t, round(value / scale, 2))

    for axis, (t, v) in list(pending.items()):    # the last move of a burst
        if abs(v - last.get(axis, (0, 0.0))[1]) > 0:
            write(axis, t, v)
    for name in list(down):                       # still held at the end
        release(name, end)
    rest = [(end, axis, 0.0) for axis, (_, v) in sorted(last.items()) if v != 0.0]
    values.extend(rest)                           # replay ends at rest, as it began

    # One step per instant: values set together, presses with equal holds
    steps: dict[tuple[float, int], list[str]] = {}
    for t, axis, v in values:
        steps.setdefault((round(t, 3), 0), []).append(f"{axis}={_fmt_v(v)}")
    for t, name, ms in presses:
        steps.setdefault((round(t, 3), ms), []).append(name)
    parts = []
    for (t, ms), inputs in sorted(steps.items()):
        parts.append(f"{_fmt_t(t)}:{'+'.join(inputs)}" + (f"/{ms}" if ms else ""))
    script = ",".join(parts)
    summary = {"seconds": round(end, 3), "events": len(events),
               "presses": len(presses), "values": len(values),
               "steps": len(parts),
               "first_input_at": round(min((s[0] for s in steps), default=0.0), 3)}
    return script, summary
