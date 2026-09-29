"""
wsb.py — the win32 backend, isolated in Windows Sandbox.

`sandbox:` in config.yaml runs the app in Windows Sandbox instead of on the
user's desktop: its clicks move the sandbox's pointer, its keys go to the
sandbox's windows, its clipboard is the sandbox's. The Win32 calls have to be
made on that desktop, so the win32 backend runs inside, as wbox's sandbox
agent (sandbox_agent.py), and this class — on the host — forwards each call
to it through a shared folder.

What the sandbox sees, mapped from the host:

    C:\\wbox\\python   the Python running wbox (read-only)
    C:\\wbox\\site     its site-packages: wbox's dependencies (read-only)
    C:\\wbox\\src      wbox's own code (read-only)
    C:\\wbox\\io       the channel: requests, answers, screenshots (read-write)
    + the `mounts` the config asks for (the app, its data)

Nothing is installed in the sandbox, and it needs no network.

    sandbox:
      mounts:
        - {host: ./build, guest: 'C:\\app', readonly: true}
      networking: false     # default
      vgpu: true            # default; false renders in software (WARP)
      memory_mb: 4096
      keep: false           # true: kill/stop leave the sandbox up, and the
                            # next launch reuses it (no boot)
      boot_timeout: 180     # seconds for the sandbox to boot and the agent
                            # to answer
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .base import CompositorServer

log = logging.getLogger("wbox.wsb")

GUEST_ROOT = "C:\\wbox"
GUEST_PYTHON = GUEST_ROOT + "\\python"
GUEST_SITE = GUEST_ROOT + "\\site"
GUEST_SRC = GUEST_ROOT + "\\src"
GUEST_IO = GUEST_ROOT + "\\io"

SANDBOX_EXE = Path(os.environ.get("WINDIR", "C:\\Windows")) / "System32" / "WindowsSandbox.exe"
# A running sandbox, on the host: its window's process (Windows runs one
# sandbox at a time). Ending it ends the sandbox, with no dialog.
# WindowsSandboxServer.exe is not one: it stays, idle, once a sandbox ran.
SANDBOX_PROCESSES = ("WindowsSandboxRemoteSession.exe",)

CALL_TIMEOUT = 30.0
POLL_S = 0.02


def host_layout() -> dict[str, Path]:
    """Where, on the host, the Python running wbox, its packages and wbox's
    code are: what the sandbox gets, read-only."""
    import wbox

    return {
        "python": Path(sys.base_prefix),
        "site": Path(sysconfig.get_paths()["purelib"]),
        "src": Path(wbox.__file__).resolve().parent.parent,
    }


def wsb_config(mounts: list[dict], *, bootstrap: str, networking: bool = False,
               vgpu: bool = True, memory_mb: int | None = None) -> str:
    """The sandbox's configuration (a .wsb file's XML).

    `mounts`: `{host, guest, readonly}`, host paths absolute. The clipboard
    is never shared: the sandbox's is its own.
    """
    folders = "".join(
        "<MappedFolder>"
        f"<HostFolder>{escape(str(m['host']))}</HostFolder>"
        f"<SandboxFolder>{escape(str(m['guest']))}</SandboxFolder>"
        f"<ReadOnly>{'true' if m.get('readonly', True) else 'false'}</ReadOnly>"
        "</MappedFolder>"
        for m in mounts
    )
    memory = f"<MemoryInMB>{int(memory_mb)}</MemoryInMB>" if memory_mb else ""
    return (
        "<Configuration>"
        f"<vGPU>{'Enable' if vgpu else 'Disable'}</vGPU>"
        f"<Networking>{'Enable' if networking else 'Disable'}</Networking>"
        "<ClipboardRedirection>Disable</ClipboardRedirection>"
        "<PrinterRedirection>Disable</PrinterRedirection>"
        "<AudioInput>Disable</AudioInput>"
        "<VideoInput>Disable</VideoInput>"
        f"{memory}"
        f"<MappedFolders>{folders}</MappedFolders>"
        f"<LogonCommand><Command>{escape(bootstrap)}</Command></LogonCommand>"
        "</Configuration>"
    )


def bootstrap_script() -> str:
    """The .cmd the sandbox runs at logon: wbox's agent, on the host's Python.
    What the agent needs to know is in agent.json, beside it: nothing goes
    through cmd.exe's quoting."""
    command = f"{GUEST_PYTHON}\\python.exe -m wbox.sandbox_agent {GUEST_IO}"
    return (
        "@echo off\r\n"
        f"set PYTHONPATH={GUEST_SRC};{GUEST_SITE}\r\n"
        "set PYTHONUTF8=1\r\n"
        f"{command} > {GUEST_IO}\\agent.out 2>&1\r\n"
    )


def sandbox_running() -> bool:
    """Whether a Windows Sandbox runs on this machine (by anyone)."""
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, timeout=10).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(p.lower() in out for p in SANDBOX_PROCESSES)


class Channel:
    """The host's end of the shared folder: one request, one answer.

    Requests go down one file, `inbox`, a JSON line each, numbered: in
    Windows Sandbox the folder is a mapped share, and the guest learns that
    a new file exists only seconds later (its client caches "not found"),
    while a file it already knows is read afresh. Answers come back as files
    of their own (`responses/<seq>.json`): the host sees those at once."""

    def __init__(self, io_dir: Path):
        self.io_dir = io_dir
        self.inbox = io_dir / "inbox"
        self.responses = io_dir / "responses"

    def reset(self) -> None:
        """Empties the folder for a new sandbox (keeps it: it is mapped)."""
        self.io_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.io_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        self.inbox.write_bytes(b"")
        self.responses.mkdir()
        (self.io_dir / "shots").mkdir()

    def ready(self) -> bool:
        return (self.io_dir / "agent.ready").exists()

    def _post(self, method: str, args: dict) -> int:
        """Appends a request to the inbox; answers its number. Two host
        processes (wbox_ctl runs one per command) may post: the number and
        the line are taken under a lock."""
        lock = self.io_dir / "inbox.lock"
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    lock.unlink(missing_ok=True)  # left by a process that died holding it
                time.sleep(0.005)
        try:
            counter = self.io_dir / "seq"
            try:
                seq = int(counter.read_text()) + 1
            except (OSError, ValueError):
                seq = 1
            counter.write_text(str(seq))
            line = json.dumps({"seq": seq, "method": method, "args": args, "sent": time.time()}) + "\n"
            # One write: the agent never reads half a line as a whole one.
            with open(self.inbox, "ab") as f:
                f.write(line.encode("utf-8"))
            return seq
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    def call(self, method: str, args: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        if not self.inbox.exists():
            return {"error": "the sandbox agent is not there"}
        from ..sandbox_agent import response_name

        rid = response_name(self._post(method, args or {}))
        answer = self.responses / rid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if answer.exists():
                try:
                    data = json.loads(answer.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(POLL_S)
                    continue
                answer.unlink(missing_ok=True)
                return data
            time.sleep(POLL_S)
        # A late answer is simply never read.
        return {"error": f"{method}: no answer from the sandbox agent in {timeout:.0f}s"}


class SandboxCompositor(CompositorServer):
    """The win32 backend in Windows Sandbox, driven from the host."""

    compositor_name = "win32-sandbox"

    def __init__(self, *, screen: str = "1280x800", instance_name: str = "", timeouts: dict | None = None,
                 title_hint: str = "", sandbox: dict | None = None, config_dir: str | Path = ".",
                 headless: bool = False):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts)
        self.title_hint = title_hint
        self.options = dict(sandbox or {})
        self.config_dir = Path(config_dir)
        self.headless = headless
        name = instance_name or self.compositor_name
        self.io_dir = Path(tempfile.gettempdir()) / f"wbox_{name}_sandbox"
        self.channel = Channel(self.io_dir)
        self._name = name

    # ── The sandbox ──────────────────────────────────────────────

    def _mounts(self) -> list[dict]:
        layout = host_layout()
        mounts = [
            {"host": layout["python"], "guest": GUEST_PYTHON, "readonly": True},
            {"host": layout["site"], "guest": GUEST_SITE, "readonly": True},
            {"host": layout["src"], "guest": GUEST_SRC, "readonly": True},
            {"host": self.io_dir, "guest": GUEST_IO, "readonly": False},
        ]
        for m in self.options.get("mounts") or []:
            host = Path(os.path.expandvars(str(m["host"])))
            if not host.is_absolute():
                host = (self.config_dir / host).resolve()
            mounts.append({"host": host, "guest": m["guest"], "readonly": bool(m.get("readonly", True))})
        return mounts

    def _agent_alive(self) -> bool:
        return self.channel.ready() and "error" not in self.channel.call("ping", timeout=3)

    def _boot(self) -> dict:
        if not SANDBOX_EXE.exists():
            return {"error": "Windows Sandbox is not turned on (setup.ps1 turns it on; "
                             "or, as admin: Enable-WindowsOptionalFeature -Online "
                             "-FeatureName Containers-DisposableClientVM -All, then reboot)"}
        if sandbox_running():
            return {"error": "a Windows Sandbox runs already, and Windows runs one at a time: close it first"}
        for m in self.options.get("mounts") or []:
            host = Path(os.path.expandvars(str(m["host"])))
            host = host if host.is_absolute() else (self.config_dir / host)
            if not host.is_dir():
                return {"error": f"sandbox mount: {host} is not a folder"}
        self.channel.reset()
        bootstrap = self.io_dir / "bootstrap.cmd"
        bootstrap.write_text(bootstrap_script(), encoding="utf-8")
        (self.io_dir / "agent.json").write_text(json.dumps({
            "name": self._name, "title_hint": self.title_hint, "timeouts": self.timeouts,
        }), encoding="utf-8")
        wsb = self.io_dir / "sandbox.wsb"
        wsb.write_text(wsb_config(
            self._mounts(), bootstrap=f"{GUEST_IO}\\bootstrap.cmd",
            networking=bool(self.options.get("networking", False)),
            vgpu=bool(self.options.get("vgpu", True)),
            memory_mb=self.options.get("memory_mb"),
        ), encoding="utf-8")
        log.info("starting Windows Sandbox: %s", wsb)
        proc = subprocess.Popen([str(SANDBOX_EXE), str(wsb)])
        self.state.compositor_proc = proc
        self.state.compositor_pid = proc.pid
        boot_timeout = float(self.options.get("boot_timeout", 180))
        deadline = time.monotonic() + boot_timeout
        while time.monotonic() < deadline:
            if self.channel.ready():
                self._save()
                if self.headless:
                    self._minimize_window()
                return {"status": "booted"}
            time.sleep(0.5)
        out = self.io_dir / "agent.out"
        detail = out.read_text(encoding="utf-8", errors="replace")[-2000:] if out.exists() else ""
        self._shutdown()
        return {"error": f"the sandbox agent did not answer in {boot_timeout:.0f}s", "agent_output": detail}

    def _save(self) -> None:
        # The sandbox outlives this process (wbox_ctl: one per command): what
        # finds it again is the channel, whose folder is named after the
        # instance. The state file only says a launch happened.
        self.state.x_display = str(self.io_dir)
        self.state.save(self._state_file)

    def _minimize_window(self) -> None:
        """headless: the sandbox's window out of the way (it keeps running)."""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Windows Sandbox")
            if hwnd:
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        except Exception as e:  # cosmetic: never fail a launch for it
            log.debug("could not minimize the sandbox window: %s", e)

    def _shutdown(self) -> list[str]:
        """Ends this instance's sandbox: its agent stops the app and leaves,
        then the sandbox's window closes — which ends the sandbox, with no
        dialog (Windows turned off from inside says "the remote environment
        is shutting down" in a modal box on the user's desktop). A sandbox
        this instance did not start (no agent of ours) is never touched."""
        done = []
        if not self.channel.ready():
            return done
        answer = self.channel.call("shutdown", timeout=15)
        if "error" not in answer:
            done.append("agent")
        for image in SANDBOX_PROCESSES:
            subprocess.run(["taskkill", "/f", "/im", image], capture_output=True, timeout=15)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and sandbox_running():
            time.sleep(0.5)
        done.append("sandbox")
        (self.io_dir / "agent.ready").unlink(missing_ok=True)
        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.x_display = ""
        self.state.clear(self._state_file)
        return done

    # ── Lifecycle ────────────────────────────────────────────────

    def launch(self, app_cmd: list[str], app_env: dict[str, str] | None = None) -> dict:
        if not app_cmd:
            return {"error": "no app command configured"}
        if not self._agent_alive():
            booted = self._boot()
            if "error" in booted:
                return booted
        wait = (float(self.timeouts.get("window_discovery", 10)) + float(self.timeouts.get("edit_control", 3))
                + float(self.timeouts.get("app_render", 3)) + 15)
        result = self.channel.call("launch", {"app_cmd": list(app_cmd), "app_env": dict(app_env or {})},
                                   timeout=wait)
        result.setdefault("sandbox", str(self.io_dir))
        return result

    def stop(self, force: bool = False) -> dict:
        if not self._agent_alive():
            return {"status": "not_running"}
        result = self.channel.call("stop", {"force": force}, timeout=CALL_TIMEOUT)
        if not self.options.get("keep"):
            result["sandbox"] = self._shutdown()
        return result

    def kill(self, aggressive: bool = True) -> dict:
        result = {"status": "killed", "killed": []}
        if self._agent_alive():
            result = self.channel.call("kill", {"aggressive": aggressive}, timeout=CALL_TIMEOUT)
        if not self.options.get("keep"):
            result["sandbox"] = self._shutdown()
        return result

    def is_running(self) -> bool:
        if not self._agent_alive():
            return False
        return bool(self.channel.call("is_running").get("result"))

    # ── Forwarded calls ──────────────────────────────────────────

    def _forward(self, method: str, **args) -> dict:
        if not self._agent_alive():
            return {"error": "the sandbox is not running"}
        return self.channel.call(method, args)

    def screenshot(self, name: str | None = None, scale: float | None = None, region: str | None = None) -> dict:
        if scale or region:
            return {"error": "scale/region not supported by the win32 backend"}
        path = self._next_screenshot_path(name)
        result = self._forward("screenshot", name=path.name)
        if "error" in result:
            return result
        guest_file = self.io_dir / "shots" / path.name
        if not guest_file.exists():
            return {"error": f"the sandbox made no screenshot file ({result})"}
        result["path"] = str(self._bring_home(path))
        return result

    def click(self, x: int, y: int, button: int = 1) -> dict:
        return self._forward("click", x=x, y=y, button=button)

    def type_text(self, text: str, delay_ms: int = 12) -> dict:
        return self._forward("type_text", text=text, delay_ms=delay_ms)

    def key(self, shortcut: str) -> dict:
        return self._forward("key", shortcut=shortcut)

    def keys(self, shortcuts: list[str], delay_ms: int = 100) -> dict:
        return self._forward("keys", shortcuts=list(shortcuts), delay_ms=delay_ms)

    def mouse_move(self, x: int, y: int) -> dict:
        result = self._forward("mouse_move", x=x, y=y)
        if "error" not in result:
            self._last_mouse_x, self._last_mouse_y = x, y
        return result

    def list_windows(self) -> dict:
        return self._forward("list_windows")

    def focus_window(self, title: str = "", app_id: str = "") -> dict:
        return self._forward("focus_window", title=title, app_id=app_id)

    def get_size(self) -> dict:
        return self._forward("get_size")

    def resize(self, width: int, height: int) -> dict:
        return self._forward("resize", width=width, height=height)

    def clipboard_read(self) -> dict:
        return self._forward("clipboard_read")

    def clipboard_write(self, text: str) -> dict:
        return self._forward("clipboard_write", text=text)

    def scroll(self, x: int, y: int, notches: int, horizontal: bool = False) -> dict:
        return self._forward("scroll", x=x, y=y, notches=notches, horizontal=horizontal)

    def double_click(self, x: int, y: int, button: int = 1, interval: float = 0.08) -> dict:
        return self._forward("double_click", x=x, y=y, button=button, interval=interval)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: int = 1, steps: int = 10,
             seconds: float = 0.3) -> dict:
        if not self._agent_alive():
            return {"error": "the sandbox is not running"}
        return self.channel.call("drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button,
                                          "steps": steps, "seconds": seconds},
                                 timeout=CALL_TIMEOUT + max(0.0, seconds))

    def hold(self, keys: list[str], actions: list[dict]) -> dict:
        # Its screenshots are taken in the sandbox: named by the host, as
        # screenshot() does, then brought home.
        shots, sent = [], []
        for action in actions:
            if action.get("type") == "screenshot":
                path = self._next_screenshot_path(action.get("name"))
                shots.append(path)
                action = {**action, "name": path.name}
            sent.append(action)
        result = self._forward("hold", keys=list(keys), actions=sent)
        if "screenshots" in result:
            result["screenshots"] = [str(self._bring_home(p)) for p in shots
                                     if (self.io_dir / "shots" / p.name).exists()]
        return result

    def record(self, seconds: float, region: str | None = None, fps: float | None = None,
               name: str | None = None, during: dict | None = None) -> dict:
        """Filmed in the sandbox, the frames brought home as one folder."""
        if not self._agent_alive():
            return {"error": "the sandbox is not running"}
        name = name or f"record_{int(time.time())}"
        result = self.channel.call("record", {"seconds": seconds, "region": region, "fps": fps,
                                              "name": name, "during": during},
                                   timeout=CALL_TIMEOUT + max(0.0, float(seconds)))
        filmed = self.io_dir / "shots" / name
        if "frames_dir" in result and filmed.is_dir():
            home = self.state.screenshot_dir / name
            home.mkdir(parents=True, exist_ok=True)
            for frame in filmed.iterdir():
                shutil.move(str(frame), home / frame.name)
            filmed.rmdir()
            result["frames_dir"] = str(home)
        return result

    def _bring_home(self, path: Path) -> Path:
        """A screenshot the sandbox made, moved to where the host wants it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.io_dir / "shots" / path.name), path)
        return path

    def _start_compositor(self, app_cmd, app_env, wl_before, x11_before):
        pass

    def _start_app(self, app_cmd, app_env):
        pass
