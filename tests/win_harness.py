"""
win_harness.py — the Windows tests' harness: where the test apps run (the
desktop, or one Windows Sandbox for the session) and how they are driven.
Used by test_windows.py and test_windows_density.py; the `place` fixture is
in conftest.py.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

CRASH_DUMMY_DIR = Path(__file__).parent / "crash-dummy"
TITLE = "wbox crash dummy"
TOLERANCE = 3  # pixels


APPS = {
    # app: (script, title, log and command env names)
    "crash": ("crash_dummy.py", TITLE, "CRASH_DUMMY_LOG", "CRASH_DUMMY_FIFO", "CRASH_DUMMY_DPI"),
    "edit": ("edit_dummy.py", "wbox edit dummy", "EDIT_DUMMY_LOG", None, "EDIT_DUMMY_DPI"),
}


class Place:
    """Where the apps run: the desktop, or one Windows Sandbox for the whole
    session (its mounts are fixed when it boots, so every app shares them).

    The apps' logs and command files live in `shared`, a folder both sides
    see: on the desktop simply a host folder; in the sandbox, a read-write
    mount of it."""

    def __init__(self, where: str):
        self.where = where
        self.instance = f"test-win32-{where}"
        self.shared = Path(tempfile.mkdtemp(prefix=f"wbox_{where}_"))

    def config(self, title: str) -> dict:
        cfg = {
            "name": self.instance,
            "compositor": "win32",
            "title_hint": title,
            "timeouts": {"window_discovery": 30, "edit_control": 2, "app_render": 2},
            "_config_dir": str(CRASH_DUMMY_DIR),
        }
        if self.where == "sandbox":
            cfg["sandbox"] = {
                "mounts": [
                    {"host": str(CRASH_DUMMY_DIR), "guest": "C:\\dummy", "readonly": True},
                    {"host": str(self.shared), "guest": "C:\\shared", "readonly": False},
                ],
                # One boot for the session: each app's kill leaves it up.
                "keep": True,
                "boot_timeout": 300,
            }
        return cfg

    def paths(self) -> tuple[str, str, str]:
        """The app's python, the apps' folder and the shared folder, as the
        app sees them."""
        if self.where == "sandbox":
            return "C:\\wbox\\python\\python.exe", "C:\\dummy", "C:\\shared"
        return sys.executable, str(CRASH_DUMMY_DIR), str(self.shared)

    def close(self) -> None:
        if self.where == "sandbox":
            from wbox.compositor.wsb import SandboxCompositor

            SandboxCompositor(instance_name=self.instance)._shutdown()
        shutil.rmtree(self.shared, ignore_errors=True)


class WindowsHarness:
    """One test app under the win32 backend, in a Place, at a DPI awareness."""

    def __init__(self, place: Place, app: str = "crash", dpi: str = "permonitor", mode: str = "fixed"):
        self.place = place
        self.mode = mode  # the crash dummy's: fixed (not resizable) or normal
        self.where = place.where
        self.app, self.dpi = app, dpi
        script, self.title, self._log_env, self._cmd_env, self._dpi_env = APPS[app]
        self._script = script
        self.log_path = place.shared / f"{app}-{dpi}.log"
        self.cmd_path = place.shared / f"{app}-{dpi}.cmd"
        self.comp = None
        self._mark = 0

    def launch(self) -> dict:
        from wbox.server import build_compositor

        for path in (self.log_path, self.cmd_path):
            path.unlink(missing_ok=True)
        self.comp = build_compositor(self.place.config(self.title))
        self.comp.state.screenshot_dir = self.place.shared / "shots"
        python, apps, shared = self.place.paths()
        env = {self._log_env: f"{shared}\\{self.log_path.name}", self._dpi_env: self.dpi}
        if self._cmd_env:
            env[self._cmd_env] = f"{shared}\\{self.cmd_path.name}"
        if self.app == "crash":
            env.update({"CRASH_DUMMY_MODE": self.mode, "CRASH_DUMMY_SIZE": "800x600"})
        result = self.comp.launch([python, f"{apps}\\{self._script}"], env)
        if "error" not in result:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and "ready" not in self._text():
                time.sleep(0.1)
            time.sleep(0.5)
        return result

    def kill(self) -> None:
        if self.comp:
            try:
                self.comp.kill(aggressive=True)
            except Exception:
                pass

    def _text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def mark(self) -> None:
        self._mark = len(self._text().splitlines())

    def lines(self, prefix: str = "") -> list[str]:
        return [l for l in self._text().splitlines()[self._mark:] if prefix in l]

    def all_lines(self, prefix: str = "") -> list[str]:
        return [l for l in self._text().splitlines() if prefix in l]

    def send(self, command: str) -> None:
        with open(self.cmd_path, "a", encoding="utf-8") as f:
            f.write(command + "\n")

    def wait_line(self, prefix: str, timeout: float = 5) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.lines(prefix)
            if found:
                return found[-1]
            time.sleep(0.05)
        return None


def _sandbox_unavailable() -> str | None:
    from wbox.compositor import wsb

    if not wsb.SANDBOX_EXE.exists():
        return "Windows Sandbox is not turned on"
    if wsb.sandbox_running():
        return "a Windows Sandbox is already running"
    return None


def _params():
    params = [pytest.param("sandbox", id="sandbox")]
    params.append(pytest.param("desktop", id="desktop", marks=pytest.mark.skipif(
        os.environ.get("WBOX_TEST_DESKTOP", "") not in ("1", "true", "yes"),
        reason="moves your real pointer: set WBOX_TEST_DESKTOP=1")))
    return params


def _launched(place, app="crash", dpi="permonitor", mode="fixed"):
    h = WindowsHarness(place, app, dpi, mode)
    result = h.launch()
    if result.get("status") != "running":
        # "already_running" is a failure too: the backend drives one app at
        # a time, and the app asked for was never started.
        h.kill()
        pytest.fail(f"launch failed: {result}")
    return h


