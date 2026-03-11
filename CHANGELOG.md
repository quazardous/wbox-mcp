# Changelog

<!--
  This changelog is meant for humans, not machines.
  Keep it readable — no commit hashes, no ultra-technical jargon.
  Focus on what changed and why it matters for users.
  Format: https://semver.org
-->

## [0.2.0] - 2026-03-11

### Added

- **`wboxr register` / `unregister`**: write MCP entries directly into `.mcp.json` instead of copy-pasting snippets
- **`--update-claude-settings`**: auto-add wildcard permission (`mcp__<name>__*`) to Claude settings so all tools are allowed without prompts
- **Non-interactive wizard**: `wboxr init` now supports CLI flags (`--name`, `--app-command`, `--app-env`, `--from`, etc.) for scripted setup
- **Smart default directory**: when running `wboxr init` inside a project root (.git, pyproject.toml...), defaults to `./wbox` instead of cluttering the root
- **`--mcp-dir`**: explicit flag to choose where config/log/screenshots go

### Changed

- MCP entries now use **absolute paths** (no `cwd`) — more reliable across MCP clients
- setup.sh **auto-installs system deps** by default (use `--no-install-deps` to skip)
- Centralized version management with `--version` flag on both CLIs

## [0.1.0] - 2026-03-11

First release

### Added

- **Compositor backends**: weston (desktop, resizable) and cage (kiosk, fullscreen) with nested Wayland + Xwayland support
- **MCP tools**: launch, stop, kill, screenshot, click, type_text, key, keys, mouse_move, get_size, resize, clean, tail_log, debug_input
- **Custom script tools**: add your own shell scripts as MCP tools via config.yaml
- **wboxr CLI**: setup wizard (`wboxr init`), tool management (`wboxr tool add/remove/list`), instance discovery (`wboxr list`)
- **wbox-mcp CLI**: MCP stdio server (`wbox-mcp serve`)
- **setup.sh installer**: one-liner curl install, `--dev-mode` for local development, `--install-dir` for custom paths, auto-installs system deps (xdotool, weston, cage, grim...)
- **Built-in logging**: all tool calls logged to `./log/wbox-mcp.log`, readable via `tail_log` tool
- **Pre-launch hooks**: run shell scripts before app launch
- **State persistence**: compositor survives MCP server restarts (state saved to `/tmp/`)
- **xterm example**: minimal working config in `examples/xterm/`
