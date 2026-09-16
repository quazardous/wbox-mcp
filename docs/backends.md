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
| Screenshot | `PrintWindow` (`PW_RENDERFULLCONTENT`) | Yes |
| Clicks | `PostMessage`, or `SendInput` — see below | **Usually not** |
| Text input | Clipboard + `Ctrl+V` via `SendInput` | No (steals focus) |
| Single keys | `PostMessage WM_KEYDOWN/UP` | Yes |
| Key combos | `SendInput` | No (briefly steals focus) |
| Modal dialogs | `EnumChildWindows` / `EnumWindows` | Yes |
| Clipboard | Win32 clipboard API | Yes |

**"Background" is narrower than it looks on Windows.** Only one click target
qualifies: `click` uses `PostMessage` *only* when the coordinates land on the
discovered edit control. Menus, tabs, toolbars, XAML dialogs, and anything
outside the client area all route to `SendInput`, which calls
`SetForegroundWindow` and then warps the **real mouse cursor** to the target
before clicking. Your pointer jumps, and the window comes to the front.

`type_text` never uses `WM_CHAR`. It saves the clipboard, writes the text to
it, sends `Ctrl+V` through `SendInput`, and restores the clipboard afterwards
— so typing also takes focus, and briefly puts your text on the system
clipboard.

## Windows limitations

None of this applies to the Linux backends, where the app lives in its own
compositor. On Windows the app is an ordinary process on your desktop.

**There is no isolation.** The app shares your screen, your seat and your
clipboard. It can see and be seen by everything else you have open. If you
need the app sandboxed, you need the Linux path.

**Elevated (administrator) apps mostly fail, and one way fails silently.**
User Interface Privilege Isolation stops a medium-integrity process — which is
what wbox is, unless you run it elevated too — from driving a high-integrity
window:

- `PostMessage` to an elevated window **returns success and is silently
  discarded**. This is the dangerous one: wbox reports `{"ok": true, "method":
  "PostMessage"}` for a click that never happened.
- `SendInput` **mouse** events toward an elevated window are dropped by the
  window manager before they arrive.
- `SendInput` **keyboard** events can still reach elevated windows, and the
  clipboard is not subject to UIPI at all — so `type_text` and key combos may
  work on an app where clicking does nothing.

If an elevated app is the target, run wbox elevated as well. Otherwise expect
clicks that report success and change nothing.

**Minimized and hidden windows don't capture.** `PrintWindow` asks the window
to draw itself, so a minimized window has nothing to draw: the capture comes
back blank or stale rather than failing outright. `screenshot` only reports
`PrintWindow failed` when the API refuses both attempts
(`PW_RENDERFULLCONTENT`, then flags `0`) — a blank-but-valid bitmap is
returned as a normal screenshot. Occluded windows are fine; minimized ones are
not. Some GPU-composited and DirectComposition surfaces also render as blank
or black under `PrintWindow`, which is why the `PW_RENDERFULLCONTENT` flag is
tried first.

**`type_text` can lose a non-text clipboard.** The save-and-restore only
handles `CF_UNICODETEXT`. If your clipboard held an image, files, or any other
format, there is nothing to restore and the original content is gone once wbox
overwrites it. A clipboard history tool will also capture whatever was typed.

**Which operations disturb you**, in one list:

| Operation | Takes focus | Moves your cursor |
|-----------|-------------|-------------------|
| `screenshot` | No | No |
| `click` on the edit control | No | No |
| `click` anywhere else | Yes | **Yes** |
| `type_text` | Yes | No |
| `key` without modifiers, no modal | No | No |
| `key` with modifiers, or with a modal open | Yes | No |
| `clipboard_read` / `clipboard_write` | No | No |

> These limits are derived from the implementation in
> `src/wbox/compositor/win32.py` and from documented Windows behaviour. They
> have **not** been re-verified on a Windows machine for this revision — if you
> hit something that contradicts the table, the table is what's wrong.

## Windows-specific config

| Key | Description |
|-----|-------------|
| `compositor: win32` | Auto-detected on Windows, explicit on cross-platform configs |
| `title_hint` | Substring to match in window title (helps find the right window when the app spawns multiple processes) |
| `timeouts.window_discovery` | How long to wait for the app window to appear (default: 10s) |
| `timeouts.edit_control` | How long to wait for the text input control (default: 3s) |
