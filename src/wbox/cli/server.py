"""
wbox-mcp CLI — MCP server entry point.

Usage:
    wbox-mcp serve [config.yaml]
"""

from __future__ import annotations

import asyncio
import sys


def main():
    args = sys.argv[1:]

    if args and args[0] in ("-V", "--version"):
        from wbox import __version__
        print(f"wbox-mcp {__version__}")
        sys.exit(0)

    if not args or args[0] == "serve":
        config_path = args[1] if len(args) > 1 else "config.yaml"
        from wbox.server import amain
        asyncio.run(amain(config_path))
    else:
        print(f"Usage: wbox-mcp serve [config.yaml]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
