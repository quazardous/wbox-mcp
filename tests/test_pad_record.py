#!/usr/bin/env python3
"""
test_pad_record.py — a session recorded as a pad_script, and played again (#17).

Two parts:

- the script syntax for analog values and the events → script conversion,
  which need no pad and always run;
- round trips on real virtual pads: record a pad while it is driven (by a
  script, or through pad_proxy from a second pad standing in for the
  controller in your hands), replay the recording on a fresh pad, record that
  too, and compare. These need write access to /dev/uinput.

Run:
    python -m pytest tests/test_pad_record.py -v
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from wbox.gamepad import (ABS_HAT0X, AXES, BUTTONS, EV_ABS, EV_KEY, STICK_MAX,  # noqa: E402
                          parse_pad_script, uinput_problem)
from wbox.gamepad_record import events_to_script  # noqa: E402


# ── The syntax: analog values ───────────────────────────────────────


def test_axis_values_parse():
    steps = parse_pad_script("1:lx=-0.42+ly=0.3,2:lt=0.6,3:a")
    assert steps[0] == (1.0, [("axis", "lx", -0.42), ("axis", "ly", 0.3)], 200)
    assert steps[1] == (2.0, [("axis", "lt", 0.6)], 200)
    assert steps[2] == (3.0, [("button", "a")], 200)


@pytest.mark.parametrize("bad,why", [
    ("1:lx=1.5", "goes from -1 to 1"),
    ("1:rt=-0.1", "goes from 0 to 1"),
    ("1:lx=0.5/300", "holds until the next one"),
    ("1:zz=0.1", "unknown axis"),
    ("1:lx=abc", "not a number"),
])
def test_axis_values_refused(bad, why):
    with pytest.raises(ValueError, match=why):
        parse_pad_script(bad)


def test_old_scripts_still_parse():
    """Everything pad_script took before keeps its meaning."""
    steps = parse_pad_script("0.5:a,1:lup/1500,3:lt+rb")
    assert [s[2] for s in steps] == [200, 1500, 200]
    assert steps[1][1] == [("stick", "left", 0, -1)]
    assert steps[2][1] == [("trigger", "lt"), ("button", "rb")]


# ── Events → script ─────────────────────────────────────────────────


def _key(t, name, down):
    return (t, EV_KEY, BUTTONS[name], 1 if down else 0)


def _axis(t, name, value, tmax=255):
    scale = tmax if name in ("lt", "rt") else STICK_MAX
    return (t, EV_ABS, AXES[name], round(value * scale))


def test_presses_keep_their_measured_hold():
    script, info = events_to_script(
        [_key(0.5, "a", True), _key(0.68, "a", False),
         _key(1.0, "b", True), _key(1.05, "x", True),
         _key(1.25, "b", False), _key(1.30, "x", False)], 2.0, 255)
    assert script == "0.500:a/180,1.000:b/250,1.050:x/250"
    assert info["presses"] == 3 and info["first_input_at"] == 0.5


def test_dpad_from_the_hat():
    script, _ = events_to_script(
        [(1.0, EV_ABS, ABS_HAT0X, -1), (1.3, EV_ABS, ABS_HAT0X, 1),
         (1.5, EV_ABS, ABS_HAT0X, 0)], 2.0, 255)
    assert script == "1.000:left/300,1.300:right/200"


def test_a_button_held_at_the_end_is_released_there():
    script, _ = events_to_script([_key(1.0, "a", True)], 3.0, 255)
    assert script == "1.000:a/2000"


def test_a_stick_sweep_is_sampled_and_ends_exactly():
    ev = [_axis(2.0 + i * 0.004, "lx", min(1.0, i / 125)) for i in range(126)]
    ev.append(_axis(3.5, "lx", 0.0))
    script, info = events_to_script(ev, 4.0, 255, rate=30)
    values = [p.split(":")[1] for p in script.split(",")]
    times = [float(p.split(":")[0]) for p in script.split(",")]
    # ~30 a second over the 0.5 s sweep, not 126
    assert 12 <= len(values) <= 20, script
    # samples are a period apart; the value the sweep ended on keeps its own
    # time (it may come sooner), and so does the return to rest
    sampled = times[:-2]
    assert all(b - a >= 1 / 30 - 1e-6 for a, b in zip(sampled, sampled[1:]))
    assert values[-2] == "lx=1", "the end of the sweep is kept"
    assert script.endswith("3.500:lx=0"), "the return to rest is exact"


def test_a_value_held_after_a_fast_move_keeps_its_time():
    """The last value of a burst is written when it happened, not at the end."""
    ev = [_axis(1.0, "ly", -0.5), _axis(1.01, "ly", -0.8), _axis(5.0, "ly", 0.0)]
    script, _ = events_to_script(ev, 6.0, 255, rate=30)
    assert script == "1.000:ly=-0.5,1.010:ly=-0.8,5.000:ly=0"


def test_the_replay_ends_at_rest():
    script, _ = events_to_script([_axis(1.0, "rt", 0.5, tmax=1023)], 2.0, 1023)
    assert script == "1.000:rt=0.5,2.000:rt=0"


def test_the_script_parses_back():
    ev = [_key(0.5, "a", True), _key(0.7, "a", False), _axis(1.0, "lx", 0.33),
          _axis(1.0, "ly", -0.5), _axis(2.0, "lx", 0.0), _axis(2.0, "ly", 0.0)]
    script, _ = events_to_script(ev, 3.0, 255)
    assert script == "0.500:a/200,1.000:lx=0.33+ly=-0.5,2.000:lx=0+ly=0"
    parse_pad_script(script)


# ── Round trips on virtual pads ─────────────────────────────────────

needs_uinput = pytest.mark.skipif(uinput_problem() is not None,
                                  reason=uinput_problem() or "")


def _wait_readable(node: str, seconds: float = 3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if os.access(node, os.R_OK):
            return True
        time.sleep(0.1)
    return False


def _steps(script):
    """(time, inputs) of a script, for comparing two recordings."""
    return [(float(p.split(":")[0]), p.split(":", 1)[1]) for p in script.split(",")]


def _by_input(script):
    """input name → [(time, value or hold ms)]: two recordings group inputs
    into steps a little differently (each axis is its own event), so they are
    compared input by input."""
    out = {}
    for t, inputs in _steps(script):
        body, _, ms = inputs.partition("/")
        for token in body.split("+"):
            name, _, value = token.partition("=")
            out.setdefault(name, []).append((t, float(value) if value else int(ms or 200)))
    return out


def _same_run(a: str, b: str, tolerance: float = 0.03):
    ia, ib = _by_input(a), _by_input(b)
    assert sorted(ia) == sorted(ib), (a, b)
    for name in ia:
        assert len(ia[name]) == len(ib[name]), (name, a, b)
        for (ta, va), (tb, vb) in zip(ia[name], ib[name]):
            assert abs(ta - tb) <= tolerance, (name, ta, tb)
            if isinstance(va, float):
                assert abs(va - vb) <= 0.011, (name, va, vb)
            else:
                assert abs(va - vb) <= tolerance * 1000, (name, va, vb)


@needs_uinput
def test_record_a_script_and_play_the_recording():
    from wbox.gamepad import VirtualGamepad, play_script
    from wbox.gamepad_record import PadRecorder
    original = "0.200:a/150,0.500:lx=0.5+ly=-0.25,0.800:lx=0+ly=0,1.000:up/120,1.200:rt=0.8,1.400:rt=0"
    first = VirtualGamepad("xbox360").open()
    second = VirtualGamepad("xbox360").open()
    try:
        rec = PadRecorder(first).start()
        play_script(first, parse_pad_script(original))
        rec.stop()
        recorded, _ = rec.script()
        _same_run(original, recorded)

        rec2 = PadRecorder(second).start()
        play_script(second, parse_pad_script(recorded))
        rec2.stop()
        replayed, _ = rec2.script()
        _same_run(recorded, replayed)
    finally:
        first.close()
        second.close()


@needs_uinput
def test_record_through_the_proxy():
    """The real use: a controller in your hands, proxied, recorded.

    Pad A stands in for the controller; the proxy mirrors it onto pad B, the
    box's; the recorder listens to B."""
    from wbox.gamepad import VirtualGamepad
    from wbox.gamepad_proxy import GamepadProxy
    from wbox.gamepad_record import PadRecorder
    a = VirtualGamepad("xbox360").open()
    b = VirtualGamepad("xbox360").open()
    if not _wait_readable(a.event_node):
        a.close(); b.close()
        pytest.fail(f"{a.event_node} is not readable: a udev rule gives input "
                    "devices to the input group")
    proxy = GamepadProxy(b, a.event_node)
    proxy.start()
    time.sleep(0.4)
    try:
        rec = PadRecorder(b).start()
        a.set_buttons(["a"], True); time.sleep(0.2); a.set_buttons(["a"], False)
        time.sleep(0.2)
        a.set_stick("left", 0.6, 0); time.sleep(0.3); a.set_stick("left", 0, 0)
        time.sleep(0.2)
        rec.stop()
        script, info = rec.script()
    finally:
        proxy.stop()
        a.close()
        b.close()
    names = [s[1].split("/")[0] for s in _steps(script)]
    assert names[0] == "a", script
    assert any(n.startswith("lx=0.6") for n in names), script
    assert "lx=0" in names, script
    a_hold = int(_steps(script)[0][1].split("/")[1])
    assert 170 <= a_hold <= 260, script


# ── The tools and the config key ────────────────────────────────────


def test_the_tools_are_listed_and_answer_without_a_pad():
    from wbox.server import _PAD_TOOLS, _call_pad, build_compositor
    names = {t.name for t in _PAD_TOOLS}
    assert {"pad_proxy", "pad_proxy_stop", "pad_record_start", "pad_record_stop"} <= names
    comp = build_compositor({"compositor": "labwc", "headless": True})
    for tool in ("pad_proxy", "pad_record_start"):
        r = _call_pad(comp, tool, {})
        assert "no gamepad" in r["error"], r
    assert "not recording" in _call_pad(comp, "pad_record_stop", {})["error"]
    assert "give script or file" in _call_pad(comp, "pad_script", {})["error"]


def test_pad_script_reads_a_recording(tmp_path):
    from wbox.server import _call_pad, build_compositor
    comp = build_compositor({"compositor": "labwc", "headless": True})
    f = tmp_path / "run.padscript"
    f.write_text("0.5:a/100,1:lx=0.5,2:lx=0\n")
    # no pad in this box: the file was read, and the pad is what is missing
    assert "no gamepad" in _call_pad(comp, "pad_script", {"file": str(f)})["error"]
    assert "cannot read" in _call_pad(comp, "pad_script",
                                      {"file": str(tmp_path / "nope")})["error"]


def test_proxy_config_key():
    from wbox.server import build_compositor
    comp = build_compositor({"compositor": "labwc", "headless": True,
                             "gamepad": {"profile": "xbox360", "proxy": "auto",
                                         "grab": True}})
    assert comp._gamepad_proxy_settings() == ("auto", True)
    plain = build_compositor({"compositor": "labwc", "headless": True,
                              "gamepad": "xbox360"})
    assert plain._gamepad_proxy_settings() is None


def test_a_scenario_replays_a_recording(tmp_path):
    from wbox.scenario import ScenarioError, load_scenario
    (tmp_path / "walk.padscript").write_text("0.5:a/100,1:lx=0.5,2:lx=0\n")
    sc_file = tmp_path / "s.yaml"
    sc_file.write_text("steps:\n  - pad_script: {file: walk.padscript}\n")
    sc = load_scenario(sc_file)
    assert sc["steps"][0]["args"]["script"] == "0.5:a/100,1:lx=0.5,2:lx=0"
    digest = sc["_digest"]
    (tmp_path / "walk.padscript").write_text("0.5:b/100\n")
    assert load_scenario(sc_file)["_digest"] != digest, "the recording is part of the run"
    (tmp_path / "walk.padscript").write_text("0.5:lx=7\n")
    with pytest.raises(ScenarioError, match="goes from -1 to 1"):
        load_scenario(sc_file)
    sc_file.write_text("steps:\n  - pad_script: {file: nope.padscript}\n")
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(sc_file)


@needs_uinput
def test_the_proxy_starts_at_launch_or_the_launch_fails(tmp_path):
    """`gamepad: {proxy: ...}` in a real box: mirrored from the start, and a
    controller that is not there is a failed launch, not an ignored key."""
    import shutil
    if not shutil.which("labwc"):
        pytest.fail("labwc is not installed")
    from wbox.gamepad import VirtualGamepad
    from wbox.server import build_compositor
    hand = VirtualGamepad("xbox360").open()          # the controller in your hands
    if not _wait_readable(hand.event_node):
        hand.close()
        pytest.fail(f"{hand.event_node} is not readable")
    cfg = {"compositor": "labwc", "headless": True, "name": "test-pad-proxy-launch",
           "timeouts": {"app_render": 0}, "audio": "host"}
    box = build_compositor({**cfg, "gamepad": {"profile": "xbox360", "isolate": False,
                                                "proxy": hand.event_node}})
    box.state.screenshot_dir = tmp_path
    try:
        r = box.launch(["sleep", "30"], {})
        assert "error" not in r, r
        assert box._pad_proxy is not None and box._pad_proxy.status()["running"]
        hand.set_buttons(["y"], True)
        time.sleep(0.4)
        assert "y" in box.pad_state()["buttons"]
        hand.set_buttons(["y"], False)
    finally:
        box.kill(aggressive=True)
        hand.close()
    missing = build_compositor({**cfg, "gamepad": {"isolate": False,
                                                    "proxy": "/dev/input/event-none"}})
    try:
        r = missing.launch(["sleep", "30"], {})
        assert "error" in r and "gamepad proxy" in r["error"], r
        assert not missing.is_running()
    finally:
        missing.kill(aggressive=True)
