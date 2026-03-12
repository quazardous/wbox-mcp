"""
win32.py — Windows desktop backend for wbox-mcp.

Launches an app as a normal Windows process and controls it via Win32 API:
- Screenshots via PrintWindow (works in background)
- Input via PostMessage (works in background)
- Clipboard via Win32 clipboard API
- No compositor needed — the app runs on the Windows desktop.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from .base import CompositorServer

log = logging.getLogger(__name__)

# ── DPI awareness (must be set before any window operations) ─────

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Win32 constants ──────────────────────────────────────────────

WM_CLOSE = 0x0010
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEMOVE = 0x0200

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

PW_RENDERFULLCONTENT = 0x2

SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

CWP_SKIPINVISIBLE = 0x0001

# Virtual key codes
VK_MAP: dict[str, int] = {
    "backspace": 0x08, "bs": 0x08,
    "tab": 0x09,
    "return": 0x0D, "enter": 0x0D,
    "shift": 0x10,
    "control": 0x11, "ctrl": 0x11,
    "alt": 0x12, "menu": 0x12,
    "pause": 0x13,
    "capital": 0x14, "capslock": 0x14,
    "escape": 0x1B, "esc": 0x1B,
    "space": 0x20,
    "prior": 0x21, "pageup": 0x21, "page_up": 0x21,
    "next": 0x22, "pagedown": 0x22, "page_down": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "print": 0x2A, "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "super": 0x5B, "win": 0x5B, "lwin": 0x5B,
    "apps": 0x5D,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62,
    "numpad3": 0x63, "numpad4": 0x64, "numpad5": 0x65,
    "numpad6": 0x66, "numpad7": 0x67, "numpad8": 0x68,
    "numpad9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D,
    "decimal": 0x6E, "divide": 0x6F,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
    "numlock": 0x90, "scrolllock": 0x91,
}

# Add single-char keys: a-z, 0-9
for _c in range(ord("a"), ord("z") + 1):
    VK_MAP[chr(_c)] = _c - 32  # VK codes for A-Z are 0x41-0x5A
for _c in range(ord("0"), ord("9") + 1):
    VK_MAP[chr(_c)] = _c  # VK codes for 0-9 are 0x30-0x39

MODIFIER_KEYS = {"ctrl", "control", "shift", "alt", "menu", "super", "win", "lwin"}

# ── Win32 API bindings ───────────────────────────────────────────

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

PostMessageW = user32.PostMessageW
SendMessageW = user32.SendMessageW
FindWindowExW = user32.FindWindowExW
EnumWindows = user32.EnumWindows
EnumChildWindows = user32.EnumChildWindows
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetClassNameW = user32.GetClassNameW
IsWindowVisible = user32.IsWindowVisible
IsWindow = user32.IsWindow
GetClientRect = user32.GetClientRect
GetWindowRect = user32.GetWindowRect
PrintWindow = user32.PrintWindow
SetWindowPos = user32.SetWindowPos
ChildWindowFromPointEx = user32.ChildWindowFromPointEx
ClientToScreen = user32.ClientToScreen
MapWindowPoints = user32.MapWindowPoints
SetForegroundWindow = user32.SetForegroundWindow

GetDC = user32.GetDC
ReleaseDC = user32.ReleaseDC
CreateCompatibleDC = gdi32.CreateCompatibleDC
CreateCompatibleBitmap = gdi32.CreateCompatibleBitmap
SelectObject = gdi32.SelectObject
DeleteObject = gdi32.DeleteObject
DeleteDC = gdi32.DeleteDC
GetDIBits = gdi32.GetDIBits

OpenClipboard = user32.OpenClipboard
CloseClipboard = user32.CloseClipboard
EmptyClipboard = user32.EmptyClipboard
GetClipboardData = user32.GetClipboardData
SetClipboardData = user32.SetClipboardData
GetClipboardData.restype = ctypes.c_void_p

GlobalAlloc = kernel32.GlobalAlloc
GlobalAlloc.restype = ctypes.c_void_p
GlobalLock = kernel32.GlobalLock
GlobalLock.restype = ctypes.c_void_p
GlobalUnlock = kernel32.GlobalUnlock
GlobalSize = kernel32.GlobalSize


# ── BITMAPINFOHEADER ─────────────────────────────────────────────

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


# ── Helper functions ─────────────────────────────────────────────

def get_window_title(hwnd: int) -> str:
    length = GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_windows_by_pid(pid: int) -> list[int]:
    """Find all top-level visible windows belonging to a process."""
    results: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        proc_id = wt.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid and IsWindowVisible(hwnd):
            results.append(hwnd)
        return True

    EnumWindows(callback, 0)
    return results


def find_windows_by_title(title_sub: str) -> list[int]:
    """Find all top-level visible windows whose title contains the substring."""
    results: list[int] = []
    lower = title_sub.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if IsWindowVisible(hwnd):
            title = get_window_title(hwnd)
            if lower in title.lower():
                results.append(hwnd)
        return True

    EnumWindows(callback, 0)
    return results


def find_window_wait(pid: int, title_hint: str, timeout: float = 10) -> int | None:
    """Wait for a window to appear. Try PID first, fallback to title."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        windows = find_windows_by_pid(pid)
        if windows:
            return windows[0]
        if title_hint:
            windows = find_windows_by_title(title_hint)
            if windows:
                return windows[0]
        time.sleep(0.3)
    return None


def find_edit_child(hwnd: int, timeout: float = 5) -> int:
    """Find the input child control (for typing). Returns hwnd itself as fallback."""
    # Common edit control class names (ordered by specificity)
    edit_classes = [
        "RichEditD2DPT",                      # Win11 Notepad
        "Edit",                                # Classic Win32
        "RichEdit20W",                         # WordPad, etc.
        "Chrome_RenderWidgetHostHWND",         # Chromium-based apps
        "_WwG",                                # MS Word
        "EXCEL7",                              # MS Excel
    ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for cls_name in edit_classes:
            # Try via FindWindowExW chain (handles WinUI3)
            # e.g. Notepad: Notepad -> NotepadTextBox -> RichEditD2DPT
            child = _find_child_recursive_fw(hwnd, cls_name)
            if child:
                return child
        time.sleep(0.3)

    return hwnd


def _find_child_recursive_fw(parent: int, class_name: str, depth: int = 0) -> int | None:
    """Find a child window by class name using FindWindowExW, searching breadth-first."""
    if depth > 5:
        return None
    child = 0
    while True:
        child = FindWindowExW(parent, child, None, None)
        if not child:
            break
        cls = get_class_name(child)
        if cls == class_name and IsWindowVisible(child):
            return child
    # Recurse into children
    child = 0
    while True:
        child = FindWindowExW(parent, child, None, None)
        if not child:
            break
        found = _find_child_recursive_fw(child, class_name, depth + 1)
        if found:
            return found
    return None


def client_offset(hwnd: int) -> tuple[int, int]:
    """Compute pixel offset from window top-left to client area top-left."""
    rect = wt.RECT()
    GetWindowRect(hwnd, ctypes.byref(rect))
    pt = wt.POINT(0, 0)
    ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x - rect.left, pt.y - rect.top)


def child_at_point(hwnd: int, cx: int, cy: int) -> int:
    """Find the deepest child window at client coordinates (cx, cy)."""
    pt = wt.POINT(cx, cy)
    child = ChildWindowFromPointEx(hwnd, pt, CWP_SKIPINVISIBLE)
    if child and child != hwnd:
        # Recurse: convert to child coords and look deeper
        src_pt = wt.POINT(cx, cy)
        MapWindowPoints(hwnd, child, ctypes.byref(src_pt), 1)
        deeper = child_at_point(child, src_pt.x, src_pt.y)
        return deeper
    return hwnd


def map_to_child(parent: int, child: int, x: int, y: int) -> tuple[int, int]:
    """Convert coordinates from parent client space to child client space."""
    if parent == child:
        return (x, y)
    pt = wt.POINT(x, y)
    MapWindowPoints(parent, child, ctypes.byref(pt), 1)
    return (pt.x, pt.y)


# ── PNG encoding (no Pillow) ─────────────────────────────────────

def bgra_to_png(data: bytes, width: int, height: int) -> bytes:
    """Convert raw BGRA pixel buffer to PNG using only stdlib."""
    raw_rows = bytearray()
    stride = width * 4
    for y in range(height):
        raw_rows.append(0)  # filter byte: None
        offset = y * stride
        for x in range(width):
            px = offset + x * 4
            b, g, r, a = data[px], data[px + 1], data[px + 2], data[px + 3]
            raw_rows.extend((r, g, b, a))

    compressed = zlib.compress(bytes(raw_rows), 9)

    def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
        c = chunk_type + chunk_data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(chunk_data)) + c + struct.pack(">I", crc)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


# ── Screenshot capture ───────────────────────────────────────────

def capture_window_raw(hwnd: int) -> tuple[bytes, int, int] | None:
    """Capture a window via PrintWindow. Returns (bgra_bytes, width, height) or None."""
    rect = wt.RECT()
    GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = GetDC(hwnd)
    mem_dc = CreateCompatibleDC(hwnd_dc)
    bitmap = CreateCompatibleBitmap(hwnd_dc, width, height)
    old_bmp = SelectObject(mem_dc, bitmap)

    ok = PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
    if not ok:
        ok = PrintWindow(hwnd, mem_dc, 0)

    if not ok:
        SelectObject(mem_dc, old_bmp)
        DeleteObject(bitmap)
        DeleteDC(mem_dc)
        ReleaseDC(hwnd, hwnd_dc)
        return None

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = width
    bmi.biHeight = -height  # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0

    buf_size = width * height * 4
    buf = ctypes.create_string_buffer(buf_size)
    GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)

    SelectObject(mem_dc, old_bmp)
    DeleteObject(bitmap)
    DeleteDC(mem_dc)
    ReleaseDC(hwnd, hwnd_dc)

    return (buf.raw, width, height)


def capture_window(hwnd: int) -> tuple[bytes, int, int] | None:
    """Capture a window via PrintWindow. Returns (png_bytes, width, height) or None."""
    result = capture_window_raw(hwnd)
    if result is None:
        return None
    bgra, width, height = result
    return (bgra_to_png(bgra, width, height), width, height)


# ── Key combo parsing ────────────────────────────────────────────

def parse_shortcut(shortcut: str) -> tuple[list[int], int]:
    """Parse 'ctrl+shift+s' into (modifier_vks, main_vk)."""
    parts = [p.strip().lower() for p in shortcut.split("+")]
    modifiers: list[int] = []
    main_key = 0

    for i, part in enumerate(parts):
        if part in MODIFIER_KEYS and i < len(parts) - 1:
            vk = VK_MAP.get(part)
            if vk:
                modifiers.append(vk)
        else:
            # This is the main key
            vk = VK_MAP.get(part)
            if vk:
                main_key = vk
            elif len(part) == 1:
                # Single character — use VkKeyScanW
                result = user32.VkKeyScanW(ord(part))
                if result != -1:
                    main_key = result & 0xFF
                    # High byte has shift state
                    if result & 0x100:
                        modifiers.append(VK_MAP["shift"])
                else:
                    main_key = ord(part.upper())
            break

    return (modifiers, main_key)


# ── SendInput structures ─────────────────────────────────────────

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT(ctypes.Structure):
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("padding", ctypes.c_byte * 32),  # covers HARDWAREINPUT
        ]

    _fields_ = [
        ("type", wt.DWORD),
        ("_input", _INPUT_UNION),
    ]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Mouse event flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000


def _make_keyboard_input(vk: int, flags: int = 0) -> INPUT:
    scan = user32.MapVirtualKeyW(vk, 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp._input.ki.wVk = vk
    inp._input.ki.wScan = scan
    inp._input.ki.dwFlags = flags
    return inp


def _make_mouse_input(abs_x: int, abs_y: int, flags: int) -> INPUT:
    """Create a mouse INPUT at absolute screen coordinates.

    SendInput MOUSEEVENTF_ABSOLUTE uses a 0-65535 coordinate space mapped
    to the virtual screen (all monitors).
    """
    sm_x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    sm_y = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    sm_w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    sm_h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    norm_x = int((abs_x - sm_x) * 65535 / sm_w)
    norm_y = int((abs_y - sm_y) * 65535 / sm_h)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp._input.mi.dx = norm_x
    inp._input.mi.dy = norm_y
    inp._input.mi.dwFlags = flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
    return inp


# ── Win32 Compositor ─────────────────────────────────────────────

class Win32Compositor(CompositorServer):
    """Windows desktop backend — app runs as a normal process, controlled via Win32 API."""

    compositor_name = "win32"

    def __init__(
        self,
        *,
        screen: str = "1280x800",
        instance_name: str = "",
        timeouts: dict | None = None,
        title_hint: str = "",
    ):
        super().__init__(screen=screen, instance_name=instance_name, timeouts=timeouts)
        self.title_hint = title_hint
        self._hwnd: int = 0
        self._edit_hwnd: int = 0
        self._window_pid: int = 0  # real PID from GetWindowThreadProcessId

    # ── Lifecycle ────────────────────────────────────────────────

    def launch(
        self,
        app_cmd: list[str],
        app_env: dict[str, str] | None = None,
    ) -> dict:
        if self.is_running():
            return {
                "status": "already_running",
                "pid": self.state.compositor_pid,
                "hwnd": hex(self._hwnd),
            }

        if not app_cmd:
            return {"error": "no app command configured"}

        app_env = app_env or {}
        env = os.environ.copy()
        env.update(app_env)

        log.info("Launching app: %s", " ".join(app_cmd))

        self.state.compositor_proc = subprocess.Popen(
            app_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = self.state.compositor_proc.pid
        self.state.compositor_pid = pid
        self.state.app_pid = pid

        # Wait for window
        wnd_timeout = self.timeouts.get("window_discovery", 10)
        hwnd = find_window_wait(pid, self.title_hint, timeout=wnd_timeout)

        if not hwnd:
            return {
                "error": f"no window found for pid={pid} title_hint={self.title_hint!r} (timeout={wnd_timeout}s)",
                "pid": pid,
            }

        self._hwnd = hwnd
        # Store the real PID that owns the window (may differ from Popen PID
        # on Win11 apps that re-parent to a child process)
        real_pid = wt.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(real_pid))
        self._window_pid = real_pid.value
        title = get_window_title(hwnd)
        cls = get_class_name(hwnd)
        log.info("Window found: hwnd=%#x pid=%d title=%r class=%r", hwnd, self._window_pid, title, cls)

        # Find edit child for text input
        edit_timeout = self.timeouts.get("edit_control", 3)
        self._edit_hwnd = find_edit_child(hwnd, timeout=edit_timeout)
        if self._edit_hwnd != hwnd:
            log.info("Edit child: hwnd=%#x class=%r", self._edit_hwnd, get_class_name(self._edit_hwnd))

        # Wait for app to render
        render_wait = self.timeouts.get("app_render", 2)
        time.sleep(render_wait)

        # Persist state
        self.state.wayland_display = ""  # not used on Windows
        self.state.x_display = hex(self._hwnd)  # repurpose for hwnd storage
        self.state.save(self._state_file)

        return {
            "status": "running",
            "pid": pid,
            "hwnd": hex(self._hwnd),
            "window_title": title,
            "window_class": cls,
        }

    def reload_state(self) -> None:
        """Reload state from disk."""
        super().reload_state()
        # Restore hwnd from x_display field
        if self.state.x_display and self.state.x_display.startswith("0x"):
            try:
                hwnd = int(self.state.x_display, 16)
                if IsWindow(hwnd):
                    self._hwnd = hwnd
                    self._edit_hwnd = find_edit_child(hwnd, timeout=1)
            except ValueError:
                pass

    def stop(self, force: bool = False) -> dict:
        if not self.is_running():
            return {"status": "not_running"}

        pid = self.state.compositor_pid

        if force:
            return self._force_stop(pid)

        # Try graceful close via WM_CLOSE
        if self._hwnd and IsWindow(self._hwnd):
            PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            # Wait briefly for window to close
            for _ in range(20):  # 2 seconds
                if not IsWindow(self._hwnd):
                    break
                time.sleep(0.1)

            # If a "save?" dialog appeared, the window is still alive
            if IsWindow(self._hwnd):
                # Dismiss save dialogs by finding and closing popup windows
                self._dismiss_save_dialog()
                # Wait again
                for _ in range(20):
                    if not IsWindow(self._hwnd):
                        break
                    time.sleep(0.1)

        # If still alive, force kill
        if self._hwnd and IsWindow(self._hwnd):
            return self._force_stop(pid)

        # Clean up process if it's still around
        if self.state.compositor_proc and self.state.compositor_proc.poll() is None:
            self.state.compositor_proc.terminate()
            try:
                self.state.compositor_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.state.compositor_proc.kill()

        self._cleanup_state()
        return {"status": "stopped", "pid": pid}

    def _force_stop(self, pid: int) -> dict:
        """Force-kill the process (uses HWND to find real PID)."""
        result = self.kill(aggressive=True)
        result["original_pid"] = pid
        return result

    def _dismiss_save_dialog(self):
        """Find and dismiss 'Save?' modal dialogs spawned by WM_CLOSE.

        Handles both classic Win32 dialogs (#32770) and WinUI3/XAML
        ContentDialogs (rendered inside the main HWND).
        """
        # Strategy 1: Classic Win32 modal dialogs (separate HWND)
        modals = self._find_modal_windows()
        for modal_hwnd in modals:
            cls = get_class_name(modal_hwnd)
            log.info("Dismissing classic dialog: hwnd=%#x class=%r", modal_hwnd, cls)
            PostMessageW(modal_hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.3)
            if IsWindow(modal_hwnd):
                PostMessageW(modal_hwnd, WM_KEYDOWN, VK_MAP["escape"], 0)
                time.sleep(0.1)
                PostMessageW(modal_hwnd, WM_KEYUP, VK_MAP["escape"], 0)

        # Strategy 2: WinUI3/XAML ContentDialog (no separate HWND)
        # If the window is still alive after trying classic dismiss,
        # use SendInput to press Escape or Tab+Enter to dismiss
        if IsWindow(self._hwnd):
            log.info("Trying SendInput Escape to dismiss XAML ContentDialog")
            SetForegroundWindow(self._hwnd)
            time.sleep(0.05)
            # Send Tab to reach "Don't Save" then Enter, or just Escape
            # Escape usually cancels the close, so try Tab+Tab+Enter first
            # ("Don't Save" is typically the 2nd button in Notepad's dialog)
            inputs = [
                _make_keyboard_input(VK_MAP["tab"], 0),
                _make_keyboard_input(VK_MAP["tab"], 0x0002),
            ]
            array = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))
            time.sleep(0.1)
            inputs = [
                _make_keyboard_input(VK_MAP["enter"], 0),
                _make_keyboard_input(VK_MAP["enter"], 0x0002),
            ]
            array = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))
            time.sleep(0.5)

    def kill(self, aggressive: bool = True) -> dict:
        killed = []
        pids_killed: set[int] = set()

        def _terminate_pid(pid: int, label: str):
            if pid in pids_killed or pid == 0:
                return
            try:
                handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                if handle:
                    kernel32.TerminateProcess(handle, 1)
                    kernel32.CloseHandle(handle)
                    pids_killed.add(pid)
                    killed.append(f"{label}(pid={pid})")
            except Exception as e:
                log.warning("Failed to kill %s pid=%d: %s", label, pid, e)

        # 1. Get PID from live window handle
        if self._hwnd and IsWindow(self._hwnd):
            real_pid = wt.DWORD()
            GetWindowThreadProcessId(self._hwnd, ctypes.byref(real_pid))
            if real_pid.value:
                _terminate_pid(real_pid.value, "window_owner")

        # 2. Use stored window PID (survives after window is gone)
        if self._window_pid:
            _terminate_pid(self._window_pid, "stored_window_pid")

        # 3. Kill the Popen process
        if self.state.compositor_proc:
            popen_pid = self.state.compositor_proc.pid
            if popen_pid not in pids_killed:
                if self.state.compositor_proc.poll() is None:
                    self.state.compositor_proc.kill()
                    killed.append(f"popen(pid={popen_pid})")

        self._cleanup_state()
        return {"status": "killed", "killed": killed}

    def _cleanup_state(self):
        self.state.compositor_proc = None
        self.state.compositor_pid = 0
        self.state.app_proc = None
        self.state.app_pid = 0
        self.state.wayland_display = ""
        self.state.x_display = ""
        self._hwnd = 0
        self._edit_hwnd = 0
        self._window_pid = 0
        self.state.clear(self._state_file)

    def is_running(self) -> bool:
        # On Windows, some apps (Win11 Notepad, etc.) re-parent to a child process,
        # so the original Popen process may exit while the app window is still alive.
        # Primary check: is the window still valid?
        if self._hwnd and IsWindow(self._hwnd):
            return True

        # Fallback: check the process
        if self.state.compositor_proc is not None:
            return self.state.compositor_proc.poll() is None

        if self.state.compositor_pid:
            try:
                os.kill(self.state.compositor_pid, 0)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                return False

        return False

    def _refresh_edit_hwnd(self):
        """Re-discover the edit child (needed after tab switch in WinUI3 apps)."""
        if not self._hwnd or not IsWindow(self._hwnd):
            return
        new_edit = find_edit_child(self._hwnd, timeout=0.5)
        if new_edit != self._edit_hwnd:
            log.info("Edit child changed: %#x -> %#x (%s)",
                     self._edit_hwnd, new_edit, get_class_name(new_edit))
            self._edit_hwnd = new_edit

    # ── Screenshot ───────────────────────────────────────────────

    def screenshot(self, name: str | None = None) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}
        if not self._hwnd:
            return {"error": "no window handle"}

        self.state.screenshot_seq += 1
        if not name:
            name = f"win32_{self.state.screenshot_seq:04d}.png"
        elif not name.endswith(".png"):
            name += ".png"

        # Capture main window
        main_capture = capture_window_raw(self._hwnd)
        if main_capture is None:
            return {"error": "PrintWindow failed"}

        main_buf, main_w, main_h = main_capture
        main_rect = wt.RECT()
        GetWindowRect(self._hwnd, ctypes.byref(main_rect))

        # Find and capture modal dialogs (same thread, visible, on top)
        modals = self._find_modal_windows()
        if modals:
            # Composite: draw modals over the main window image
            composite = bytearray(main_buf)
            for modal_hwnd in modals:
                modal_capture = capture_window_raw(modal_hwnd)
                if modal_capture is None:
                    continue
                mbuf, mw, mh = modal_capture
                mrect = wt.RECT()
                GetWindowRect(modal_hwnd, ctypes.byref(mrect))
                # Position relative to main window
                ox = mrect.left - main_rect.left
                oy = mrect.top - main_rect.top
                # Blit modal onto composite
                for y in range(mh):
                    dy = oy + y
                    if dy < 0 or dy >= main_h:
                        continue
                    for x in range(mw):
                        dx = ox + x
                        if dx < 0 or dx >= main_w:
                            continue
                        src_idx = (y * mw + x) * 4
                        dst_idx = (dy * main_w + dx) * 4
                        composite[dst_idx:dst_idx + 4] = mbuf[src_idx:src_idx + 4]
            png_data = bgra_to_png(bytes(composite), main_w, main_h)
            log.info("Screenshot composited with %d modal(s)", len(modals))
        else:
            png_data = bgra_to_png(main_buf, main_w, main_h)

        out_path = self.state.screenshot_dir / name
        out_path.write_bytes(png_data)

        result = {"path": str(out_path), "size": out_path.stat().st_size}
        if modals:
            result["modal_visible"] = True
            result["modal_count"] = len(modals)
        elif self._has_xaml_dialog():
            result["modal_visible"] = True
            result["modal_type"] = "xaml_content_dialog"
        return result

    def _find_modal_windows(self) -> list[int]:
        """Find visible modal/dialog windows owned by our main window."""
        if not self._hwnd:
            return []
        modals: list[int] = []

        GW_OWNER = 4

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def callback(hwnd, _lparam):
            if hwnd == self._hwnd or not IsWindowVisible(hwnd):
                return True
            # Only include windows directly owned by our main window
            owner = user32.GetWindow(hwnd, GW_OWNER)
            if owner == self._hwnd:
                modals.append(hwnd)
            return True

        EnumWindows(callback, 0)
        return modals

    def _has_xaml_dialog(self) -> bool:
        """Detect WinUI3/XAML ContentDialog overlay.

        When a ContentDialog is shown, WinUI3 inserts a full-size
        DesktopChildSiteBridge as the first child of the main window.
        It covers the entire client area (acts as a modal overlay).
        """
        if not self._hwnd:
            return False
        # Get client area size for comparison
        crect = wt.RECT()
        GetClientRect(self._hwnd, ctypes.byref(crect))
        client_w, client_h = crect.right, crect.bottom
        if client_w <= 0 or client_h <= 0:
            return False

        # Check first few children for a full-size DesktopChildSiteBridge
        child = 0
        checked = 0
        while checked < 5:
            child = FindWindowExW(self._hwnd, child, None, None)
            if not child:
                break
            checked += 1
            if not IsWindowVisible(child):
                continue
            cls = get_class_name(child)
            if "DesktopChildSiteBridge" not in cls:
                continue
            rect = wt.RECT()
            GetWindowRect(child, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            # A ContentDialog overlay covers >= 90% of the client area
            if w >= client_w * 0.9 and h >= client_h * 0.9:
                log.debug("XAML ContentDialog detected: %#x size=%dx%d", child, w, h)
                return True
        return False

    # ── Input ────────────────────────────────────────────────────

    def _use_sendinput_click(self, x: int, y: int) -> bool:
        """Decide whether to use SendInput (real mouse) vs PostMessage.

        Use PostMessage only when the click targets the edit control
        (the one case where background input works reliably).
        Everything else — menus, tabs, XAML dialogs, intermediate HWNDs
        like DesktopChildSiteBridge — needs SendInput with real mouse.
        """
        dx, dy = client_offset(self._hwnd)
        cx, cy = x - dx, y - dy
        if cx < 0 or cy < 0:
            return True  # outside client area
        target = child_at_point(self._hwnd, cx, cy)
        # PostMessage only when we hit the edit control directly
        if self._edit_hwnd and self._edit_hwnd != self._hwnd and target == self._edit_hwnd:
            return False
        return True

    def _sendinput_click(self, x: int, y: int, button: int = 1) -> dict:
        """Click using SendInput with absolute screen coordinates.

        Briefly brings window to foreground. Covers WinUI3/XAML elements.
        """
        # Convert window-relative coords to screen coords
        rect = wt.RECT()
        GetWindowRect(self._hwnd, ctypes.byref(rect))
        abs_x = rect.left + x
        abs_y = rect.top + y

        SetForegroundWindow(self._hwnd)
        time.sleep(0.05)

        if button == 1:
            down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        elif button == 2:
            down_flag, up_flag = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
        elif button == 3:
            down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        else:
            return {"error": f"unsupported button: {button}"}

        inputs = [
            _make_mouse_input(abs_x, abs_y, down_flag),
            _make_mouse_input(abs_x, abs_y, up_flag),
        ]
        array = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))

        # After a SendInput click (tab switch, menu, etc.), refresh the edit
        # child in case the active tab/document changed
        time.sleep(0.15)
        self._refresh_edit_hwnd()

        log.debug("SendInput click at screen (%d, %d) button=%d", abs_x, abs_y, button)
        return {"ok": True, "method": "SendInput", "screen_pos": (abs_x, abs_y)}

    def click(self, x: int, y: int, button: int = 1) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        # Convert from screenshot coords (window-relative) to client coords
        dx, dy = client_offset(self._hwnd)
        cx, cy = x - dx, y - dy

        # Decide: PostMessage (background) vs SendInput (foreground)
        if self._use_sendinput_click(x, y):
            log.info("Using SendInput click (no child HWND or modal visible) at (%d, %d)", x, y)
            return self._sendinput_click(x, y, button)

        # PostMessage path — background click to a specific child HWND
        target = child_at_point(self._hwnd, cx, cy)
        tx, ty = map_to_child(self._hwnd, target, cx, cy)

        lparam = (ty << 16) | (tx & 0xFFFF)

        if button == 1:
            PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
            time.sleep(0.02)
            PostMessageW(target, WM_LBUTTONUP, 0, lparam)
        elif button == 2:
            PostMessageW(target, WM_MBUTTONDOWN, MK_MBUTTON, lparam)
            time.sleep(0.02)
            PostMessageW(target, WM_MBUTTONUP, 0, lparam)
        elif button == 3:
            PostMessageW(target, WM_RBUTTONDOWN, MK_RBUTTON, lparam)
            time.sleep(0.02)
            PostMessageW(target, WM_RBUTTONUP, 0, lparam)

        return {"ok": True, "method": "PostMessage", "target_hwnd": hex(target), "client_pos": (tx, ty)}

    def type_text(self, text: str, delay_ms: int = 12) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        # Use clipboard + Ctrl+V to type text.
        # This always targets the active tab/document (unlike PostMessage
        # which goes to a fixed HWND that may belong to a hidden tab).
        # Save current clipboard content
        old_clip = None
        if OpenClipboard(0):
            try:
                handle = GetClipboardData(CF_UNICODETEXT)
                if handle:
                    ptr = GlobalLock(ctypes.c_void_p(handle))
                    if ptr:
                        try:
                            old_clip = ctypes.wstring_at(ptr)
                        finally:
                            GlobalUnlock(ctypes.c_void_p(handle))
            finally:
                CloseClipboard()

        # Write text to clipboard
        result = self.clipboard_write(text)
        if "error" in result:
            return result

        # Ctrl+V to paste
        paste_result = self._send_key_combo([VK_MAP["ctrl"]], VK_MAP["v"])
        time.sleep(0.1)

        # Restore previous clipboard content
        if old_clip is not None:
            self.clipboard_write(old_clip)

        return {"ok": True, "length": len(text), "method": "clipboard_paste"}

    def key(self, shortcut: str) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        modifiers, main_key = parse_shortcut(shortcut)

        if not main_key and not modifiers:
            return {"error": f"unknown key: {shortcut!r}"}

        has_modifiers = len(modifiers) > 0

        # When a XAML modal/dialog is visible, PostMessage to the edit control
        # won't reach the dialog buttons — use SendInput instead
        modal_visible = bool(self._find_modal_windows()) or self._has_xaml_dialog()

        if has_modifiers or modal_visible:
            # Key combos with modifiers, or any key when modal is visible:
            # need SetForegroundWindow + SendInput
            return self._send_key_combo(modifiers, main_key)
        else:
            # Simple key press — PostMessage works fine (background)
            target = self._edit_hwnd or self._hwnd
            scan = user32.MapVirtualKeyW(main_key, 0)  # MAPVK_VK_TO_VSC
            lparam_down = (scan << 16) | 1
            lparam_up = (scan << 16) | 1 | (1 << 30) | (1 << 31)
            PostMessageW(target, WM_KEYDOWN, main_key, lparam_down)
            time.sleep(0.01)
            PostMessageW(target, WM_KEYUP, main_key, lparam_up)
            return {"ok": True, "method": "PostMessage"}

    def _send_key_combo(self, modifiers: list[int], main_key: int) -> dict:
        """Send a key combo using SendInput (requires brief foreground focus)."""
        # Briefly bring window to front for SendInput
        if self._hwnd:
            SetForegroundWindow(self._hwnd)
            time.sleep(0.05)

        inputs = []

        # Build INPUT structs
        for vk in modifiers:
            inputs.append(_make_keyboard_input(vk, flags=0))
        if main_key:
            inputs.append(_make_keyboard_input(main_key, flags=0))
            inputs.append(_make_keyboard_input(main_key, flags=0x0002))  # KEYEVENTF_KEYUP
        for vk in reversed(modifiers):
            inputs.append(_make_keyboard_input(vk, flags=0x0002))

        if inputs:
            array = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(INPUT))

        return {"ok": True, "method": "SendInput"}

    def mouse_move(self, x: int, y: int) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        if self._use_sendinput_click(x, y):
            # SendInput path — handles WinUI3 elements
            rect = wt.RECT()
            GetWindowRect(self._hwnd, ctypes.byref(rect))
            abs_x = rect.left + x
            abs_y = rect.top + y
            inp = _make_mouse_input(abs_x, abs_y, MOUSEEVENTF_MOVE)
            array = (INPUT * 1)(inp)
            user32.SendInput(1, ctypes.byref(array), ctypes.sizeof(INPUT))
            return {"ok": True, "method": "SendInput"}

        dx, dy = client_offset(self._hwnd)
        cx, cy = x - dx, y - dy
        target = child_at_point(self._hwnd, cx, cy)
        tx, ty = map_to_child(self._hwnd, target, cx, cy)
        lparam = (ty << 16) | (tx & 0xFFFF)
        PostMessageW(target, WM_MOUSEMOVE, 0, lparam)
        return {"ok": True, "method": "PostMessage"}

    # ── Window geometry ──────────────────────────────────────────

    def get_size(self) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        rect = wt.RECT()
        GetWindowRect(self._hwnd, ctypes.byref(rect))
        crect = wt.RECT()
        GetClientRect(self._hwnd, ctypes.byref(crect))

        return {
            "window_width": rect.right - rect.left,
            "window_height": rect.bottom - rect.top,
            "client_width": crect.right,
            "client_height": crect.bottom,
            "x": rect.left,
            "y": rect.top,
        }

    def resize(self, width: int, height: int) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        # Adjust for non-client area (title bar, borders)
        rect = wt.RECT(0, 0, width, height)
        style = user32.GetWindowLongW(self._hwnd, -16)  # GWL_STYLE
        ex_style = user32.GetWindowLongW(self._hwnd, -20)  # GWL_EXSTYLE
        has_menu = user32.GetMenu(self._hwnd) != 0
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, has_menu, ex_style)

        full_w = rect.right - rect.left
        full_h = rect.bottom - rect.top

        SetWindowPos(self._hwnd, 0, 0, 0, full_w, full_h, SWP_NOMOVE | SWP_NOZORDER)
        return {"ok": True, "client_width": width, "client_height": height}

    # ── Clipboard ────────────────────────────────────────────────

    def clipboard_read(self) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        if not OpenClipboard(0):
            return {"error": "could not open clipboard"}

        try:
            handle = GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return {"error": "no text in clipboard"}

            ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not ptr:
                return {"error": "could not lock clipboard data"}

            try:
                text = ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(handle))

            return {"text": text}
        finally:
            CloseClipboard()

    def clipboard_write(self, text: str) -> dict:
        if not self.is_running():
            return {"error": "app is not running"}

        encoded = text.encode("utf-16-le") + b"\x00\x00"
        size = len(encoded)

        if not OpenClipboard(0):
            return {"error": "could not open clipboard"}

        try:
            EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.c_size_t(size))
            if not h:
                return {"error": "GlobalAlloc failed"}

            ptr = kernel32.GlobalLock(ctypes.c_void_p(h))
            if not ptr:
                return {"error": "GlobalLock failed"}

            ctypes.memmove(ptr, encoded, size)
            kernel32.GlobalUnlock(ctypes.c_void_p(h))
            user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h))
        finally:
            CloseClipboard()

        return {"ok": True, "length": len(text)}

    # ── Debug ────────────────────────────────────────────────────

    def debug_input(self, test_key: str = "a", target: str = "xev") -> dict:
        """On Windows, report window info instead of xev."""
        if not self.is_running():
            return {"error": "app is not running"}

        modals = self._find_modal_windows()
        xaml_dialog = self._has_xaml_dialog()
        info = {
            "hwnd": hex(self._hwnd),
            "edit_hwnd": hex(self._edit_hwnd),
            "window_title": get_window_title(self._hwnd),
            "window_class": get_class_name(self._hwnd),
            "edit_class": get_class_name(self._edit_hwnd) if self._edit_hwnd else "",
            "is_window": bool(IsWindow(self._hwnd)),
            "client_offset": client_offset(self._hwnd),
            "modal_visible": bool(modals) or xaml_dialog,
        }
        if modals:
            info["modals"] = [
                {"hwnd": hex(m), "title": get_window_title(m), "class": get_class_name(m)}
                for m in modals
            ]
        if xaml_dialog:
            info["modal_type"] = "xaml_content_dialog"
        return info

    # ── Unused base class hooks (not called on Windows) ──────────

    def _start_compositor(self, app_cmd, app_env, wl_before, x11_before):
        pass  # Not used — launch() is overridden

    def _start_app(self, app_cmd, app_env):
        pass  # Not used — launch() handles everything
