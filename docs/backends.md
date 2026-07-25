# Backends & Input Reference

## Compositors (Linux)

| Compositor | Type | Resizable | Movable | Virtual-input protocols | Headless | Best input backend |
|------------|------|-----------|---------|-------------------------|----------|--------------------|
| **labwc** (default) | Stacking WM | Yes | Yes | Yes | Yes | **`hybrid`** |
| weston | Reference | Yes | Yes | No | Yes (pixman) | `x11` |
| cage | Kiosk | No | No | Yes | Yes | `hybrid` |

**labwc + hybrid is the recommended setup.** labwc is a lightweight wlroots-based stacking WM (Openbox-inspired). The nested compositor window is resizable and movable on your host desktop. Combined with the `hybrid` input backend, it provides pixel-perfect mouse and keyboard input through Wayland virtual-input protocols, plus a reliable clipboard (xclip via Xwayland). Zero interference with the user's desktop.

Other compositors:
- **weston** — Wayland reference compositor. Resizable, but implements neither the virtual-pointer nor the virtual-keyboard protocol, so only the `x11` input backend works.
- **cage** — Kiosk compositor. Fixed-size fullscreen, no resize/move. wlroots-based, so `hybrid` works but you can't resize.

All three support `headless: true` (offscreen, no window on the host desktop). weston is forced onto the pixman software renderer there: its headless default is the *noop* renderer, which draws nothing and leaves screenshots waiting forever.

## Keyboard layout

Input injection assumes US keycode positions. On a host with another layout (AZERTY…), set `keyboard_layout: us` so the nested seat — and its Xwayland, which otherwise keeps the host layout — agree with what wbox sends. Without it, `type_text("abc123")` can arrive as `qbc!@#`.

## Input backends (Linux)

Controls how keyboard, mouse, and clipboard input is injected into the nested compositor.

| Preset | Keyboard | Mouse | Clipboard | Interferes with host? | Compositors |
|--------|----------|-------|-----------|-----------------------|-------------|
| **`hybrid`** (default) | **wbox-keyboard** | **wbox-pointer** | xclip (x11) | No | labwc, cage |
| `x11` | xdotool | xdotool | xclip/xsel | No | all |
| `wayland` | **wbox-keyboard** | **wbox-pointer** | wl-clipboard | No | labwc, cage |

**`hybrid`** is the recommended default:
- **Keyboard**: wbox-keyboard — built-in pure Python client using `zwp_virtual_keyboard_manager_v1`, sending US-position keycodes
- **Mouse**: wbox-pointer — same client, using `zwlr_virtual_pointer_manager_v1` for pixel-perfect absolute positioning
- **Clipboard**: xclip via Xwayland — reliable and isolated

Zero interference with the user's desktop. No kernel-level input injection.

**Why not wtype?** wlroots compositors (labwc, cage) force the seat keymap onto every keyboard device and ignore the keymap wtype uploads, so its keycodes are decoded against the wrong table — `abc123` arrives as `Escape 1 2 3 4 5`. wbox-keyboard sends keycodes at US positions instead, which survives that. wtype remains selectable per-function.

**ydotool is available but not recommended**: it injects through `/dev/uinput` into the *host* seat, so it moves the user's real mouse and its absolute coordinates address the host screen — they can never be accurate inside the nested compositor.

Per-function override:

```yaml
input_backend:
  keyboard: wbox-keyboard  # wbox-keyboard, wtype, or xdotool
  mouse: wbox-pointer      # wbox-pointer, xdotool, or ydotool
  clipboard: x11           # x11 or wayland
```

## Win32 backend (Windows)

No compositor needed. Uses Win32 APIs directly:

| Function | API | Background? |
|----------|-----|-------------|
| Screenshot | `PrintWindow` | Yes |
| Text input | `PostMessage WM_CHAR` | Yes |
| Clicks | `PostMessage WM_LBUTTONDOWN/UP` | Yes |
| Key combos | `SendInput` | No (briefly steals focus) |
| Modal dialogs | `EnumChildWindows` | Yes |
| Clipboard | Win32 clipboard API | Yes |

## Windows-specific config

| Key | Description |
|-----|-------------|
| `compositor: win32` | Auto-detected on Windows, explicit on cross-platform configs |
| `title_hint` | Substring to match in window title (helps find the right window when the app spawns multiple processes) |
| `timeouts.window_discovery` | How long to wait for the app window to appear (default: 10s) |
| `timeouts.edit_control` | How long to wait for the text input control (default: 3s) |
