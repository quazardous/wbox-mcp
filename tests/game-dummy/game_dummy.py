#!/usr/bin/env python3
"""
game_dummy.py — a test app that reads its input the way a game does.

A GUI toolkit reacts to key events; a game reads the keyboard's state once a
frame, and its controllers through SDL's GameController API. This app does
both, at 60 frames a second, and writes what it saw to a JSON file every few
frames, which the tests read:

    frames               frames drawn
    keys.<name>          {"presses": n, "held_frames": n} — counted from the
                         polled state, not from events
    keydown_events.<name>  KEYDOWN events received (to show a press arrived
                         even when the polled state never had it down)
    together             frames with w and shift both down
    pads                 controller names SDL opened
    buttons.<name>       presses per SDL controller button name
    axes.<name>          largest |value| seen per SDL axis name
    rumble_ok            whether SDL accepted a rumble on the pad
    focus                ["gained"|"lost", frame] as SDL reports the window's
                         keyboard focus; SDL drops pad events while it is lost
    dev_input            what /dev/input holds, as this process sees it

Usage: game_dummy.py <state.json> [rumble_ms]
"""

import json
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "wayland")

import pygame  # noqa: E402
from pygame._sdl2 import controller  # noqa: E402

KEYS = {"a": pygame.K_a, "w": pygame.K_w, "shift": pygame.K_LSHIFT}
BUTTON_NAMES = {
    pygame.CONTROLLER_BUTTON_A: "a", pygame.CONTROLLER_BUTTON_B: "b",
    pygame.CONTROLLER_BUTTON_X: "x", pygame.CONTROLLER_BUTTON_Y: "y",
    pygame.CONTROLLER_BUTTON_LEFTSHOULDER: "lb",
    pygame.CONTROLLER_BUTTON_RIGHTSHOULDER: "rb",
    pygame.CONTROLLER_BUTTON_BACK: "back", pygame.CONTROLLER_BUTTON_START: "start",
    pygame.CONTROLLER_BUTTON_GUIDE: "guide",
    pygame.CONTROLLER_BUTTON_LEFTSTICK: "ls", pygame.CONTROLLER_BUTTON_RIGHTSTICK: "rs",
    pygame.CONTROLLER_BUTTON_DPAD_UP: "up", pygame.CONTROLLER_BUTTON_DPAD_DOWN: "down",
    pygame.CONTROLLER_BUTTON_DPAD_LEFT: "left", pygame.CONTROLLER_BUTTON_DPAD_RIGHT: "right",
}
AXIS_NAMES = {
    pygame.CONTROLLER_AXIS_LEFTX: "leftx", pygame.CONTROLLER_AXIS_LEFTY: "lefty",
    pygame.CONTROLLER_AXIS_RIGHTX: "rightx", pygame.CONTROLLER_AXIS_RIGHTY: "righty",
    pygame.CONTROLLER_AXIS_TRIGGERLEFT: "lt", pygame.CONTROLLER_AXIS_TRIGGERRIGHT: "rt",
}


def main():
    out = sys.argv[1]
    rumble_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    pygame.init()
    controller.init()
    screen = pygame.display.set_mode((640, 360))
    pygame.display.set_caption("game_dummy")
    clock = pygame.time.Clock()
    pads = []
    seen = {
        "frames": 0,
        "keys": {k: {"presses": 0, "held_frames": 0} for k in KEYS},
        "keydown_events": {k: 0 for k in KEYS},
        "together": 0,
        "pads": [], "buttons": {}, "axes": {}, "rumble_ok": None, "focus": [],
        "dev_input": sorted(os.listdir("/dev/input")) if os.path.isdir("/dev/input") else [],
    }
    prev = {k: False for k in KEYS}
    by_code = {v: k for k, v in KEYS.items()}
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return
            if e.type == pygame.WINDOWFOCUSGAINED:
                seen["focus"].append(["gained", seen["frames"]])
            elif e.type == pygame.WINDOWFOCUSLOST:
                seen["focus"].append(["lost", seen["frames"]])
            elif e.type == pygame.KEYDOWN and e.key in by_code:
                seen["keydown_events"][by_code[e.key]] += 1
            elif e.type == pygame.CONTROLLERDEVICEADDED:
                pad = controller.Controller(e.device_index)
                pads.append(pad)
                seen["pads"].append(pad.name)
                if rumble_ms:
                    seen["rumble_ok"] = bool(pad.rumble(0.5, 1.0, rumble_ms))
            elif e.type == pygame.CONTROLLERBUTTONDOWN:
                name = BUTTON_NAMES.get(e.button, str(e.button))
                seen["buttons"][name] = seen["buttons"].get(name, 0) + 1
            elif e.type == pygame.CONTROLLERAXISMOTION:
                name = AXIS_NAMES.get(e.axis, str(e.axis))
                seen["axes"][name] = max(seen["axes"].get(name, 0), abs(e.value))
        state = pygame.key.get_pressed()
        for name, code in KEYS.items():
            down = bool(state[code])
            if down and not prev[name]:
                seen["keys"][name]["presses"] += 1
            if down:
                seen["keys"][name]["held_frames"] += 1
            prev[name] = down
        if state[pygame.K_w] and state[pygame.K_LSHIFT]:
            seen["together"] += 1
        screen.fill((30, 30, 30))
        pygame.display.flip()
        clock.tick(60)
        seen["frames"] += 1
        if seen["frames"] % 5 == 0:
            tmp = out + ".tmp"
            with open(tmp, "w") as f:
                json.dump(seen, f)
            os.replace(tmp, out)


if __name__ == "__main__":
    main()
