# wbox-mcp

**Run any desktop app in an isolated box that Claude can see and control — without touching your desktop.**

Most computer-use servers automate *your* screen: they move your real mouse, steal your focus, and see whatever you have open. wbox gives the app its own nested compositor instead — on Windows, a Windows Sandbox of its own. Claude clicks and types inside that box, screenshots it pixel-perfect, and your desktop never knows it happened. You can keep working while it does.

![LibreOffice Writer running inside wbox, headless](docs/images/writer-headless.png)

*LibreOffice Writer, launched and typed into by Claude through wbox — captured with `headless: true`, so this window never rendered on anyone's desktop.*

## Why not just use a desktop-takeover server?

| | wbox | Typical computer-use / desktop MCP |
|---|---|---|
| **Isolated from your desktop** | Yes — nested compositor, own seat | No — shares your screen and input |
| **Runs in the background** | Yes — keep working while it drives | No — it owns your mouse and focus |
| **Offscreen / headless** | Yes — `headless: true`, nothing renders | Rarely |
| **Can the app see your screen?** | No | Yes — everything you have open |
| **Can it read your clipboard?** | Only if you let it (`clipboard_bridge`) | Yes |
| **Safe for CI** | Yes | No |

That column is wbox on Linux, and on Windows with `sandbox:` (Windows Sandbox). On Windows without it the app runs on your desktop, with none of this — see [Platform features](#platform-features).

That combination is the whole point. If you only need to automate the machine you're staring at, a simpler server will do.

## Install

```bash
# Linux
curl -sSL https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.sh | bash

# Windows
irm https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.ps1 | iex
```

On Windows, read [docs/windows.md](docs/windows.md) first — isolation there comes from Windows Sandbox (`sandbox:` in config.yaml; Pro, Enterprise or Education), which the installer turns on, asking for admin rights; without it the app runs on your desktop. The installer sets up Python too if the machine has none.

**What that script does**, so you can decide before running it: it installs the missing system packages through your distro's package manager (`dnf`, `apt` or `pacman`, asking first), clones this repo into `~/.local/share/wbox-mcp`, installs it into a venv there, and symlinks the two commands `wboxr` and `wbox-mcp` into `~/.local/bin`. It touches nothing else and needs `sudo` only for the packages.

**Manual install**, if you'd rather not pipe a script into a shell:

```bash
# 1. System packages — required
sudo apt install labwc grim xdotool x11-xkb-utils wlr-randr wlrctl
#    ...and optional, for extra compositors and input backends
sudo apt install weston cage xclip wl-clipboard wtype

# 2. wbox itself
git clone https://github.com/quazardous/wbox-mcp.git ~/.local/share/wbox-mcp
cd ~/.local/share/wbox-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
ln -s ~/.local/share/wbox-mcp/.venv/bin/{wboxr,wbox-mcp} ~/.local/bin/
```

Package names differ per distro — `setxkbmap` is `x11-xkb-utils` on Debian/Ubuntu, `setxkbmap` on Fedora, `xorg-setxkbmap` on Arch. The setup script knows the mapping for all three.

## Quick start

```bash
cd my-project/
wboxr init --register
```

That creates `wbox/config.yaml` and registers the server in `.mcp.json`. The wizard adapts to your platform.

```bash
# Non-interactive
wboxr init --name writer --app-command "soffice --writer" --register
```

## End to end

`wboxr init --register` writes this into your `.mcp.json`:

```json
{
  "mcpServers": {
    "writer": {
      "type": "stdio",
      "command": "/home/you/.local/bin/wbox-mcp",
      "args": ["serve", "--mcp-dir", "/home/you/my-project/wbox"]
    }
  }
}
```

And `wbox/config.yaml` looks like this — the whole minimal form:

```yaml
name: writer
compositor: labwc          # labwc | weston | cage | win32
screen: "1280x800"
input_backend: hybrid      # hybrid | x11 | wayland
headless: false            # true = runs offscreen, nothing on your desktop
keyboard_layout: us        # pin this on a non-US host, or typing comes out wrong
app:
  command: soffice --writer
```

Then just ask:

> Open Writer, type "Hello World", and show me a screenshot.

Claude calls `launch`, `type_text` and `screenshot` on its own. The window never appears on your desktop if `headless: true`, and never steals your focus either way.

Two settings are worth knowing before you automate a real app:

- **`keyboard_layout`** — input injection assumes US keycode positions. On an AZERTY host without this, `type_text("abc123")` can arrive as `qbc!@#`.
- **`post_launch_keys`** — shortcuts sent once the app has rendered, e.g. `["super+a"]` to maximize it. Slow apps like LibreOffice usually want this plus a longer `timeouts.app_render`.

## Platform features

Windows has two modes, and they do not give the same thing: with `sandbox:` in config.yaml the app runs in Windows Sandbox; without it, on your desktop.

| Feature | Linux | Windows, `sandbox:` | Windows, desktop |
|---------|-------|---------------------|------------------|
| Where the app runs | Nested Wayland compositor | Windows Sandbox, a throwaway Windows | Your desktop, an ordinary process |
| App isolation | Full | Full — it gets the folders you mount, and no network unless asked | **None** |
| Interferes with host | No | No | Most clicks move your cursor and take focus |
| Background operation | Yes | Yes | Screenshots only — most input takes focus |
| Offscreen / headless | Yes (`headless: true`) | The sandbox's window minimized (`headless: true`) | No — `headless` is ignored |
| Screenshot | grim (pixel-perfect) | PrintWindow | PrintWindow — works behind other windows |
| Keyboard | wbox-keyboard (virtual keyboard) | SendInput (Unicode) / PostMessage, the sandbox's keyboard | SendInput (Unicode) / PostMessage, yours |
| Mouse | wbox-pointer (virtual pointer) | SendInput / PostMessage, the sandbox's pointer | SendInput / PostMessage, your pointer |
| Clipboard | xclip + bridge to host (switchable) | The sandbox's own, never shared | Yours, shared with the app |
| Window management | wlrctl (list/focus) | Win32 (list/focus) | Win32 (list/focus) |
| Resize | The display (wlr-randr) | The window: its client area, to the pixel | The window: its client area, to the pixel |
| `record` | Yes (not weston) | Yes — assembled on the host | Yes |
| Start-up | Instant | About 15 s to boot; `keep: true` reuses it | Instant |
| Needs | labwc, weston or cage | Windows 10/11 Pro, Enterprise or Education; one sandbox at a time | Windows 10+ |

**Linux** — the app runs inside a nested Wayland compositor (labwc, weston or cage). Full isolation: the app cannot see or interfere with your desktop. Clipboard is bridged automatically, and you can switch that bridge off. Keyboard and mouse are injected through Wayland virtual-input protocols, so nothing leaks onto your own seat.

Set `headless: true` and the nested session runs offscreen — no window on your desktop, while screenshots, clicks and keystrokes keep working exactly the same. Handy for test suites and unattended runs.

**Windows** — the app is driven through Win32 APIs. With `sandbox:` it runs in Windows Sandbox, a throwaway Windows of its own that boots in about 15 seconds: the pointer, keyboard and clipboard wbox uses are the sandbox's, and yours are left alone. Without it, the app runs as a normal process on your desktop: it shares your desktop and your clipboard, and most clicks warp your real mouse cursor and pull the window to the front. Screenshots work even when the window is covered.

```yaml
name: my-app
compositor: win32          # auto-detected on Windows
title_hint: "My App"       # substring of the window title, as localized
sandbox:                   # leave this out to run on your desktop
  mounts:
    - {host: ./build, guest: 'C:\app', readonly: true}
app:
  command: 'C:\app\myapp.exe'
```

The sandbox is a fresh Windows: the app has to come from a folder you mount, and nothing you installed on the host is in it.

On the desktop, two things keep it from harming you. `type_text` types the characters and never touches your clipboard. And when the app window can't be brought to the front, the calls that would press a real button or key — `click`, `dblclick`, `drag`, `scroll`, `hold`, `mouse_move`, key combos — refuse and name the window that is in the way, instead of clicking it. Elevated apps remain largely off-limits. [docs/windows.md](docs/windows.md) has what was measured to work, the limitations, and troubleshooting.

## MCP tools

`launch` · `stop` · `kill` · `screenshot` · `record` · `click` · `dblclick` · `drag` · `scroll` · `hold` · `type_text` · `key` · `keys` · `mouse_move` · `get_mouse_position` · `get_size` · `resize` · `list_windows` · `focus_window` · `clipboard_read` · `clipboard_write` · `tail_log` · `clean` · `debug_input`

Plus custom script tools via `wboxr tool add`.

### Seeing what a screenshot cannot show

`record` films the display for a few seconds instead of catching one instant.
A region films several times faster than the full screen, and `during` plays a
drag, a click or a keystroke while the capture runs, so the film and the
gesture need no script to line them up.

It returns the frames and a summary — how many, the interval actually
achieved, and **which frames differ from the one before**. That last one is
what catches a flicker: a value that changes and changes back. Pass
`assemble: "gif"` (or `"mp4"`) to also get a film; the frames are kept either
way, since they are what a per-frame analysis reads.

Not available under weston, which announces no wlr-screencopy for the capture
to read.

On Windows it films the app's window, in Windows Sandbox too — measured there,
17 frames/s for an 800×600 window and 32/s for a 300×200 region — and a
sandbox's film is assembled on the host, where ffmpeg is.

### When something goes wrong

- **`stop`** — graceful shutdown: SIGTERM, then SIGKILL after `timeouts.stop`.
- **`kill`** — force-kills every compositor and app process and clears the recorded state. This is the one to reach for if a session died mid-launch and left a stale socket behind; the next `launch` also cleans stale sockets on its own.
- **`clean`** — housekeeping only: removes logs and screenshots. It does not kill anything.
- **`tail_log`** — read the app's log without leaving the conversation.

## Documentation

- [docs/usage.md](docs/usage.md) — CLI flags, config.yaml reference, MCP tools details, requirements
- [docs/backends.md](docs/backends.md) — compositor comparison, input backends
- [docs/matrix.md](docs/matrix.md) — what's verified to work on Linux, per compositor and backend
- [docs/windows.md](docs/windows.md) — the Windows backend: Windows Sandbox, install, input routing, measured status, limitations, troubleshooting

## License

MIT
