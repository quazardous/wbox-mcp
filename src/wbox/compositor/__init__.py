import sys

from .base import CompositorServer, CompositorState

__all__ = [
    "CompositorServer",
    "CompositorState",
]

if sys.platform == "win32":
    from .win32 import Win32Compositor
    __all__.append("Win32Compositor")
else:
    from .cage import CageCompositor
    from .weston import WestonCompositor
    __all__.extend(["CageCompositor", "WestonCompositor"])
