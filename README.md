# wbox-mcp

Nested Wayland/X11 compositor as an MCP server for GUI automation with Claude.

Run any desktop app (LibreOffice, GIMP, Firefox...) inside a sandboxed compositor and control it via MCP tools: screenshots, keyboard, mouse, custom scripts.

## Install

### Remote (recommended)

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

### From a local clone (dev mode)

```bash
git clone https://github.com/quazardous/wbox-mcp.git
cd wbox-mcp
./setup.sh --dev-mode
```

Dev mode uses the repo in place — edits to source take effect immediately.

### Update

```bash
# Remote install
~/.local/share/wbox-mcp/setup.sh

# Dev mode
git pull && ./setup.sh --dev-mode
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

### Non-interactive mode

```bash
wboxr init --name my-app \
  --compositor weston \
  --app-command "soffice --writer" \
  --app-env "GDK_BACKEND=x11" \
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
| `launch` | Start compositor + app |
| `stop` | Graceful shutdown |
| `kill` | Force kill + cleanup |
| `screenshot` | Capture display (returns image) |
| `click` | Click at (x, y) |
| `type_text` | Type into focused widget |
| `key` | Send keyboard shortcut (e.g. `ctrl+s`) |
| `keys` | Send multiple keys in sequence |
| `mouse_move` | Move mouse |
| `get_size` | Get display dimensions |
| `resize` | Resize display |
| `tail_log` | Show MCP server logs |
| `clean` | Delete logs and screenshots |
| `debug_input` | Test keyboard input delivery |

## Custom script tools

```bash
wboxr tool add
# Tool name: deploy
# Script path: ./scripts/deploy.sh
# Description: Build and deploy my extension
```

A bash template is created automatically. Scripts receive env vars:
- `WBOX_WAYLAND_DISPLAY` — compositor's Wayland display
- `WBOX_X_DISPLAY` — compositor's Xwayland display
- Plus any app env you configured

## config.yaml

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

All paths are relative to the config directory. Each instance is self-contained: config, logs, and screenshots live together.

## License

MIT
