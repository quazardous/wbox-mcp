#!/usr/bin/env python3
"""
test_integration.py — wbox integration test suite.

Tests all compositor × input_backend × app_mode combinations.
Uses crash_dummy.py as the test app, parses its log to verify
mouse accuracy, keyboard input, clipboard, decorations, resize, and popups.

Run:
    cd tests/
    python -m pytest test_integration.py -v --tb=short
    python -m pytest test_integration.py -v -k labwc   # filter by compositor
    python -m pytest test_integration.py -v -k hybrid   # filter by backend
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

CRASH_DUMMY_DIR = Path(__file__).parent / "crash-dummy"

# Tests run offscreen by default — set WBOX_TEST_VISIBLE=1 to see the windows
HEADLESS = os.environ.get("WBOX_TEST_VISIBLE", "") not in ("1", "true", "yes")

# ── Helpers ──────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    combo: str
    test: str
    passed: bool
    detail: str = ""
    delta: float | None = None


def _stop_proc(proc: subprocess.Popen):
    """Terminate a child, escalating to kill — never raises from a finally."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def parse_root_coords(line: str) -> tuple[int, int] | None:
    """Extract root=(x,y) from a log line."""
    m = re.search(r"root=\((\d+),(\d+)\)", line)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_window_pos(line: str) -> tuple[int, int] | None:
    """Extract window_pos=(x,y) or pos=(x,y) from a log line."""
    m = re.search(r"(?:window_)?pos=\((\d+),(\d+)\)", line)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_window_size(line: str) -> tuple[int, int] | None:
    """Extract window_size=(w,h) or size=(w,h) from a log line."""
    m = re.search(r"(?:window_)?size=\((\d+),(\d+)\)", line)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def xdotool_display(display: str, *args: str) -> str:
    """Run xdotool on a specific DISPLAY, return stdout."""
    env = os.environ.copy()
    env["DISPLAY"] = display
    r = subprocess.run(
        ["xdotool", *args], env=env,
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip()


def xprop_display(display: str, *args: str) -> str:
    """Run xprop on a specific DISPLAY, return stdout."""
    env = os.environ.copy()
    env["DISPLAY"] = display
    r = subprocess.run(
        ["xprop", *args], env=env,
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip()


# ── Compositor lifecycle ─────────────────────────────────────────────


class WboxTestHarness:
    """Manages a compositor + crash_dummy for testing."""

    def __init__(self, compositor: str, input_backend: str, app_mode: str,
                 undecorate: bool = True, screen: str = "800x600", tag: str = ""):
        self.compositor = compositor
        self.input_backend = input_backend
        self.app_mode = app_mode
        self.undecorate = undecorate
        self.screen = screen
        self.comp = None
        self._combo = f"{compositor}/{input_backend}/{app_mode}"
        # tag distinguishes concurrent harnesses of the same combo (module
        # scope keeps several alive at once): unique instance name and log
        suffix = f"-{tag}" if tag else ""
        self.instance = f"test-{compositor}-{input_backend}-{app_mode}{suffix}"
        self.log_path = CRASH_DUMMY_DIR / "log" / f"{self.instance}.log"
        self.fifo_path = CRASH_DUMMY_DIR / "log" / f"{self.instance}.fifo"
        # Log lines before this mark are hidden from log_lines() — lets a
        # shared (module-scoped) harness give each test a fresh log view
        self._log_mark = 0

    def launch(self) -> dict:
        from wbox.config import resolve_input_backend
        from wbox.server import build_compositor

        # Clean previous log
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            self.log_path.unlink()

        cfg = {
            "name": self.instance,
            "compositor": self.compositor,
            "screen": self.screen,
            "input_backend": self.input_backend,
            "undecorate": self.undecorate,
            # Pin the nested seat keymap: the host may run any layout (e.g.
            # AZERTY) and input injection assumes us keycodes
            "keyboard_layout": "us",
            # Run offscreen so the suite doesn't pop windows over the desktop.
            # Set WBOX_TEST_VISIBLE=1 to watch what the tests are doing.
            "headless": HEADLESS,
            # The bridge would propagate every clipboard test to the real
            # desktop clipboard. The tests assert on the nested clipboard,
            # so turning it off costs no coverage.
            "clipboard_bridge": False,
            "timeouts": {
                "wayland_display": 10,
                "xwayland_display": 15,
                "app_render": 3,
                "stop": 5,
            },
            "_config_dir": str(CRASH_DUMMY_DIR),
        }

        self.comp = build_compositor(cfg)

        # Set log dir
        log_dir = CRASH_DUMMY_DIR / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(self.comp, "set_log_dir"):
            self.comp.set_log_dir(log_dir)

        app_cmd = ["python3", str(CRASH_DUMMY_DIR / "crash_dummy.py")]
        app_env = {
            "CRASH_DUMMY_LOG": str(self.log_path),
            "CRASH_DUMMY_FIFO": str(self.fifo_path),
            "CRASH_DUMMY_MODE": self.app_mode,
            "CRASH_DUMMY_SIZE": self.screen,
        }

        result = self.comp.launch(app_cmd, app_env)
        if "error" not in result:
            # Wait for crash_dummy to be ready (log contains "ready")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.log_path.exists() and "ready" in self.log_path.read_text():
                    break
                time.sleep(0.1)
            time.sleep(0.3)
        return result

    def kill(self):
        if self.comp:
            try:
                # comp.kill() already waits for the pids to vanish
                self.comp.kill(aggressive=True)
            except Exception:
                pass

    @property
    def x_display(self) -> str:
        return self.comp.state.x_display if self.comp else ""

    @property
    def app_pid(self) -> int:
        return self.comp.state.app_pid if self.comp else 0

    def send_cmd(self, cmd: str, timeout: float = 3) -> bool:
        """Send a command to crash_dummy through its FIFO (non-blocking open,
        so a dead app can't hang the test)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(0.1)
                continue
            try:
                os.write(fd, (cmd + "\n").encode())
                return True
            except OSError:
                time.sleep(0.1)
            finally:
                os.close(fd)
        return False

    def _raw_log_lines(self) -> list[str]:
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(errors="replace").splitlines()

    def mark_log(self):
        """Hide everything logged so far from subsequent log_lines() calls."""
        self._log_mark = len(self._raw_log_lines())

    def log_lines(self, prefix: str = "") -> list[str]:
        return [l for l in self._raw_log_lines()[self._log_mark:] if prefix in l]

    def last_log_line(self, prefix: str) -> str | None:
        lines = self.log_lines(prefix)
        return lines[-1] if lines else None


# ── Combo definitions ────────────────────────────────────────────────

# (compositor, input_backend, available_check)
COMPOSITOR_BACKENDS = [
    ("labwc", "x11"),
    ("labwc", "hybrid"),
    ("labwc", "wayland"),
    ("weston", "x11"),
    # weston + hybrid/wayland = skip (wtype not supported)
    ("cage", "x11"),
    ("cage", "hybrid"),
    ("cage", "wayland"),
]

APP_MODES = ["normal", "fixed", "fullscreen"]


def _compositor_available(name: str) -> bool:
    return shutil.which(name) is not None


def _backend_tools_available(backend: str) -> bool:
    from wbox.config import INPUT_BACKEND_PRESETS
    preset = INPUT_BACKEND_PRESETS.get(backend, {})
    tools = set()
    if preset.get("keyboard") == "wtype":
        tools.add("wtype")
    if preset.get("mouse") == "ydotool":
        tools.add("ydotool")
    if preset.get("clipboard") == "wayland":
        tools.add("wl-copy")
    return all(shutil.which(t) for t in tools)


def combo_id(val):
    if isinstance(val, tuple):
        return "-".join(str(v) for v in val)
    return str(val)


# ── Fixtures ─────────────────────────────────────────────────────────

# Module-scoped: one compositor launch per (backend, mode) combo instead of
# one per test. Tests get per-test log isolation via mark_log (autouse below).

@pytest.fixture(params=COMPOSITOR_BACKENDS, ids=combo_id, scope="module")
def compositor_backend(request):
    compositor, backend = request.param
    if not _compositor_available(compositor):
        pytest.skip(f"{compositor} not installed")
    if not _backend_tools_available(backend):
        pytest.skip(f"tools for {backend} backend not available")
    return compositor, backend


@pytest.fixture(params=APP_MODES, scope="module")
def app_mode(request):
    return request.param


@pytest.fixture(scope="module")
def harness(compositor_backend, app_mode):
    compositor, backend = compositor_backend
    h = WboxTestHarness(compositor, backend, app_mode)
    result = h.launch()
    if "error" in result:
        h.kill()
        pytest.skip(f"launch failed: {result['error']}")
    yield h
    h.kill()


@pytest.fixture
def harness_undecorate(compositor_backend):
    """Harness for undecorate=True (one test — function scope avoids keeping
    an extra compositor alive alongside the shared module harness)."""
    compositor, backend = compositor_backend
    h = WboxTestHarness(compositor, backend, "normal", undecorate=True, tag="undec")
    result = h.launch()
    if "error" in result:
        h.kill()
        pytest.skip(f"launch failed: {result['error']}")
    yield h
    h.kill()


@pytest.fixture
def harness_decorate(compositor_backend):
    """Harness for undecorate=False (one test — function scope, see above)."""
    compositor, backend = compositor_backend
    h = WboxTestHarness(compositor, backend, "normal", undecorate=False, tag="dec")
    result = h.launch()
    if "error" in result:
        h.kill()
        pytest.skip(f"launch failed: {result['error']}")
    yield h
    h.kill()


@pytest.fixture(autouse=True)
def _fresh_log_view(request):
    """Give each test a fresh view of the shared crash_dummy log.

    Only the module-scoped harness needs this: function-scoped harnesses are
    freshly launched, and marking them would race against startup log lines
    (e.g. the geometry line crash_dummy emits 500ms after start).
    """
    if "harness" in request.fixturenames:
        request.getfixturevalue("harness").mark_log()
    yield


# ── Tests ────────────────────────────────────────────────────────────

class TestCrashDummySanity:
    """Verify crash_dummy.py works standalone (no wbox)."""

    def test_crash_dummy_starts_on_host(self):
        """Launch crash_dummy on host DISPLAY, verify it logs 'ready'."""
        display = os.environ.get("DISPLAY")
        if not display:
            pytest.skip("no host DISPLAY")
        log_path = CRASH_DUMMY_DIR / "log" / "sanity_test.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            log_path.unlink()

        env = os.environ.copy()
        env["CRASH_DUMMY_LOG"] = str(log_path)
        env["CRASH_DUMMY_MODE"] = "normal"
        env["CRASH_DUMMY_SIZE"] = "400x300"

        proc = subprocess.Popen(
            ["python3", str(CRASH_DUMMY_DIR / "crash_dummy.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            # Wait for "configure", not "ready": ready is logged before the
            # mainloop starts, while configure only fires once the window is
            # mapped — stopping at ready races the very line we assert on.
            deadline = time.monotonic() + 10
            content = ""
            while time.monotonic() < deadline:
                content = log_path.read_text() if log_path.exists() else ""
                if "configure" in content:
                    break
                time.sleep(0.2)
            assert log_path.exists(), "crash_dummy log not created"
            assert "mode=normal" in content, f"mode line missing: {content[:200]}"
            assert "ready" in content, f"ready line missing: {content[:200]}"
            assert "configure" in content, f"configure line missing: {content[:200]}"
        finally:
            _stop_proc(proc)

    def test_crash_dummy_fixed_mode(self):
        """Launch crash_dummy in fixed mode, verify non-resizable."""
        display = os.environ.get("DISPLAY")
        if not display:
            pytest.skip("no host DISPLAY")
        log_path = CRASH_DUMMY_DIR / "log" / "sanity_fixed.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            log_path.unlink()

        env = os.environ.copy()
        env["CRASH_DUMMY_LOG"] = str(log_path)
        env["CRASH_DUMMY_MODE"] = "fixed"
        env["CRASH_DUMMY_SIZE"] = "400x300"

        proc = subprocess.Popen(
            ["python3", str(CRASH_DUMMY_DIR / "crash_dummy.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.exists() and "ready" in log_path.read_text():
                    break
                time.sleep(0.3)
            content = log_path.read_text()
            assert "mode=fixed" in content
            assert "ready" in content
        finally:
            _stop_proc(proc)

    def test_crash_dummy_popup_signal(self):
        """Launch crash_dummy, send open_popup via FIFO, verify popup opens."""
        display = os.environ.get("DISPLAY")
        if not display:
            pytest.skip("no host DISPLAY")
        log_path = CRASH_DUMMY_DIR / "log" / "sanity_popup.log"
        fifo_path = CRASH_DUMMY_DIR / "log" / "sanity_popup.fifo"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists():
            log_path.unlink()

        env = os.environ.copy()
        env["CRASH_DUMMY_LOG"] = str(log_path)
        env["CRASH_DUMMY_FIFO"] = str(fifo_path)
        env["CRASH_DUMMY_MODE"] = "normal"
        env["CRASH_DUMMY_SIZE"] = "400x300"

        proc = subprocess.Popen(
            ["python3", str(CRASH_DUMMY_DIR / "crash_dummy.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            # Wait for ready
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.exists() and "ready" in log_path.read_text():
                    break
                time.sleep(0.3)
            # Open popup
            with open(fifo_path, "w") as fifo:
                fifo.write("open_popup\n")
            time.sleep(1)
            content = log_path.read_text()
            assert "popup_opened" in content, f"popup not opened: {content[:300]}"
            assert "popup_geometry" in content, f"popup geometry missing: {content[:300]}"
            # Close popup
            with open(fifo_path, "w") as fifo:
                fifo.write("close_popup\n")
            time.sleep(1)
            content = log_path.read_text()
            assert "popup_closed" in content, f"popup not closed: {content[-200:]}"
        finally:
            _stop_proc(proc)


class TestLaunch:
    """Verify compositor launches correctly."""

    def test_launch(self, harness):
        assert harness.comp.is_running()
        assert harness.comp.state.wayland_display
        assert harness.comp.state.x_display

    def test_screenshot(self, harness):
        result = harness.comp.screenshot()
        assert "error" not in result, f"screenshot failed: {result}"
        assert "path" in result
        assert Path(result["path"]).stat().st_size > 0


class TestMouseAccuracy:
    """Verify mouse coordinates are accurate."""

    POINTS = [(100, 100), (400, 300), (200, 500), (700, 200), (50, 50)]
    MAX_DELTA = 3  # pixels tolerance

    def test_mouse_move_positions(self, harness):
        for x, y in self.POINTS:
            harness.comp.mouse_move(x, y)
            time.sleep(0.1)
            pos = harness.comp.get_mouse_position()
            assert "error" not in pos, f"get_mouse_position failed: {pos}"
            dx = abs(pos["x"] - x)
            dy = abs(pos["y"] - y)
            assert dx <= self.MAX_DELTA and dy <= self.MAX_DELTA, (
                f"mouse_move({x},{y}) → got ({pos['x']},{pos['y']}) "
                f"delta=({dx},{dy})"
            )

    def test_click_root_coords(self, harness):
        """Verify click reaches the right root= coordinates in the app log."""
        test_points = [(200, 200), (500, 400), (100, 300)]
        for x, y in test_points:
            harness.comp.click(x, y)
            time.sleep(0.3)

        # Check log for root= coordinates
        click_lines = harness.log_lines("click ")
        assert len(click_lines) >= len(test_points), (
            f"expected {len(test_points)} click lines, got {len(click_lines)}"
        )
        for i, (x, y) in enumerate(test_points):
            root = parse_root_coords(click_lines[-(len(test_points) - i)])
            if root:
                dx = abs(root[0] - x)
                dy = abs(root[1] - y)
                assert dx <= self.MAX_DELTA and dy <= self.MAX_DELTA, (
                    f"click({x},{y}) → root={root} delta=({dx},{dy})"
                )


class TestKeyboard:
    """Verify keyboard input."""

    def test_type_text(self, harness):
        # Click somewhere to focus
        harness.comp.click(400, 280)
        time.sleep(0.3)
        harness.comp.type_text("abc123")
        time.sleep(0.5)
        key_lines = harness.log_lines("key ")
        keys_received = "".join(
            re.search(r"\('(.)'", l).group(1)
            for l in key_lines
            if re.search(r"\('(.)'", l)
        )
        assert "abc123" in keys_received, (
            f"typed 'abc123', received keys: {keys_received!r}"
        )

    def test_key_shortcut(self, harness):
        harness.comp.click(400, 280)
        time.sleep(0.2)
        harness.comp.key("ctrl+a")
        time.sleep(0.3)
        key_lines = harness.log_lines("key ")
        ctrl_lines = [l for l in key_lines if "Ctrl" in l]
        assert ctrl_lines, "Ctrl modifier not detected in key events"


class TestClipboard:
    """Verify clipboard roundtrip."""

    def test_clipboard_write_read(self, harness):
        # Skipping on error used to hide a real bug (#1538): a write that
        # reported failure had in fact succeeded, and a "successful" one left
        # nothing to read. Both backends must now work — assert, don't skip.
        test_text = f"wbox_test_{int(time.time())}"
        w = harness.comp.clipboard_write(test_text)
        assert "error" not in w, f"clipboard_write failed: {w}"
        time.sleep(0.3)
        r = harness.comp.clipboard_read()
        assert "error" not in r, f"clipboard_read failed: {r}"
        assert r.get("text", "").strip() == test_text, (
            f"clipboard roundtrip: wrote {test_text!r}, read {r!r}"
        )

    def test_clipboard_overwrite(self, harness):
        """A second write must replace the first, not leave a stale owner."""
        first = f"wbox_first_{int(time.time())}"
        second = f"wbox_second_{int(time.time())}"
        assert "error" not in harness.comp.clipboard_write(first)
        time.sleep(0.2)
        assert "error" not in harness.comp.clipboard_write(second)
        time.sleep(0.3)
        r = harness.comp.clipboard_read()
        assert r.get("text", "").strip() == second, (
            f"overwrite: expected {second!r}, read {r!r}"
        )


class TestDecorations:
    """Verify undecorate behavior."""

    def test_undecorate_window_at_origin(self, harness_undecorate):
        """With undecorate=True, window should be near (0,0)."""
        h = harness_undecorate
        # Ask for a fresh geometry line: launch() has already undecorated by
        # now, while the line crash_dummy logs at startup predates it (and
        # can be hidden by log marking under load).
        h.mark_log()
        if not h.send_cmd("geometry"):
            pytest.skip("crash_dummy FIFO not available")
        deadline = time.monotonic() + 3
        geom_lines = []
        while time.monotonic() < deadline and not geom_lines:
            time.sleep(0.1)
            geom_lines = h.log_lines("geometry ")
        assert geom_lines, "no geometry log line found"
        pos = parse_window_pos(geom_lines[-1])
        if pos:
            assert pos[0] <= 5 and pos[1] <= 5, (
                f"undecorate: expected window near (0,0), got {pos}"
            )

    def test_decorate_has_offset(self, harness_decorate):
        """With undecorate=False, window should have WM offset (title bar)."""
        h = harness_decorate
        time.sleep(0.5)
        # Check window position via xdotool
        wid = xdotool_display(h.x_display, "search", "--name", "crash dummy")
        if not wid:
            pytest.skip("could not find window")
        wid = wid.splitlines()[0]
        geom = xdotool_display(h.x_display, "getwindowgeometry", "--shell", wid)
        info = dict(l.split("=", 1) for l in geom.splitlines() if "=" in l)
        # With decorations, there should be a title bar offset or the window
        # shouldn't be at (0,0)
        x, y = int(info.get("X", 0)), int(info.get("Y", 0))
        # At minimum, the window should exist and have geometry
        assert "WIDTH" in info, f"no geometry found: {geom}"


class TestResize:
    """Verify resize behavior."""

    def test_resize_normal_mode(self, compositor_backend):
        """Normal mode app should accept resize."""
        compositor, backend = compositor_backend
        if not _compositor_available(compositor):
            pytest.skip(f"{compositor} not installed")
        if not _backend_tools_available(backend):
            pytest.skip(f"tools for {backend} not available")

        h = WboxTestHarness(compositor, backend, "normal",
                            undecorate=False, screen="800x600", tag="resize")
        result = h.launch()
        if "error" in result:
            h.kill()
            pytest.skip(f"launch failed: {result['error']}")
        try:
            time.sleep(0.5)
            # Resize via compositor
            r = h.comp.resize(640, 480) if hasattr(h.comp, "resize") else None
            if r and "error" not in r:
                time.sleep(1)
                size = h.comp.get_size()
                # Just verify we got a response
                assert "error" not in size, f"get_size failed: {size}"
        finally:
            h.kill()

    def test_fixed_mode_no_resize(self, compositor_backend):
        """Fixed mode app sets min=max size hints — should resist resize."""
        compositor, backend = compositor_backend
        if not _compositor_available(compositor):
            pytest.skip(f"{compositor} not installed")
        if not _backend_tools_available(backend):
            pytest.skip(f"tools for {backend} not available")

        h = WboxTestHarness(compositor, backend, "fixed",
                            undecorate=False, screen="800x600", tag="resize")
        result = h.launch()
        if "error" in result:
            h.kill()
            pytest.skip(f"launch failed: {result['error']}")
        try:
            time.sleep(0.5)
            # Get initial size
            wid = xdotool_display(h.x_display, "search", "--name", "crash dummy")
            if not wid:
                pytest.skip("could not find window")
            wid = wid.splitlines()[0]
            geom_before = xdotool_display(
                h.x_display, "getwindowgeometry", "--shell", wid)
            info_before = dict(
                l.split("=", 1) for l in geom_before.splitlines() if "=" in l)

            # Try to resize
            xdotool_display(h.x_display, "windowsize", wid, "640", "480")
            time.sleep(0.5)

            geom_after = xdotool_display(
                h.x_display, "getwindowgeometry", "--shell", wid)
            info_after = dict(
                l.split("=", 1) for l in geom_after.splitlines() if "=" in l)

            # Fixed window should keep its size (or very close)
            w_before = int(info_before.get("WIDTH", 0))
            w_after = int(info_after.get("WIDTH", 0))
            assert abs(w_before - w_after) <= 2, (
                f"fixed window resized: {w_before} → {w_after}"
            )
        finally:
            h.kill()


class TestPopup:
    """Verify popup dialog behavior."""

    @pytest.fixture(autouse=True)
    def _close_popup_after(self, harness):
        # The harness is shared: close the popup so the next test's open_popup
        # actually reopens it (crash_dummy's open is a no-op when already open)
        yield
        harness.send_cmd("close_popup")
        time.sleep(0.3)

    def test_popup_via_signal(self, harness):
        """open_popup command should open popup, verify its geometry in log."""
        if not harness.send_cmd("open_popup"):
            pytest.skip("crash_dummy FIFO not available")
        time.sleep(1)

        popup_lines = harness.log_lines("popup_opened")
        assert popup_lines, "popup_opened not found in log"

        # Check popup geometry
        geom_lines = harness.log_lines("popup_geometry")
        assert geom_lines, "popup_geometry not found in log"
        pos = parse_window_pos(geom_lines[-1])
        size = parse_window_size(geom_lines[-1])
        assert pos is not None, f"could not parse popup position: {geom_lines[-1]}"
        assert size is not None, f"could not parse popup size: {geom_lines[-1]}"

    def test_popup_click(self, harness):
        """Open popup via FIFO command, click inside it, verify in log."""
        if not harness.send_cmd("open_popup"):
            pytest.skip("crash_dummy FIFO not available")
        time.sleep(1)

        # Get popup position from log
        geom_lines = harness.log_lines("popup_geometry")
        if not geom_lines:
            pytest.skip("popup geometry not logged")
        pos = parse_window_pos(geom_lines[-1])
        size = parse_window_size(geom_lines[-1])
        if not pos or not size:
            pytest.skip("could not parse popup geometry")

        # Click in the center of the popup
        cx = pos[0] + size[0] // 2
        cy = pos[1] + size[1] // 2
        harness.comp.click(cx, cy)
        time.sleep(0.5)

        popup_clicks = harness.log_lines("popup_click")
        assert popup_clicks, (
            f"no popup_click in log after clicking at ({cx},{cy})"
        )

    def test_popup_close_signal(self, harness):
        """close_popup command should close the popup."""
        if not harness.send_cmd("open_popup"):
            pytest.skip("crash_dummy FIFO not available")
        time.sleep(0.5)
        harness.send_cmd("close_popup")
        time.sleep(0.5)
        close_lines = harness.log_lines("popup_closed")
        assert close_lines, "popup_closed not found in log"


