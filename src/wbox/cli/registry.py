"""
wboxr CLI — Registry/admin tool for wbox-mcp.

Usage:
    wboxr init [directory] [OPTIONS]   Setup wizard (create or reconfigure)
    wboxr tool add [directory]         Add a custom script tool
    wboxr tool remove <name> [dir]     Remove a tool
    wboxr tool list [directory]        List all tools
    wboxr register [directory]         Register in .mcp.json
    wboxr unregister <name>            Remove from .mcp.json
    wboxr list                         Find instances in Claude settings

Init options (non-interactive mode):
    --name NAME              Instance name
    --compositor TYPE        weston, cage, or win32
    --screen WxH             Screen size (e.g. 1280x800) — Linux only
    --weston-backend TYPE    wayland or x11 — Linux only
    --weston-shell TYPE      kiosk or desktop — Linux only
    --title-hint TEXT        Window title substring — Windows only
    --app-command CMD        App command to launch
    --app-env KEY=VALUE      Environment variable (repeatable)
    --pre-launch CMD         Pre-launch script (repeatable)
    --tool NAME:SCRIPT:DESC  Custom tool (repeatable)
    --mcp-dir DIR            Explicit directory for config/log/screenshots
    --from FILE              Load config from a YAML template
    --register               Auto-register in .mcp.json after init

If no directory or --mcp-dir is given:
    - In a project root (.git, pyproject.toml, etc.) → defaults to ./wbox
    - Otherwise → defaults to current directory

Register options:
    --global                 Register in ~/.claude.json instead of .mcp.json
    --file PATH              Register in a specific file
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

from wbox.config import DEFAULT_CONFIG, load_config, save_config

_IS_WIN32 = sys.platform == "win32"


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _prompt(label: str, default: str = "") -> str:
    """Prompt user with optional default value."""
    if default:
        raw = input(f"  {label} ({default}): ").strip()
        return raw if raw else default
    raw = input(f"  {label}: ").strip()
    return raw


def _prompt_yn(label: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    raw = input(f"  {label} ({d}): ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _prompt_env() -> dict[str, str]:
    """Prompt for environment variables."""
    env: dict[str, str] = {}
    print("  Environment variables (KEY=VALUE, empty line to finish):")
    while True:
        raw = input("    > ").strip()
        if not raw:
            break
        if "=" not in raw:
            print("    Invalid format, use KEY=VALUE")
            continue
        k, v = raw.split("=", 1)
        env[k.strip()] = v.strip()
    return env


_PROJECT_ROOT_MARKERS = (
    ".git", ".hg", ".svn",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "Makefile", "CMakeLists.txt", "pom.xml", "build.gradle",
    ".mcp.json", "CLAUDE.md",
)


def _is_project_root(d: Path) -> bool:
    """Check if directory looks like a project root."""
    return any((d / marker).exists() for marker in _PROJECT_ROOT_MARKERS)


def _resolve_default_dir() -> Path:
    """Smart default: ./wbox if in a project root, else current dir."""
    cwd = Path.cwd()
    if _is_project_root(cwd):
        return cwd / "wbox"
    return cwd


def _resolve_config_path(directory: str | None) -> Path:
    if directory:
        d = Path(directory)
    else:
        d = _resolve_default_dir()
    return d / "config.yaml"


def _detect_wbox_command() -> tuple[str, list[str]]:
    """Detect the best way to invoke wbox-mcp."""
    # 1. Installed mode — wbox-mcp on PATH
    if shutil.which("wbox-mcp"):
        return "wbox-mcp", ["serve"]
    # 2. Dev mode — absolute path to venv binary
    venv_bin = Path(sys.executable).parent / "wbox-mcp"
    if _IS_WIN32:
        # Windows: check for .exe or .cmd shim
        for ext in (".exe", ".cmd", ""):
            candidate = venv_bin.with_suffix(ext)
            if candidate.exists():
                return str(candidate.resolve()), ["serve"]
    elif venv_bin.exists():
        return str(venv_bin.resolve()), ["serve"]
    # 3. Fallback: uvx
    return "uvx", ["wbox-mcp", "serve"]


def _build_mcp_entry(cfg: dict, config_path: Path) -> dict:
    """Build the MCP server entry for .mcp.json.

    Uses absolute paths for both command and config — does NOT rely on cwd
    since not all MCP clients support it reliably.
    """
    command, base_args = _detect_wbox_command()
    abs_config = str(config_path.resolve())
    args = base_args + [abs_config]
    return {
        "type": "stdio",
        "command": command,
        "args": args,
    }


# ── Parse init flags ──────────────────────────────────────────────


def _parse_init_args(args: list[str]) -> tuple[str | None, dict]:
    """Parse init subcommand args. Returns (directory, flags)."""
    directory = None
    flags: dict = {
        "app_env": [],
        "pre_launch": [],
        "tools": [],
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--name":
            flags["name"] = args[i + 1]; i += 2
        elif a == "--compositor":
            flags["compositor"] = args[i + 1]; i += 2
        elif a == "--screen":
            flags["screen"] = args[i + 1]; i += 2
        elif a == "--weston-backend":
            flags["weston_backend"] = args[i + 1]; i += 2
        elif a == "--weston-shell":
            flags["weston_shell"] = args[i + 1]; i += 2
        elif a == "--title-hint":
            flags["title_hint"] = args[i + 1]; i += 2
        elif a == "--app-command":
            flags["app_command"] = args[i + 1]; i += 2
        elif a == "--app-env":
            flags["app_env"].append(args[i + 1]); i += 2
        elif a == "--pre-launch":
            flags["pre_launch"].append(args[i + 1]); i += 2
        elif a == "--tool":
            flags["tools"].append(args[i + 1]); i += 2
        elif a == "--mcp-dir":
            directory = args[i + 1]; i += 2
        elif a == "--from":
            flags["from_file"] = args[i + 1]; i += 2
        elif a == "--register":
            flags["register"] = True; i += 1
        elif a == "--update-claude-settings":
            flags["update_claude_settings"] = True; i += 1
        elif not a.startswith("-"):
            directory = a; i += 1
        else:
            print(f"Unknown init option: {a}", file=sys.stderr)
            sys.exit(1)
    return directory, flags


# ── Commands ───────────────────────────────────────────────────────


def cmd_init(args: list[str]):
    """Full wizard or non-interactive init."""
    directory, flags = _parse_init_args(args)

    # In interactive mode without explicit directory, let user confirm/change
    has_flags = any(k in flags for k in ("name", "compositor", "app_command", "from_file"))
    if _is_interactive() and not has_flags and directory is None:
        default_dir = _resolve_default_dir()
        cwd = Path.cwd()
        if _is_project_root(cwd):
            print(f"Project root detected ({cwd.name}/)")
            chosen = _prompt("MCP directory", str(default_dir.relative_to(cwd)))
            directory = str(cwd / chosen)
        # else: use cwd, no prompt needed

    config_path = _resolve_config_path(directory)

    # --from: load template
    if "from_file" in flags:
        template = Path(flags["from_file"])
        if not template.exists():
            print(f"Error: template not found: {template}", file=sys.stderr)
            sys.exit(1)
        cfg = yaml.safe_load(template.read_text()) or {}
    else:
        existing = load_config(config_path) if config_path.exists() else {}
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: v for k, v in existing.items() if not k.startswith("_")})

    # Check if we have enough flags for non-interactive mode
    has_flags = any(k in flags for k in ("name", "compositor", "app_command", "from_file"))
    interactive = _is_interactive() and not has_flags

    if interactive:
        _init_interactive(cfg, config_path)
    else:
        _init_noninteractive(cfg, flags)

    # Save
    config_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(cfg, config_path)
    print(f"Saved: {config_path}")

    # Create directories
    base = config_path.parent
    log_cfg = cfg.get("log", {})
    log_dir = log_cfg.get("dir", "./log") if isinstance(log_cfg, dict) else log_cfg
    (base / log_dir).mkdir(parents=True, exist_ok=True)
    (base / cfg.get("screenshot_dir", "./screenshots")).mkdir(parents=True, exist_ok=True)

    # Register or print snippet
    ucs = flags.get("update_claude_settings", False)
    if flags.get("register"):
        _do_register(cfg, config_path, update_claude_settings=ucs)
    else:
        _print_claude_snippet(cfg, config_path)
        if interactive and _prompt_yn("\nRegister in .mcp.json?", default=True):
            do_ucs = _prompt_yn("Allow all tools in Claude settings?", default=True)
            _do_register(cfg, config_path, update_claude_settings=do_ucs)


def _init_interactive(cfg: dict, config_path: Path):
    """Interactive wizard mode."""
    if config_path.exists():
        print(f"Found existing config: {config_path}")
        print("Press Enter to keep current values.\n")
    else:
        print("Creating new wbox-mcp instance.\n")

    cfg["name"] = _prompt("Instance name", cfg.get("name", "my-wbox"))

    if _IS_WIN32:
        # Windows: compositor is always win32
        cfg["compositor"] = "win32"
        print(f"  Compositor: win32 (auto-detected)")
        # Remove Linux-only keys
        cfg.pop("screen", None)
        cfg.pop("weston_shell", None)
        cfg.pop("weston_backend", None)

        # title_hint — helps find the right window
        cfg["title_hint"] = _prompt(
            "Window title hint (substring to match)",
            cfg.get("title_hint", ""),
        )
    else:
        # Linux: choose compositor
        cfg["compositor"] = _prompt("Compositor [weston/cage]", cfg.get("compositor", "weston"))
        cfg["screen"] = _prompt("Screen size", cfg.get("screen", "1280x800"))

        if cfg["compositor"] == "weston":
            cfg["weston_shell"] = _prompt("Weston shell [kiosk/desktop]", cfg.get("weston_shell", "kiosk"))
            cfg["weston_backend"] = _prompt("Weston backend [wayland/x11]", cfg.get("weston_backend", "x11"))
        else:
            cfg.pop("weston_shell", None)
            cfg.pop("weston_backend", None)

    # App
    app_cfg = cfg.get("app", {})
    if isinstance(app_cfg, str):
        app_cfg = {"command": app_cfg}
    app_cfg["command"] = _prompt("App command", app_cfg.get("command", ""))

    if _prompt_yn("Configure environment variables?", default=bool(app_cfg.get("env"))):
        if app_cfg.get("env"):
            print(f"  Current env: {app_cfg['env']}")
            if _prompt_yn("  Replace (y) or add to existing (n)?"):
                app_cfg["env"] = _prompt_env()
            else:
                new_env = _prompt_env()
                app_cfg["env"].update(new_env)
        else:
            app_cfg["env"] = _prompt_env()
    cfg["app"] = app_cfg

    if not _IS_WIN32:
        # Pre-launch hooks (Linux only — runs in compositor env)
        pre = app_cfg.get("pre_launch", [])
        if _prompt_yn("Configure pre-launch scripts?", default=bool(pre)):
            if pre:
                print(f"  Current pre-launch: {pre}")
                if _prompt_yn("  Replace?"):
                    pre = []
            print("  Pre-launch scripts (shell commands, empty line to finish):")
            while True:
                raw = input("    > ").strip()
                if not raw:
                    break
                pre.append(raw)
            app_cfg["pre_launch"] = pre
        cfg["app"] = app_cfg

    # Log
    log_cfg = cfg.get("log", {})
    if isinstance(log_cfg, str):
        log_cfg = {"dir": log_cfg}
    log_cfg["dir"] = _prompt("Log directory", log_cfg.get("dir", "./log"))
    log_cfg["level"] = _prompt("Log level [debug/info/warn/error]", log_cfg.get("level", "info"))
    cfg["log"] = log_cfg

    # Screenshots
    cfg["screenshot_dir"] = _prompt("Screenshot directory", cfg.get("screenshot_dir", "./screenshots"))

    # Windows timeouts
    if _IS_WIN32:
        timeouts = cfg.get("timeouts", {})
        if _prompt_yn("Configure timeouts?", default=False):
            timeouts["window_discovery"] = int(_prompt(
                "Window discovery timeout (seconds)",
                str(timeouts.get("window_discovery", 10)),
            ))
            timeouts["edit_control"] = int(_prompt(
                "Edit control timeout (seconds)",
                str(timeouts.get("edit_control", 3)),
            ))
            timeouts["app_render"] = int(_prompt(
                "App render delay (seconds)",
                str(timeouts.get("app_render", 3)),
            ))
        cfg["timeouts"] = timeouts

    # Tools
    if _prompt_yn("Add custom script tools?", default=False):
        _wizard_add_tools(cfg)


def _init_noninteractive(cfg: dict, flags: dict):
    """Non-interactive mode: apply flags to config."""
    if not _is_interactive() and not any(k in flags for k in ("name", "compositor", "app_command", "from_file")):
        print("Error: non-interactive mode requires --name, --app-command, --from, or other flags.", file=sys.stderr)
        print("Run 'wboxr init --help' for usage.", file=sys.stderr)
        sys.exit(1)

    if "name" in flags:
        cfg["name"] = flags["name"]
    if "compositor" in flags:
        cfg["compositor"] = flags["compositor"]
    elif _IS_WIN32 and "compositor" not in cfg:
        cfg["compositor"] = "win32"
    if "screen" in flags:
        cfg["screen"] = flags["screen"]
    if "weston_backend" in flags:
        cfg["weston_backend"] = flags["weston_backend"]
    if "weston_shell" in flags:
        cfg["weston_shell"] = flags["weston_shell"]
    if "title_hint" in flags:
        cfg["title_hint"] = flags["title_hint"]

    # App
    app_cfg = cfg.get("app", {})
    if isinstance(app_cfg, str):
        app_cfg = {"command": app_cfg}
    if "app_command" in flags:
        app_cfg["command"] = flags["app_command"]
    if flags.get("app_env"):
        env = app_cfg.get("env", {})
        for entry in flags["app_env"]:
            if "=" not in entry:
                print(f"Error: invalid --app-env format: {entry} (use KEY=VALUE)", file=sys.stderr)
                sys.exit(1)
            k, v = entry.split("=", 1)
            env[k.strip()] = v.strip()
        app_cfg["env"] = env
    if flags.get("pre_launch"):
        app_cfg["pre_launch"] = flags["pre_launch"]
    cfg["app"] = app_cfg

    # Tools
    if flags.get("tools"):
        tools = cfg.get("tools", {})
        for entry in flags["tools"]:
            parts = entry.split(":", 2)
            if len(parts) < 2:
                print(f"Error: invalid --tool format: {entry} (use NAME:SCRIPT[:DESCRIPTION])", file=sys.stderr)
                sys.exit(1)
            name = parts[0]
            script = parts[1]
            desc = parts[2] if len(parts) > 2 else f"Custom tool: {name}"
            tools[name] = {"script": script, "description": desc}
        cfg["tools"] = tools


def _wizard_add_tools(cfg: dict):
    """Interactive loop to add script tools."""
    tools = cfg.get("tools", {})
    script_ext = ".ps1" if _IS_WIN32 else ".sh"
    while True:
        name = _prompt("Tool name (empty to stop)", "")
        if not name:
            break
        script = _prompt(f"  Script path for '{name}'", f"./scripts/{name}{script_ext}")
        description = _prompt(f"  Description", f"Custom tool: {name}")

        tools[name] = {
            "script": script,
            "description": description,
        }

        # Create script template if it doesn't exist
        script_path = Path(cfg.get("_config_dir", ".")) / script
        if not script_path.exists():
            script_path.parent.mkdir(parents=True, exist_ok=True)
            if _IS_WIN32:
                script_path.write_text(f"""# {name} — {description}
#
# Custom tool script for wbox-mcp (Windows)
#

Write-Host "{name}: not implemented yet"
""")
            else:
                script_path.write_text(f"""#!/usr/bin/env bash
# {name} — {description}
#
# Available env vars:
#   WBOX_WAYLAND_DISPLAY  — compositor's Wayland display
#   WBOX_X_DISPLAY        — compositor's Xwayland display
#
set -euo pipefail

echo "{name}: not implemented yet"
""")
                script_path.chmod(0o755)
            print(f"  Created template: {script_path}")

        if not _prompt_yn("  Add another tool?"):
            break
    cfg["tools"] = tools


def cmd_tool_add(directory: str | None = None):
    """Add a single custom script tool."""
    config_path = _resolve_config_path(directory)
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run 'wboxr init' first.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(config_path)
    tools = cfg.get("tools", {})

    name = _prompt("Tool name")
    if not name:
        return

    if name in tools:
        print(f"  Tool '{name}' already exists. Overwriting.")

    script_ext = ".ps1" if _IS_WIN32 else ".sh"
    script = _prompt(f"Script path", f"./scripts/{name}{script_ext}")
    description = _prompt(f"Description", f"Custom tool: {name}")

    tools[name] = {
        "script": script,
        "description": description,
    }
    cfg["tools"] = tools
    save_config(cfg, config_path)
    print(f"Added tool '{name}' to {config_path}")

    # Create script template
    base = config_path.parent
    script_path = base / script
    if not script_path.exists():
        script_path.parent.mkdir(parents=True, exist_ok=True)
        if _IS_WIN32:
            script_path.write_text(f"""# {name} — {description}
#
# Custom tool script for wbox-mcp (Windows)
#

Write-Host "{name}: not implemented yet"
""")
        else:
            script_path.write_text(f"""#!/usr/bin/env bash
# {name} — {description}
#
# Available env vars:
#   WBOX_WAYLAND_DISPLAY  — compositor's Wayland display
#   WBOX_X_DISPLAY        — compositor's Xwayland display
#
set -euo pipefail

echo "{name}: not implemented yet"
""")
            script_path.chmod(0o755)
        print(f"Created template: {script_path}")


def cmd_tool_remove(name: str, directory: str | None = None):
    """Remove a custom script tool."""
    config_path = _resolve_config_path(directory)
    if not config_path.exists():
        print(f"Error: {config_path} not found.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(config_path)
    tools = cfg.get("tools", {})

    if name not in tools:
        print(f"Tool '{name}' not found in config.", file=sys.stderr)
        sys.exit(1)

    del tools[name]
    cfg["tools"] = tools
    save_config(cfg, config_path)
    print(f"Removed tool '{name}' from {config_path}")


def cmd_tool_list(directory: str | None = None):
    """List configured tools."""
    config_path = _resolve_config_path(directory)
    if not config_path.exists():
        print(f"Error: {config_path} not found.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(config_path)

    builtins = [
        "launch", "stop", "kill", "screenshot", "click", "type_text",
        "key", "keys", "mouse_move", "get_size", "resize",
        "clipboard_read", "clipboard_write",
        "clean", "tail_log", "debug_input",
    ]
    print("Built-in tools:")
    for t in builtins:
        print(f"  {t}")

    tools = cfg.get("tools", {})
    if tools:
        print("\nCustom tools:")
        for name, tdef in tools.items():
            desc = tdef.get("description", "")
            script = tdef.get("script", "")
            print(f"  {name:20s} {script:30s} {desc}")
    else:
        print("\nNo custom tools configured.")


# ── Register / Unregister ─────────────────────────────────────────


def _resolve_mcp_json(global_: bool = False, file_: str | None = None) -> Path:
    """Resolve the target .mcp.json path."""
    if file_:
        return Path(file_)
    if global_:
        return Path.home() / ".claude.json"
    return Path.cwd() / ".mcp.json"


def _do_register(cfg: dict, config_path: Path, global_: bool = False,
                  file_: str | None = None, update_claude_settings: bool = False):
    """Write MCP entry into .mcp.json."""
    mcp_json = _resolve_mcp_json(global_, file_)
    name = cfg.get("name", "my-wbox")
    entry = _build_mcp_entry(cfg, config_path)

    # Read existing or create new
    data = {}
    if mcp_json.exists():
        try:
            data = json.loads(mcp_json.read_text())
        except Exception:
            pass

    if "mcpServers" not in data:
        data["mcpServers"] = {}

    data["mcpServers"][name] = entry
    mcp_json.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Registered '{name}' in {mcp_json}")

    if update_claude_settings:
        _add_claude_permission(name)


def _find_claude_settings() -> Path:
    """Find the best Claude settings file to write permissions to.

    Prefers .claude/settings.local.json (user-local, gitignored) if it exists,
    otherwise falls back to .claude/settings.json.
    """
    local = Path.home() / ".claude" / "settings.local.json"
    if local.exists():
        return local
    return Path.home() / ".claude" / "settings.json"


def _add_claude_permission(server_name: str):
    """Add mcp__<name>__* wildcard permission to Claude settings."""
    settings_path = _find_claude_settings()
    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text())
        except Exception:
            pass

    if "permissions" not in data:
        data["permissions"] = {}
    if "allow" not in data["permissions"]:
        data["permissions"]["allow"] = []

    perm = f"mcp__{server_name}__*"
    if perm not in data["permissions"]["allow"]:
        data["permissions"]["allow"].append(perm)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Added permission '{perm}' to {settings_path}")
    else:
        print(f"Permission '{perm}' already in {settings_path}")


def cmd_register(args: list[str]):
    """Register a wbox-mcp instance in .mcp.json."""
    directory = None
    global_ = False
    file_ = None
    update_claude_settings = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--global":
            global_ = True; i += 1
        elif a == "--file":
            file_ = args[i + 1]; i += 2
        elif a == "--update-claude-settings":
            update_claude_settings = True; i += 1
        elif not a.startswith("-"):
            directory = a; i += 1
        else:
            print(f"Unknown option: {a}", file=sys.stderr)
            sys.exit(1)

    config_path = _resolve_config_path(directory)
    if not config_path.exists():
        print(f"Error: {config_path} not found. Run 'wboxr init' first.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(config_path)
    _do_register(cfg, config_path, global_, file_, update_claude_settings)


def cmd_unregister(args: list[str]):
    """Remove a wbox-mcp instance from .mcp.json."""
    if not args or args[0].startswith("-"):
        print("Usage: wboxr unregister <name> [--global] [--file PATH]", file=sys.stderr)
        sys.exit(1)

    name = args[0]
    global_ = False
    file_ = None

    i = 1
    while i < len(args):
        a = args[i]
        if a == "--global":
            global_ = True; i += 1
        elif a == "--file":
            file_ = args[i + 1]; i += 2
        else:
            print(f"Unknown option: {a}", file=sys.stderr)
            sys.exit(1)

    mcp_json = _resolve_mcp_json(global_, file_)
    if not mcp_json.exists():
        print(f"Error: {mcp_json} not found.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(mcp_json.read_text())
    servers = data.get("mcpServers", {})

    if name not in servers:
        print(f"'{name}' not found in {mcp_json}", file=sys.stderr)
        sys.exit(1)

    del servers[name]
    data["mcpServers"] = servers
    mcp_json.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Unregistered '{name}' from {mcp_json}")


def cmd_list():
    """List wbox-mcp instances found in Claude settings."""
    paths = [
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "settings.json",
    ]
    cwd = Path.cwd()
    paths.append(cwd / ".mcp.json")

    found = []
    for p in paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            servers = data.get("mcpServers", {})
            for name, sdef in servers.items():
                cmd = sdef.get("command", "")
                cmd_args = sdef.get("args", [])
                full_cmd = f"{cmd} {' '.join(cmd_args)}"
                if "wbox-mcp" in full_cmd or "wbox" in name:
                    cwd_val = sdef.get("cwd", "")
                    found.append((name, cwd_val, str(p)))
        except Exception:
            continue

    if found:
        print(f"Found {len(found)} wbox-mcp instance(s):\n")
        for name, cwd_val, source in found:
            print(f"  {name:20s} {cwd_val}")
            print(f"  {'':20s} (from {source})")
    else:
        print("No wbox-mcp instances found in Claude settings.")


def _print_claude_snippet(cfg: dict, config_path: Path):
    """Print the Claude MCP config snippet."""
    name = cfg.get("name", "my-wbox")
    entry = _build_mcp_entry(cfg, config_path)
    snippet = {name: entry}

    print(f"\nAdd this to your .mcp.json:")
    print(f"  \"mcpServers\": {json.dumps(snippet, indent=4)}")


# ── Main ───────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    cmd = args[0]

    if cmd in ("-V", "--version"):
        from wbox import __version__
        print(f"wboxr {__version__}")
        sys.exit(0)

    if cmd == "init":
        cmd_init(args[1:])
    elif cmd == "tool":
        if len(args) < 2:
            print("Usage: wboxr tool [add|remove|list] ...", file=sys.stderr)
            sys.exit(1)
        subcmd = args[1]
        if subcmd == "add":
            cmd_tool_add(args[2] if len(args) > 2 else None)
        elif subcmd == "remove":
            if len(args) < 3:
                print("Usage: wboxr tool remove <name> [directory]", file=sys.stderr)
                sys.exit(1)
            cmd_tool_remove(args[2], args[3] if len(args) > 3 else None)
        elif subcmd == "list":
            cmd_tool_list(args[2] if len(args) > 2 else None)
        else:
            print(f"Unknown subcommand: tool {subcmd}", file=sys.stderr)
            sys.exit(1)
    elif cmd == "register":
        cmd_register(args[1:])
    elif cmd == "unregister":
        cmd_unregister(args[1:])
    elif cmd == "list":
        cmd_list()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
