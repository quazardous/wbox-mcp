#!/usr/bin/env python3
"""
wbox-pointer — CLI wrapper around wbox.pointer (Wayland virtual pointer).

Usage:
    wbox-pointer.py move <x> <y>
    wbox-pointer.py click <x> <y> [button]

Environment:
    WAYLAND_DISPLAY   Target compositor (required)
    WBOX_SCREEN       Display size WxH (overrides auto-detect)
"""

import sys
from pathlib import Path

try:
    from wbox.pointer import main
except ImportError:
    # Running from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
    from wbox.pointer import main

if __name__ == "__main__":
    main()
