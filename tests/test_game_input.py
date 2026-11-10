#!/usr/bin/env python3
"""
test_game_input.py — wbox driving a game: held keys and the virtual pad.

A game is not a form. It reads the keyboard's state once a frame, so a press
and release sent together are never seen; and it reads controllers through
SDL, not through Wayland. tests/game-dummy/game_dummy.py reads its input that
way and reports what it saw.

Three layers, each skipped when what it needs is missing:
    - parsing and ioctl numbers: always
    - held keys in a box: labwc and pygame (pygame-ce) in the test Python
    - the virtual pad: the above, plus write access to /dev/uinput and bwrap

Run:
    python -m pytest tests/test_game_input.py -v
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from wbox import gamepad as gp  # noqa: E402
from wbox.pointer import _parse_shortcut, _MODIFIERS  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="uinput and the Linux compositors: not on Windows")

GAME = Path(__file__).parent / "game-dummy" / "game_dummy.py"


# ── Without a box ────────────────────────────────────────────────────


class TestParsing:
    def test_ioctl_numbers_match_linux_uinput_h(self):
        # The values the kernel headers give on x86-64: a wrong size in a
        # struct format would change the number and the ioctl would fail
        # with EINVAL, or worse, write past the buffer.
        assert gp.UI_DEV_SETUP == 0x405c5503
        assert gp.UI_ABS_SETUP == 0x401c5504
        assert gp.UI_SET_EVBIT == 0x40045564
        assert gp.UI_SET_KEYBIT == 0x40045565
        assert gp.UI_SET_ABSBIT == 0x40045567
        assert gp.UI_SET_FFBIT == 0x4004556b
        assert gp.UI_DEV_CREATE == 0x5501
        assert gp.UI_DEV_DESTROY == 0x5502
        assert gp.UI_BEGIN_FF_UPLOAD == 0xc06855c8
        assert gp.UI_END_FF_UPLOAD == 0x406855c9
        assert gp.UI_BEGIN_FF_ERASE == 0xc00c55ca
        assert gp.UI_END_FF_ERASE == 0x400c55cb
        assert gp._ui_get_sysname(64) == 0x8040552c
        assert struct.calcsize(gp._EVENT_FMT) == 24

    def test_script_syntax(self):
        steps = gp.parse_pad_script("0.5:A,2:LUP/1500,3:ZL+R,4:plus")
        assert steps == [
            (0.5, [("button", "a")], 200),
            (2.0, [("stick", "left", 0, -1)], 1500),
            (3.0, [("trigger", "zl"), ("button", "r")], 200),
            (4.0, [("button", "plus")], 200),
        ]

    @pytest.mark.parametrize("bad", ["x:A", "1:NOPE", "1:A/zz", "", "1", "-1:A"])
    def test_script_errors_name_the_step(self, bad):
        with pytest.raises(ValueError):
            gp.parse_pad_script(bad)

    def test_button_aliases(self):
        assert gp._parse_button("plus") == gp._parse_button("start")
        assert gp._parse_button("L") == gp._parse_button("lb")
        assert gp._parse_button("dpad_up") == ("hat", gp.ABS_HAT0Y, -1)
        with pytest.raises(ValueError):
            gp._parse_button("turbo")

    def test_a_modifier_alone_is_a_key_with_its_mask(self):
        # "shift" to run: the key event alone would leave the compositor's
        # modifier state without shift.
        mask, code = _MODIFIERS["shift"]
        assert _parse_shortcut("shift") == (code, mask, [])


# ── In a box ─────────────────────────────────────────────────────────


def _need_box():
    if not shutil.which("labwc"):
        pytest.skip("labwc not installed")
    if importlib.util.find_spec("pygame") is None:
        pytest.skip("pygame not installed in the test Python "
                    "(pip install pygame-ce)")


def _need_pad():
    _need_box()
    problem = gp.uinput_problem()
    if problem:
        pytest.skip(problem)
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")


class Game:
    """game_dummy in a headless labwc box."""

    def __init__(self, tmp: Path, extra_cfg: dict | None = None, rumble_ms: int = 0):
        from wbox.server import build_compositor
        cfg = {"compositor": "labwc", "headless": True, "screen": "800x480",
               "keyboard_layout": "us", "audio": "null"}
        cfg.update(extra_cfg or {})
        self.box = build_compositor(cfg)
        self.box.state.screenshot_dir = tmp
        self.state_file = tmp / "game_state.json"
        self.launch = self.box.launch(
            [sys.executable, str(GAME), str(self.state_file), str(rumble_ms)], {})
        assert "error" not in self.launch, self.launch
        self.wait(lambda s: s["frames"] > 30, "the game to draw")

    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def wait(self, cond, what: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.state()
            if s and cond(s):
                return s
            time.sleep(0.05)
        pytest.fail(f"timed out waiting for {what}; last state: {self.state()}")

    def settle(self) -> dict:
        """The state once a few more frames have been drawn."""
        frames = self.state().get("frames", 0)
        return self.wait(lambda s: s["frames"] >= frames + 10, "a few frames")

    def stop(self):
        self.box.stop()


@pytest.fixture
def game(tmp_path):
    _need_box()
    g = Game(tmp_path)
    yield g
    g.stop()


class TestHeldKeys:
    def test_a_tap_is_never_seen_down_by_a_polling_game(self, game):
        # Why hold_ms exists: the events arrive, the state never shows the key.
        for _ in range(5):
            assert "error" not in game.box.key("a")
            time.sleep(0.1)
        s = game.settle()
        assert s["keydown_events"]["a"] == 5
        assert s["keys"]["a"]["presses"] == 0

    def test_hold_ms_makes_every_press_count(self, game):
        for _ in range(5):
            assert "error" not in game.box.key("a", hold_ms=50)
            time.sleep(0.1)
        s = game.settle()
        assert s["keys"]["a"]["presses"] == 5

    def test_key_hold_ms_applies_to_keys(self, game):
        game.box.key_hold_ms = 50
        assert game.box.keys(["a", "a", "a"], delay_ms=100)["sent"] == 3
        assert game.settle()["keys"]["a"]["presses"] == 3

    def test_press_holds_keys_together(self, game):
        r = game.box.press(["shift", "w"], 500)
        assert r.get("ok"), r
        s = game.settle()
        # 500 ms at 60 fps is 30 frames: allow for scheduling at both ends
        assert s["together"] >= 20
        assert s["keys"]["w"]["presses"] == 1

    def test_key_down_survives_other_keys_until_key_up(self, game):
        assert game.box.key_down(["w"])["held"] == ["w"]
        time.sleep(0.3)
        assert game.box.key("a", hold_ms=50).get("ok")
        time.sleep(0.2)
        assert game.box.key_up()["held"] == []
        s = game.settle()
        assert s["keys"]["w"]["presses"] == 1          # one press, not two
        assert s["keys"]["w"]["held_frames"] >= 20     # held across the tap
        assert s["keys"]["a"]["presses"] == 1

    def test_press_leaves_keys_held_before_it_alone(self, game):
        game.box.key_down(["w"])
        game.box.press(["w", "a"], 50)
        assert game.box._vptr_client().held_keys() == ["w"]
        game.box.key_up()


@pytest.fixture
def pad_game(tmp_path):
    _need_pad()
    g = Game(tmp_path, {"gamepad": "xbox360"}, rumble_ms=6000)
    yield g
    g.stop()


class TestGamepad:
    def test_sdl_sees_one_xbox_pad_and_nothing_else(self, pad_game):
        s = pad_game.wait(lambda s: s["pads"], "SDL to open the pad")
        assert s["pads"] == ["X360 Controller"]
        node = pad_game.launch["gamepad"]["node"]
        assert s["dev_input"] == [Path(node).name], "the box leaked host devices"

    def test_buttons_and_dpad(self, pad_game):
        pad_game.wait(lambda s: s["pads"], "SDL to open the pad")
        assert pad_game.box.pad_press(["a"], 100).get("ok")
        assert pad_game.box.pad_press(["b", "x", "up"], 100).get("ok")
        s = pad_game.settle()
        for name in ("a", "b", "x", "up"):
            assert s["buttons"].get(name) == 1, (name, s["buttons"])

    def test_sticks_and_triggers_reach_their_ends(self, pad_game):
        pad_game.wait(lambda s: s["pads"], "SDL to open the pad")
        pad_game.box.pad_stick("left", 0, -1, hold_ms=100)
        pad_game.box.pad_trigger("rt", 1.0, hold_ms=100)
        s = pad_game.settle()
        assert s["axes"]["lefty"] >= 32767
        assert s["axes"]["rt"] >= 32767
        st = pad_game.box.pad_state()
        assert st["left"] == [0.0, 0.0] and st["rt"] == 0.0   # recentred

    def test_script_plays_on_time(self, pad_game):
        pad_game.wait(lambda s: s["pads"], "SDL to open the pad")
        r = pad_game.box.pad_script("0:y,0.3:lb/100,0.6:start")
        assert r.get("ok"), r
        assert r["max_late_ms"] < 20
        s = pad_game.settle()
        for name in ("y", "lb", "start"):
            assert s["buttons"].get(name) == 1, (name, s["buttons"])
        assert pad_game.box.pad_state()["buttons"] == []

    def test_rumble_the_game_asked_for(self, pad_game):
        s = pad_game.wait(lambda s: s["rumble_ok"] is not None, "the rumble call")
        assert s["rumble_ok"]
        r = pad_game.box.pad_rumble()
        assert r["playing"]
        assert r["strong"] == pytest.approx(0.5, abs=0.01)
        assert r["weak"] == pytest.approx(1.0, abs=0.01)

    def test_no_pad_without_the_setting(self, game):
        assert "gamepad" not in game.launch
        assert "error" in game.box.pad_press(["a"])
