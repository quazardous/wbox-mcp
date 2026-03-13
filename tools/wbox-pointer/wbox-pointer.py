#!/usr/bin/env python3
"""
wbox-pointer — Pure Python Wayland virtual pointer.

Injects absolute mouse motion and button events via
zwlr_virtual_pointer_manager_v1. No C dependencies — just Python 3 + stdlib.

Usage:
    wbox-pointer.py move <x> <y>
    wbox-pointer.py click <x> <y> [button]

Environment:
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

# wl_pointer_button_state
RELEASED = 0
PRESSED = 1

# wl_output mode flags
WL_OUTPUT_MODE_CURRENT = 0x1

# Protocol interface names
VPTR_MGR = "zwlr_virtual_pointer_manager_v1"


class WaylandClient:
    """Minimal Wayland wire-protocol client (pure Python)."""

    def __init__(self):
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
        # output size
        self.screen_w = 0
        self.screen_h = 0

    # ── Connection ──

    def connect(self):
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        path = display if "/" in display else os.path.join(runtime, display)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    # ── Wire protocol ──

    def _alloc(self):
        oid = self.next_id
        self.next_id += 1
        return oid

    def _send(self, obj_id, opcode, payload=b""):
        size = 8 + len(payload)
        hdr = struct.pack("=II", obj_id, (size << 16) | (opcode & 0xFFFF))
        self.sock.sendall(hdr + payload)

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
            if flags & WL_OUTPUT_MODE_CURRENT:
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

    def setup(self):
        """Connect, discover globals, bind needed interfaces, get screen size."""
        self.connect()
        self.get_registry()

        # Bind globals
        self._seat_id = self.bind("wl_seat", 1)
        self._output_id = self.bind("wl_output", 4)
        self._vptr_mgr_id = self.bind(VPTR_MGR, 2)

        if not self._vptr_mgr_id:
            raise RuntimeError("compositor does not support wlr-virtual-pointer")

        # Roundtrip to receive wl_output.mode events
        if self._output_id:
            self.roundtrip()

    def move(self, x, y):
        """Create virtual pointer, send absolute motion, destroy."""
        vp = self._alloc()
        # zwlr_virtual_pointer_manager_v1.create_virtual_pointer (opcode 0)
        #   args: seat(object), id(new_id)
        self._send(self._vptr_mgr_id, 0,
                   self._uint(self._seat_id) + self._uint(vp))

        t = _now_ms()

        # zwlr_virtual_pointer_v1.motion_absolute (opcode 1)
        #   args: time(u), x(u), y(u), x_extent(u), y_extent(u)
        self._send(vp, 1,
                   self._uint(t) +
                   self._uint(x) + self._uint(y) +
                   self._uint(self.screen_w) + self._uint(self.screen_h))

        # frame (opcode 4)
        self._send(vp, 4)

        self.roundtrip()

        # destroy (opcode 5)
        self._send(vp, 5)
        return vp

    def click(self, x, y, button=1):
        """Move + click."""
        btn_map = {1: BTN_LEFT, 2: BTN_MIDDLE, 3: BTN_RIGHT}
        btn = btn_map.get(button, BTN_LEFT)

        vp = self._alloc()
        self._send(self._vptr_mgr_id, 0,
                   self._uint(self._seat_id) + self._uint(vp))

        t = _now_ms()

        # motion_absolute
        self._send(vp, 1,
                   self._uint(t) +
                   self._uint(x) + self._uint(y) +
                   self._uint(self.screen_w) + self._uint(self.screen_h))
        self._send(vp, 4)  # frame

        # button press
        t = _now_ms()
        self._send(vp, 2,
                   self._uint(t) + self._uint(btn) + self._uint(PRESSED))
        self._send(vp, 4)  # frame

        self.roundtrip()
        time.sleep(0.02)

        # button release
        t = _now_ms()
        self._send(vp, 2,
                   self._uint(t) + self._uint(btn) + self._uint(RELEASED))
        self._send(vp, 4)  # frame

        self.roundtrip()

        self._send(vp, 5)  # destroy


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

    wl = WaylandClient()

    # Override screen size from env
    screen_env = os.environ.get("WBOX_SCREEN")
    if screen_env and "x" in screen_env:
        w, h = screen_env.split("x")
        wl.screen_w, wl.screen_h = int(w), int(h)

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
