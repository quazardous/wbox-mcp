"""
config.py — Configuration loading, saving, and validation for wbox-mcp.

Config is stored as config.yaml in the project directory.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import sys

_IS_WIN32 = sys.platform == "win32"

INPUT_BACKEND_PRESETS = {
    "x11": {"keyboard": "xdotool", "mouse": "xdotool", "clipboard": "x11"},
    "wayland": {"keyboard": "wtype", "mouse": "ydotool", "clipboard": "wayland"},
    "hybrid": {"keyboard": "wtype", "mouse": "wbox-pointer", "clipboard": "x11"},
}

_VALID_BACKENDS = {
    "keyboard": ("xdotool", "wtype"),
    "mouse": ("xdotool", "ydotool", "wbox-pointer"),
    "clipboard": ("x11", "wayland"),
}


def resolve_input_backend(value: str | dict) -> dict:
    """Resolve input_backend config (string preset or per-function dict) to a full dict.

    Returns dict with keys: keyboard, mouse, clipboard.
    """
    if isinstance(value, str):
        if value not in INPUT_BACKEND_PRESETS:
            raise ValueError(
                f"unknown input_backend preset {value!r}, "
                f"expected one of {list(INPUT_BACKEND_PRESETS)} or a dict"
            )
        return dict(INPUT_BACKEND_PRESETS[value])

    if isinstance(value, dict):
        # Fill missing keys from x11 defaults
        resolved = dict(INPUT_BACKEND_PRESETS["x11"])
        for k, v in value.items():
            if k not in _VALID_BACKENDS:
                raise ValueError(
                    f"unknown input_backend key {k!r}, "
                    f"expected one of {list(_VALID_BACKENDS)}"
                )
            if v not in _VALID_BACKENDS[k]:
                raise ValueError(
                    f"invalid input_backend.{k} value {v!r}, "
                    f"expected one of {_VALID_BACKENDS[k]}"
                )
            resolved[k] = v
        return resolved

    raise ValueError(f"input_backend must be a string or dict, got {type(value).__name__}")


DEFAULT_CONFIG = {
    "name": "my-wbox",
    "compositor": "win32" if _IS_WIN32 else "labwc",
    "screen": "1280x800",
    **({} if _IS_WIN32 else {
        "weston_shell": "kiosk",
        "weston_backend": "x11",
        "input_backend": "hybrid",  # preset: "x11", "wayland", "hybrid", or dict
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


def apply_overrides(cfg: dict, overrides: list[str]) -> dict:
    """Apply key=value overrides to config dict.

    Supports dotted keys for nested values (e.g. "log.level=debug").
    Values are parsed as YAML scalars (so numbers, bools work naturally).
    """
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"invalid override {item!r}, expected key=value")
        key, raw_value = item.split("=", 1)
        # Parse value as YAML scalar
        value = yaml.safe_load(raw_value)

        parts = key.split(".")
        target = cfg
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
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
