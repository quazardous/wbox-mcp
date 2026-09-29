# Windows

wbox runs on Windows, but not the way it runs on Linux. Read the first section
before you decide whether it fits what you want to do.

## What you get, and what you don't

On Linux the app lives inside a nested compositor: its own display, its own
seat, nothing shared with your desktop. **On Windows there is no compositor.**
The app is an ordinary process on your desktop, driven through Win32 APIs.

That buys you screenshots of a window that is behind other windows, typing
that never touches your clipboard, and clicks that are pixel-accurate. It does
**not** buy you isolation:

- the app shares your screen, your keyboard and mouse, and your clipboard;
- most clicks move your real mouse cursor and bring the window to the front;
- `headless: true` is ignored — the window is always on your desktop.

**Unless you add `sandbox:`**: the app then runs in Windows Sandbox, a
throwaway Windows of its own, and none of the above touches you — see
[Isolation: Windows Sandbox](#isolation-windows-sandbox).

## Isolation: Windows Sandbox

```yaml
compositor: win32
title_hint: "My App"
sandbox:
  mounts:
    - {host: ./build, guest: 'C:\app', readonly: true}
app:
  command: 'C:\app\myapp.exe'
```

With `sandbox:`, wbox starts [Windows
Sandbox](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/),
runs its win32 backend inside it, and drives the app there. The pointer it
moves, the keys it presses, the clipboard it writes are the sandbox's. The
tools and their answers are the same; the app's paths are the sandbox's.

What the sandbox gets from the host, and nothing else:

| In the sandbox | What | Access |
|---|---|---|
| `C:\wbox\python` | the Python running wbox | read-only |
| `C:\wbox\site`, `C:\wbox\src` | wbox and its dependencies | read-only |
| `C:\wbox\io` | the channel: requests, answers, screenshots | read-write |
| your `mounts` | the app, its data | read-only unless `readonly: false` |

Nothing is installed in the sandbox: it boots and shows the app in about 15
seconds. It has no network and no clipboard shared with yours.

| Key | Default | |
|---|---|---|
| `mounts` | none | `{host, guest, readonly}`; `host` relative to config.yaml |
| `networking` | `false` | the app gets a network (through the host) |
| `vgpu` | `true` | `false` renders in software (WARP) |
| `memory_mb` | Windows' | the sandbox's memory |
| `keep` | `false` | `kill`/`stop` leave the sandbox up, and the next `launch` reuses it, with no boot |
| `boot_timeout` | `180` | seconds to boot and answer |

`headless: true` minimizes the sandbox's window: the app keeps running, and
screenshots and input keep working (measured). The window itself cannot go: a
sandbox started without one (`wsb start`) has no logged-on session, and
nothing runs in it.

Windows Sandbox needs Windows 10/11 **Pro, Enterprise or Education**, turned
on once — `setup.ps1` does it, asking for admin rights — then a **reboot**.
Windows runs **one sandbox at a time**: `launch` refuses while another is
open, and `kill` only ever closes the sandbox wbox started.

**The sandbox is a fresh Windows**: nothing you installed is in it. Notably
no Visual C++ Redistributable — no `vcruntime140.dll` — so a program built
against it does not even start there (`no window found`, and nothing in its
log). Build it with the C runtime linked in (Rust: `-C
target-feature=+crt-static`; C/C++: `/MT`), or mount what it needs.

The sandbox's display density is your screen's: Windows Sandbox passes it on,
and it cannot be changed from inside (tried: the display settings API, the
registry, compatibility layers on the sandbox's client).

How it works: the sandbox's logon starts wbox's agent (`wbox.sandbox_agent`),
which drives the app with the same win32 backend as on the desktop; the host
sends it each call through `C:\wbox\io`. The requests are lines appended to
one file, the answers files of their own: a mapped folder is a network share
to the sandbox, which learns of a new file only seconds later but reads a file
it knows afresh — so a call takes a few tens of milliseconds, not five
seconds. Closing the sandbox's window ends it, with no dialog; turning
Windows off from inside would leave one on your desktop.

## Install

```powershell
irm https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.ps1 | iex
```

It installs whatever is missing: `uv`, `git`, and Python. For Python it tries
`winget` first and falls back to a copy managed by uv, which needs no admin
rights — so a machine with no Python at all ends up with one either way.

It also turns on Windows Sandbox, for `sandbox:` (asking for admin rights;
reboot afterwards). `-NoSandbox` leaves it off; on Windows Home it is not
available, and the installer says so.

From a clone, run it past PowerShell's script policy, which blocks local
`.ps1` files by default (the one-line install above is not affected):

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -DevMode
```

It does not trust the `python.exe` Windows ships by default. That file is an
*App Execution Alias* that only opens the Microsoft Store; an earlier version
of the installer mistook it for Python, skipped installing it, and failed.
Candidates are now run and must answer.

**Manual install** with [uv](https://docs.astral.sh/uv/), which brings its own
Python:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
git clone https://github.com/quazardous/wbox-mcp.git $HOME\.local\share\wbox-mcp
cd $HOME\.local\share\wbox-mcp
uv venv --python 3.12 .venv
uv pip install -e . --python .venv\Scripts\python.exe
```

No system packages are needed: the backend calls the Win32 API through
`ctypes`. Windows 10 or later.

## Configuration

```yaml
name: writer
compositor: win32            # auto-detected on Windows
title_hint: "LibreOffice"    # substring of the window title to look for
screenshot_dir: ./screenshots
timeouts:
  window_discovery: 10       # seconds to wait for the window to appear
  edit_control: 3            # seconds to wait for the text-input control
  app_render: 3              # seconds to let the app draw before returning
app:
  command: "C:/Program Files/LibreOffice/program/soffice.exe --writer"
```

**`title_hint` is matched against the localized title.** Win11 Notepad is
`Bloc-notes` on a French system, `Editor` on a German one. A hint of
`"Notepad"` finds nothing there.

`title_hint` also matters more than it looks: many Windows apps hand their
window to a different process than the one wbox started (Win11 Notepad does),
so the process id alone does not find it.

### Keys that do nothing on Windows

These are accepted, so a config shared with Linux still loads, but the Win32
backend ignores them:

| Key | Why it is ignored |
|-----|-------------------|
| `headless` | there is no offscreen display to render to (with `sandbox:`, it minimizes the sandbox's window) |
| `screen` | no virtual output; the window keeps the size the app gives it — use `resize` after `launch` |
| `input_backend` | input routing is decided per call, see below |
| `keyboard_layout` | `type_text` sends Unicode, so text is layout-proof; `key` shortcuts follow the host's own layout |
| `undecorate` | Windows draws the title bar; screenshots include it |
| `clipboard_bridge` | there is one clipboard, and it is yours |
| `weston_shell`, `weston_backend` | Linux compositors only |

## How input is delivered

| Operation | API | Takes focus |
|-----------|-----|-------------|
| `screenshot` | `PrintWindow` (`PW_RENDERFULLCONTENT`) | No |
| `click` on the text-input control | `PostMessage` | No |
| `click` anywhere else | `SendInput` | **Yes, and moves your cursor** |
| `mouse_move` | same routing as `click` | as `click` |
| `type_text` | `SendInput` `KEYEVENTF_UNICODE`, else posted `WM_CHAR` | Only if the window can come forward |
| `key` without modifiers, no dialog open | `PostMessage` | No |
| `key` with modifiers, or with a dialog open | `SendInput` | Yes |
| `clipboard_read` / `clipboard_write` | Win32 clipboard API | No |
| `scroll`, `dblclick`, `drag`, `hold` | `SendInput` | **Yes, and moves your cursor** — refused, like `click`, when the window cannot come forward |

### Clicks

`click` uses `PostMessage` only when the point lands on the text-input control
wbox discovered at launch. Everything else — menus, tabs, toolbars, dialogs,
WinUI3/XAML content — goes through `SendInput`, which brings the window to the
front and presses a real mouse button at that screen position.

`PostMessage` is not a universal background click either. Some toolkits ignore
posted mouse messages outright — Tk does, measured — so it is kept for the one
case where it is reliable.

### Display density

wbox works in physical pixels — a screenshot's — and so do its clicks, at any
display scaling and whatever DPI awareness the app declares. An app that is
not per-monitor aware counts in logical pixels (the physical ones divided by
the scaling), and Windows translates both routes for it: `SendInput` moves
the real pointer, and a posted mouse message to a DPI-unaware window is
scaled by Windows from the sender's awareness. Converting a posted click to
the app's pixels first would scale it twice: measured at 150 %, a click
aimed at (60,40) in the app's own pixels arrived at (40,27).

`tests/test_windows_density.py` holds this for apps that are DPI unaware,
system aware and per-monitor aware, through both routes. It measures the
scaling from the app itself instead of assuming one, so it holds at whatever
density it runs: 150 % on the machine it was written on (and in Windows
Sandbox there), 100 % on CI's runner.

### When the window cannot come to the front, input is refused

Windows refuses to bring a window forward for a process that is not already in
front. A `SendInput` click in that situation lands wherever the cursor is — in
*your* window, not the app's.

So `click`, `mouse_move` and key combos check two things before injecting
anything: that the app window really is in front, and that the target point
really belongs to it. If either fails, the call returns an error instead:

```json
{
  "error": "refused to click: could not bring the window to the foreground, so the click would have landed in another window",
  "screen_pos": [807, 603],
  "would_have_hit": "Untitled - Notepad"
}
```

`would_have_hit` names the window in the way. In 0.6.0 and earlier these calls
clicked it and returned `{"ok": true}`.

### Typing

`type_text` never touches the clipboard. It types the characters, one of two
ways:

- **The window can come forward** — `SendInput` with `KEYEVENTF_UNICODE`, one
  character per call, at least 20 ms apart. That pacing is load-bearing: a
  single `SendInput` carrying the whole string is silently mangled by WinUI3
  controls (`hello wbox 12345` arrived as `hello wbox 5555`), even though the
  identical call is flawless on Tk. The foreground is re-checked before every
  character, so if you click elsewhere mid-sentence typing stops and reports
  how far it got.
- **It cannot** — characters are posted to the window as `WM_CHAR`, taking no
  focus at all. wbox briefly attaches its input queue to the app's thread and
  marks the window active; without that, toolkits such as Tk silently drop
  every posted character.

Both handle accented text, emoji and other characters outside the Basic
Multilingual Plane, and send `\n` and `\t` as Enter and Tab.

Typing is therefore slow on purpose — about 0.6 s for a 30-character string.

In 0.6.0 and earlier `type_text` pasted through the clipboard and destroyed any
image, file selection or rich text you had copied.

## Status

Measured on Windows 11 Pro (build 26200), French locale, **150% display
scaling**, CPython 3.12, against a Tk test app and Win11 Notepad.

| Feature | Status |
|---------|--------|
| `launch`, `stop`, `kill` | ✅ |
| `screenshot`, including a window hidden behind others | ✅ does not take focus |
| `screenshot` with a dialog open | ✅ dialog composited over the window |
| `click`, window in front | ✅ exact to the pixel |
| `click` / `mouse_move` / key combo, window not in front | ✅ refused, names the blocking window |
| `type_text` | ✅ both routes, accents, emoji, newlines — clipboard untouched |
| `key` without modifiers | ✅ without taking focus |
| `keys`, `clipboard_read`, `clipboard_write`, `get_size` | ✅ |
| `screenshot` of a minimized window | ⚠️ returns a blank image, not an error |
| `resize` | ✅ the client area exactly the size asked, at 150 % |
| `screenshot(scale=…)`, `screenshot(region=…)` | ❌ returns an error |
| `list_windows` (with `activated`, `maximized`, `minimized`), `focus_window` | ✅ |
| `headless` | ✅ with `sandbox:` (the sandbox's window minimized), ❌ ignored without |
| `screen` | ❌ ignored |
| Isolation from your desktop | ✅ with `sandbox:`, ❌ none without |
| Clicks on DPI-unaware and system-aware apps | ✅ both routes, at 150 % and 100 % |
| `scroll` (both ways), `drag` (stepped), `dblclick`, `hold` (released even when an action fails) | ✅ |

Rows not listed here have not been checked on Windows.

`tests/test_windows.py` and `tests/test_windows_density.py` check the
backend end to end against two test apps: in Windows Sandbox when it is on,
and on the desktop when `WBOX_TEST_DESKTOP=1` (it moves your pointer). CI
runs the desktop half on a Windows runner; the sandbox half runs where
Windows Sandbox does — a runner has none.

## Limitations

**No isolation without `sandbox:`.** Covered at the top; it is the one that
matters most.

**Display scaling makes screenshots bigger, not sharper.** Most apps are not
DPI-aware, so at 150% scaling Windows renders them at their own size and
stretches the result. An 800×600 app captures as 1224×959: 2.25× the pixels —
and the tokens — for a blurrier image. Clicks stay accurate, because wbox reads
and clicks in the same physical-pixel space; only the cost goes up.

**Minimized windows capture blank.** `PrintWindow` asks the window to draw
itself, and a minimized window draws nothing. You get a small, valid, empty
image and no error — measured at 356×59 with six distinct colours. Windows
that are merely covered by others capture fine.

**Some GPU-composited surfaces capture black.** `PW_RENDERFULLCONTENT` is tried
first for this reason, and fixes most of them, not all.

**Elevated (administrator) apps mostly cannot be driven.** User Interface
Privilege Isolation stops a normal process from driving an elevated one.
Posted messages to it *report success and are discarded*, so a click can
return `{"ok": true}` having done nothing; `SendInput` mouse events are dropped;
keyboard input may still get through. Run wbox elevated too if you must target
an elevated app. (Derived from documented Windows behaviour; not measured.)

## Troubleshooting

**`refused to click: … would have landed in another window`** — the app window
could not be brought to the front. Something else is in the way; the error's
`would_have_hit` says what. This is wbox declining to click your window, not a
failure to click the app's.

**`no window found for pid=…`** — check `title_hint` against the *localized*
window title, and raise `timeouts.window_discovery` for slow starters.

**Win11 Notepad changes what you typed.** Its own autocorrect rewrites words
as they are committed: `héllo ` becomes `hello `. That is the app, not wbox —
a person typing gets the same result.

**Win11 Notepad starts with old text.** It restores the previous session's
tabs. Clear the document before asserting on its contents.

**Every clipboard call fails with access denied.** A process that was killed
while holding the clipboard can leave it locked for the whole session, and
Windows does not always say who holds it. Signing out clears it; restarting
the *Clipboard User Service* (`cbdhsvc_*`) usually does too.

## Another way to isolate: a separate desktop

Windows Sandbox is what `sandbox:` uses. This section records what an
earlier prototype established about a lighter way, not implemented, so that
a next step starts from measurements rather than assumptions.

The closest thing Windows has to a nested compositor is a **separate desktop
object** (`CreateDesktop`). An app started on one runs normally but is
invisible from yours, with its own window list, its own active window and its
own cursor. Measured:

| | On a separate desktop |
|---|---|
| Window absent from your desktop's window list | ✅ |
| `PrintWindow` capture | ✅ identical to a normal window |
| Posted keystrokes and `WM_CHAR` typing | ✅ |
| Posted mouse clicks | ✅ same as on a normal desktop, same toolkit caveats |
| Your cursor and focus untouched | ✅ |
| `SendInput` | ❌ `ERROR_ACCESS_DENIED` — only works on the desktop on screen |
| Clipboard | ⚠️ still shared: it belongs to the window station, not the desktop |

So a Windows `headless` mode is feasible, at the cost of `SendInput`: apps that
ignore posted mouse messages would not be clickable in it. UI Automation is
the likely answer to that, and has not been tried.
