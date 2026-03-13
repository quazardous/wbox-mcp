#!/usr/bin/env python3
"""
mcp-dev — Dynamic MCP wrapper for running any app in wbox.

Does NOT import wbox. Delegates to a worker subprocess that imports wbox fresh.
This means:
  - configure() changes config on the fly (compositor, app, screen, etc.)
  - reload() restarts worker → picks up code changes
  - send_cmd() talks to crash dummy via FIFO (when using crash dummy)

Default app is crash dummy. Set app_command to run anything else.
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

TESTS_DIR = Path(__file__).resolve().parent.parent
CRASH_DUMMY_PY = TESTS_DIR / "crash-dummy" / "crash_dummy.py"

server = Server("mcp-dev")

# ── Shared state ──

_config = {
    "compositor": "labwc",
    "input_backend": "hybrid",
    "screen": "800x600",
    "undecorate": True,
    # App config — empty = crash dummy (default)
    "app_command": "",
    "app_env": {},
    "post_launch_keys": [],
    "post_launch_keys_delay": 0.5,
    "keyboard_layout": "",
    # Crash dummy specific (ignored when app_command is set)
    "app_mode": "normal",
    "app_size": "800x600",
}
_worker = None      # Worker instance
_work_dir = None     # Temp working directory


class Worker:
    """Manages a worker subprocess that runs wbox operations."""

    def __init__(self, work_dir: str):
        self.work_dir = work_dir
        self.proc = None

    async def start(self, config: dict):
        cfg = self._build_wbox_config(config)
        config_path = os.path.join(self.work_dir, "config.yaml")
        with open(config_path, "w") as f:
            yaml.dump(cfg, f)

        worker_py = str(Path(__file__).parent / "worker.py")
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, worker_py, config_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.work_dir,
        )
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=10)
        msg = json.loads(line)
        if not msg.get("ready"):
            raise RuntimeError(f"Worker failed to start: {msg}")

    async def call(self, method: str, **kwargs) -> dict:
        if not self.proc or self.proc.returncode is not None:
            return {"error": "worker not running"}
        cmd = json.dumps({"method": method, "args": kwargs}) + "\n"
        self.proc.stdin.write(cmd.encode())
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=30)
        if not line:
            return {"error": "worker closed unexpectedly"}
        return json.loads(line)

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.call("stop"), timeout=10)
            except Exception:
                pass
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None

    def _build_wbox_config(self, config: dict) -> dict:
        custom_cmd = config.get("app_command", "")
        post_keys = config.get("post_launch_keys", [])
        post_keys_delay = config.get("post_launch_keys_delay", 0.5)
        if custom_cmd:
            app = {
                "command": custom_cmd,
                "env": config.get("app_env", {}),
                "post_launch_keys": post_keys,
                "post_launch_keys_delay": post_keys_delay,
            }
            name = "mcp-dev"
        else:
            # Default: crash dummy
            app = {
                "command": f"{sys.executable} {CRASH_DUMMY_PY}",
                "env": {
                    "CRASH_DUMMY_LOG": os.path.join(self.work_dir, "crash_dummy.log"),
                    "CRASH_DUMMY_FIFO": os.path.join(self.work_dir, "crash_dummy.fifo"),
                    "CRASH_DUMMY_MODE": config.get("app_mode", "normal"),
                    "CRASH_DUMMY_SIZE": config.get("app_size", "800x600"),
                },
                "post_launch_keys": post_keys,
                "post_launch_keys_delay": post_keys_delay,
            }
            name = "crash-dummy-dev"

        return {
            "name": name,
            "compositor": config["compositor"],
            "screen": config["screen"],
            "input_backend": config["input_backend"],
            "undecorate": config.get("undecorate", True),
            "keyboard_layout": config.get("keyboard_layout", ""),
            "log": {"dir": os.path.join(self.work_dir, "log"), "level": "debug"},
            "screenshot_dir": os.path.join(self.work_dir, "screenshots"),
            "app": app,
        }


# ── Helpers ──

def _ensure_work_dir() -> str:
    global _work_dir
    if not _work_dir:
        _work_dir = tempfile.mkdtemp(prefix="mcp-dev-")
        os.makedirs(os.path.join(_work_dir, "log"), exist_ok=True)
        os.makedirs(os.path.join(_work_dir, "screenshots"), exist_ok=True)
    return _work_dir


def _fifo_path() -> str:
    return os.path.join(_work_dir or "", "crash_dummy.fifo")


def _log_path() -> str:
    return os.path.join(_work_dir or "", "crash_dummy.log")


def _text(msg) -> list:
    if isinstance(msg, dict):
        msg = json.dumps(msg, indent=2)
    return [TextContent(type="text", text=str(msg))]


# ── MCP Tools ──

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="configure",
            description="Set wbox config. Default app is crash dummy. Set app_command to run any app.",
            inputSchema={
                "type": "object",
                "properties": {
                    "compositor": {"type": "string", "enum": ["labwc", "weston", "cage"]},
                    "input_backend": {"type": "string", "description": "x11, wayland, hybrid, or per-function dict"},
                    "screen": {"type": "string", "description": "e.g. 1280x800"},
                    "undecorate": {"type": "boolean"},
                    "app_command": {"type": "string", "description": "App command to launch (empty = crash dummy)"},
                    "app_env": {"type": "object", "description": "Environment variables for the app"},
                    "post_launch_keys": {"type": "array", "items": {"type": "string"}, "description": "Keys to send after app launch (e.g. ['F11'] for fullscreen)"},
                    "post_launch_keys_delay": {"type": "number", "description": "Delay in seconds before/between post_launch_keys (default 0.5)"},
                    "keyboard_layout": {"type": "string", "description": "XKB keyboard layout (e.g. fr, us, de). Empty = inherit from host"},
                    "app_mode": {"type": "string", "enum": ["normal", "fixed", "fullscreen"], "description": "Crash dummy mode (ignored if app_command set)"},
                    "app_size": {"type": "string", "description": "Crash dummy size (ignored if app_command set)"},
                },
            },
        ),
        Tool(name="launch", description="Launch wbox with current config (default: crash dummy)",
             inputSchema={"type": "object"}),
        Tool(name="stop", description="Stop wbox compositor and crash dummy",
             inputSchema={"type": "object"}),
        Tool(name="reload", description="Kill worker subprocess — next launch uses fresh code",
             inputSchema={"type": "object"}),
        Tool(name="status", description="Show current config and worker state",
             inputSchema={"type": "object"}),
        # ── compositor tools ──
        Tool(name="screenshot", description="Take a screenshot",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
             }}),
        Tool(name="click", description="Click at (x, y)",
             inputSchema={"type": "object", "properties": {
                 "x": {"type": "integer"}, "y": {"type": "integer"},
                 "button": {"type": "integer", "default": 1},
             }, "required": ["x", "y"]}),
        Tool(name="mouse_move", description="Move mouse to (x, y)",
             inputSchema={"type": "object", "properties": {
                 "x": {"type": "integer"}, "y": {"type": "integer"},
             }, "required": ["x", "y"]}),
        Tool(name="type_text", description="Type text via keyboard",
             inputSchema={"type": "object", "properties": {
                 "text": {"type": "string"},
             }, "required": ["text"]}),
        Tool(name="key", description="Press key combo (e.g. ctrl+a)",
             inputSchema={"type": "object", "properties": {
                 "shortcut": {"type": "string"},
             }, "required": ["shortcut"]}),
        Tool(name="clipboard_read", description="Read clipboard",
             inputSchema={"type": "object"}),
        Tool(name="clipboard_write", description="Write to clipboard",
             inputSchema={"type": "object", "properties": {
                 "text": {"type": "string"},
             }, "required": ["text"]}),
        Tool(name="get_mouse_position", description="Get mouse position",
             inputSchema={"type": "object"}),
        Tool(name="get_size", description="Get compositor display size",
             inputSchema={"type": "object"}),
        Tool(name="resize", description="Resize display",
             inputSchema={"type": "object", "properties": {
                 "width": {"type": "integer"}, "height": {"type": "integer"},
             }, "required": ["width", "height"]}),
        # ── window management tools ──
        Tool(name="list_windows", description="List all windows/toplevels in the compositor",
             inputSchema={"type": "object"}),
        Tool(name="focus_window", description="Focus/raise a window by title or app_id",
             inputSchema={"type": "object", "properties": {
                 "title": {"type": "string", "description": "Window title (substring match)"},
                 "app_id": {"type": "string", "description": "Application ID"},
             }}),
        # ── crash dummy specific tools ──
        Tool(name="send_cmd", description="Send command to crash dummy FIFO (only works with crash dummy app)",
             inputSchema={"type": "object", "properties": {
                 "command": {"type": "string"},
             }, "required": ["command"]}),
        Tool(name="tail_log", description="Show last N lines of crash dummy log (only works with crash dummy app)",
             inputSchema={"type": "object", "properties": {
                 "lines": {"type": "integer", "default": 50},
             }}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    global _worker, _config

    # ── Meta tools ──

    if name == "configure":
        for k, v in arguments.items():
            if k in _config:
                _config[k] = v
        return _text({"configured": _config})

    if name == "status":
        return _text({
            "config": _config,
            "worker_running": _worker is not None and _worker.proc and _worker.proc.returncode is None,
            "work_dir": _work_dir,
        })

    if name == "launch":
        wd = _ensure_work_dir()
        if _worker:
            await _worker.stop()
        worker = Worker(wd)
        await worker.start(_config)
        _worker = worker
        result = await worker.call("launch")
        return _text(result)

    if name == "stop":
        if _worker:
            result = await _worker.call("stop")
            await _worker.stop()
            _worker = None
            return _text(result)
        return _text({"error": "not running"})

    if name == "reload":
        if _worker:
            try:
                await _worker.call("stop")
            except Exception:
                pass
            await _worker.stop()
            _worker = None
        return _text({"reloaded": True, "hint": "use launch to start with fresh code"})

    # ── Screenshot (returns image) ──

    if name == "screenshot":
        if not _worker:
            return _text({"error": "not running"})
        result = await _worker.call("screenshot", **arguments)
        if "error" in result:
            return _text(result)
        path = result.get("path", "")
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            return [ImageContent(type="image", data=data, mimeType="image/png")]
        return _text(result)

    # ── Crash dummy direct tools ──

    if name == "send_cmd":
        fifo = _fifo_path()
        cmd = arguments.get("command", "")
        if not fifo or not os.path.exists(fifo):
            return _text({"error": f"FIFO not found — is crash dummy running?"})
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, (cmd + "\n").encode())
            os.close(fd)
        except OSError as e:
            return _text({"error": f"FIFO write failed: {e}"})
        if cmd.strip() == "dump":
            await asyncio.sleep(0.3)
            log = _log_path()
            if os.path.exists(log):
                with open(log) as f:
                    for line in reversed(f.readlines()):
                        if "DUMP " in line:
                            # Extract JSON part
                            idx = line.index("DUMP ")
                            return _text(line[idx + 5:].strip())
            return _text({"sent": cmd, "warning": "dump not found in log"})
        return _text({"sent": cmd})

    if name == "tail_log":
        log = _log_path()
        n = arguments.get("lines", 50)
        if not log or not os.path.exists(log):
            return _text({"error": "log not found"})
        with open(log) as f:
            lines = f.readlines()
        return _text("".join(lines[-n:]))

    # ── Proxy to worker ──

    if not _worker:
        return _text({"error": "not running — use launch first"})

    result = await _worker.call(name, **arguments)
    return _text(result)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
