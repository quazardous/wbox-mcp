# Changelog

<!--
  This changelog is meant for humans, not machines.
  Keep it readable — no commit hashes, no ultra-technical jargon.
  Focus on what changed and why it matters for users.
  Format: https://semver.org
-->

## [0.4.0] - 2026-03-12

### Added

- **Native Wayland input backend**: new `input_backend: "wayland"` config option — uses `wtype` for keyboard input and `ydotool` for mouse, bypassing Xwayland entirely
- **Wayland clipboard support**: `clipboard_read`/`clipboard_write` now work with `wl-paste`/`wl-copy` when using the wayland input backend
- **Deterministic Wayland socket naming** (Weston): socket is now `wbox-<instance>` instead of auto-assigned `wayland-N`, eliminating collisions between concurrent instances
- **Script tool timeout**: custom script tools now respect a configurable timeout (`timeout` per tool, or global `tool_timeout`) — kills runaway scripts instead of hanging forever
- **Zombie process detection**: `_pid_alive()` now checks `/proc/<pid>/status` to detect zombie processes that fool `kill -0`

### Changed

- **Graceful stop with escalation**: `stop()` sends SIGTERM, waits up to `timeouts.stop` (default 10s), then escalates to SIGKILL — returns `"force_killed"` status when needed
- **Robust socket cleanup**: X11 lock file PID is checked before removing sockets; deterministic Wayland sockets are also cleaned; sockets are cleaned on both `stop()` and `kill()`
- setup.sh now lists `wtype`, `ydotool`, and `wl-clipboard` as optional dependencies

## [0.3.0] - 2026-03-11

### Added

- **Configurable timeouts**: `timeouts.wayland_display`, `timeouts.xwayland_display`, `timeouts.app_render` in config.yaml — no more hardcoded waits
- **Concurrent instance support**: state files now use instance name (`/tmp/wbox_<name>_state.json`) instead of compositor type, allowing multiple wbox instances to run simultaneously
- **Cage stderr logging**: cage compositor stderr is now captured to `./log/cage-compositor.log` for debugging (previously discarded)
- **Clipboard tools**: `clipboard_read` and `clipboard_write` MCP tools — read/write the compositor's X11 clipboard via xclip or xsel

### Changed

- xclip added to optional system dependencies in setup.sh

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
