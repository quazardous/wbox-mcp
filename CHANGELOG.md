# Changelog

<!--
  This changelog is meant for humans, not machines.
  Keep it readable — no commit hashes, no ultra-technical jargon.
  Focus on what changed and why it matters for users.
  Format: https://semver.org
-->

## [0.6.0] - 2026-09-16

### Added

- **Headless mode**: `headless: true` (alias `quiet`, or `wboxr init --headless`) runs the nested session offscreen — nothing appears on your desktop, while screenshots, clicks and keystrokes keep working exactly the same. Ideal for test suites and unattended runs. labwc and cage render to an offscreen output; weston is switched to its software renderer automatically.
- **`clipboard_bridge` option** (labwc): the host↔sandbox clipboard sync added in 0.5.0 is now switchable. It still defaults to `true`, but while the bridge is up the sandboxed app can read everything you copy — passwords included. Set it to `false` to keep the sandbox clipboard isolated.
- **`wbox-keyboard` backend**: built-in virtual-keyboard client, no external tool. It uploads a US keymap and sends US-position keycodes, which is what makes typed text survive compositors that force their seat keymap onto every keyboard. Now the keyboard of the `hybrid` and `wayland` presets.
- **`keyboard_layout` works on every compositor** (was labwc-only) and is now forced onto the nested Xwayland too. Without it, `type_text("abc123")` can arrive as `qbc!@#` on an AZERTY host.
- **Screenshot `scale` and `region`**: shrink or crop the capture to cut image token cost. The result now also returns the file path.
- **`setxkbmap` is a required dependency**; `wtype` and `ydotool` become optional.

### Changed

- **`hybrid` and `wayland` presets now use the built-in clients** — `wbox-keyboard` + `wbox-pointer` instead of wtype and xdotool/ydotool. wtype's keymap is ignored by wlroots compositors, so its keycodes decoded as `Escape 1 2 3 4 5`; ydotool injects into the *host* seat through `/dev/uinput`, so its absolute coordinates can never address the nested compositor. Both remain selectable per function.
- **One Wayland connection per session**: the virtual pointer and keyboard are created once during launch instead of once per operation — no fork per click, and no more dropped first events.
- **Faster hot paths**: Windows screenshots encode in ~30ms instead of ~1-3s per 1280×800 frame; an xdotool click spawns 2 processes instead of 5; `keys` sends a whole shortcut sequence in a single spawn; launch detects its sockets in tens of milliseconds instead of 300ms steps; compositor calls run off the event loop, which `launch` could previously block for ~28s.
- **The test suite runs headless by default** (`WBOX_TEST_VISIBLE=1` to watch it), including the standalone sanity tests, which now get their own Xvfb. A full run opens no window at all.
- **Script tool output is capped** (first 20 and last 80 lines, with the full log path always included), and tool results are rendered as compact JSON instead of Python repr.

### Fixed

- **Typed text was garbled on non-US hosts**, from two independent causes: the nested Xwayland kept the system layout instead of the compositor's, and wlroots compositors ignore the keymap wtype uploads.
- **Clicks landed in the wrong place, or nowhere at all.** cage opened its output at 1280×720 whatever the configured size, scaling every coordinate; the first pointer event after launch was swallowed because the nested compositor had no pointer position yet; and a virtual pointer created per operation made every click the first event on a brand-new device, dropped roughly one time in five.
- **`clipboard_write` reported failure on a write that had succeeded** (cage), or lost the selection immediately (labwc, with the bridge running). Wayland keeps no clipboard storage — the source client has to stay alive to serve every paste, so wbox now keeps its own.
- **Undecorating a window left it at its decorated position** (labwc): offset by the titlebar instead of snapped to the origin.
- **weston captured nothing in headless mode**: its headless backend defaults to a no-op renderer, so there was no framebuffer to capture and the screenshot waited forever. The pixman renderer is now forced.
- **`wboxr register` destroyed every other entry** of an unparseable `.mcp.json` or Claude settings file. It now refuses with an explicit error, and writes atomically.
- **`wboxr init` wrote script templates relative to the current directory** instead of next to the config file.
- **`clean` deleted the log file the running server still held open**, silently losing all logging until the next restart.
- **The clipboard bridge and the pointer connection leaked on `kill`** — teardown only ran on `stop`.
- **Zombie children after `kill`**; stale Wayland sockets silently reused under a running compositor; a compositor able to block on a stderr pipe nobody drained.
- **Windows**: `list_windows` and `focus_window` errored out (they inherited the Linux implementations), and `get_mouse_position` always returned (0, 0).

## [0.5.0] - 2026-03-13

### Added

- **Clipboard bridge** (labwc): bidirectional clipboard sync between the nested compositor and the host via `wl-paste --watch` + `wl-copy`. Copy-paste now works seamlessly in both directions. Loop-proof via shared hash guard file.
- **`list_windows` tool**: list all windows/toplevels in the compositor via `wlrctl toplevel list` — useful for finding modal dialogs that may be hidden behind the main window.
- **`focus_window` tool**: focus/raise a window by title or app_id via `wlrctl toplevel focus` — brings hidden modals to front.
- **`post_launch_keys` config**: list of keyboard shortcuts sent after app renders (e.g. `["super+a"]` to maximize via labwc keybind). Configurable delay between keys.
- **`keyboard_layout` config** (labwc): set XKB keyboard layout for the nested compositor (e.g. `fr` for AZERTY). Empty inherits from host.
- **wtype modifier translation**: `super` → `logo` mapping for wtype compatibility (wtype uses "logo" not "super" for the Super key).
- **`wlrctl` as required dependency**: added to setup.sh and documentation.

### Changed

- **labwc rc.xml simplified**: removed windowRules (unreliable for actions at map time). Window management now done via `post_launch_keys` and `wlrctl`.
- **`wlr-randr` added to required deps** (was missing from setup.sh in 0.4.0).

## [0.4.0] - 2026-03-12

### Added

- **Windows Win32 backend**: full MCP server for Windows — launch any app and control it via screenshots, keyboard, mouse using Win32 APIs (PrintWindow, PostMessage, SendInput)
- **setup.ps1**: Windows installer — auto-installs Python/uv/git via winget, creates .cmd shims, updates PATH
- **Platform-aware wizard**: `wboxr init` adapts to the platform — auto-detects `win32` on Windows, asks for `title_hint` and timeouts instead of compositor/screen/weston options
- **`--title-hint`**: CLI flag for non-interactive init on Windows
- **XAML ContentDialog detection**: detects WinUI3 modal overlays (DesktopChildSiteBridge) and reports `modal_visible` in screenshot/debug responses
- **SendInput mouse fallback**: clicks on WinUI3/XAML elements (menus, tabs, dialogs) that lack child HWNDs automatically use SendInput with absolute screen coordinates
- **Win32 clipboard tools**: `clipboard_read` and `clipboard_write` via native Win32 API (no external tools needed)
- **Native Wayland input backend**: new `input_backend: "wayland"` config option — uses `wtype` for keyboard input and `ydotool` for mouse, bypassing Xwayland entirely
- **Wayland clipboard support**: `clipboard_read`/`clipboard_write` now work with `wl-paste`/`wl-copy` when using the wayland input backend
- **Deterministic Wayland socket naming** (Weston): socket is now `wbox-<instance>` instead of auto-assigned `wayland-N`, eliminating collisions between concurrent instances
- **Script tool timeout**: custom script tools now respect a configurable timeout (`timeout` per tool, or global `tool_timeout`) — kills runaway scripts instead of hanging forever
- **Zombie process detection**: `_pid_alive()` now checks `/proc/<pid>/status` to detect zombie processes that fool `kill -0`
- **labwc compositor backend**: wlroots-based stacking WM — resizable/movable window on the host, supports `hybrid` input backend. New default compositor on Linux.
- **Granular input backend config**: `input_backend` can now be a string preset (`hybrid`, `x11`, `wayland`) or a per-function dict (`keyboard`, `mouse`, `clipboard`). The `hybrid` preset (wtype keyboard + xdotool mouse + wl-clipboard) is the new default — zero interference with the user's desktop.
- **`--input-backend` CLI flag**: non-interactive init now supports `--input-backend hybrid|x11|wayland`
- **`examples/config.sample.yaml`**: full config reference with all options documented
- **`get_mouse_position` tool**: returns current cursor coordinates inside the compositor
- **Headless script tools**: custom script tools with `headless: true` run without requiring the compositor to be running
- **Script tool arguments as env vars**: MCP tool arguments are forwarded to scripts as `WBOX_ARG_<NAME>` environment variables

### Changed

- **`type_text` uses clipboard+paste on Windows**: Ctrl+V via SendInput always targets the active tab/document (fixes WinUI3 multi-tab apps like Notepad Win11)
- **`click` hybrid routing**: PostMessage for edit control (background), SendInput for everything else (menus, tabs, dialogs)
- **`key` modal routing**: automatically uses SendInput when a modal dialog (classic or XAML) is visible
- **`kill` finds real PID via HWND**: uses `GetWindowThreadProcessId` + stored PID to kill re-parented child processes (Win11 Notepad, etc.) — no more orphan processes
- **`clean` tolerates locked files**: skips files held by the running MCP server instead of crashing
- **Platform-conditional imports**: compositor modules loaded only on their target platform
- **README rewritten**: dual Linux/Windows documentation, init flags table, platform-specific config examples
- **Graceful stop with escalation**: `stop()` sends SIGTERM, waits up to `timeouts.stop` (default 10s), then escalates to SIGKILL — returns `"force_killed"` status when needed
- **Robust socket cleanup**: X11 lock file PID is checked before removing sockets; deterministic Wayland sockets are also cleaned; sockets are cleaned on both `stop()` and `kill()`
- **Default compositor changed to labwc** (from weston) — resizable, wlroots protocols, hybrid input
- **Default input backend changed to hybrid** (from x11) — wtype keyboard + xdotool mouse, no host interference
- **setup.sh**: `labwc`, `grim`, `xdotool`, `wtype` are now required deps; `weston`, `cage`, `ydotool` are optional
- **Wizard**: prompts for input backend on Linux; auto-forces `x11` for weston (no wlroots protocol support)

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
