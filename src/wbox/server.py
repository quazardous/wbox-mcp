"""
server.py — Generic MCP stdio server for wbox-mcp.

Loads config.yaml, creates a compositor backend,
exposes compositor tools + custom script-mapped tools + built-in log tool.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import os
import shlex
import subprocess
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    ImageContent,
    TextContent,
    Tool,
)

import sys

from .compositor import CompositorServer
from .config import load_config, resolve_dir

log = logging.getLogger(__name__)


def build_compositor(cfg: dict) -> CompositorServer:
    """Build a compositor backend from config."""
    # Auto-detect backend on Windows if not specified
    default_backend = "win32" if sys.platform == "win32" else "labwc"
    backend = cfg.get("compositor", default_backend)
    screen = cfg.get("screen", "1280x800")
    instance_name = cfg.get("name", "")
    timeouts = cfg.get("timeouts", {})
    input_backend = cfg.get("input_backend", "hybrid")

    if backend == "win32":
        from .compositor.win32 import Win32Compositor
        return Win32Compositor(
            screen=screen,
            instance_name=instance_name,
            timeouts=timeouts,
            title_hint=cfg.get("title_hint", ""),
        )
    elif backend == "weston":
        from .compositor.weston import WestonCompositor
        return WestonCompositor(
            screen=screen,
            shell=cfg.get("weston_shell", "kiosk"),
            backend=cfg.get("weston_backend", "wayland"),
            instance_name=instance_name,
            timeouts=timeouts,
            input_backend=input_backend,
        )
    elif backend == "labwc":
        from .compositor.labwc import LabwcCompositor
        comp = LabwcCompositor(
            screen=screen,
            instance_name=instance_name,
            timeouts=timeouts,
            input_backend=input_backend,
        )
        return comp
    else:
        from .compositor.cage import CageCompositor
        return CageCompositor(
            screen=screen,
            instance_name=instance_name,
            timeouts=timeouts,
            input_backend=input_backend,
        )


def _build_app_cmd(cfg: dict) -> list[str]:
    """Build app command from config."""
    app_cfg = cfg.get("app", {})
    command = app_cfg.get("command", "")
    if not command:
        return []
    if isinstance(command, list):
        return command
    return shlex.split(command)


def _build_app_env(cfg: dict) -> dict[str, str]:
    """Build app environment from config."""
    app_cfg = cfg.get("app", {})
    return dict(app_cfg.get("env", {}))


# ── Script-mapped tools ────────────────────────────────────────────


async def _run_script_tool(
    compositor: CompositorServer,
    tool_def: dict,
    arguments: dict,
    cfg: dict,
) -> str:
    """Execute a script-mapped tool."""
    script = tool_def["script"]
    args = list(tool_def.get("args", []))

    context = {
        "wayland_display": compositor.state.wayland_display,
        "x_display": compositor.state.x_display,
        "compositor_pid": str(
            compositor.state.compositor_pid
            or (compositor.state.compositor_proc.pid if compositor.state.compositor_proc else "")
        ),
        "app_pid": str(compositor.state.app_pid or ""),
    }
    # Add all app env vars to context
    app_env = _build_app_env(cfg)
    context.update(app_env)
    context.update(arguments)

    resolved_args = [a.format(**context) for a in args]

    if not tool_def.get("headless"):
        if not compositor.state.wayland_display:
            compositor.reload_state()

        if not compositor.is_running():
            return f"Error: compositor is not running. Call 'launch' first.\n(state: wayland_display={compositor.state.wayland_display!r}, pid={compositor.state.compositor_pid})"

    env = os.environ.copy()
    # Remove venv from env so scripts use system tools
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    if "VIRTUAL_ENV" in os.environ:
        venv_bin = os.environ["VIRTUAL_ENV"] + "/bin"
        env["PATH"] = ":".join(
            p for p in env.get("PATH", "").split(":") if p != venv_bin
        )
    env["WBOX_WAYLAND_DISPLAY"] = compositor.state.wayland_display
    env["WBOX_X_DISPLAY"] = compositor.state.x_display
    # Also set generic COMPOSITOR_ prefix for compat
    env["COMPOSITOR_WAYLAND_DISPLAY"] = compositor.state.wayland_display
    env["COMPOSITOR_X_DISPLAY"] = compositor.state.x_display
    # Forward app env
    env.update(app_env)
    # Forward MCP tool arguments as WBOX_ARG_<NAME> env vars
    for k, v in arguments.items():
        env[f"WBOX_ARG_{k.upper()}"] = str(v)

    cmd = [script] + resolved_args
    tool_name = Path(script).stem
    log_dir = cfg.get("_log_dir", Path("/tmp"))
    logfile = log_dir / f"{tool_name}.log"
    log.info("Running script tool: %s (log: %s)", " ".join(cmd), logfile)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cwd = str(Path(cfg.get("_config_dir", ".")).resolve())

    f = logfile.open("w")
    f.write(f"=== {tool_name} ===\n")
    f.write(f"started: {now}\n")
    f.write(f"command: {' '.join(cmd)}\n")
    f.write(f"cwd:     {cwd}\n")
    f.write(f"env:\n")
    for k in sorted(env):
        if k.startswith(("WBOX_", "COMPOSITOR_", "DISPLAY", "WAYLAND_", "GDK_", "SAL_")):
            f.write(f"  {k}={env[k]}\n")
    f.write(f"\n--- output ---\n")
    f.flush()

    # Timeout: per-tool > global config > 120s default
    timeout = tool_def.get("timeout", cfg.get("tool_timeout", 120))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        cwd=cwd,
    )

    stdout_lines = []
    timed_out = False
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not line:
                break
            text = line.decode(errors="replace")
            stdout_lines.append(text)
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            f.write(f"[{ts}] {text}")
            f.flush()
    except Exception as exc:
        f.write(f"\n--- exception: {exc} ---\n")

    if timed_out:
        f.write(f"\n--- TIMEOUT after {timeout}s, killing ---\n")
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass

    if proc.returncode is None:
        await proc.wait()

    stdout_text = "".join(stdout_lines)

    end = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f.write(f"\n--- finished: {end}, exit_code: {proc.returncode} ---\n")
    f.close()

    if timed_out:
        return f"Script timed out after {timeout}s (killed)\n{stdout_text}\n(full log: {logfile})"
    if proc.returncode != 0:
        return f"Script exited with code {proc.returncode}\n{stdout_text}\n(full log: {logfile})"
    return stdout_text or "(no output)"


# ── MCP Server ──────────────────────────────────────────────────────


def create_server(cfg: dict) -> tuple[Server, CompositorServer]:
    compositor = build_compositor(cfg)
    script_tools = cfg.get("tools", {})
    app_cmd = _build_app_cmd(cfg)
    app_env = _build_app_env(cfg)

    # Resolve log and screenshot dirs
    cfg["_log_dir"] = resolve_dir(cfg, "log.dir", "./log")
    cfg["_screenshot_dir"] = resolve_dir(cfg, "screenshot_dir", "./screenshots")
    compositor.state.screenshot_dir = cfg["_screenshot_dir"]

    # Set compositor log dir for stderr capture (Linux only)
    if sys.platform != "win32":
        from .compositor.cage import CageCompositor
        from .compositor.labwc import LabwcCompositor
        if isinstance(compositor, (CageCompositor, LabwcCompositor)):
            compositor.set_log_dir(cfg["_log_dir"])

    # Setup file logging
    log_level = cfg.get("log", {}).get("level", "info").upper()
    log_file = cfg["_log_dir"] / "wbox-mcp.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger("wbox").addHandler(file_handler)
    logging.getLogger("wbox").setLevel(getattr(logging, log_level, logging.INFO))

    server_name = cfg.get("name", "wbox-mcp")
    mcp = Server(server_name)

    # Pre-launch hooks from config
    pre_launch_scripts = cfg.get("app", {}).get("pre_launch", [])

    @mcp.list_tools()
    async def list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="launch",
                description="Launch the compositor with the app inside",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="stop",
                description="Stop the compositor and app",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="kill",
                description="Force-kill all compositor processes and clean state",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "aggressive": {
                            "type": "boolean",
                            "description": "Also kill orphan compositor processes",
                            "default": True,
                        },
                    },
                },
            ),
            Tool(
                name="screenshot",
                description="Take a screenshot of the compositor display",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Optional filename for the screenshot",
                        },
                    },
                },
            ),
            Tool(
                name="click",
                description="Click at position (x, y)",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "X coordinate"},
                        "y": {"type": "integer", "description": "Y coordinate"},
                        "button": {
                            "type": "integer",
                            "description": "Mouse button (1=left, 2=middle, 3=right)",
                            "default": 1,
                        },
                    },
                    "required": ["x", "y"],
                },
            ),
            Tool(
                name="type_text",
                description="Type text into the focused widget",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to type"},
                    },
                    "required": ["text"],
                },
            ),
            Tool(
                name="key",
                description="Send a keyboard shortcut (e.g. 'alt+F12', 'Escape', 'ctrl+s')",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "shortcut": {
                            "type": "string",
                            "description": "Key combination",
                        },
                    },
                    "required": ["shortcut"],
                },
            ),
            Tool(
                name="keys",
                description="Send multiple keyboard shortcuts in sequence with a delay between each",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "shortcuts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of key combinations to send in order",
                        },
                        "shortcut": {
                            "type": "string",
                            "description": "Single shortcut to repeat (use with 'repeat')",
                        },
                        "repeat": {
                            "type": "integer",
                            "description": "Number of times to repeat 'shortcut' (default 1)",
                            "default": 1,
                        },
                        "delay_ms": {
                            "type": "integer",
                            "description": "Delay in milliseconds between each key press (default 100)",
                            "default": 100,
                        },
                    },
                },
            ),
            Tool(
                name="mouse_move",
                description="Move mouse to (x, y) without clicking",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                },
            ),
            Tool(
                name="get_mouse_position",
                description="Get the current mouse cursor position",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_size",
                description="Get the current compositor display size",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="resize",
                description="Resize the compositor display",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "width": {"type": "integer", "description": "New width"},
                        "height": {"type": "integer", "description": "New height"},
                    },
                    "required": ["width", "height"],
                },
            ),
            Tool(
                name="clean",
                description="Clean logs and screenshots",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="tail_log",
                description="Show the last N lines of the wbox-mcp log",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "lines": {
                            "type": "integer",
                            "description": "Number of lines to show (default 50)",
                            "default": 50,
                        },
                    },
                },
            ),
            Tool(
                name="debug_input",
                description="Debug keyboard input: test key delivery via different methods",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "test_key": {
                            "type": "string",
                            "description": "Key to test (default: 'a')",
                            "default": "a",
                        },
                        "target": {
                            "type": "string",
                            "description": "Target: 'xev' (baseline), 'active' (focus method), 'window' (XSendEvent)",
                            "default": "xev",
                            "enum": ["xev", "active", "window"],
                        },
                    },
                },
            ),
            Tool(
                name="clipboard_read",
                description="Read text from the compositor's X11 clipboard",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="clipboard_write",
                description="Write text to the compositor's X11 clipboard",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to write to clipboard"},
                    },
                    "required": ["text"],
                },
            ),
        ]

        for name, tdef in script_tools.items():
            schema = tdef.get("schema", {"type": "object", "properties": {}})
            tools.append(
                Tool(
                    name=name,
                    description=tdef.get("description", f"Custom tool: {name}"),
                    inputSchema=schema,
                )
            )

        return tools

    @mcp.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent]:
        log.info("tool_call: %s %s", name, arguments or "")

        if name == "launch":
            # Run pre-launch scripts
            cwd = str(Path(cfg.get("_config_dir", ".")).resolve())
            for script in pre_launch_scripts:
                log.info("pre_launch: %s", script)
                result = subprocess.run(
                    script, shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    return [TextContent(type="text", text=f"pre_launch failed: {script}\n{result.stderr}")]

            result = compositor.launch(app_cmd, app_env)
            log.info("launch result: %s", result)
            return [TextContent(type="text", text=str(result))]

        if name == "stop":
            result = compositor.stop()
            log.info("stop result: %s", result)
            return [TextContent(type="text", text=str(result))]

        if name == "kill":
            result = compositor.kill(aggressive=arguments.get("aggressive", True))
            log.info("kill result: %s", result)
            return [TextContent(type="text", text=str(result))]

        if name == "screenshot":
            result = compositor.screenshot(arguments.get("name"))
            if "error" in result:
                return [TextContent(type="text", text=result["error"])]
            img_path = Path(result["path"])
            log.info("screenshot: %s (%d bytes)", img_path, result["size"])
            img_data = base64.standard_b64encode(img_path.read_bytes()).decode()
            return [
                ImageContent(type="image", data=img_data, mimeType="image/png"),
            ]

        if name == "click":
            result = compositor.click(
                arguments["x"], arguments["y"], arguments.get("button", 1)
            )
            return [TextContent(type="text", text=str(result))]

        if name == "type_text":
            result = compositor.type_text(arguments["text"])
            return [TextContent(type="text", text=str(result))]

        if name == "key":
            result = compositor.key(arguments["shortcut"])
            return [TextContent(type="text", text=str(result))]

        if name == "keys":
            shortcuts = arguments.get("shortcuts")
            if not shortcuts:
                shortcut = arguments.get("shortcut")
                if not shortcut:
                    return [TextContent(type="text", text="Error: provide 'shortcuts' list or 'shortcut' + 'repeat'")]
                repeat = arguments.get("repeat", 1)
                shortcuts = [shortcut] * repeat
            delay_ms = arguments.get("delay_ms", 100)
            results = []
            for i, sc in enumerate(shortcuts):
                result = compositor.key(sc)
                results.append(f"{sc}: {result}")
                if "error" in result:
                    break
                if i < len(shortcuts) - 1 and delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
            return [TextContent(type="text", text=f"Sent {len(results)}/{len(shortcuts)} keys\n" + "\n".join(results))]

        if name == "mouse_move":
            result = compositor.mouse_move(arguments["x"], arguments["y"])
            return [TextContent(type="text", text=str(result))]

        if name == "get_mouse_position":
            result = compositor.get_mouse_position()
            return [TextContent(type="text", text=str(result))]

        if name == "get_size":
            result = compositor.get_size()
            return [TextContent(type="text", text=str(result))]

        if name == "resize":
            result = compositor.resize(arguments["width"], arguments["height"])
            return [TextContent(type="text", text=str(result))]

        if name == "clean":
            cleaned = []
            for d, label in [
                (cfg.get("_log_dir"), "logs"),
                (cfg.get("_screenshot_dir"), "screenshots"),
            ]:
                if d and d.exists():
                    count = 0
                    skipped = 0
                    for f in d.iterdir():
                        if f.is_file():
                            try:
                                f.unlink()
                                count += 1
                            except PermissionError:
                                skipped += 1
                    msg = f"{label}: {count} files removed"
                    if skipped:
                        msg += f" ({skipped} locked, skipped)"
                    cleaned.append(msg)
            return [TextContent(type="text", text="\n".join(cleaned) or "nothing to clean")]

        if name == "tail_log":
            n = arguments.get("lines", 50)
            log_file = cfg["_log_dir"] / "wbox-mcp.log"
            if not log_file.exists():
                return [TextContent(type="text", text="No log file found")]
            lines = log_file.read_text().splitlines()
            tail = lines[-n:]
            return [TextContent(type="text", text="\n".join(tail))]

        if name == "debug_input":
            result = compositor.debug_input(
                arguments.get("test_key", "a"),
                arguments.get("target", "xev"),
            )
            return [TextContent(type="text", text=str(result))]

        if name == "clipboard_read":
            result = compositor.clipboard_read()
            if "error" in result:
                return [TextContent(type="text", text=result["error"])]
            return [TextContent(type="text", text=result["text"])]

        if name == "clipboard_write":
            result = compositor.clipboard_write(arguments["text"])
            return [TextContent(type="text", text=str(result))]

        # Script-mapped tools
        if name in script_tools:
            output = await _run_script_tool(
                compositor, script_tools[name], arguments, cfg
            )
            return [TextContent(type="text", text=output)]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return mcp, compositor


async def amain(config_path: str | None = None):
    cfg = load_config(config_path or "config.yaml")
    if not cfg.get("_config_dir"):
        cfg["_config_dir"] = str(Path(config_path).parent) if config_path else "."

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    mcp, compositor = create_server(cfg)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp.run(
                read_stream, write_stream, mcp.create_initialization_options()
            )
    finally:
        if compositor.state.compositor_proc is not None and compositor.is_running():
            compositor.stop()
