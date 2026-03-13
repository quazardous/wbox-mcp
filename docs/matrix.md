# Compatibility Matrix

## Scoring

| Score | Meaning |
|-------|---------|
| 2 | Works, no issues |
| 1 | Partial, workaround or limitation |
| 0 | Does not work |
| — | Not applicable (not counted) |

## Compositor × Input Backend

### labwc

| Feature | x11 | hybrid | wayland |
|---|---|---|---|
| Launch | 2 | 2 | 2 |
| Screenshot | 2 | 2 | 2 |
| Mouse move | 2 | 2 | 1¹ |
| Mouse click | 2 | 2 | 1¹ |
| Keyboard type | 2 | 2 | 2 |
| Key shortcut | 2 | 2 | 2 |
| Clipboard write | 2 | 1² | 1² |
| Clipboard read | 2 | 1² | 1² |
| Undecorate | 2 | 2 | 2 |
| Maximize (0,0) | 2 | 2 | 2 |
| Resize | 2 | 2 | 2 |
| Fixed-size app | 2 | 2 | 2 |
| Popup open | 2 | 2 | 2 |
| Popup click | 2 | 2 | 1¹ |
| Mouse position query | 2 | 2 | 1³ |
| **Total** | **30/30** | **28/30** | **24/30** |

1. ydotool uses /dev/uinput (kernel level), events go to host compositor — mouse offset in nested compositors
2. wl-copy may hang if WAYLAND_DISPLAY not correctly forwarded — x11 clipboard more reliable
3. Wayland has no API to query cursor position — returns last tracked position only

### weston (kiosk)

| Feature | x11 | hybrid | wayland |
|---|---|---|---|
| Launch | 2 | 0¹ | 0¹ |
| Screenshot | 2 | — | — |
| Mouse move | 2 | — | — |
| Mouse click | 2 | — | — |
| Keyboard type | 2 | — | — |
| Key shortcut | 2 | — | — |
| Clipboard write | 2 | — | — |
| Clipboard read | 2 | — | — |
| Undecorate | — | — | — |
| Maximize (0,0) | —² | — | — |
| Resize | 0³ | — | — |
| Fixed-size app | 2 | — | — |
| Popup open | 2 | — | — |
| Popup click | 2 | — | — |
| Mouse position query | 2 | — | — |
| **Total** | **24/26** | **0/2** | **0/2** |

1. weston does not implement wlroots protocols — wtype/ydotool fail, only x11 backend works
2. weston kiosk auto-fullscreens, no decorations by default
3. weston kiosk window not resizable on host

### cage

| Feature | x11 | hybrid | wayland |
|---|---|---|---|
| Launch | 2 | 2 | 2 |
| Screenshot | 2 | 2 | 2 |
| Mouse move | 2 | 2 | 1¹ |
| Mouse click | 2 | 2 | 1¹ |
| Keyboard type | 2 | 2 | 2 |
| Key shortcut | 2 | 2 | 2 |
| Clipboard write | 2 | 1² | 1² |
| Clipboard read | 2 | 1² | 1² |
| Undecorate | —³ | —³ | —³ |
| Maximize (0,0) | —³ | —³ | —³ |
| Resize | 0⁴ | 0⁴ | 0⁴ |
| Fixed-size app | 2 | 2 | 2 |
| Popup open | 2 | 2 | 2 |
| Popup click | 2 | 2 | 1¹ |
| Mouse position query | 2 | 2 | 1⁵ |
| **Total** | **24/26** | **22/26** | **18/26** |

1. ydotool offset — same as labwc
2. wl-copy timeout — same as labwc
3. cage is a kiosk compositor — always fullscreen, no decorations
4. cage window not resizable on host
5. ydotool position query — same as labwc

## App Mode × Undecorate

| Mode | Undecorate | Maximize | Notes |
|---|---|---|---|
| fullscreen | —¹ | 2 | overrideredirect, no WM interaction |
| normal | 2 | 1² | _MOTIF_WM_HINTS removes title bar |
| fixed | 2 | 0³ | app refuses resize (min=max hints) |

1. overrideredirect bypasses WM — no decorations to remove
2. WM may reposition window after undecorate
3. app sets min_size=max_size, WM cannot resize to fill display

## Overall Ranking

| Combo | Score | Max | % |
|---|---|---|---|
| labwc + x11 | 30 | 30 | 100% |
| labwc + hybrid | 28 | 30 | 93% |
| weston + x11 | 24 | 26 | 92% |
| cage + x11 | 24 | 26 | 92% |
| labwc + wayland | 24 | 30 | 80% |
| cage + hybrid | 22 | 26 | 85% |
| cage + wayland | 18 | 26 | 69% |
| weston + hybrid | 0 | 2 | 0% |
| weston + wayland | 0 | 2 | 0% |

**Recommended**: labwc + hybrid (93%) — best balance of isolation (wtype keyboard doesn't touch host) and reliability (xdotool mouse is accurate).
