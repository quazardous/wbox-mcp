#!/usr/bin/env python3
"""
Worker subprocess for mcp-dev.

Imports wbox fresh on startup, manages compositor lifecycle.
Communicates via line-delimited JSON on stdin/stdout.

Protocol:
  → {"method": "launch"}
  ← {"status": "running", "pid": 123, ...}
  → {"method": "click", "args": {"x": 100, "y": 200}}
  ← {"ok": true}
"""

import json
import os
import shlex
import sys

import yaml


def create_compositor(cfg):
    """Create a compositor instance from config dict."""
    from wbox.config import resolve_input_backend

    backend = cfg.get("compositor", "labwc")
    screen = cfg.get("screen", "1280x800")
    instance_name = cfg.get("name", "")
    timeouts = cfg.get("timeouts", {})
    input_backend = cfg.get("input_backend", "hybrid")
    undecorate = cfg.get("undecorate", True)

    if backend == "labwc":
        from wbox.compositor.labwc import LabwcCompositor
        return LabwcCompositor(
            screen=screen, instance_name=instance_name,
            timeouts=timeouts, input_backend=input_backend,
            undecorate=undecorate,
            keyboard_layout=cfg.get("keyboard_layout", ""),
        )
    elif backend == "weston":
        from wbox.compositor.weston import WestonCompositor
        return WestonCompositor(
            screen=screen,
            shell=cfg.get("weston_shell", "kiosk"),
            backend=cfg.get("weston_backend", "wayland"),
            instance_name=instance_name, timeouts=timeouts,
            input_backend=input_backend, undecorate=undecorate,
        )
    elif backend == "cage":
        from wbox.compositor.cage import CageCompositor
        return CageCompositor(
            screen=screen, instance_name=instance_name,
            timeouts=timeouts, input_backend=input_backend,
            undecorate=undecorate,
        )
    else:
        raise ValueError(f"unknown compositor: {backend}")


def respond(data):
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def main():
    config_path = sys.argv[1]
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    compositor = create_compositor(cfg)
    raw_cmd = cfg.get("app", {}).get("command", "")
    app_command = shlex.split(raw_cmd) if isinstance(raw_cmd, str) else list(raw_cmd)
    app_env = cfg.get("app", {}).get("env", {})
    post_launch_keys = cfg.get("app", {}).get("post_launch_keys", [])
    post_launch_keys_delay = cfg.get("app", {}).get("post_launch_keys_delay", 0.5)

    respond({"ready": True})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            respond({"error": f"invalid JSON: {e}"})
            continue

        method = cmd.get("method", "")
        args = cmd.get("args", {})

        try:
            if method == "launch":
                result = compositor.launch(app_command, app_env)
                if post_launch_keys and "error" not in result:
                    compositor.send_post_launch_keys(post_launch_keys, delay=post_launch_keys_delay)
            elif method == "stop":
                result = compositor.stop()
            elif method == "kill":
                result = compositor.kill(**args)
            elif method == "screenshot":
                result = compositor.screenshot(**args)
            elif method == "click":
                result = compositor.click(**args)
            elif method == "mouse_move":
                result = compositor.mouse_move(**args)
            elif method == "type_text":
                result = compositor.type_text(**args)
            elif method == "key":
                result = compositor.key(**args)
            elif method == "clipboard_read":
                result = compositor.clipboard_read()
            elif method == "clipboard_write":
                result = compositor.clipboard_write(**args)
            elif method == "get_mouse_position":
                result = compositor.get_mouse_position()
            elif method == "get_size":
                result = compositor.get_size()
            elif method == "resize":
                result = compositor.resize(**args)
            elif method == "list_windows":
                result = compositor.list_windows()
            elif method == "focus_window":
                result = compositor.focus_window(**args)
            elif method == "is_running":
                result = {"running": compositor.is_running()}
            else:
                result = {"error": f"unknown method: {method}"}
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}

        respond(result)


if __name__ == "__main__":
    main()
