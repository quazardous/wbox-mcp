# wbox-mcp

MCP server for GUI automation with Claude — run any desktop app and control it via screenshots, keyboard, mouse.

## Install

```bash
# Linux
curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash

# Windows
irm https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.ps1 | iex
```

## Quick start

```bash
cd my-project/
wboxr init --register
```

Creates `wbox/config.yaml`, registers in `.mcp.json`. The wizard adapts to your platform.

```bash
# Non-interactive
wboxr init --name my-app --app-command "soffice --writer" --register
```

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

**Linux** — the app runs inside a nested Wayland compositor (labwc). Full isolation: the app cannot see or interfere with your desktop. Clipboard is bridged automatically.

**Windows** — the app runs as a normal process. Win32 APIs control it in the background while you keep working.

## MCP tools

`launch` · `stop` · `kill` · `screenshot` · `click` · `type_text` · `key` · `keys` · `mouse_move` · `get_mouse_position` · `get_size` · `resize` · `list_windows` · `focus_window` · `clipboard_read` · `clipboard_write` · `tail_log` · `clean` · `debug_input`

Plus custom script tools via `wboxr tool add`.

## Documentation

- [docs/usage.md](docs/usage.md) — CLI flags, config.yaml reference, MCP tools details, requirements
- [docs/backends.md](docs/backends.md) — compositor comparison, input backends, compatibility matrix

## License

MIT
