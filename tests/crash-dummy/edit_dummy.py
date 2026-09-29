#!/usr/bin/env python3
"""
edit_dummy.py — a plain Win32 window with an Edit control, for the win32
backend's tests (Windows only, ctypes only).

The crash dummy is Tk, and Tk ignores posted mouse messages: wbox clicks it
with SendInput. The one thing wbox posts clicks to is an Edit control, so
this is the app that tests that path — at any DPI awareness, which is where
it goes wrong: a posted message's coordinates are not translated by Windows.

Environment:
    EDIT_DUMMY_LOG   the log (default log/edit_dummy.log)
    EDIT_DUMMY_DPI   unaware | system | permonitor (default permonitor)

Log lines, all in the app's own coordinates (logical for an unaware app):
    ready dpi=<window dpi> window=(left,top,width,height) client=(width,height)
          edit=(screen x,screen y)
    lbutton (x,y)    a click received by the Edit control, in its client space
    char 'c'         a character received by the Edit control
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

if sys.platform != "win32":
    sys.exit("edit_dummy runs on Windows only")

LOG_PATH = os.environ.get("EDIT_DUMMY_LOG", "log/edit_dummy.log")
DPI_MODE = os.environ.get("EDIT_DUMMY_DPI", "permonitor")
TITLE = "wbox edit dummy"

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Before any window exists.
_CONTEXTS = {"unaware": -1, "system": -2, "permonitor": -4}
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_CONTEXTS[DPI_MODE]))

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY = 0x0002
WM_SETFOCUS = 0x0007
WM_CHAR = 0x0102
WM_LBUTTONDOWN = 0x0201
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
ES_MULTILINE = 0x0004
GWLP_WNDPROC = -4


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.GetDpiForWindow.argtypes = [wt.HWND]

os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
_log = open(LOG_PATH, "a", buffering=1, encoding="utf-8")


def log(msg: str) -> None:
    _log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def _signed(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


_edit_proc = None


@WNDPROC
def edit_proc(hwnd, msg, wparam, lparam):
    if msg == WM_LBUTTONDOWN:
        log(f"lbutton ({_signed(lparam & 0xFFFF)},{_signed((lparam >> 16) & 0xFFFF)})")
    elif msg == WM_CHAR:
        log(f"char {chr(wparam)!r}")
    return user32.CallWindowProcW(_edit_proc, hwnd, msg, wparam, lparam)


_edit = None


@WNDPROC
def main_proc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if msg == WM_SETFOCUS and _edit:
        # As any form does: the keyboard goes to its text field.
        user32.SetFocus(_edit)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> None:
    global _edit_proc, _edit
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=main_proc, hInstance=hinst, lpszClassName="WboxEditDummy",
                   hbrBackground=ctypes.c_void_p(6))  # COLOR_WINDOW + 1
    user32.RegisterClassW(ctypes.byref(wc))
    main = user32.CreateWindowExW(0, "WboxEditDummy", TITLE, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                  100, 100, 520, 360, None, None, hinst, None)
    edit = user32.CreateWindowExW(0, "Edit", "", WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE,
                                  20, 20, 460, 260, main, None, hinst, None)
    _edit_proc = user32.SetWindowLongPtrW(edit, GWLP_WNDPROC, ctypes.cast(edit_proc, ctypes.c_void_p))
    _edit = edit
    user32.SetFocus(edit)

    rect, client, origin = wt.RECT(), wt.RECT(), wt.POINT(0, 0)
    user32.GetWindowRect(main, ctypes.byref(rect))
    user32.GetClientRect(main, ctypes.byref(client))
    user32.ClientToScreen(edit, ctypes.byref(origin))
    log(f"mode={DPI_MODE}")
    log(f"ready dpi={user32.GetDpiForWindow(main)} "
        f"window=({rect.left},{rect.top},{rect.right - rect.left},{rect.bottom - rect.top}) "
        f"client=({client.right},{client.bottom}) edit=({origin.x},{origin.y})")

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
