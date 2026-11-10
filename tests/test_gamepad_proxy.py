#!/usr/bin/env python3
"""
test_gamepad_proxy.py — a real controller mirrored onto the box's pad.

The proxy reads a real pad's evdev node and replays it on the virtual one. A
test cannot press a real pad, but it does not need to: a second VirtualGamepad
*is* a real evdev node as far as the proxy is concerned. Pad A stands in for
the controller in your hands, pad B is the box's, and the proxy joins them.

That chaining found the d-pad bug this module now guards: both directions of a
hat share one axis, so pressing one and releasing the other zeroed what had
just been pressed.

Skipped when what it needs is missing: write access to /dev/uinput.

Run:
    python -m pytest tests/test_gamepad_proxy.py -v
"""

from __future__ import annotations

import os
import time

import pytest

from wbox.gamepad import VirtualGamepad, uinput_problem
from wbox.gamepad_proxy import GamepadProxy, find_pad

pytestmark = pytest.mark.skipif(uinput_problem() is not None,
                                reason=uinput_problem() or "")

SETTLE = 0.4          # the proxy forwards in a thread


def _wait_readable(node: str, seconds: float = 3.0) -> bool:
    """udev sets the permissions of a fresh evdev node a moment after it appears."""
    end = time.time() + seconds
    while time.time() < end:
        if os.access(node, os.R_OK):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def pads():
    """Pad A (the stand-in for a real controller), pad B (the box's), joined."""
    a = VirtualGamepad("xbox360").open()
    b = VirtualGamepad("xbox360").open()
    if not _wait_readable(a.event_node):
        a.close()
        b.close()
        pytest.skip(f"{a.event_node} is not readable here: a udev rule gives "
                    "input devices to the input group")
    proxy = GamepadProxy(b, a.event_node)
    proxy.start()
    time.sleep(SETTLE)
    try:
        yield a, b, proxy
    finally:
        proxy.stop()
        b.close()
        a.close()


def test_find_pad_skips_the_excluded_node():
    pad = VirtualGamepad("xbox360").open()
    try:
        assert find_pad() is not None, "the pad just made should be findable"
        other = find_pad(exclude=pad.event_node)
        assert other != pad.event_node
    finally:
        pad.close()


def test_button_press_and_release(pads):
    a, b, _ = pads
    a.set_buttons(["a"], True)
    time.sleep(SETTLE)
    assert b.state()["buttons"] == ["a"]
    a.set_buttons(["a"], False)
    time.sleep(SETTLE)
    assert b.state()["buttons"] == []


def test_several_buttons_at_once(pads):
    a, b, _ = pads
    a.set_buttons(["b", "start"], True)
    time.sleep(SETTLE)
    assert sorted(b.state()["buttons"]) == ["b", "start"]


def test_dpad_direction_survives_its_opposite(pads):
    """The hat regression: left must stay down, not be zeroed by right's release."""
    a, b, _ = pads
    a.set_buttons(["left"], True)
    time.sleep(SETTLE)
    assert b.state()["buttons"] == ["left"]
    a.set_buttons(["left"], False)
    time.sleep(SETTLE)
    assert b.state()["buttons"] == []


def test_stick_and_trigger(pads):
    a, b, _ = pads
    a.set_stick("left", 1.0, -1.0)
    a.set_trigger("rt", 1.0)
    time.sleep(SETTLE)
    st = b.state()
    assert st["left"][0] > 0.9 and st["left"][1] < -0.9
    assert st["rt"] > 0.9


def test_stop_counts_what_it_forwarded_and_leaves_the_pad_neutral(pads):
    a, b, proxy = pads
    a.set_buttons(["y"], True)
    time.sleep(SETTLE)
    result = proxy.stop()
    assert result["events"] > 0
    assert b.state()["buttons"] == []
