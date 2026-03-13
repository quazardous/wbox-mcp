# Backends & Input Reference

## Compositors (Linux)

| Compositor | Type | Resizable | Movable | wlroots protocols | Best input backend |
|------------|------|-----------|---------|-------------------|--------------------|
| **labwc** (default) | Stacking WM | Yes | Yes | Yes | **`hybrid`** |
| weston | Reference | Yes | Yes | No | `x11` |
| cage | Kiosk | No | No | Yes | `hybrid` |

**labwc + hybrid is the recommended setup.** labwc is a lightweight wlroots-based stacking WM (Openbox-inspired). The nested compositor window is resizable and movable on your host desktop. Combined with the `hybrid` input backend, it provides pixel-perfect mouse input (via wbox-pointer, a built-in Wayland virtual pointer), native keyboard input (wtype), and reliable clipboard (xclip via Xwayland). Zero interference with the user's desktop.

Other compositors:
- **weston** — Wayland reference compositor. Resizable but does NOT support wlroots protocols (wtype, virtual-pointer), so only the `x11` input backend works.
- **cage** — Kiosk compositor. Fixed-size fullscreen, no resize/move. wlroots-based, so `hybrid` works but you can't resize.

## Input backends (Linux)

Controls how keyboard, mouse, and clipboard input is injected into the nested compositor.

| Preset | Keyboard | Mouse | Clipboard | Interferes with host? | Compositors |
|--------|----------|-------|-----------|-----------------------|-------------|
| **`hybrid`** (default) | wtype | **wbox-pointer** | xclip (x11) | No | labwc, cage |
| `x11` | xdotool | xdotool | xclip/xsel | No | all |
| `wayland` | wtype | ydotool | wl-clipboard | **Yes** (mouse) | labwc, cage |

**`hybrid`** is the recommended default:
- **Keyboard**: wtype — native Wayland protocol, isolated in the nested compositor
- **Mouse**: wbox-pointer — built-in pure Python tool using the `zwlr_virtual_pointer_manager_v1` Wayland protocol for pixel-perfect absolute positioning
- **Clipboard**: xclip via Xwayland — reliable and isolated

Zero interference with the user's desktop. No kernel-level input injection.

**`wayland`** is NOT recommended: ydotool uses `/dev/uinput` which injects events at the kernel level, moving the user's real mouse.

Per-function override:

```yaml
input_backend:
  keyboard: wtype        # wtype or xdotool
  mouse: wbox-pointer    # wbox-pointer, xdotool, or ydotool
  clipboard: x11         # x11 or wayland
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
