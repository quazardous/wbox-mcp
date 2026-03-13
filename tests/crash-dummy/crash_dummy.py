#!/usr/bin/env python3
"""
crash_dummy — wbox debug/test GUI.

Modes (CRASH_DUMMY_MODE env var):
  normal     — standard resizable window with WM decorations
  fixed      — fixed-size window (non-resizable) with WM decorations
  fullscreen — overrideredirect, fills display, no WM decorations

Commands (via FIFO at CRASH_DUMMY_FIFO, default log/crash_dummy.fifo):
  dump              — dump layout positions of all widgets
  open_popup        — open popup dialog
  close_popup       — close popup dialog
  ping              — respond with "pong" in log
  set_text <text>   — insert text in text area
  Any unrecognized command is logged as-is.

Logs all events to file (line-buffered) + GUI event log.
"""

import json
import os
import signal
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

MODE = os.environ.get("CRASH_DUMMY_MODE", "normal")
LOG_PATH = os.environ.get("CRASH_DUMMY_LOG", "log/crash_dummy.log")
FIFO_PATH = os.environ.get("CRASH_DUMMY_FIFO", "log/crash_dummy.fifo")
FIXED_SIZE = os.environ.get("CRASH_DUMMY_SIZE", "800x600")


class CrashDummy:
    def __init__(self):
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        self._log_file = open(LOG_PATH, "w", buffering=1)
        self._popup = None
        self._cmd_queue: list[str] = []
        self.root = tk.Tk()
        # HiDPI support
        self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72)
        self.root.title("wbox crash dummy")
        self.root.configure(bg="#1e1e2e")

        # ── Mode setup ──
        if MODE == "fullscreen":
            self.root.overrideredirect(True)
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{sw}x{sh}+0+0")
        elif MODE == "fixed":
            w, h = FIXED_SIZE.split("x")
            self.root.geometry(f"{w}x{h}")
            self.root.resizable(False, False)
        else:  # normal
            w, h = FIXED_SIZE.split("x")
            self.root.geometry(f"{w}x{h}")

        self._log_file.write(f"[{time.strftime('%H:%M:%S')}] mode={MODE} size={FIXED_SIZE}\n")

        # ── Styles ──
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Header.TLabel", font=("monospace", 14, "bold"),
                         foreground="#cdd6f4", background="#1e1e2e")
        style.configure("Info.TLabel", font=("monospace", 11),
                         foreground="#a6adc8", background="#1e1e2e")
        style.configure("Value.TLabel", font=("monospace", 13),
                         foreground="#a6e3a1", background="#313244",
                         padding=5)

        # ── Header ──
        self.header_label = ttk.Label(self.root, text=f"wbox crash dummy [{MODE}]",
                  style="Header.TLabel")
        self.header_label.pack(pady=(10, 5))

        # ── Mouse info panel ──
        self.mouse_frame = tk.Frame(self.root, bg="#313244", bd=1, relief="solid")
        self.mouse_frame.pack(fill="x", padx=15, pady=5)

        self.mouse_label = ttk.Label(self.mouse_frame, text="Mouse: (-, -)",
                                      style="Value.TLabel")
        self.mouse_label.pack(side="left", padx=10, pady=8)

        self.click_label = ttk.Label(self.mouse_frame, text="Last click: -",
                                      style="Value.TLabel")
        self.click_label.pack(side="right", padx=10, pady=8)

        # ── Keyboard info panel ──
        self.kb_frame = tk.Frame(self.root, bg="#313244", bd=1, relief="solid")
        self.kb_frame.pack(fill="x", padx=15, pady=5)

        self.key_label = ttk.Label(self.kb_frame, text="Last key: -",
                                    style="Value.TLabel")
        self.key_label.pack(side="left", padx=10, pady=8)

        self.modifier_label = ttk.Label(self.kb_frame, text="Modifiers: -",
                                         style="Value.TLabel")
        self.modifier_label.pack(side="right", padx=10, pady=8)

        # ── Text input area ──
        self.text_label = ttk.Label(self.root, text="Type here (tests type_text / key):",
                  style="Info.TLabel")
        self.text_label.pack(anchor="w", padx=15, pady=(10, 2))
        self.text_area = tk.Text(self.root, height=4, font=("monospace", 12),
                                  bg="#313244", fg="#cdd6f4",
                                  insertbackground="#f5e0dc", bd=1,
                                  relief="solid")
        self.text_area.pack(fill="x", padx=15, pady=2)

        # ── Clipboard + popup buttons ──
        self.btn_frame = tk.Frame(self.root, bg="#1e1e2e")
        self.btn_frame.pack(fill="x", padx=15, pady=5)

        self.clip_btn = tk.Button(self.btn_frame, text="Read clipboard",
                  font=("monospace", 10), bg="#45475a",
                  fg="#cdd6f4", activebackground="#585b70",
                  command=self._read_clipboard)
        self.clip_btn.pack(side="left")

        self.clip_label = ttk.Label(self.btn_frame, text="Clipboard: -",
                                     style="Info.TLabel")
        self.clip_label.pack(side="left", padx=10)

        self.popup_btn = tk.Button(self.btn_frame, text="Open popup",
                  font=("monospace", 10), bg="#45475a",
                  fg="#cdd6f4", activebackground="#585b70",
                  command=self._open_popup)
        self.popup_btn.pack(side="right")

        # ── Event log ──
        self.log_label = ttk.Label(self.root, text="Event log:",
                  style="Info.TLabel")
        self.log_label.pack(anchor="w", padx=15, pady=(10, 2))
        self.log_frame = tk.Frame(self.root, bg="#313244", bd=1, relief="solid")
        self.log_frame.pack(fill="both", expand=True, padx=15, pady=(2, 15))

        self.log_text = tk.Text(self.log_frame, font=("monospace", 9),
                                 bg="#313244", fg="#89b4fa",
                                 insertbackground="#313244", bd=0,
                                 state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

        # ── Bind events ──
        self.root.bind("<Motion>", self._on_motion)
        self.root.bind("<Button>", self._on_click)
        self.root.bind("<KeyPress>", self._on_key)
        self.root.bind("<Configure>", self._on_configure)

        # ── FIFO command listener ──
        self._setup_fifo()
        self._poll_commands()

        # Log initial window geometry after mainloop starts
        self.root.after(500, self._log_geometry)

        self._log("ready")

    # ── FIFO ──

    def _setup_fifo(self):
        """Create FIFO and start reader thread."""
        os.makedirs(os.path.dirname(FIFO_PATH), exist_ok=True)
        # Remove stale FIFO
        try:
            os.unlink(FIFO_PATH)
        except FileNotFoundError:
            pass
        os.mkfifo(FIFO_PATH)
        self._fifo_thread = threading.Thread(target=self._fifo_reader, daemon=True)
        self._fifo_thread.start()
        self._log_file.write(f"[{time.strftime('%H:%M:%S')}] fifo={FIFO_PATH}\n")

    def _fifo_reader(self):
        """Background thread: read commands from FIFO, push to queue."""
        while True:
            try:
                with open(FIFO_PATH, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._cmd_queue.append(line)
            except OSError:
                break

    def _poll_commands(self):
        """Process queued commands from the FIFO in the tkinter main thread."""
        while self._cmd_queue:
            cmd_line = self._cmd_queue.pop(0)
            parts = cmd_line.split(None, 1)
            cmd = parts[0]
            payload = parts[1] if len(parts) > 1 else ""
            self._handle_command(cmd, payload)
        self.root.after(100, self._poll_commands)

    def _handle_command(self, cmd: str, payload: str):
        """Dispatch a command received via FIFO."""
        if cmd == "ping":
            self._log("pong")
        elif cmd == "dump":
            self._dump_layout()
        elif cmd == "open_popup":
            self._open_popup()
        elif cmd == "close_popup":
            self._close_popup()
        elif cmd == "set_text":
            self.text_area.delete("1.0", "end")
            self.text_area.insert("1.0", payload)
            self._log(f"set_text len={len(payload)}")
        else:
            self._log(f"unknown_cmd: {cmd!r} payload={payload!r}")

    # ── Logging ──

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_file.write(line + "\n")
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _log_geometry(self):
        g = self.root.geometry()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        self._log(f"geometry window_pos=({x},{y}) window_size=({w},{h}) geometry={g}")

    def _dump_layout(self):
        """Dump position and size of every named widget to the log."""
        self.root.update_idletasks()
        widgets = {
            "root": self.root,
            "header_label": self.header_label,
            "mouse_frame": self.mouse_frame,
            "mouse_label": self.mouse_label,
            "click_label": self.click_label,
            "kb_frame": self.kb_frame,
            "key_label": self.key_label,
            "modifier_label": self.modifier_label,
            "text_label": self.text_label,
            "text_area": self.text_area,
            "btn_frame": self.btn_frame,
            "clip_btn": self.clip_btn,
            "clip_label": self.clip_label,
            "popup_btn": self.popup_btn,
            "log_label": self.log_label,
            "log_frame": self.log_frame,
            "log_text": self.log_text,
        }
        dump = {}
        for name, w in widgets.items():
            try:
                dump[name] = {
                    "x": w.winfo_rootx(),
                    "y": w.winfo_rooty(),
                    "w": w.winfo_width(),
                    "h": w.winfo_height(),
                }
            except tk.TclError:
                dump[name] = {"error": "widget not available"}

        if self._popup and self._popup.winfo_exists():
            try:
                dump["popup"] = {
                    "x": self._popup.winfo_rootx(),
                    "y": self._popup.winfo_rooty(),
                    "w": self._popup.winfo_width(),
                    "h": self._popup.winfo_height(),
                }
            except tk.TclError:
                pass

        self._log(f"DUMP {json.dumps(dump)}")

    # ── Events ──

    def _on_configure(self, event):
        if event.widget == self.root:
            self._log_file.write(
                f"[{time.strftime('%H:%M:%S')}] configure "
                f"size=({event.width},{event.height}) pos=({event.x},{event.y})\n"
            )

    def _on_motion(self, event):
        self.mouse_label.configure(text=f"Mouse: ({event.x}, {event.y})")
        ts = time.strftime("%H:%M:%S")
        self._log_file.write(
            f"[{ts}] motion ({event.x},{event.y}) "
            f"root=({event.x_root},{event.y_root})\n"
        )

    def _on_click(self, event):
        btn_names = {1: "left", 2: "middle", 3: "right"}
        btn = btn_names.get(event.num, f"btn{event.num}")
        self.click_label.configure(
            text=f"Last click: {btn} @ ({event.x}, {event.y})")
        self._log(
            f"click {btn} at ({event.x},{event.y}) "
            f"root=({event.x_root},{event.y_root})"
        )

    def _on_key(self, event):
        mods = []
        if event.state & 0x1:
            mods.append("Shift")
        if event.state & 0x4:
            mods.append("Ctrl")
        if event.state & 0x8:
            mods.append("Alt")
        if event.state & 0x40:
            mods.append("Super")
        mod_str = "+".join(mods) if mods else "-"

        display = event.keysym
        if event.char and event.char.isprintable():
            display = f"{event.keysym} ({event.char!r})"

        self.key_label.configure(text=f"Last key: {display}")
        self.modifier_label.configure(text=f"Modifiers: {mod_str}")
        self._log(f"key {display} mods={mod_str}")

    def _read_clipboard(self):
        try:
            content = self.root.clipboard_get()
            short = content[:80] + ("..." if len(content) > 80 else "")
            self.clip_label.configure(text=f"Clipboard: {short}")
            self._log(f"clipboard={content!r}")
        except tk.TclError:
            self.clip_label.configure(text="Clipboard: (empty)")
            self._log("clipboard=EMPTY")

    # ── Popup ──

    def _open_popup(self):
        if self._popup and self._popup.winfo_exists():
            return
        popup = tk.Toplevel(self.root)
        popup.title("crash dummy popup")
        popup.configure(bg="#1e1e2e")
        if MODE == "fullscreen":
            # overrideredirect parent: WM can't stack transient above it,
            # so make the popup override-redirect too and center manually.
            popup.overrideredirect(True)
            pw, ph = 400, 200
            rx = self.root.winfo_rootx() + (self.root.winfo_width() - pw) // 2
            ry = self.root.winfo_rooty() + (self.root.winfo_height() - ph) // 2
            popup.geometry(f"{pw}x{ph}+{rx}+{ry}")
        else:
            popup.geometry("400x200")
            popup.transient(self.root)
        self._popup = popup

        ttk.Label(popup, text="This is a dialog window",
                  style="Header.TLabel").pack(pady=(20, 10))

        self.popup_mouse = ttk.Label(popup, text="Mouse: (-, -)",
                                      style="Value.TLabel")
        self.popup_mouse.pack(pady=5)

        popup.bind("<Motion>", lambda e: (
            self.popup_mouse.configure(text=f"Mouse: ({e.x}, {e.y})"),
            self._log_file.write(
                f"[{time.strftime('%H:%M:%S')}] popup_motion ({e.x},{e.y}) "
                f"root=({e.x_root},{e.y_root})\n"
            ),
        ))
        popup.bind("<Button>", lambda e: self._log(
            f"popup_click {e.num} at ({e.x},{e.y}) "
            f"root=({e.x_root},{e.y_root})"
        ))

        tk.Button(popup, text="Close", font=("monospace", 10),
                  bg="#45475a", fg="#cdd6f4",
                  command=self._close_popup).pack(pady=10)

        # Log popup geometry after it's mapped
        popup.after(300, lambda: self._log(
            f"popup_geometry pos=({popup.winfo_x()},{popup.winfo_y()}) "
            f"size=({popup.winfo_width()},{popup.winfo_height()})"
        ))
        self._log("popup_opened")

    def _close_popup(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None
            self._log("popup_closed")

    def run(self):
        try:
            self.root.mainloop()
        finally:
            try:
                os.unlink(FIFO_PATH)
            except OSError:
                pass


if __name__ == "__main__":
    CrashDummy().run()
