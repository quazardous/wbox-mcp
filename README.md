# wbox-mcp

Nested Wayland/X11 compositor as an MCP server for GUI automation with Claude.

Run any desktop app (LibreOffice, GIMP, Firefox...) inside a sandboxed compositor and control it via MCP tools: screenshots, keyboard, mouse, custom scripts.

## Install

### Remote (recommended)

```bash
curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash
```

Clones to `~/.local/share/wbox-mcp`, installs, symlinks `wboxr` + `wbox-mcp` to `~/.local/bin`.

Custom install dir:

```bash
curl ... | bash -s -- --install-dir ~/my/path
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

```bash
mkdir my-app-mcp && cd my-app-mcp
wboxr init
```

The wizard asks for your app command, compositor settings, and generates a `config.yaml` + a Claude MCP config snippet to paste in `.mcp.json`:

```json
{
  "mcpServers": {
    "my-app": {
      "command": "wbox-mcp",
      "args": ["serve"],
      "cwd": "/path/to/my-app-mcp"
    }
  }
}
```

## Requirements

- Linux with Wayland (or X11)
- `weston` or `cage` (compositor)
- `xdotool` (input injection)
- `grim` (screenshots for cage) or `weston-screenshooter` (for weston)
- `python3`, `uv`, `git`

## CLI

### wboxr (admin)

```bash
wboxr init [dir]              # Setup wizard (create or reconfigure)
wboxr tool add [dir]          # Add a custom script tool
wboxr tool remove <name>      # Remove a tool
wboxr tool list               # List all tools
wboxr list                    # Find instances in Claude settings
```

### wbox-mcp (MCP server)

```bash
wbox-mcp serve [config.yaml]  # Start MCP stdio server
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

Add tools that run shell scripts:

```bash
wboxr tool add
# Tool name: deploy
# Script path: ./scripts/deploy.sh
# Description: Build and deploy my extension
```

Scripts receive env vars: `WBOX_WAYLAND_DISPLAY`, `WBOX_X_DISPLAY`, plus any app env you configured.

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

## License

MIT
