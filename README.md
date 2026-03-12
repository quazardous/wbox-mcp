# wbox-mcp

MCP server for GUI automation with Claude — run any desktop app and control it via screenshots, keyboard, mouse.

**Linux**: sandboxed nested Wayland/X11 compositor (weston, cage).
**Windows**: direct Win32 API backend (PrintWindow + PostMessage) — works in the background while you use your PC.

## Install

### Linux

```bash
curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash
```

Clones to `~/.local/share/wbox-mcp`, installs Python package, symlinks `wboxr` + `wbox-mcp` to `~/.local/bin`, and installs system dependencies (xdotool, weston, grim...) via your package manager.

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

**Linux** — asks for compositor (weston/cage), screen size, weston options, pre-launch scripts.

**Windows** — auto-detects `win32` backend, asks for window title hint, optional timeouts. No compositor/screen config needed.

### Non-interactive mode

```bash
# Linux
wboxr init --name my-app \
  --compositor weston \
  --app-command "soffice --writer" \
  --app-env "GDK_BACKEND=x11" \
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

System dependencies are installed automatically by `setup.sh`. To install manually:

```bash
# Fedora
sudo dnf install xdotool weston cage grim xorg-x11-utils

# Ubuntu/Debian
sudo apt install xdotool weston cage grim x11-utils

# Arch
sudo pacman -S xdotool weston cage grim xorg-xev
```

Also needed: `python3`, `uv`, `git`.

### Windows

No system dependencies — the Win32 backend uses `ctypes` to call Windows APIs directly (PrintWindow, PostMessage, SendInput).

Needed: `python`, `uv`, `git` (auto-installed by `setup.ps1`).

**Windows 10+** required.

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
| `--compositor TYPE` | `weston`, `cage`, or `win32` (auto-detected on Windows) |
| `--screen WxH` | Screen size, e.g. `1280x800` (Linux only) |
| `--weston-backend TYPE` | `wayland` or `x11` (Linux only) |
| `--weston-shell TYPE` | `kiosk` or `desktop` (Linux only) |
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
| `stop` | Graceful shutdown |
| `kill` | Force kill + cleanup |
| `screenshot` | Capture display (returns image, includes modal dialogs) |
| `click` | Click at (x, y) |
| `type_text` | Type into focused widget |
| `key` | Send keyboard shortcut (e.g. `ctrl+s`) |
| `keys` | Send multiple keys in sequence |
| `mouse_move` | Move mouse |
| `get_size` | Get display dimensions |
| `resize` | Resize display |
| `clipboard_read` | Read text from clipboard |
| `clipboard_write` | Write text to clipboard |
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

## config.yaml

### Linux (Wayland compositor)

```yaml
name: my-app
compositor: weston
screen: "1280x800"
weston_shell: kiosk
weston_backend: x11

log:
  dir: ./log
  level: info

screenshot_dir: ./screenshots

app:
  command: "my-app --flag"
  env:
    GDK_BACKEND: x11
  pre_launch:
    - "./scripts/setup_profile.sh"

tools:
  deploy:
    script: "./scripts/deploy.sh"
    description: "Build and deploy"
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

**Windows-specific config:**

| Key | Description |
|-----|-------------|
| `compositor: win32` | Auto-detected on Windows, explicit on cross-platform configs |
| `title_hint` | Substring to match in window title (helps find the right window when the app spawns multiple processes) |
| `timeouts.window_discovery` | How long to wait for the app window to appear (default: 10s) |
| `timeouts.edit_control` | How long to wait for the text input control (default: 3s) |

All paths are relative to the config directory. Each instance is self-contained: config, logs, and screenshots live together.

## How it works

### Linux

The app runs inside a **nested Wayland compositor** (weston or cage). Input is injected via `xdotool` through Xwayland, screenshots via `grim` or `weston-screenshooter`. The compositor provides full isolation — the app cannot interfere with your desktop.

### Windows

The app runs as a **normal Windows process**. The backend uses Win32 APIs:

- **Screenshots**: `PrintWindow` — captures the window even when it's behind other windows
- **Text input**: `PostMessage WM_CHAR` — types without stealing focus
- **Clicks**: `PostMessage WM_LBUTTONDOWN/UP` — clicks without stealing focus
- **Key combos**: `SendInput` — for shortcuts like `ctrl+s` (briefly brings window to foreground)
- **Modal dialogs**: automatically detected and composited into screenshots

You can keep working in VS Code while Claude controls the app in the background. The only moment focus is briefly stolen is for key combos with modifiers (ctrl, alt, shift).

## License

MIT
