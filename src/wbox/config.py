"""
config.py — Configuration loading, saving, and validation for wbox-mcp.

Config is stored as config.yaml in the project directory.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import sys

_IS_WIN32 = sys.platform == "win32"

DEFAULT_CONFIG = {
    "name": "my-wbox",
    "compositor": "win32" if _IS_WIN32 else "weston",
    "screen": "1280x800",
    **({} if _IS_WIN32 else {
        "weston_shell": "kiosk",
        "weston_backend": "x11",
        "input_backend": "x11",  # "x11" (xdotool) or "wayland" (wtype/ydotool)
    }),
    "log": {
        "dir": "./log",
        "level": "info",
    },
    "screenshot_dir": "./screenshots",
    "timeouts": {
        **({
            "window_discovery": 10,
            "edit_control": 3,
        } if _IS_WIN32 else {
            "wayland_display": 10,
            "xwayland_display": 15,
        }),
        "app_render": 3,
        "stop": 10,
    },
    "title_hint": "" if _IS_WIN32 else None,
    "app": {
        "command": "",
        "env": {},
    },
    "tools": {},
}
# Remove None values
DEFAULT_CONFIG = {k: v for k, v in DEFAULT_CONFIG.items() if v is not None}


def load_config(path: str | Path) -> dict:
    """Load config.yaml from the given path."""
    p = Path(path)
    if not p.exists():
        return {}
    cfg = yaml.safe_load(p.read_text()) or {}
    cfg["_config_dir"] = str(p.parent)
    cfg["_config_path"] = str(p)
    return cfg


def save_config(cfg: dict, path: str | Path) -> None:
    """Save config to yaml, stripping internal keys."""
    p = Path(path)
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    p.write_text(yaml.dump(clean, default_flow_style=False, sort_keys=False))


def resolve_dir(cfg: dict, key: str, default: str) -> Path:
    """Resolve a config dir relative to the config file's parent."""
    config_dir = Path(cfg.get("_config_dir", ".")).resolve()
    # Handle nested keys like log.dir
    value = cfg
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            value = default
            break
    d = config_dir / str(value)
    d.mkdir(parents=True, exist_ok=True)
    return d
