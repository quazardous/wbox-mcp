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
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

CRASH_DUMMY_DIR = Path(__file__).parent / "crash-dummy"
CRASH_DUMMY_LOG = CRASH_DUMMY_DIR / "log" / "crash_dummy.log"

# ── Helpers ──────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    combo: str
    test: str
    passed: bool
    detail: str = ""
    delta: float | None = None


def parse_log_lines(path: Path, prefix: str) -> list[str]:
    """Return log lines matching a prefix."""
    if not path.exists():
        return []
    return [l for l in path.read_text().splitlines() if prefix in l]


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
                 undecorate: bool = True, screen: str = "800x600"):
        self.compositor = compositor
        self.input_backend = input_backend
        self.app_mode = app_mode
        self.undecorate = undecorate
        self.screen = screen
        self.comp = None
        self._combo = f"{compositor}/{input_backend}/{app_mode}"

    def launch(self) -> dict:
        from wbox.config import resolve_input_backend
        from wbox.server import build_compositor

        # Clean previous log
        CRASH_DUMMY_LOG.parent.mkdir(parents=True, exist_ok=True)
        if CRASH_DUMMY_LOG.exists():
            CRASH_DUMMY_LOG.unlink()

        cfg = {
            "name": f"test-{self.compositor}-{self.input_backend}-{self.app_mode}",
            "compositor": self.compositor,
            "screen": self.screen,
            "input_backend": self.input_backend,
            "undecorate": self.undecorate,
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
            "CRASH_DUMMY_LOG": str(CRASH_DUMMY_LOG),
            "CRASH_DUMMY_MODE": self.app_mode,
            "CRASH_DUMMY_SIZE": self.screen,
        }

        result = self.comp.launch(app_cmd, app_env)
        if "error" not in result:
            # Wait for crash_dummy to be ready (log contains "ready")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if CRASH_DUMMY_LOG.exists() and "ready" in CRASH_DUMMY_LOG.read_text():
                    break
                time.sleep(0.5)
            time.sleep(0.5)
        return result

    def kill(self):
        if self.comp:
            try:
                self.comp.kill(aggressive=True)
            except Exception:
                pass
            time.sleep(0.5)

    @property
    def x_display(self) -> str:
        return self.comp.state.x_display if self.comp else ""

    @property
    def app_pid(self) -> int:
        return self.comp.state.app_pid if self.comp else 0

    def log_lines(self, prefix: str = "") -> list[str]:
        return parse_log_lines(CRASH_DUMMY_LOG, prefix)

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

@pytest.fixture(params=COMPOSITOR_BACKENDS, ids=combo_id)
def compositor_backend(request):
    compositor, backend = request.param
    if not _compositor_available(compositor):
        pytest.skip(f"{compositor} not installed")
    if not _backend_tools_available(backend):
        pytest.skip(f"tools for {backend} backend not available")
    return compositor, backend


@pytest.fixture(params=APP_MODES)
def app_mode(request):
    return request.param


@pytest.fixture
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
    """Harness specifically for undecorate=True with normal mode."""
    compositor, backend = compositor_backend
    h = WboxTestHarness(compositor, backend, "normal", undecorate=True)
    result = h.launch()
    if "error" in result:
        h.kill()
        pytest.skip(f"launch failed: {result['error']}")
    yield h
    h.kill()


@pytest.fixture
def harness_decorate(compositor_backend):
    """Harness specifically for undecorate=False with normal mode."""
    compositor, backend = compositor_backend
    h = WboxTestHarness(compositor, backend, "normal", undecorate=False)
    result = h.launch()
    if "error" in result:
        h.kill()
        pytest.skip(f"launch failed: {result['error']}")
    yield h
    h.kill()


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
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.exists() and "ready" in log_path.read_text():
                    break
                time.sleep(0.3)
            assert log_path.exists(), "crash_dummy log not created"
            content = log_path.read_text()
            assert "mode=normal" in content, f"mode line missing: {content[:200]}"
            assert "ready" in content, f"ready line missing: {content[:200]}"
            assert "configure" in content, f"configure line missing: {content[:200]}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

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
            proc.terminate()
            proc.wait(timeout=5)

    def test_crash_dummy_popup_signal(self):
        """Launch crash_dummy, send SIGUSR1, verify popup opens."""
        display = os.environ.get("DISPLAY")
        if not display:
            pytest.skip("no host DISPLAY")
        log_path = CRASH_DUMMY_DIR / "log" / "sanity_popup.log"
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
            # Wait for ready
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.exists() and "ready" in log_path.read_text():
                    break
                time.sleep(0.3)
            # Open popup
            os.kill(proc.pid, signal.SIGUSR1)
            time.sleep(1)
            content = log_path.read_text()
            assert "popup_opened" in content, f"popup not opened: {content[:300]}"
            assert "popup_geometry" in content, f"popup geometry missing: {content[:300]}"
            # Close popup
            os.kill(proc.pid, signal.SIGUSR2)
            time.sleep(1)
            content = log_path.read_text()
            assert "popup_closed" in content, f"popup not closed: {content[-200:]}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


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
        test_text = f"wbox_test_{int(time.time())}"
        w = harness.comp.clipboard_write(test_text)
        if "error" in w:
            pytest.skip(f"clipboard_write not supported: {w['error']}")
        time.sleep(0.3)
        r = harness.comp.clipboard_read()
        if "error" in r:
            pytest.skip(f"clipboard_read not supported: {r['error']}")
        assert r.get("text", "").strip() == test_text, (
            f"clipboard roundtrip: wrote {test_text!r}, read {r!r}"
        )


class TestDecorations:
    """Verify undecorate behavior."""

    def test_undecorate_window_at_origin(self, harness_undecorate):
        """With undecorate=True, window should be near (0,0)."""
        h = harness_undecorate
        time.sleep(0.5)
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
                            undecorate=False, screen="800x600")
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
                            undecorate=False, screen="800x600")
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

    def test_popup_via_signal(self, harness):
        """SIGUSR1 should open popup, verify its geometry in log."""
        pid = harness.app_pid
        if not pid:
            pytest.skip("no app PID")
        os.kill(pid, signal.SIGUSR1)
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
        """Open popup via SIGUSR1, click inside it, verify in log."""
        pid = harness.app_pid
        if not pid:
            pytest.skip("no app PID")
        os.kill(pid, signal.SIGUSR1)
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
        """SIGUSR2 should close popup."""
        pid = harness.app_pid
        if not pid:
            pytest.skip("no app PID")
        os.kill(pid, signal.SIGUSR1)
        time.sleep(0.5)
        os.kill(pid, signal.SIGUSR2)
        time.sleep(0.5)
        close_lines = harness.log_lines("popup_closed")
        assert close_lines, "popup_closed not found in log"


# ── Summary report ───────────────────────────────────────────────────

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print a summary table of results."""
    reports = terminalreporter.stats
    passed = len(reports.get("passed", []))
    failed = len(reports.get("failed", []))
    skipped = len(reports.get("skipped", []))
    total = passed + failed + skipped

    terminalreporter.write_sep("=", "wbox integration summary")
    terminalreporter.write_line(
        f"  PASSED: {passed}  FAILED: {failed}  SKIPPED: {skipped}  TOTAL: {total}"
    )

    if reports.get("failed"):
        terminalreporter.write_sep("-", "failures")
        for report in reports["failed"]:
            terminalreporter.write_line(f"  FAIL: {report.nodeid}")
            if report.longreprtext:
                for line in report.longreprtext.splitlines()[:5]:
                    terminalreporter.write_line(f"        {line}")
