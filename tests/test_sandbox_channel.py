"""
test_sandbox_channel.py — the Windows Sandbox mode's plumbing, without a sandbox.

The host's end (compositor/wsb.py: Channel) and the agent's (sandbox_agent.py:
serve) talk through a folder. Here both ends run in this process, the agent on
a thread with a fake backend: what is checked is the channel, the .wsb the
host writes and the calls it forwards — on any OS, in CI.

The real thing — the agent in Windows Sandbox driving the crash dummy — is in
test_windows.py.
"""

from __future__ import annotations

import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from wbox import sandbox_agent  # noqa: E402
from wbox.compositor import wsb  # noqa: E402


class FakeBackend:
    """Records what it is asked; answers like the win32 backend."""

    def __init__(self):
        self.calls = []

    def click(self, x, y, button=1):
        self.calls.append(("click", x, y, button))
        return {"ok": True, "method": "SendInput"}

    def is_running(self):
        return True

    def key(self, shortcut):
        raise RuntimeError("the keyboard fell off")

    def screenshot(self, name=None, scale=None, region=None):
        self.calls.append(("screenshot", name))
        return {"path": name, "size": 1}


@pytest.fixture
def agent(tmp_path):
    """An agent serving tmp_path on a thread; the host's channel to it."""
    backend = FakeBackend()
    thread = threading.Thread(target=sandbox_agent.serve, args=(tmp_path, backend), daemon=True)
    thread.start()
    channel = wsb.Channel(tmp_path)
    for _ in range(200):
        if channel.ready():
            break
        threading.Event().wait(0.01)
    yield channel, backend
    channel.call("shutdown", timeout=5)
    thread.join(timeout=5)


class TestChannel:
    def test_a_call_reaches_the_backend_and_its_answer_comes_back(self, agent):
        channel, backend = agent
        assert channel.call("click", {"x": 10, "y": 20}) == {"ok": True, "method": "SendInput"}
        assert backend.calls == [("click", 10, 20, 1)]

    def test_a_value_that_is_not_a_dict_comes_back_wrapped(self, agent):
        channel, _ = agent
        assert channel.call("is_running") == {"result": True}

    def test_the_backend_failing_is_an_error_not_a_dead_agent(self, agent):
        channel, _ = agent
        assert "the keyboard fell off" in channel.call("key", {"shortcut": "a"})["error"]
        assert channel.call("ping")["status"] == "ok"

    def test_only_the_backends_calls_are_reachable(self, agent):
        channel, _ = agent
        assert "unknown method" in channel.call("__init__")["error"]
        assert "unknown method" in channel.call("_forward")["error"]

    def test_calls_in_a_row_keep_their_order(self, agent):
        channel, backend = agent
        for i in range(20):
            channel.call("click", {"x": i, "y": i})
        assert [c[1] for c in backend.calls] == list(range(20))

    def test_no_agent_means_an_error_in_time_not_a_hang(self, tmp_path):
        channel = wsb.Channel(tmp_path)
        channel.reset()
        answer = channel.call("ping", timeout=0.3)
        assert "no answer" in answer["error"]

    def test_requests_are_numbered_in_order_across_channels(self, tmp_path):
        """wbox_ctl runs one process per command: each has its own Channel on
        the same folder, and each answer is found by its request's number."""
        import json

        first, second = wsb.Channel(tmp_path), wsb.Channel(tmp_path)
        first.reset()
        first.call("ping", timeout=0.05)
        second.call("ping", timeout=0.05)
        first.call("ping", timeout=0.05)
        lines = (tmp_path / "inbox").read_text(encoding="utf-8").splitlines()
        assert [json.loads(l)["seq"] for l in lines] == [1, 2, 3]

    def test_a_line_being_written_waits_for_its_newline(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.write_bytes(b'{"seq": 1}\n{"seq": 2, "meth')
        lines, offset = sandbox_agent.read_new_lines(inbox, 0)
        assert lines == ['{"seq": 1}'] and offset == len(b'{"seq": 1}\n')
        with open(inbox, "ab") as f:
            f.write(b'od": "ping"}\n')
        lines, _ = sandbox_agent.read_new_lines(inbox, offset)
        assert lines == ['{"seq": 2, "method": "ping"}']

    def test_an_unanswered_request_does_not_block_the_next(self, tmp_path):
        channel = wsb.Channel(tmp_path)
        channel.reset()
        assert "no answer" in channel.call("ping", timeout=0.05)["error"]
        # An agent that comes late answers both, in order.
        thread = threading.Thread(target=sandbox_agent.serve, args=(tmp_path, FakeBackend()), daemon=True)
        thread.start()
        assert channel.call("ping", timeout=5)["status"] == "ok"
        channel.call("shutdown", timeout=5)
        thread.join(timeout=5)

    def test_shutdown_ends_the_agent(self, tmp_path):
        thread = threading.Thread(target=sandbox_agent.serve, args=(tmp_path, FakeBackend()), daemon=True)
        thread.start()
        channel = wsb.Channel(tmp_path)
        while not channel.ready():
            threading.Event().wait(0.01)
        assert channel.call("shutdown")["status"] == "shutting_down"
        thread.join(timeout=5)
        assert not thread.is_alive()


class TestWsbConfig:
    def _parse(self, **kw):
        mounts = [
            {"host": Path("C:/py"), "guest": "C:\\wbox\\python", "readonly": True},
            {"host": Path("C:/io & more"), "guest": "C:\\wbox\\io", "readonly": False},
        ]
        return ET.fromstring(wsb.wsb_config(mounts, bootstrap="C:\\wbox\\io\\bootstrap.cmd", **kw))

    def test_isolated_by_default(self):
        root = self._parse()
        assert root.findtext("Networking") == "Disable"
        assert root.findtext("ClipboardRedirection") == "Disable"
        assert root.findtext("vGPU") == "Enable"

    def test_the_mounts_and_their_rights(self):
        folders = self._parse().findall("MappedFolders/MappedFolder")
        assert [f.findtext("SandboxFolder") for f in folders] == ["C:\\wbox\\python", "C:\\wbox\\io"]
        assert [f.findtext("ReadOnly") for f in folders] == ["true", "false"]
        # Escaped, and read back as written.
        assert folders[1].findtext("HostFolder") == str(Path("C:/io & more"))

    def test_the_logon_command_starts_the_agent(self):
        assert self._parse().findtext("LogonCommand/Command") == "C:\\wbox\\io\\bootstrap.cmd"

    def test_options(self):
        root = self._parse(networking=True, vgpu=False, memory_mb=2048)
        assert root.findtext("Networking") == "Enable"
        assert root.findtext("vGPU") == "Disable"
        assert root.findtext("MemoryInMB") == "2048"

    def test_the_bootstrap_runs_wbox_from_the_mounts(self):
        script = wsb.bootstrap_script()
        assert "PYTHONPATH=C:\\wbox\\src;C:\\wbox\\site" in script
        assert "C:\\wbox\\python\\python.exe -m wbox.sandbox_agent C:\\wbox\\io > C:\\wbox\\io\\agent.out" in script


class TestWindowsCommandLine:
    """app.command for the win32 backend: Windows paths as written."""

    def _split(self, command):
        from wbox.server import _build_app_cmd

        return _build_app_cmd({"compositor": "win32", "app": {"command": command}})

    def test_backslashes_are_path_separators(self):
        assert self._split("C:\\tvty\\tvty.exe --flag") == ["C:\\tvty\\tvty.exe", "--flag"]

    def test_double_quotes_group_and_go(self):
        assert self._split('"C:\\Program Files\\App\\app.exe" --writer') == [
            "C:\\Program Files\\App\\app.exe", "--writer"]

    def test_a_list_is_taken_as_it_is(self):
        assert self._split(["C:\\a b\\c.exe", "x"]) == ["C:\\a b\\c.exe", "x"]


class TestHostSide:
    def test_the_sandbox_backend_is_chosen_by_the_sandbox_key(self, tmp_path):
        from wbox.server import build_compositor

        comp = build_compositor({"compositor": "win32", "name": "chan-test", "sandbox": {"keep": True},
                                 "_config_dir": str(tmp_path)})
        assert isinstance(comp, wsb.SandboxCompositor)
        assert comp.options == {"keep": True}

    def test_relative_mounts_are_the_config_dirs(self, tmp_path):
        (tmp_path / "app").mkdir()
        comp = wsb.SandboxCompositor(instance_name="chan-test", config_dir=tmp_path,
                                     sandbox={"mounts": [{"host": "app", "guest": "C:\\app"}]})
        mounts = comp._mounts()
        app = [m for m in mounts if m["guest"] == "C:\\app"][0]
        assert app["host"] == (tmp_path / "app").resolve()
        assert app["readonly"] is True
        io = [m for m in mounts if m["guest"] == wsb.GUEST_IO][0]
        assert io["readonly"] is False
        # Only the channel is writable from inside.
        assert [m["guest"] for m in mounts if not m["readonly"]] == [wsb.GUEST_IO]

    def test_calls_are_forwarded_and_screenshots_come_home(self, tmp_path):
        comp = wsb.SandboxCompositor(instance_name="chan-test", config_dir=tmp_path)
        comp.io_dir = tmp_path / "io"
        comp.channel = wsb.Channel(comp.io_dir)
        comp.channel.reset()
        comp.state.screenshot_dir = tmp_path / "shots"

        class Shooter(FakeBackend):
            def screenshot(self, name=None, scale=None, region=None):
                (comp.io_dir / "shots" / name).write_bytes(b"\x89PNG")
                return {"path": f"C:\\wbox\\io\\shots\\{name}", "size": 4}

        backend = Shooter()
        thread = threading.Thread(target=sandbox_agent.serve, args=(comp.io_dir, backend), daemon=True)
        thread.start()
        while not comp.channel.ready():
            threading.Event().wait(0.01)
        try:
            assert comp.click(3, 4) == {"ok": True, "method": "SendInput"}
            shot = comp.screenshot("look")
            assert shot["path"] == str(tmp_path / "shots" / "look.png")
            assert Path(shot["path"]).read_bytes() == b"\x89PNG"
            assert not (comp.io_dir / "shots" / "look.png").exists()
            assert "not supported" in comp.scroll(1, 1, 1)["error"]
        finally:
            comp.channel.call("shutdown", timeout=5)
            thread.join(timeout=5)
