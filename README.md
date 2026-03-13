# wbox-mcp

MCP server for GUI automation with Claude — run any desktop app and control it via screenshots, keyboard, mouse.

**Linux**: sandboxed nested Wayland compositor (**labwc** + **hybrid** input) — pixel-perfect automation with zero interference with the user's desktop.
**Windows**: direct Win32 API backend (PrintWindow + PostMessage) — works in the background while you use your PC.

## Install

### Linux

```bash
curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash
```

Clones to `~/.local/share/wbox-mcp`, installs Python package, symlinks `wboxr` + `wbox-mcp` to `~/.local/bin`, and installs system dependencies (labwc, grim, xdotool, wtype...) via your package manager.

```bash
# Custom install dir
curl ... | bash -s -- --install-dir ~/my/path

# Skip system deps install
curl ... | bash -s -- --no-install-deps
```

### Windows

```powershell
irm https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.ps1 | iex
```

Installs to `~\.local\share\wbox-mcp`, creates shims in `~\.local\bin`, auto-installs Python/uv/git via winget if missing. No system dependencies needed — the Win32 backend uses only built-in Windows APIs.

```powershell
# Custom install dir
.\setup.ps1 -InstallDir C:\my\path

# Dev mode
.\setup.ps1 -DevMode
```

### From a local clone (dev mode)

```bash
git clone https://github.com/quazardous/wbox-mcp.git
cd wbox-mcp

# Linux
./setup.sh --dev-mode

# Windows
.\setup.ps1 -DevMode
```

### Update

```bash
# Linux remote install
~/.local/share/wbox-mcp/setup.sh

# Linux dev mode
git pull && ./setup.sh --dev-mode

# Windows
& "$HOME\.local\share\wbox-mcp\setup.ps1"
```

## Quick start

### Inside an existing project

```bash
cd my-project/
wboxr init
```

Project root detected (.git, pyproject.toml, etc.) — defaults to `./wbox/`:

```
my-project/
├── src/
├── wbox/              ← created by wboxr init
│   ├── config.yaml
│   ├── log/
│   └── screenshots/
└── .mcp.json          ← updated by --register
```

### Standalone

```bash
mkdir my-app-mcp && cd my-app-mcp
wboxr init
```

No project root detected — config goes in the current directory.

The interactive wizard adapts to your platform:

**Linux** — asks for compositor (labwc/weston/cage), screen size, input backend, pre-launch scripts.

**Windows** — auto-detects `win32` backend, asks for window title hint, optional timeouts. No compositor/screen config needed.

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

### Register in Claude

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

No system dependencies — the Win32 backend uses `ctypes` to call Windows APIs directly (PrintWindow, PostMessage, SendInput).

Needed: `python`, `uv`, `git` (auto-installed by `setup.ps1`).

**Windows 10+** required.

## Platform features

| Feature | Linux | Windows |
|---------|-------|---------|
| Screenshot | grim (pixel-perfect) | PrintWindow (background) |
| Keyboard | wtype (Wayland protocol) | PostMessage / SendInput |
| Mouse | wbox-pointer (virtual pointer) | PostMessage / SendInput |
| Clipboard | xclip + bridge to host | Win32 clipboard API |
| Window management | wlrctl (list/focus) | EnumChildWindows |
| Resize display | wlr-randr | N/A |
| App isolation | Full (nested compositor) | None (normal process) |
| Background operation | Yes (isolated display) | Yes (PostMessage) |
| Interferes with host | No | Key combos briefly steal focus |

**Linux**: labwc + hybrid input is the recommended setup. See [docs/backends.md](docs/backends.md) for compositor comparison, input backend details, and compatibility matrix.

**Windows**: Win32 backend, no compositor needed. See [docs/backends.md](docs/backends.md) for Win32 API details.

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

**Init options:**

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

### wbox-mcp (MCP server — for Claude)

```bash
wbox-mcp serve [config.yaml]      # Start MCP stdio server
wbox-mcp --version                # Show version
```

## Built-in MCP tools

| Tool | Description |
|------|-------------|
| `launch` | Start compositor/app |
| `stop` | Graceful shutdown (SIGTERM → SIGKILL on Linux) |
| `kill` | Force kill + cleanup |
| `screenshot` | Capture display (returns image, includes modal dialogs) |
| `click` | Click at (x, y) |
| `type_text` | Type into focused widget |
| `key` | Send keyboard shortcut (e.g. `ctrl+s`) |
| `keys` | Send multiple keys in sequence |
| `mouse_move` | Move mouse |
| `get_size` | Get display dimensions |
| `resize` | Resize display (labwc/weston only) |
| `list_windows` | List all windows/toplevels in the compositor |
| `focus_window` | Focus/raise a window by title or app_id |
| `clipboard_read` | Read text from clipboard |
| `clipboard_write` | Write text to clipboard |
| `get_mouse_position` | Get current cursor coordinates |
| `tail_log` | Show MCP server logs |
| `clean` | Delete logs and screenshots |
| `debug_input` | Test keyboard input delivery |

## Custom script tools

```bash
wboxr tool add
# Tool name: deploy
# Script path: ./scripts/deploy.sh    (Linux)
#              ./scripts/deploy.ps1   (Windows)
# Description: Build and deploy my extension
```

A script template is created automatically (`.sh` on Linux, `.ps1` on Windows). On Linux, scripts receive env vars:
- `WBOX_WAYLAND_DISPLAY` — compositor's Wayland display
- `WBOX_X_DISPLAY` — compositor's Xwayland display
- Plus any app env you configured

Script tools support configurable timeouts (per-tool `timeout:` or global `tool_timeout:`).

## config.yaml

### Linux (Wayland compositor)

```yaml
name: my-app
compositor: labwc          # labwc (default), weston, or cage
screen: "1280x800"
input_backend: hybrid      # hybrid (default), x11, wayland, or dict

log:
  dir: ./log
  level: info

screenshot_dir: ./screenshots

keyboard_layout: ""           # XKB layout (e.g. fr, us, de). Empty = inherit from host

app:
  command: "my-app --flag"
  env:
    SAL_USE_VCLPLUGIN: gtk3
  pre_launch:
    - "./scripts/setup_profile.sh"
  post_launch_keys: ["super+a"]     # keys sent after app renders (e.g. maximize)
  post_launch_keys_delay: 0.5       # delay in seconds before/between keys

tools:
  deploy:
    script: "./scripts/deploy.sh"
    description: "Build and deploy"
    timeout: 60              # optional per-tool timeout (seconds)
```

### Windows (Win32 backend)

```yaml
name: my-app
compositor: win32
title_hint: "LibreOffice"

log:
  dir: ./log
  level: info

screenshot_dir: ./screenshots

timeouts:
  window_discovery: 10
  edit_control: 3
  app_render: 3

app:
  command: "C:/Program Files/LibreOffice/program/soffice.exe --writer"
  env: {}

tools: {}
```

All paths are relative to the config directory. Each instance is self-contained: config, logs, and screenshots live together.

See [`examples/config.sample.yaml`](examples/config.sample.yaml) for a full reference and [docs/backends.md](docs/backends.md) for backend-specific config options.

## How it works

**Linux** — the app runs inside a **nested Wayland compositor** (labwc). Full isolation: the app cannot see or interfere with your desktop. A clipboard bridge syncs copy-paste between the nested compositor and the host automatically.

**Windows** — the app runs as a normal process. Win32 APIs (PrintWindow, PostMessage) let Claude control it in the background while you keep working. Only key combos with modifiers briefly steal focus.

## License

MIT
