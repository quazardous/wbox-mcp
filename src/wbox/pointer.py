"""
pointer.py — Pure Python Wayland virtual input (pointer + keyboard).

Injects absolute mouse motion/button events via
zwlr_virtual_pointer_manager_v1 and keyboard events via
zwp_virtual_keyboard_manager_v1. No C dependencies — just Python 3 + stdlib.

The keyboard path uploads a US-layout keymap whose keycodes sit at the
standard evdev positions. Compositors that honor per-device keymaps (sway)
interpret our events through it; compositors that force the seat keymap onto
every keyboard (labwc/cage via wlr_keyboard_group) still decode them
correctly as long as the seat layout is "us" — which the wbox compositor
backends pin via keyboard_layout.

Used as a library by the compositor backends (persistent connection), and as
a CLI via tools/wbox-pointer/wbox-pointer.py:

    wbox-pointer.py move <x> <y>
    wbox-pointer.py click <x> <y> [button]

Environment (CLI):
    WAYLAND_DISPLAY   Target compositor (required)
    WBOX_SCREEN       Display size WxH (overrides auto-detect)
"""

import os
import socket
import struct
import sys
import time

# Linux input button codes
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

# wl_pointer_button_state / wl_keyboard_key_state
RELEASED = 0
PRESSED = 1

# wl_output mode flags
WL_OUTPUT_MODE_CURRENT = 0x1

# Protocol interface names
VPTR_MGR = "zwlr_virtual_pointer_manager_v1"
VKBD_MGR = "zwp_virtual_keyboard_manager_v1"

# XKB real-modifier masks (fixed indices in every keymap)
MOD_SHIFT = 1 << 0
MOD_CTRL = 1 << 2
MOD_ALT = 1 << 3
MOD_SUPER = 1 << 6

# ── US keymap tables ─────────────────────────────────────────────────
# (evdev_code, plain_keysym, shifted_keysym) — keysyms at the standard US
# positions. Characters are derived from the keysym names below.

_US_KEYS = [
    (1, "Escape", None),
    (2, "1", "exclam"), (3, "2", "at"), (4, "3", "numbersign"),
    (5, "4", "dollar"), (6, "5", "percent"), (7, "6", "asciicircum"),
    (8, "7", "ampersand"), (9, "8", "asterisk"), (10, "9", "parenleft"),
    (11, "0", "parenright"), (12, "minus", "underscore"),
    (13, "equal", "plus"), (14, "BackSpace", None), (15, "Tab", None),
    (16, "q", "Q"), (17, "w", "W"), (18, "e", "E"), (19, "r", "R"),
    (20, "t", "T"), (21, "y", "Y"), (22, "u", "U"), (23, "i", "I"),
    (24, "o", "O"), (25, "p", "P"), (26, "bracketleft", "braceleft"),
    (27, "bracketright", "braceright"), (28, "Return", None),
    (29, "Control_L", None),
    (30, "a", "A"), (31, "s", "S"), (32, "d", "D"), (33, "f", "F"),
    (34, "g", "G"), (35, "h", "H"), (36, "j", "J"), (37, "k", "K"),
    (38, "l", "L"), (39, "semicolon", "colon"),
    (40, "apostrophe", "quotedbl"), (41, "grave", "asciitilde"),
    (42, "Shift_L", None), (43, "backslash", "bar"),
    (44, "z", "Z"), (45, "x", "X"), (46, "c", "C"), (47, "v", "V"),
    (48, "b", "B"), (49, "n", "N"), (50, "m", "M"),
    (51, "comma", "less"), (52, "period", "greater"),
    (53, "slash", "question"), (56, "Alt_L", None), (57, "space", None),
    (59, "F1", None), (60, "F2", None), (61, "F3", None), (62, "F4", None),
    (63, "F5", None), (64, "F6", None), (65, "F7", None), (66, "F8", None),
    (67, "F9", None), (68, "F10", None), (87, "F11", None), (88, "F12", None),
    (102, "Home", None), (103, "Up", None), (104, "Prior", None),
    (105, "Left", None), (106, "Right", None), (107, "End", None),
    (108, "Down", None), (109, "Next", None), (110, "Insert", None),
    (111, "Delete", None), (125, "Super_L", None),
]

# keysym name → printable character (for keysyms whose name isn't the char)
_KEYSYM_CHARS = {
    "exclam": "!", "at": "@", "numbersign": "#", "dollar": "$",
    "percent": "%", "asciicircum": "^", "ampersand": "&", "asterisk": "*",
    "parenleft": "(", "parenright": ")", "minus": "-", "underscore": "_",
    "equal": "=", "plus": "+", "bracketleft": "[", "braceleft": "{",
    "bracketright": "]", "braceright": "}", "semicolon": ";", "colon": ":",
    "apostrophe": "'", "quotedbl": '"', "grave": "`", "asciitilde": "~",
    "backslash": "\\", "bar": "|", "comma": ",", "less": "<",
    "period": ".", "greater": ">", "slash": "/", "question": "?",
    "space": " ", "Return": "\n", "Tab": "\t",
}


def _keysym_char(sym: str) -> str | None:
    if sym in _KEYSYM_CHARS:
        return _KEYSYM_CHARS[sym]
    if len(sym) == 1:  # letters and digits
        return sym
    return None


# char → (evdev_code, needs_shift)
CHAR_MAP: dict[str, tuple[int, bool]] = {}
# keysym name → evdev_code (unshifted position), for shortcut parsing
KEYSYM_CODES: dict[str, int] = {}
for _code, _plain, _shifted in _US_KEYS:
    KEYSYM_CODES[_plain] = _code
    _ch = _keysym_char(_plain)
    if _ch is not None and _ch not in CHAR_MAP:
        CHAR_MAP[_ch] = (_code, False)
    if _shifted:
        _ch = _keysym_char(_shifted)
        if _ch is not None and _ch not in CHAR_MAP:
            CHAR_MAP[_ch] = (_code, True)

# xdotool-style aliases → canonical keysym
_KEY_ALIASES = {
    "enter": "Return", "esc": "Escape", "backspace": "BackSpace",
    "del": "Delete", "ins": "Insert", "page_up": "Prior", "pageup": "Prior",
    "page_down": "Next", "pagedown": "Next", "tab": "Tab", "home": "Home",
    "end": "End", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right", "return": "Return", "escape": "Escape",
    "delete": "Delete", "insert": "Insert",
}

# modifier name → (mask, evdev_code)
_MODIFIERS = {
    "shift": (MOD_SHIFT, 42), "ctrl": (MOD_CTRL, 29),
    "control": (MOD_CTRL, 29), "alt": (MOD_ALT, 56),
    "super": (MOD_SUPER, 125), "meta": (MOD_SUPER, 125),
    "logo": (MOD_SUPER, 125), "win": (MOD_SUPER, 125),
    "cmd": (MOD_SUPER, 125),
}


def keymap_text() -> str:
    """Generate the US xkb keymap uploaded with the virtual keyboard."""
    keycodes = []
    symbols = []
    for code, plain, shifted in _US_KEYS:
        name = f"K{code}"
        keycodes.append(f"        <{name}> = {code + 8};")
        if shifted:
            symbols.append(
                f'        key <{name}> {{ type= "TWO_LEVEL", '
                f"[ {plain}, {shifted} ] }};"
            )
        else:
            symbols.append(
                f'        key <{name}> {{ type= "ONE_LEVEL", [ {plain} ] }};'
            )
    return (
        "xkb_keymap {\n"
        '    xkb_keycodes "wbox" {\n'
        "        minimum = 8;\n"
        "        maximum = 255;\n"
        + "\n".join(keycodes) + "\n"
        "    };\n"
        '    xkb_types "wbox" {\n'
        '        type "ONE_LEVEL" {\n'
        "            modifiers = none;\n"
        '            level_name[Level1] = "Any";\n'
        "        };\n"
        '        type "TWO_LEVEL" {\n'
        "            modifiers = Shift;\n"
        "            map[Shift] = Level2;\n"
        '            level_name[Level1] = "Base";\n'
        '            level_name[Level2] = "Shift";\n'
        "        };\n"
        "    };\n"
        '    xkb_compatibility "wbox" {\n'
        "    };\n"
        '    xkb_symbols "wbox" {\n'
        + "\n".join(symbols) + "\n"
        "        modifier_map Shift { <K42> };\n"
        "        modifier_map Control { <K29> };\n"
        "        modifier_map Mod1 { <K56> };\n"
        "        modifier_map Mod4 { <K125> };\n"
        "    };\n"
        "};\n"
    )


def _parse_shortcut(shortcut: str) -> tuple[int, int, list[int]]:
    """Parse "ctrl+shift+a" → (key_code, mod_mask, mod_key_codes)."""
    parts = shortcut.split("+")
    key = parts[-1]
    mask = 0
    mod_codes = []
    for m in parts[:-1]:
        try:
            mmask, mcode = _MODIFIERS[m.lower()]
        except KeyError:
            raise ValueError(f"unknown modifier {m!r} in {shortcut!r}")
        mask |= mmask
        mod_codes.append(mcode)

    sym = _KEY_ALIASES.get(key.lower(), key)
    if sym in KEYSYM_CODES:
        return KEYSYM_CODES[sym], mask, mod_codes
    if key in CHAR_MAP:
        code, shifted = CHAR_MAP[key]
        if shifted:
            mask |= MOD_SHIFT
            mod_codes.append(_MODIFIERS["shift"][1])
        return code, mask, mod_codes
    raise ValueError(f"unknown key {key!r} in {shortcut!r}")


class WaylandClient:
    """Minimal Wayland wire-protocol client (pure Python)."""

    def __init__(self, forced_size: tuple[int, int] | None = None,
                 fallback_size: tuple[int, int] | None = None):
        self.sock = None
        self.next_id = 2  # 1 = wl_display
        self.recv_buf = b""
        # discovered globals: {name_uint: (interface_str, version_uint)}
        self.globals = {}
        # bound object ids
        self._registry_id = 0
        self._seat_id = 0
        self._output_id = 0
        self._vptr_mgr_id = 0
        self._vptr_id = 0
        self._vkbd_mgr_id = 0
        self._vkbd_id = 0
        # Output size for motion_absolute extents. The real output size from
        # wl_output.mode is authoritative (compositors may ignore the
        # configured size — cage opens at 1280x720 regardless), and mode
        # events on later roundtrips track resizes. forced_size wins over
        # detection (explicit override); fallback_size only seeds the value
        # until a mode event arrives.
        self._size_forced = forced_size is not None
        self.screen_w, self.screen_h = forced_size or fallback_size or (0, 0)

    # ── Connection ──

    def connect(self, display: str | None = None):
        display = display or os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        path = display if "/" in display else os.path.join(runtime, display)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None
        self._vptr_id = 0
        self._vkbd_id = 0

    # ── Wire protocol ──

    def _alloc(self):
        oid = self.next_id
        self.next_id += 1
        return oid

    def _send(self, obj_id, opcode, payload=b""):
        size = 8 + len(payload)
        hdr = struct.pack("=II", obj_id, (size << 16) | (opcode & 0xFFFF))
        self.sock.sendall(hdr + payload)

    def _send_fd(self, obj_id, opcode, payload, fd):
        """Send a request carrying a file descriptor (SCM_RIGHTS)."""
        size = 8 + len(payload)
        hdr = struct.pack("=II", obj_id, (size << 16) | (opcode & 0xFFFF))
        self.sock.sendmsg(
            [hdr + payload],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))],
        )

    def _recv_exact(self, n):
        while len(self.recv_buf) < n:
            data = self.sock.recv(4096)
            if not data:
                raise EOFError("connection closed")
            self.recv_buf += data
        out, self.recv_buf = self.recv_buf[:n], self.recv_buf[n:]
        return out

    def _read_event(self):
        hdr = self._recv_exact(8)
        obj_id, so = struct.unpack("=II", hdr)
        size, opcode = so >> 16, so & 0xFFFF
        payload = self._recv_exact(size - 8) if size > 8 else b""
        return obj_id, opcode, payload

    # ── Packing helpers ──

    @staticmethod
    def _uint(v):
        return struct.pack("=I", v & 0xFFFFFFFF)

    @staticmethod
    def _string(s):
        b = s.encode() + b"\x00"
        pad = (4 - len(b) % 4) % 4
        return struct.pack("=I", len(b)) + b + b"\x00" * pad

    def _new_id_untyped(self, interface, version, oid):
        """Encode new_id for wl_registry.bind (interface+version+id)."""
        return self._string(interface) + self._uint(version) + self._uint(oid)

    # ── Unpacking helpers ──

    @staticmethod
    def _get_uint(data, off):
        return struct.unpack_from("=I", data, off)[0], off + 4

    @staticmethod
    def _get_int(data, off):
        return struct.unpack_from("=i", data, off)[0], off + 4

    @staticmethod
    def _get_string(data, off):
        length = struct.unpack_from("=I", data, off)[0]
        off += 4
        s = data[off:off + length - 1].decode()
        pad = (4 - length % 4) % 4
        return s, off + length + pad

    # ── Protocol operations ──

    def roundtrip(self):
        """wl_display.sync + drain until callback.done."""
        cb = self._alloc()
        self._send(1, 0, self._uint(cb))  # wl_display.sync → new callback
        while True:
            oid, op, payload = self._read_event()
            self._dispatch(oid, op, payload)
            if oid == cb and op == 0:  # wl_callback.done
                return

    def _dispatch(self, oid, op, payload):
        # wl_display.error (opcode 0) — surface protocol errors instead of
        # silently looping on a poisoned connection
        if oid == 1 and op == 0:
            off = 0
            obj, off = self._get_uint(payload, off)
            code, off = self._get_uint(payload, off)
            msg, off = self._get_string(payload, off)
            raise RuntimeError(f"wayland protocol error on object {obj}: {msg}")

        # wl_registry.global (opcode 0)
        if oid == self._registry_id and op == 0:
            off = 0
            name, off = self._get_uint(payload, off)
            iface, off = self._get_string(payload, off)
            ver, off = self._get_uint(payload, off)
            self.globals[name] = (iface, ver)

        # wl_output.mode (opcode 1)
        elif oid == self._output_id and op == 1:
            off = 0
            flags, off = self._get_uint(payload, off)
            w, off = self._get_int(payload, off)
            h, off = self._get_int(payload, off)
            if flags & WL_OUTPUT_MODE_CURRENT and not self._size_forced:
                self.screen_w = w
                self.screen_h = h

    def get_registry(self):
        self._registry_id = self._alloc()
        self._send(1, 1, self._uint(self._registry_id))  # wl_display.get_registry
        self.roundtrip()

    def bind(self, interface, max_ver=1):
        """Bind to a global interface. Returns object id or 0."""
        for name, (iface, ver) in self.globals.items():
            if iface == interface:
                oid = self._alloc()
                v = min(ver, max_ver)
                # wl_registry.bind (opcode 0)
                self._send(self._registry_id, 0,
                           self._uint(name) + self._new_id_untyped(interface, v, oid))
                return oid
        return 0

    def setup(self, display: str | None = None):
        """Connect, discover globals, bind needed interfaces, get screen size."""
        self.connect(display)
        self.get_registry()

        # Bind globals
        self._seat_id = self.bind("wl_seat", 1)
        self._output_id = self.bind("wl_output", 4)
        self._vptr_mgr_id = self.bind(VPTR_MGR, 2)
        self._vkbd_mgr_id = self.bind(VKBD_MGR, 1)

        if not (self._vptr_mgr_id or self._vkbd_mgr_id):
            raise RuntimeError(
                "compositor supports neither wlr-virtual-pointer "
                "nor virtual-keyboard"
            )

        # Roundtrip to receive wl_output.mode events
        if self._output_id:
            self.roundtrip()

    def _ensure_vptr(self):
        """Create the virtual pointer (once) and return its object id.

        The pointer is kept alive for the lifetime of the connection: a
        freshly created virtual pointer is a brand-new input device, and the
        compositor drops events that arrive before it finishes setting it up
        — creating one per click loses clicks at random.
        """
        if self._vptr_id:
            return self._vptr_id
        if not self._vptr_mgr_id:
            raise RuntimeError("compositor does not support wlr-virtual-pointer")
        self._vptr_id = self._alloc()
        # zwlr_virtual_pointer_manager_v1.create_virtual_pointer (opcode 0)
        #   args: seat(object), id(new_id)
        self._send(self._vptr_mgr_id, 0,
                   self._uint(self._seat_id) + self._uint(self._vptr_id))
        self.roundtrip()
        return self._vptr_id

    def _motion(self, vp, x, y):
        # zwlr_virtual_pointer_v1.motion_absolute (opcode 1)
        #   args: time(u), x(u), y(u), x_extent(u), y_extent(u)
        self._send(vp, 1,
                   self._uint(_now_ms()) +
                   self._uint(x) + self._uint(y) +
                   self._uint(self.screen_w) + self._uint(self.screen_h))
        self._send(vp, 4)  # frame

    def move(self, x, y):
        """Send absolute pointer motion."""
        vp = self._ensure_vptr()
        self._motion(vp, x, y)
        self.roundtrip()
        return vp

    def click(self, x, y, button=1):
        """Move + click."""
        btn_map = {1: BTN_LEFT, 2: BTN_MIDDLE, 3: BTN_RIGHT}
        btn = btn_map.get(button, BTN_LEFT)

        vp = self._ensure_vptr()
        self._motion(vp, x, y)

        # button press (opcode 2): time(u), button(u), state(u)
        self._send(vp, 2,
                   self._uint(_now_ms()) + self._uint(btn) + self._uint(PRESSED))
        self._send(vp, 4)  # frame

        self.roundtrip()
        time.sleep(0.02)

        # button release
        self._send(vp, 2,
                   self._uint(_now_ms()) + self._uint(btn) + self._uint(RELEASED))
        self._send(vp, 4)  # frame

        self.roundtrip()

    # ── Virtual keyboard ──

    def _ensure_vkbd(self):
        """Create the virtual keyboard and upload the US keymap (once)."""
        if self._vkbd_id:
            return
        if not self._vkbd_mgr_id:
            raise RuntimeError(
                "compositor does not support virtual-keyboard"
            )
        self._vkbd_id = self._alloc()
        # zwp_virtual_keyboard_manager_v1.create_virtual_keyboard (opcode 0)
        #   args: seat(object), id(new_id)
        self._send(self._vkbd_mgr_id, 0,
                   self._uint(self._seat_id) + self._uint(self._vkbd_id))

        km = keymap_text().encode() + b"\x00"
        # memfd is ideal but missing from some python builds — an unlinked
        # temp file is an equally valid fd to pass over the socket
        try:
            fd = os.memfd_create("wbox-keymap")
        except (AttributeError, OSError):
            import tempfile
            tmp = tempfile.TemporaryFile()
            fd = os.dup(tmp.fileno())
            tmp.close()
        try:
            os.write(fd, km)
            # zwp_virtual_keyboard_v1.keymap (opcode 0)
            #   args: format(u)=1 xkb_v1, fd(fd), size(u)
            self._send_fd(self._vkbd_id, 0,
                          self._uint(1) + self._uint(len(km)), fd)
        finally:
            os.close(fd)
        self.roundtrip()

    def warm_up(self):
        """Create the virtual devices ahead of the first real event.

        A freshly created virtual device swallows whatever is sent right
        after it — the compositor is still propagating the device (and, for
        the keyboard, its keymap) to clients. Creating them up front keeps
        the first genuine click or keystroke from being lost.
        """
        if self._vptr_mgr_id:
            self._ensure_vptr()
        if self._vkbd_mgr_id:
            self._ensure_vkbd()
        self.roundtrip()

    def _kbd_key(self, code, state):
        # zwp_virtual_keyboard_v1.key (opcode 1): time(u), key(u), state(u)
        self._send(self._vkbd_id, 1,
                   self._uint(_now_ms()) + self._uint(code) + self._uint(state))

    def _kbd_mods(self, mask):
        # zwp_virtual_keyboard_v1.modifiers (opcode 2):
        #   depressed(u), latched(u), locked(u), group(u)
        self._send(self._vkbd_id, 2,
                   self._uint(mask) + self._uint(0) +
                   self._uint(0) + self._uint(0))

    def type_text(self, text, delay_ms=12):
        """Type text through the virtual keyboard (US layout keycodes)."""
        unsupported = sorted({ch for ch in text if ch not in CHAR_MAP})
        if unsupported:
            raise ValueError(f"unsupported characters: {unsupported!r}")
        self._ensure_vkbd()
        delay = max(delay_ms, 0) / 1000.0
        shift_code = _MODIFIERS["shift"][1]
        for i, ch in enumerate(text):
            code, shifted = CHAR_MAP[ch]
            if i and delay:
                time.sleep(delay)
            if shifted:
                self._kbd_key(shift_code, PRESSED)
                self._kbd_mods(MOD_SHIFT)
            self._kbd_key(code, PRESSED)
            self._kbd_key(code, RELEASED)
            if shifted:
                self._kbd_mods(0)
                self._kbd_key(shift_code, RELEASED)
        self.roundtrip()

    def _combo(self, shortcut):
        code, mask, mod_codes = _parse_shortcut(shortcut)
        for mc in mod_codes:
            self._kbd_key(mc, PRESSED)
        if mask:
            self._kbd_mods(mask)
        self._kbd_key(code, PRESSED)
        self._kbd_key(code, RELEASED)
        if mask:
            self._kbd_mods(0)
        for mc in reversed(mod_codes):
            self._kbd_key(mc, RELEASED)

    def key_combo(self, shortcut):
        """Send one xdotool-style shortcut ("ctrl+shift+a")."""
        _parse_shortcut(shortcut)  # validate before touching the wire
        self._ensure_vkbd()
        self._combo(shortcut)
        self.roundtrip()

    def key_combos(self, shortcuts, delay_ms=100):
        """Send a sequence of shortcuts with a pause between them."""
        for s in shortcuts:
            _parse_shortcut(s)  # validate all before typing any
        self._ensure_vkbd()
        delay = max(delay_ms, 0) / 1000.0
        for i, s in enumerate(shortcuts):
            if i and delay:
                time.sleep(delay)
            self._combo(s)
        self.roundtrip()


def _now_ms():
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def main():
    if len(sys.argv) < 4:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    x = int(sys.argv[2])
    y = int(sys.argv[3])
    button = int(sys.argv[4]) if len(sys.argv) >= 5 else 1

    forced_size = None
    screen_env = os.environ.get("WBOX_SCREEN")
    if screen_env and "x" in screen_env:
        w, h = screen_env.split("x")
        forced_size = (int(w), int(h))

    wl = WaylandClient(forced_size=forced_size)

    try:
        wl.setup()

        if wl.screen_w <= 0 or wl.screen_h <= 0:
            print("Could not detect screen size (set WBOX_SCREEN=WxH)",
                  file=sys.stderr)
            sys.exit(1)

        if action == "move":
            wl.move(x, y)
        elif action == "click":
            wl.click(x, y, button)
        else:
            print(f"Unknown action: {action}", file=sys.stderr)
            sys.exit(1)
    finally:
        wl.disconnect()


if __name__ == "__main__":
    main()
