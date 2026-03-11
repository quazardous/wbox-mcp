"""
wboxr CLI — Registry/admin tool for wbox-mcp.

Usage:
    wboxr init [directory]        Full wizard (create or reconfigure)
    wboxr tool add [directory]    Add a custom script tool
    wboxr tool remove <name> [directory]
    wboxr tool list [directory]
    wboxr list                    List wbox-mcp instances from Claude settings
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from wbox.config import DEFAULT_CONFIG, load_config, save_config


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


def _resolve_config_path(directory: str | None) -> Path:
    if directory:
        d = Path(directory)
    else:
        d = Path.cwd()
    return d / "config.yaml"


# ── Commands ───────────────────────────────────────────────────────


def cmd_init(directory: str | None = None):
    """Full wizard: create or reconfigure a wbox-mcp instance."""
    config_path = _resolve_config_path(directory)
    existing = load_config(config_path) if config_path.exists() else {}

    if existing:
        print(f"Found existing config: {config_path}")
        print("Press Enter to keep current values.\n")
    else:
        print("Creating new wbox-mcp instance.\n")

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in existing.items() if not k.startswith("_")})

    # Basic settings
    cfg["name"] = _prompt("Instance name", cfg.get("name", "my-wbox"))
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

    # Pre-launch hooks
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

    # Tools
    if _prompt_yn("Add custom script tools?", default=False):
        _wizard_add_tools(cfg)

    # Save
    config_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(cfg, config_path)
    print(f"\nSaved: {config_path}")

    # Create directories
    base = config_path.parent
    (base / cfg["log"]["dir"]).mkdir(parents=True, exist_ok=True)
    (base / cfg["screenshot_dir"]).mkdir(parents=True, exist_ok=True)

    # Generate Claude MCP config snippet
    _print_claude_snippet(cfg, config_path)


def _wizard_add_tools(cfg: dict):
    """Interactive loop to add script tools."""
    tools = cfg.get("tools", {})
    while True:
        name = _prompt("Tool name (empty to stop)", "")
        if not name:
            break
        script = _prompt(f"  Script path for '{name}'", f"./scripts/{name}.sh")
        description = _prompt(f"  Description", f"Custom tool: {name}")

        tools[name] = {
            "script": script,
            "description": description,
        }

        # Create script template if it doesn't exist
        script_path = Path(cfg.get("_config_dir", ".")) / script
        if not script_path.exists():
            script_path.parent.mkdir(parents=True, exist_ok=True)
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

    script = _prompt(f"Script path", f"./scripts/{name}.sh")
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

    # Built-in tools
    builtins = [
        "launch", "stop", "kill", "screenshot", "click", "type_text",
        "key", "keys", "mouse_move", "get_size", "resize", "clean",
        "tail_log", "debug_input",
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


def cmd_list():
    """List wbox-mcp instances found in Claude settings."""
    # Check common Claude settings locations
    paths = [
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "settings.json",
    ]
    # Also check project-level .mcp.json files
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
                args = sdef.get("args", [])
                full_cmd = f"{cmd} {' '.join(args)}"
                if "wbox-mcp" in full_cmd:
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
    cwd = str(config_path.parent.resolve())

    snippet = {
        name: {
            "command": "uv",
            "args": ["run", "--with", "wbox-mcp", "wbox-mcp", "serve"],
            "cwd": cwd,
        }
    }

    print(f"\nAdd this to your Claude MCP settings (.mcp.json or claude settings):")
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
        cmd_init(args[1] if len(args) > 1 else None)
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
    elif cmd == "list":
        cmd_list()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
