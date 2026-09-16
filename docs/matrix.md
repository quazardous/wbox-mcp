# Compatibility Matrix

What actually works, per compositor and input backend. The tables below are
derived from a full run of `tests/test_integration.py`, not from memory — but
read [What the suite does not prove](#what-the-suite-does-not-prove) before
trusting a green cell, because some of them are greener than the feature is.

**Last measured**: 2026-09-16 — 262 passed, 0 failed, 0 skipped, in 6m30s.

```
labwc 0.9.6 · weston 15.0.1 · cage 0.3.1 · Python 3.12 · Linux 7.2.5 (Fedora)
.venv/bin/python -m pytest tests/test_integration.py
```

Every combination below was exercised: no compositor and no input tool was
missing on the machine, so nothing skipped out of the run.

## Legend

| Mark | Meaning |
|------|---------|
| ✅ | Verified by a test that actually asserts on the result |
| ⚠️ | Works, with the caveat in the footnote |
| ❌ | Not supported — the call returns an error |
| — | Not applicable to this compositor |
| ∅ | Combination not supported by construction (see weston) |

## Compositor × input backend

Rows are the MCP tools a user calls. Each cell covers all three app modes
(`normal`, `fixed`, `fullscreen`) unless noted.

| Feature | labwc x11 | labwc hybrid | labwc wayland | weston x11 | cage x11 | cage hybrid | cage wayland |
|---|---|---|---|---|---|---|---|
| `launch` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `screenshot` | ✅ | ✅ | ✅ | ⚠️¹ | ✅ | ✅ | ✅ |
| `mouse_move` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `click` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `type_text` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `key` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `clipboard_write` / `clipboard_read` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Popup open / click / close | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `get_mouse_position` | ⚠️² | ⚠️² | ⚠️² | ⚠️² | ⚠️² | ⚠️² | ⚠️² |
| Undecorate to origin | ✅ | ✅ | ✅ | — ³ | — ³ | — ³ | — ³ |
| `resize` | ✅ | ✅ | ✅ | ⚠️⁴ | ❌⁵ | ❌⁵ | ❌⁵ |
| Fixed-size app resists resize | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `headless` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `clipboard_bridge` | ⚠️⁶ | ⚠️⁶ | ⚠️⁶ | — ⁷ | — ⁷ | — ⁷ | — ⁷ |

weston is listed with `x11` only: it implements neither
`zwlr_virtual_pointer_manager_v1` nor `zwp_virtual_keyboard_manager_v1`, so
`hybrid` and `wayland` — both of which drive the built-in wbox-keyboard and
wbox-pointer clients through those protocols — cannot work there at all. The
combination is ∅, not a low score: there is nothing to fix short of weston
implementing the protocols.

1. weston's `screenshot` rejects the `scale` and `region` arguments
   (`weston.py`), so captures are always full-size. Token cost is on you.
2. `get_mouse_position` returns the last position wbox itself set via `click`
   or `mouse_move` — it never asks the compositor. Wayland has no protocol to
   query the cursor, and `xdotool getmouselocation` is unreliable under
   Xwayland. If something else moves the pointer, the answer is stale. This is
   backend-independent: the x11 backend behaves exactly like the others.
3. cage is a kiosk compositor, and weston defaults to its kiosk shell
   (`weston_shell: kiosk`): the app is always fullscreen and undecorated, so
   there is nothing to undecorate. The undecorate test still passes on both —
   trivially, since the window is already at the origin. `weston_shell:
   desktop` is untested here.
4. weston's `resize` kills the compositor and relaunches it at the new size.
   The new size is correct, but **the app restarts and loses its state** —
   unsaved work included. labwc resizes the live output via `wlr-randr`
   instead, with no restart.
5. cage has no resize: fixed-size fullscreen is the point of a kiosk
   compositor. The call returns an error.
6. The bridge is real and works both ways, but it is **not covered by the test
   suite** — the harness sets `clipboard_bridge: false` precisely so the tests
   don't overwrite the developer's desktop clipboard. Treat the ⚠️ as
   "verified by hand, not by CI".
7. `clipboard_bridge` is implemented on labwc only.

## App modes

`app_mode` is a property of the test app (crash dummy), not a wbox setting —
it's how the suite covers apps that behave differently.

| Mode | What the app does | Covered |
|---|---|---|
| `normal` | Ordinary resizable window, WM places it | ✅ all 7 combos |
| `fixed` | Sets `min_size == max_size`, refuses resize | ✅ all 7 combos |
| `fullscreen` | `overrideredirect(True)`, bypasses the WM entirely | ✅ all 7 combos |

Undecorating only moves *managed* toplevels: `_undecorate_x11_windows()` gates
on `WM_STATE`, because the window search also returns the app's internal
subwindows, and resizing those wrecks a tkinter layout.

## What the suite does not prove

A green run is narrower than it looks. Four gaps, in decreasing order of how
likely they are to bite:

- **Nothing runs on a real desktop.** The harness forces `headless: true`
  (`WBOX_TEST_VISIBLE=1` overrides). The windowed path — the one most users
  actually run — is exercised by hand only.
- **`keyboard_layout` is pinned to `us`** in the harness. That's the
  documented requirement, but it means the suite can never catch the AZERTY
  failure mode it was written for: on a non-US host without that setting,
  `type_text("abc123")` can still arrive as `qbc!@#`.
- **`clipboard_bridge` is switched off** for every test (see footnote 6).
- **Two tests are greener than the feature.** `test_resize_normal_mode` only
  asserts when `resize()` returns without an error, so on cage — where resize
  is unsupported — it passes without checking anything. And
  `test_decorate_has_offset` asserts only that the window *has* geometry,
  never that decorations actually offset it. Both should assert the real
  behaviour or skip explicitly; a vacuous pass is worse than a skip, because
  it reads as coverage.

## Recommendation

**labwc + hybrid**, which is the default, and which
[docs/backends.md](backends.md) recommends for the same reasons: everything in
the table works, the nested window is resizable and movable on the host, input
goes through the Wayland virtual-input protocols so nothing leaks onto your
seat, and the x11 clipboard avoids the wl-copy pitfalls of nested compositors.

- **cage + hybrid** if you want a kiosk: identical except you cannot resize.
- **weston + x11** only if you specifically need weston. Resize restarts your
  app, screenshots can't be scaled or cropped, and the two better input
  backends are unavailable.
- **`x11` on any compositor** is a solid fallback, and the only option on
  weston. It drives xdotool through Xwayland, so it can't reach a pure Wayland
  app that never touches Xwayland.

This file is regenerated by hand after a full suite run. If the "last
measured" date above is old, the numbers describe a version of wbox that may
no longer exist — rerun the suite rather than trusting them.
