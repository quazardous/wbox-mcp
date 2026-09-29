"""
test_windows_density.py — the win32 backend at any display density, against
apps of every DPI awareness (Windows only).

Run:
    python -m pytest tests/test_windows_density.py -v
"""

from __future__ import annotations

import re
import sys
import time

import pytest

from win_harness import TOLERANCE, _launched

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


# ── Density ──────────────────────────────────────────────────────────
#
# wbox counts in physical pixels (a screenshot's). An app that is not per-
# monitor DPI aware counts in logical ones: on a display scaled to S, its
# coordinates are the physical ones divided by S. Each test measures S from
# the app itself — its own idea of its size against the physical size wbox
# sees — so nothing assumes a density: the same tests hold at 100 % (CI's
# runner), 150 % (a laptop, and Windows Sandbox on it) or any other.

DPI_MODES = ["unaware", "system", "permonitor"]


def _ints(line: str, key: str) -> tuple[int, ...]:
    """The numbers of `key=(a,b)` in a log line (`key (a,b)` for a key
    ending with a space)."""
    m = re.search(re.escape(key) + ("" if key.endswith(" ") else "=") + r"\(([-\d,]+)\)", line)
    assert m, f"no {key}=(...) in {line!r}"
    return tuple(int(v) for v in m.group(1).split(","))


# One app at a time: the backend drives one, so each test has its own.
@pytest.fixture(params=DPI_MODES)
def tk_app(request, place):
    h = _launched(place, "crash", request.param)
    yield h
    h.kill()


@pytest.fixture(params=DPI_MODES)
def edit_app(request, place):
    h = _launched(place, "edit", request.param)
    yield h
    h.kill()


class TestDensity:
    def test_clicks_by_sendinput_land_in_the_apps_own_coordinates(self, tk_app):
        """Tk ignores posted clicks: wbox clicks it with the real pointer."""
        tk_app.mark()
        tk_app.send("geometry")
        line = tk_app.wait_line("geometry ")
        assert line, tk_app.all_lines()
        logical_width, _ = _ints(line, "window_size")
        size = tk_app.comp.get_size()
        scale = size["client_width"] / logical_width
        points = [(200, 200), (500, 350), (120, 420)]
        for x, y in points:
            assert "error" not in tk_app.comp.click(x, y)
            time.sleep(0.3)
        clicks = tk_app.lines("click left")[-len(points):]
        assert len(clicks) == len(points), tk_app.lines()
        for (x, y), click in zip(points, clicks):
            rx, ry = _ints(click, "root")
            ex, ey = (size["x"] + x) / scale, (size["y"] + y) / scale
            tolerance = TOLERANCE + scale
            assert abs(rx - ex) <= tolerance and abs(ry - ey) <= tolerance, (
                f"[{tk_app.dpi}, scale {scale:.2f}] click({x},{y}) read at ({rx},{ry}), expected ({ex:.0f},{ey:.0f})")

    def test_posted_clicks_land_in_the_apps_own_coordinates(self, edit_app):
        """An Edit control is clicked with a posted message, whose coordinates
        Windows never translates: wbox must speak the app's DPI itself."""
        ready = edit_app.all_lines("ready ")
        assert ready, edit_app.all_lines()
        left, top, width, _ = _ints(ready[-1], "window")
        edit_x, edit_y = _ints(ready[-1], "edit")
        size = edit_app.comp.get_size()
        scale = size["window_width"] / width
        edit_app.mark()
        # Points in the Edit control, in the app's own (logical) pixels.
        for lx, ly in [(60, 40), (300, 150), (15, 200)]:
            px = round((edit_x + lx - left) * scale)
            py = round((edit_y + ly - top) * scale)
            result = edit_app.comp.click(px, py)
            assert "error" not in result, result
            assert result.get("method") == "PostMessage", result
            line = edit_app.wait_line("lbutton ")
            assert line, edit_app.all_lines()
            gx, gy = _ints(line, "lbutton ")
            tolerance = 1 + scale
            assert abs(gx - lx) <= tolerance and abs(gy - ly) <= tolerance, (
                f"[{edit_app.dpi}, scale {scale:.2f}] aimed at ({lx},{ly}), the Edit got ({gx},{gy})")
            edit_app.mark()

    def test_typing_reaches_the_edit_control(self, edit_app):
        edit_app.mark()
        assert "error" not in edit_app.comp.type_text("dpi")
        time.sleep(0.5)
        chars = "".join(m.group(1) for l in edit_app.lines("char ") if (m := re.search(r"char '(.)'", l)))
        assert "dpi" in chars, edit_app.lines()
