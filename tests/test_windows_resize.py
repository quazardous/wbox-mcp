"""
test_windows_resize.py — `resize` on the win32 backend (Windows only).

Its own module: it needs the crash dummy resizable (`normal` mode), and the
backend drives one app at a time — test_windows.py's stays up for its whole
module.

Run:
    python -m pytest tests/test_windows_resize.py -v
"""

from __future__ import annotations

import sys

import pytest

from win_harness import _launched

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture(scope="module")
def resizable(place):
    h = _launched(place, "crash", "permonitor", mode="normal")
    yield h
    h.kill()


class TestResize:
    @pytest.mark.parametrize("width,height", [(900, 650), (700, 500), (1024, 700)])
    def test_the_client_area_gets_the_size_asked(self, resizable, width, height):
        """Under display scaling too: the frame was computed at the wrong
        DPI, and came out a couple of pixels off (900×650 → 898×648)."""
        result = resizable.comp.resize(width, height)
        assert "error" not in result, result
        size = resizable.comp.get_size()
        assert (size["client_width"], size["client_height"]) == (width, height), (result, size)
