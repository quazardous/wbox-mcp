"""
test_windows.py — the win32 backend, end to end, on the crash dummy.

Two places the app can run, the same tests for both:

- **sandbox**: in Windows Sandbox (`sandbox:` in config.yaml). Nothing
  touches your desktop. Runs when Windows Sandbox is turned on and not
  already in use; it boots once for the session (under a minute).
- **desktop**: on your desktop, the way the win32 backend works without
  `sandbox:`. Clicks move your real pointer and keys go to the front
  window, so it only runs when asked: WBOX_TEST_DESKTOP=1.

Run:
    python -m pytest tests/test_windows.py -v
    WBOX_TEST_DESKTOP=1 python -m pytest tests/test_windows.py -v -k desktop
"""

from __future__ import annotations

import json
import re
import struct
import sys
import time
from pathlib import Path

import pytest

from win_harness import TITLE, TOLERANCE, _launched

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

@pytest.fixture(scope="module")
def harness(place):
    h = _launched(place)
    yield h
    h.kill()


@pytest.fixture(autouse=True)
def _fresh_log(request):
    if "harness" in request.fixturenames:
        request.getfixturevalue("harness").mark()


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", head[16:24])


class TestLaunch:
    def test_the_app_window_is_found(self, harness):
        windows = harness.comp.list_windows()
        assert "error" not in windows, windows
        assert harness.comp.is_running()

    def test_screenshot_is_the_app_window(self, harness):
        shot = harness.comp.screenshot("launch")
        assert "error" not in shot, shot
        path = Path(shot["path"])
        assert path.exists()
        width, height = _png_size(path)
        size = harness.comp.get_size()
        assert (width, height) == (size["window_width"], size["window_height"]), (shot, size)


class TestMouse:
    POINTS = [(200, 200), (500, 350), (120, 420)]

    def test_clicks_land_where_asked(self, harness):
        """click(x, y) is window-relative (screenshot pixels): the app reads
        it at the window's corner plus (x, y), in screen coordinates."""
        size = harness.comp.get_size()
        for x, y in self.POINTS:
            result = harness.comp.click(x, y)
            assert "error" not in result, result
            time.sleep(0.3)
        clicks = harness.lines("click left")
        assert len(clicks) >= len(self.POINTS), clicks
        for (x, y), line in zip(self.POINTS, clicks[-len(self.POINTS):]):
            m = re.search(r"root=\((-?\d+),(-?\d+)\)", line)
            assert m, line
            rx, ry = int(m.group(1)), int(m.group(2))
            ex, ey = size["x"] + x, size["y"] + y
            assert abs(rx - ex) <= TOLERANCE and abs(ry - ey) <= TOLERANCE, (
                f"click({x},{y}) landed at ({rx},{ry}), expected ({ex},{ey})")


class TestKeyboard:
    def test_typed_text_arrives(self, harness):
        harness.comp.click(400, 280)
        time.sleep(0.3)
        result = harness.comp.type_text("abc123")
        assert "error" not in result, result
        time.sleep(0.5)
        received = "".join(m.group(1) for l in harness.lines("key ")
                           if (m := re.search(r"\('(.)'", l)))
        assert "abc123" in received, received

    def test_a_shortcut_carries_its_modifier(self, harness):
        harness.comp.click(400, 280)
        time.sleep(0.2)
        result = harness.comp.key("ctrl+a")
        assert "error" not in result, result
        time.sleep(0.3)
        assert [l for l in harness.lines("key ") if "Ctrl" in l], harness.lines("key ")


class TestCommands:
    def test_the_app_answers_through_its_command_file(self, harness):
        harness.send("ping")
        assert harness.wait_line("pong"), harness.lines()

    def test_the_layout_dump(self, harness):
        harness.send("dump")
        line = harness.wait_line("DUMP ")
        assert line, harness.lines()
        dump = json.loads(line.split("DUMP ", 1)[1])
        assert dump, "empty dump"


class TestGestures:
    """Wheel, drag, double click and a held modifier — each asserting what
    makes the gesture worth having, not that the call returned ok (as
    test_integration.py's TestGestures does on Linux)."""

    def test_the_wheel_turns_both_ways(self, harness):
        assert "error" not in harness.comp.scroll(400, 300, 2)
        assert "error" not in harness.comp.scroll(400, 300, -1)
        time.sleep(0.4)
        deltas = [int(re.search(r"delta=(-?\d+)", l).group(1)) for l in harness.lines("wheel delta=")]
        # Down is negative for Windows, up positive: wbox's negative is up.
        assert deltas == [-120, -120, 120], deltas

    def test_drag_travels_instead_of_teleporting(self, harness):
        harness.mark()
        assert "error" not in harness.comp.drag(300, 250, 500, 350, steps=10, seconds=0.3)
        time.sleep(0.4)
        stepped = len(harness.lines("motion"))
        harness.mark()
        assert "error" not in harness.comp.drag(300, 250, 500, 350, steps=1, seconds=0.0)
        time.sleep(0.4)
        instant = len(harness.lines("motion"))
        assert stepped > instant, f"stepped {stepped} motion events, instant {instant}"

    def test_double_click_reads_as_one_gesture(self, harness):
        assert "error" not in harness.comp.double_click(400, 300)
        time.sleep(0.5)
        assert harness.lines("dblclick"), harness.lines()

    def test_hold_keeps_the_modifier_down(self, harness):
        r = harness.comp.hold(["ctrl"], [{"type": "key", "key": "tab"}, {"type": "key", "key": "tab"}])
        assert "error" not in r, r
        time.sleep(0.5)
        taps = harness.lines("key Tab")
        assert taps, harness.lines()
        assert all("Ctrl" in l for l in taps), taps

    def test_hold_releases_when_an_action_fails(self, harness):
        assert "error" in harness.comp.hold(["ctrl"], [{"type": "nonsense"}])
        harness.mark()
        assert "error" not in harness.comp.key("a")
        time.sleep(0.4)
        lines = harness.lines("key ")
        assert lines and not any("Ctrl" in l for l in lines), lines


class TestWindows:
    """list_windows and focus_window: which window has the focus, readable,
    and settable."""

    def test_the_app_window_is_listed_and_active(self, harness):
        assert "error" not in harness.comp.click(400, 280)
        windows = harness.comp.list_windows()["windows"]
        mine = [w for w in windows if w["title"] == TITLE]
        assert mine, windows
        assert mine[0]["activated"] is True, windows
        assert mine[0]["minimized"] is False

    def test_focus_moves_between_the_apps_windows(self, harness):
        harness.send("open_popup")
        assert harness.wait_line("popup_opened"), harness.lines()
        time.sleep(0.5)
        try:
            titles = [w["title"] for w in harness.comp.list_windows()["windows"]]
            assert "crash dummy popup" in titles, titles
            assert "error" not in harness.comp.focus_window(title="popup")
            active = [w["title"] for w in harness.comp.list_windows()["windows"] if w["activated"]]
            assert active == ["crash dummy popup"], active
            assert "error" not in harness.comp.focus_window(title=TITLE)
            active = [w["title"] for w in harness.comp.list_windows()["windows"] if w["activated"]]
            assert active == [TITLE], active
        finally:
            harness.send("close_popup")
            time.sleep(0.3)
            harness.comp.focus_window(title=TITLE)


class TestRecord:
    """record: frames in a loop, and a summary that says which changed —
    silence and noise both asserted, as test_integration.py does on Linux:
    a detector that always says "something moved" detects nothing."""

    def test_a_still_window_films_without_change(self, harness):
        # Nothing moves: the pointer away from the window's hover states.
        harness.comp.mouse_move(5, 5)
        time.sleep(0.5)
        r = harness.comp.record(1.5, name="still")
        assert "error" not in r, r
        assert r["frames"] >= 5, r
        assert r["changes"] == 0, r

    def test_a_drag_filmed_shows_change(self, harness):
        r = harness.comp.record(2.0, name="drag", during={
            "type": "drag", "x1": 200, "y1": 250, "x2": 600, "y2": 400, "steps": 20, "seconds": 1.0})
        assert "error" not in r, r
        assert "during_error" not in r, r
        assert r["changes"] > 0, r
        assert Path(r["frames_dir"]).is_dir()

    def test_a_region_crops_the_frames(self, harness):
        r = harness.comp.record(0.5, region="10,40 200x100", name="region")
        assert "error" not in r, r
        first = sorted(Path(r["frames_dir"]).glob("frame_*.png"))[0]
        assert _png_size(first) == (200, 100)

    def test_a_region_outside_the_window_is_refused(self, harness):
        r = harness.comp.record(0.3, region="5000,5000 10x10", name="outside")
        assert "outside the window" in r.get("error", ""), r


class TestClipboard:
    def test_write_then_read(self, harness):
        text = f"wbox {harness.where} {time.time_ns()}"
        assert "error" not in harness.comp.clipboard_write(text)
        assert harness.comp.clipboard_read().get("text") == text


class TestIsolation:
    """What the sandbox is for: nothing of the app reaches the host."""

    def test_the_hosts_clipboard_is_untouched(self, harness):
        if harness.where != "sandbox":
            pytest.skip("the desktop shares your clipboard by design")
        import subprocess

        def host_clipboard() -> str:
            return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                                  capture_output=True, text=True, timeout=20).stdout

        before = host_clipboard()
        text = f"sandboxed {time.time_ns()}"
        assert "error" not in harness.comp.clipboard_write(text)
        after = host_clipboard()
        assert text not in after
        assert before == after

    def test_the_app_does_not_run_on_the_host(self, harness):
        if harness.where != "sandbox":
            pytest.skip("on the desktop it does")
        from wbox.compositor.win32 import find_windows_by_title

        assert find_windows_by_title(TITLE) == []
