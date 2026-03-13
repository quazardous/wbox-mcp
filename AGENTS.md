# AGENTS.md — wbox-mcp development automation

## MCP Servers

### crash-dummy (static)
The original wbox MCP server running crash dummy with fixed config.
- Config: `tests/crash-dummy/config.yaml`
- **Requires `/mcp` after code changes** (Python caches modules)
- Good for quick manual testing with known-good config

### mcp-dev (dynamic)
Dynamic wrapper that does NOT import wbox. Spawns a worker subprocess.
- **No `/mcp` needed after code changes** — use `reload` then `launch`
- Change config on the fly with `configure`

#### Workflow

```
1. configure(compositor="labwc", input_backend="hybrid", app_mode="normal")
2. launch          → starts worker subprocess + wbox + crash dummy
3. screenshot      → check display
4. send_cmd dump   → get widget positions as JSON
5. click / type_text / key / mouse_move → test inputs
6. tail_log        → read crash dummy event log
7. stop            → clean shutdown

# After code changes:
8. reload          → kills worker (next launch = fresh Python imports)
9. launch          → new worker with updated code
```

#### Testing a matrix of configs

```
for each compositor in [labwc, weston, cage]:
  for each input_backend in [x11, hybrid, wayland]:
    for each app_mode in [normal, fixed, fullscreen]:
      configure(compositor=..., input_backend=..., app_mode=...)
      launch
      send_cmd dump     → verify widget positions
      click 400 300     → verify click delivery
      type_text "test"  → verify keyboard
      screenshot        → visual check
      stop
```

## Crash Dummy Commands (FIFO)

Crash dummy listens on a named pipe (FIFO). Send commands via `send_cmd`:

| Command | Description |
|---------|-------------|
| `dump` | JSON dump of all widget positions (absolute display coords) |
| `ping` | Log "pong" (liveness check) |
| `open_popup` | Open popup dialog window |
| `close_popup` | Close popup dialog |
| `set_text <text>` | Insert text in the text input area |

The `dump` response is a JSON dict with widget names as keys and `{x, y, w, h}` as values.
Use it to verify:
- Window position (root.x, root.y)
- Widget layout integrity (all widgets have non-zero w/h)
- Decoration offset (root.x/y should be 0,0 with undecorate=True + fullscreen)

## Key Files

| File | Role |
|------|------|
| `src/wbox/compositor/base.py` | Core compositor: launch, click, type, clipboard, undecorate |
| `src/wbox/config.py` | Input backend presets (x11/wayland/hybrid), config loading |
| `src/wbox/server.py` | MCP server, compositor factory, tool dispatch |
| `src/wbox/cli/server.py` | CLI entry point, --set overrides, --mcp-dir |
| `tests/crash-dummy/crash_dummy.py` | Test GUI app (tkinter), modes, FIFO listener, event logging |
| `tests/mcp-dev/server.py` | Dynamic MCP wrapper (no wbox import) |
| `tests/mcp-dev/worker.py` | Worker subprocess (imports wbox, handles compositor) |
| `tests/test_integration.py` | Pytest suite: all compositor × backend × mode combos |
| `docs/matrix.md` | Compatibility matrix with scores |

## Common Bugs and Fixes

### Click not registering
Cause: X11 window lacks focus. Fix: `_focus_active_window()` before xdotool click (already in base.py).

### wl-copy timeout
Cause: `wl-copy` can't connect to nested Wayland compositor. Fix: use `clipboard: x11` in hybrid preset (xclip/xsel via Xwayland).

### Window decorations offset
Cause: labwc adds server-side decorations (title bar ~30px). Fix: `_undecorate_x11_windows()` sets `_MOTIF_WM_HINTS` via xprop. Do NOT use `xdotool windowmove` after — it breaks tkinter layout.

### Crash dummy layout broken
Cause: `xdotool windowmove` after tkinter renders causes widget collapse. Fix: never move X11 windows externally. Use `overrideredirect(True)` for fullscreen mode, accept WM placement for normal/fixed.

### MCP server stale code
Cause: Python module caching. Fix: use mcp-dev (worker subprocess restart) instead of crash-dummy for development.

## Input Backend Matrix

| Preset | Keyboard | Mouse | Clipboard |
|--------|----------|-------|-----------|
| x11 | xdotool | xdotool | xclip/xsel |
| wayland | wtype | ydotool | wl-copy/paste |
| hybrid | wtype | xdotool | xclip/xsel |

hybrid is the recommended default: wtype for reliable keyboard, xdotool for accurate mouse coords, x11 clipboard (avoids wl-copy issues with nested compositors).
