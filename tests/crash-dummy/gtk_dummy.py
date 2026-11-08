#!/usr/bin/env python3
"""
gtk_dummy — a native Wayland test app (GTK 4) for the backends without X11.

crash_dummy is Tk, so X11 only: it cannot run in mutter started with
--no-x11. This one logs what it receives, one line per event:

    ready
    press <window> button=<n> x=<x> y=<y>      (window: main or second)
    release <window> button=<n>
    key <keyval name> mods=<ctrl,shift,alt,super>
    text <the entry's text>
    scroll dx=<dx> dy=<dy>
    drag dx=<dx> dy=<dy>
    second mapped

Commands, one per line, on the FIFO:
    open_second    a second window, transient and modal, over the main one
    close_second
    focus_entry

Environment: GTK_DUMMY_LOG, GTK_DUMMY_FIFO, GTK_DUMMY_SIZE (WxH).
"""

import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

LOG = open(os.environ.get("GTK_DUMMY_LOG", "log/gtk_dummy.log"), "a", buffering=1)
FIFO = os.environ.get("GTK_DUMMY_FIFO", "log/gtk_dummy.fifo")
W, H = (int(v) for v in os.environ.get("GTK_DUMMY_SIZE", "800x600").split("x"))


def log(line: str) -> None:
    LOG.write(line + "\n")


def mods_of(state) -> str:
    names = []
    for mask, name in ((Gdk.ModifierType.CONTROL_MASK, "ctrl"),
                       (Gdk.ModifierType.SHIFT_MASK, "shift"),
                       (Gdk.ModifierType.ALT_MASK, "alt"),
                       (Gdk.ModifierType.SUPER_MASK, "super")):
        if state & mask:
            names.append(name)
    return ",".join(names)


def watch_clicks(widget, name: str) -> None:
    click = Gtk.GestureClick(button=0)
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    click.connect("pressed", lambda g, n, x, y: log(
        f"press {name} button={g.get_current_button()} x={int(x)} y={int(y)}"))
    click.connect("released", lambda g, n, x, y: log(
        f"release {name} button={g.get_current_button()}"))
    widget.add_controller(click)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.wbox.GtkDummy")
        self.main = None
        self.second = None

    def do_activate(self):
        win = Gtk.ApplicationWindow(application=self, title="gtk dummy")
        win.set_default_size(W, H)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.entry = Gtk.Entry()
        self.entry.connect("changed", lambda e: log(f"text {e.get_text()}"))
        box.append(self.entry)
        area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        area.set_draw_func(self.draw)
        box.append(area)
        win.set_child(box)

        watch_clicks(win, "main")
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda c, kv, code, state: log(
            f"key {Gdk.keyval_name(kv)} mods={mods_of(state)}") or False)
        win.add_controller(keys)
        scroll = Gtk.EventControllerScroll(
            flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", lambda c, dx, dy: log(
            f"scroll dx={dx:g} dy={dy:g}") or True)
        area.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-end", lambda g, dx, dy: log(
            f"drag dx={int(dx)} dy={int(dy)}"))
        area.add_controller(drag)

        self.main = win
        win.present()
        GLib.timeout_add(300, self.ready)
        threading.Thread(target=self.read_fifo, daemon=True).start()

    @staticmethod
    def draw(area, cr, width, height):
        cr.set_source_rgb(0.2, 0.4, 0.8)
        cr.paint()

    def ready(self):
        log("ready")
        return False

    def open_second(self):
        if self.second is not None:
            return
        win = Gtk.Window(title="second", transient_for=self.main, modal=True)
        win.set_default_size(W * 3 // 4, H * 3 // 4)
        area = Gtk.DrawingArea()
        area.set_draw_func(lambda a, cr, w, h: (cr.set_source_rgb(0.9, 0.5, 0.1),
                                                cr.paint()))
        win.set_child(area)
        watch_clicks(win, "second")
        win.connect("map", lambda w: log("second mapped"))
        self.second = win
        win.present()

    def close_second(self):
        if self.second is not None:
            self.second.destroy()
            self.second = None

    def command(self, cmd: str):
        if cmd == "open_second":
            self.open_second()
        elif cmd == "close_second":
            self.close_second()
        elif cmd == "focus_entry":
            self.entry.grab_focus()
        else:
            log(f"unknown command {cmd}")
        return False

    def read_fifo(self):
        if not os.path.exists(FIFO):
            os.mkfifo(FIFO)
        while True:
            with open(FIFO) as f:
                for line in f:
                    cmd = line.strip()
                    if cmd:
                        GLib.idle_add(self.command, cmd)


if __name__ == "__main__":
    sys.exit(App().run([]))
