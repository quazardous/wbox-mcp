# Usage Reference

## CLI

### wboxr (admin — for humans)

```bash
wboxr init [dir] [OPTIONS]        # Setup wizard (create or reconfigure)
wboxr init --mcp-dir DIR          # Explicit target directory
wboxr tool add [dir]              # Add a custom script tool
wboxr tool remove <name> [dir]    # Remove a tool
wboxr tool list [dir]             # List all tools
wboxr register [dir]              # Register in .mcp.json
wboxr register --global           # Register in ~/.claude.json
wboxr register --update-claude-settings  # Also add wildcard permission
wboxr unregister <name>           # Remove from .mcp.json
wboxr list                        # Find instances in Claude settings
wboxr --version                   # Show version
```

### wbox-mcp (MCP server — for Claude)

```bash
wbox-mcp serve [config.yaml]      # Start MCP stdio server
wbox-mcp --version                # Show version
```

## Init options

| Flag | Description |
|------|-------------|
| `--name NAME` | Instance name |
| `--compositor TYPE` | `labwc`, `weston`, `cage`, or `win32` (auto-detected on Windows) |
| `--screen WxH` | Screen size, e.g. `1280x800` (Linux only) |
| `--weston-backend TYPE` | `wayland` or `x11` (Linux only) |
| `--weston-shell TYPE` | `kiosk` or `desktop` (Linux only) |
| `--input-backend PRESET` | `hybrid`, `x11`, or `wayland` (Linux only) |
| `--title-hint TEXT` | Window title substring to match (Windows only) |
| `--app-command CMD` | App command to launch |
| `--app-env KEY=VALUE` | Environment variable (repeatable) |
| `--pre-launch CMD` | Pre-launch script (repeatable, Linux only) |
| `--tool NAME:SCRIPT:DESC` | Custom tool (repeatable) |
| `--from FILE` | Load config from a YAML template |
| `--register` | Auto-register in `.mcp.json` after init |
| `--update-claude-settings` | Also add wildcard permission to Claude settings |

**Directory resolution** (when no dir or `--mcp-dir` given):
- In a project root (`.git`, `pyproject.toml`, `package.json`...) → defaults to `./wbox`
- Otherwise → current directory

### Non-interactive mode

```bash
# Linux
wboxr init --name my-app \
  --compositor labwc \
  --app-command "soffice --writer" \
  --app-env "SAL_USE_VCLPLUGIN=gtk3" \
  --register

# Windows
wboxr init --name my-app \
  --app-command "soffice --writer" \
  --title-hint "LibreOffice" \
  --register
```

Or from a template:

```bash
wboxr init --from template.yaml --register
```

## Register in Claude

The wizard can auto-register in `.mcp.json`:

```bash
wboxr init --register                        # writes to .mcp.json
wboxr init --register --update-claude-settings  # also add wildcard permissions
wboxr register                               # register existing config
wboxr register --global                      # writes to ~/.claude.json
wboxr register --update-claude-settings      # also add mcp__name__* to Claude settings
wboxr unregister my-app                      # remove entry
```

Generated entry (absolute paths, no cwd needed):

```json
{
  "mcpServers": {
    "my-app": {
      "type": "stdio",
      "command": "wbox-mcp",
      "args": ["serve", "/absolute/path/to/wbox/config.yaml"]
    }
  }
}
```

`--update-claude-settings` adds `"mcp__my-app__*"` to `~/.claude/settings.local.json` (or `settings.json`) so all MCP tools are auto-allowed.

## Built-in MCP tools

| Tool | Description |
|------|-------------|
| `launch` | Start compositor/app |
| `stop` | Graceful shutdown (SIGTERM → SIGKILL on Linux) |
| `kill` | Force kill + cleanup |
| `screenshot` | Capture display (returns image, includes modal dialogs) |
| `click` | Click at (x, y) with optional button (1=left, 2=middle, 3=right) |
| `type_text` | Type text into focused widget |
| `key` | Send keyboard shortcut (e.g. `ctrl+s`, `alt+F4`, `super+a`) |
| `keys` | Send multiple keys in sequence with configurable delay |
| `mouse_move` | Move mouse to (x, y) without clicking |
| `get_mouse_position` | Get current cursor coordinates |
| `get_size` | Get display dimensions (width, height) |
| `resize` | Resize display (labwc/weston only) |
| `list_windows` | List all windows/toplevels in the compositor (via wlrctl) |
| `focus_window` | Focus/raise a window by title or app_id |
| `clipboard_read` | Read text from clipboard |
| `clipboard_write` | Write text to clipboard |
| `tail_log` | Show last N lines of the wbox-mcp log |
| `clean` | Delete logs and screenshots |
| `debug_input` | Test keyboard input delivery via different methods |

## Custom script tools

```bash
wboxr tool add
# Tool name: deploy
# Script path: ./scripts/deploy.sh    (Linux)
#              ./scripts/deploy.ps1   (Windows)
# Description: Build and deploy my extension
```

A script template is created automatically (`.sh` on Linux, `.ps1` on Windows).

### Environment variables available to scripts

| Variable | Description |
|----------|-------------|
| `WBOX_WAYLAND_DISPLAY` | Compositor's Wayland display (Linux) |
| `WBOX_X_DISPLAY` | Compositor's Xwayland display (Linux) |
| `WBOX_ARG_<NAME>` | MCP tool arguments forwarded as env vars |
| App env vars | Whatever you configured in `app.env` |

### Timeouts

Script tools support configurable timeouts:

```yaml
tool_timeout: 30              # global default (seconds)

tools:
  deploy:
    script: "./scripts/deploy.sh"
    description: "Build and deploy"
    timeout: 60              # per-tool override
  quick_check:
    script: "./scripts/check.sh"
    description: "Quick validation"
    headless: true           # runs without compositor
```

## config.yaml reference

### Linux (Wayland compositor)

```yaml
name: my-app
compositor: labwc          # labwc (default), weston, or cage
screen: "1280x800"
input_backend: hybrid      # hybrid (default), x11, wayland, or per-function dict
undecorate: true           # remove server-side window decorations (default: true)
keyboard_layout: ""        # XKB layout (e.g. fr, us, de). Empty = inherit from host

log:
  dir: ./log
  level: info              # debug, info, warning, error

screenshot_dir: ./screenshots

timeouts:
  wayland_display: 5       # wait for compositor's Wayland socket (seconds)
  xwayland_display: 5      # wait for Xwayland DISPLAY
  app_render: 3            # wait for app to render after launch
  stop: 10                 # graceful shutdown timeout before SIGKILL

app:
  command: "my-app --flag"
  env:
    SAL_USE_VCLPLUGIN: gtk3
  pre_launch:
    - "./scripts/setup_profile.sh"
  post_launch_keys: ["super+a"]     # keys sent after app renders (e.g. maximize)
  post_launch_keys_delay: 0.5       # delay in seconds before/between keys

# Per-function input backend override
# input_backend:
#   keyboard: wtype        # wtype or xdotool
#   mouse: wbox-pointer    # wbox-pointer, xdotool, or ydotool
#   clipboard: x11         # x11 or wayland

tools:
  deploy:
    script: "./scripts/deploy.sh"
    description: "Build and deploy"
    timeout: 60
```

### Windows (Win32 backend)

```yaml
name: my-app
compositor: win32          # auto-detected on Windows
title_hint: "LibreOffice"  # substring to match in window title

log:
  dir: ./log
  level: info

screenshot_dir: ./screenshots

timeouts:
  window_discovery: 10     # wait for app window to appear
  edit_control: 3          # wait for text input control
  app_render: 3            # wait for app to render

app:
  command: "C:/Program Files/LibreOffice/program/soffice.exe --writer"
  env: {}

tools: {}
```

### Path resolution

All paths are relative to the config directory. Each instance is self-contained: config, logs, and screenshots live together.

See also [`examples/config.sample.yaml`](../examples/config.sample.yaml) for a commented full reference.

## Requirements

### Linux

System dependencies are installed automatically by `setup.sh`.

**Required:** `labwc`, `grim`, `xdotool`, `wtype`, `wlr-randr`, `wlrctl`, `python3`, `uv`, `git`

**Optional:** `weston`, `cage`, `weston-screenshooter`, `xclip`/`xsel`, `wl-clipboard`, `ydotool`

Manual install:

```bash
# Fedora
sudo dnf install labwc grim xdotool wtype wlr-randr wlrctl wl-clipboard xclip

# Ubuntu/Debian
sudo apt install labwc grim xdotool wtype wlr-randr wlrctl wl-clipboard xclip

# Arch
sudo pacman -S labwc grim xdotool wtype wlr-randr wlrctl wl-clipboard xclip
```

### Windows

No system dependencies — the Win32 backend uses `ctypes` to call Windows APIs directly.

Needed: `python`, `uv`, `git` (auto-installed by `setup.ps1`). **Windows 10+** required.
