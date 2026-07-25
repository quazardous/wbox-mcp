"""
server.py — Generic MCP stdio server for wbox-mcp.

Loads config.yaml, creates a compositor backend,
exposes compositor tools + custom script-mapped tools + built-in log tool.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import logging.handlers
import os
import shlex
import subprocess
from collections import deque
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
from .config import apply_overrides, load_config, resolve_dir

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
    undecorate = cfg.get("undecorate", True)
    keyboard_layout = cfg.get("keyboard_layout", "")

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
            undecorate=undecorate,
            keyboard_layout=keyboard_layout,
        )
    elif backend == "labwc":
        from .compositor.labwc import LabwcCompositor
        comp = LabwcCompositor(
            screen=screen,
            instance_name=instance_name,
            timeouts=timeouts,
            input_backend=input_backend,
            undecorate=undecorate,
            keyboard_layout=keyboard_layout,
        )
        return comp
    else:
        from .compositor.cage import CageCompositor
        return CageCompositor(
            screen=screen,
            instance_name=instance_name,
            timeouts=timeouts,
            input_backend=input_backend,
            undecorate=undecorate,
            keyboard_layout=keyboard_layout,
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


def _reply(result) -> list[TextContent]:
    """Render a compositor result compactly for the model.

    {'ok': True, ...} noise becomes 'ok'; errors become a plain string;
    anything else is valid JSON instead of a Python repr.
    """
    if isinstance(result, dict):
        if "error" in result:
            text = f"Error: {result['error']}"
        else:
            slim = {k: v for k, v in result.items()
                    if not (k == "ok" and v is True) and v != ""}
            text = json.dumps(slim, default=str) if slim else "ok"
    else:
        text = str(result)
    return [TextContent(type="text", text=text)]


def _cap_output(text: str, logfile: Path, head: int = 20, tail: int = 80) -> str:
    """Bound script output returned to the model; the full log stays on disk."""
    lines = text.splitlines()
    if len(lines) <= head + tail:
        return text
    kept = (lines[:head]
            + [f"... ({len(lines) - head - tail} lines truncated, full log: {logfile})"]
            + lines[-tail:])
    return "\n".join(kept) + "\n"


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

    try:
        resolved_args = [a.format(**context) for a in args]
    except (KeyError, IndexError, ValueError) as exc:
        return f"Error: bad placeholder in tool args {args!r}: {exc}"

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

    # Timeout: per-tool > global config > 120s default
    timeout = tool_def.get("timeout", cfg.get("tool_timeout", 120))

    stdout_lines = []
    timed_out = False
    proc = None
    with logfile.open("w") as f:
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

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=cwd,
            )
        except OSError as exc:
            f.write(f"\n--- failed to start: {exc} ---\n")
            return f"Error: cannot run script '{script}': {exc}"

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
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
        finally:
            if timed_out:
                f.write(f"\n--- TIMEOUT after {timeout}s, killing ---\n")
            if proc.returncode is None:
                if timed_out:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()

        end = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n--- finished: {end}, exit_code: {proc.returncode} ---\n")

    stdout_text = _cap_output("".join(stdout_lines), logfile)

    if timed_out:
        return f"Script timed out after {timeout}s (killed)\n{stdout_text}\n(full log: {logfile})"
    if proc.returncode != 0:
        return f"Script exited with code {proc.returncode}\n{stdout_text}\n(full log: {logfile})"
    return (stdout_text or "(no output)") + f"\n(full log: {logfile})"


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

    # Setup file logging (rotating, and idempotent across create_server calls)
    log_level = cfg.get("log", {}).get("level", "info").upper()
    log_file = cfg["_log_dir"] / "wbox-mcp.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=2)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    wbox_logger = logging.getLogger("wbox")
    for h in [h for h in wbox_logger.handlers if isinstance(h, logging.FileHandler)]:
        wbox_logger.removeHandler(h)
        h.close()
    wbox_logger.addHandler(file_handler)
    wbox_logger.setLevel(getattr(logging, log_level, logging.INFO))

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
                        "scale": {
                            "type": "number",
                            "description": "Scale factor (e.g. 0.5 for half size) — Linux/grim only",
                        },
                        "region": {
                            "type": "string",
                            "description": "Capture region 'x,y WxH' — Linux/grim only",
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
                name="list_windows",
                description="List all windows/toplevels in the compositor",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="focus_window",
                description="Focus/raise a window by title or app_id",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Window title (substring match)"},
                        "app_id": {"type": "string", "description": "Application ID"},
                    },
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
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        script, shell=True, cwd=cwd,
                        capture_output=True, text=True, timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    return [TextContent(type="text", text=f"pre_launch timed out after 30s: {script}")]
                if result.returncode != 0:
                    out = "\n".join(s for s in (result.stdout, result.stderr) if s)
                    return [TextContent(type="text", text=f"pre_launch failed: {script}\n{out}")]

            result = await asyncio.to_thread(compositor.launch, app_cmd, app_env)
            log.info("launch result: %s", result)
            return _reply(result)

        if name == "stop":
            result = await asyncio.to_thread(compositor.stop)
            log.info("stop result: %s", result)
            return _reply(result)

        if name == "kill":
            result = await asyncio.to_thread(
                compositor.kill, aggressive=arguments.get("aggressive", True)
            )
            log.info("kill result: %s", result)
            return _reply(result)

        if name == "screenshot":
            result = await asyncio.to_thread(
                compositor.screenshot, arguments.get("name"),
                arguments.get("scale"), arguments.get("region"),
            )
            if "error" in result:
                return [TextContent(type="text", text=result["error"])]
            img_path = Path(result["path"])
            log.info("screenshot: %s (%d bytes)", img_path, result["size"])
            img_bytes = await asyncio.to_thread(img_path.read_bytes)
            img_data = base64.standard_b64encode(img_bytes).decode()
            return [
                ImageContent(type="image", data=img_data, mimeType="image/png"),
                TextContent(type="text", text=result["path"]),
            ]

        if name == "click":
            result = await asyncio.to_thread(
                compositor.click,
                arguments["x"], arguments["y"], arguments.get("button", 1),
            )
            return _reply(result)

        if name == "type_text":
            result = await asyncio.to_thread(compositor.type_text, arguments["text"])
            return _reply(result)

        if name == "key":
            result = await asyncio.to_thread(compositor.key, arguments["shortcut"])
            return _reply(result)

        if name == "keys":
            shortcuts = arguments.get("shortcuts")
            if not shortcuts:
                shortcut = arguments.get("shortcut")
                if not shortcut:
                    return [TextContent(type="text", text="Error: provide 'shortcuts' list or 'shortcut' + 'repeat'")]
                repeat = arguments.get("repeat", 1)
                shortcuts = [shortcut] * repeat
            delay_ms = arguments.get("delay_ms", 100)
            result = await asyncio.to_thread(compositor.keys, shortcuts, delay_ms)
            if "error" in result:
                text = f"Sent {result.get('sent', 0)}/{len(shortcuts)} keys\n{result['error']}"
            else:
                text = f"Sent {result.get('sent', len(shortcuts))}/{len(shortcuts)} keys"
            return [TextContent(type="text", text=text)]

        if name == "mouse_move":
            result = await asyncio.to_thread(
                compositor.mouse_move, arguments["x"], arguments["y"]
            )
            return _reply(result)

        if name == "get_mouse_position":
            result = await asyncio.to_thread(compositor.get_mouse_position)
            return _reply(result)

        if name == "get_size":
            result = await asyncio.to_thread(compositor.get_size)
            return _reply(result)

        if name == "resize":
            result = await asyncio.to_thread(
                compositor.resize, arguments["width"], arguments["height"]
            )
            return _reply(result)

        if name == "list_windows":
            result = await asyncio.to_thread(compositor.list_windows)
            return _reply(result)

        if name == "focus_window":
            result = await asyncio.to_thread(
                compositor.focus_window,
                title=arguments.get("title", ""),
                app_id=arguments.get("app_id", ""),
            )
            return _reply(result)

        if name == "clean":
            cleaned = []
            # Never unlink the live server log — the FileHandler keeps the old
            # inode open and all further logging would be silently lost
            active_log = cfg["_log_dir"] / "wbox-mcp.log"
            for d, label in [
                (cfg.get("_log_dir"), "logs"),
                (cfg.get("_screenshot_dir"), "screenshots"),
            ]:
                if d and d.exists():
                    count = 0
                    skipped = 0
                    for f in d.iterdir():
                        if f.is_file() and f != active_log:
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

            def _tail():
                with log_file.open(errors="replace") as fh:
                    return "".join(deque(fh, maxlen=n))

            return [TextContent(type="text", text=await asyncio.to_thread(_tail))]

        if name == "debug_input":
            result = await asyncio.to_thread(
                compositor.debug_input,
                arguments.get("test_key", "a"),
                arguments.get("target", "xev"),
            )
            return _reply(result)

        if name == "clipboard_read":
            result = await asyncio.to_thread(compositor.clipboard_read)
            if "error" in result:
                return [TextContent(type="text", text=result["error"])]
            return [TextContent(type="text", text=result["text"])]

        if name == "clipboard_write":
            result = await asyncio.to_thread(compositor.clipboard_write, arguments["text"])
            return _reply(result)

        # Script-mapped tools
        if name in script_tools:
            output = await _run_script_tool(
                compositor, script_tools[name], arguments, cfg
            )
            return [TextContent(type="text", text=output)]

        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return mcp, compositor


async def amain(config_path: str | None = None, overrides: list[str] | None = None):
    cfg = load_config(config_path or "config.yaml")
    if not cfg.get("_config_dir"):
        cfg["_config_dir"] = str(Path(config_path).parent) if config_path else "."
    if overrides:
        apply_overrides(cfg, overrides)

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
