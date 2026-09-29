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
