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
        rest = args[1:] if args else []
        config_path = "config.yaml"
        overrides = []
        i = 0
        while i < len(rest):
            if rest[i] in ("-s", "--set") and i + 1 < len(rest):
                overrides.append(rest[i + 1])
                i += 2
            elif rest[i].startswith("--set="):
                overrides.append(rest[i].split("=", 1)[1])
                i += 1
            elif rest[i] == "--mcp-dir" and i + 1 < len(rest):
                import os
                os.chdir(rest[i + 1])
                i += 2
            elif not rest[i].startswith("-"):
                config_path = rest[i]
                i += 1
            else:
                print(f"Unknown flag: {rest[i]}", file=sys.stderr)
                sys.exit(1)
        from wbox.server import amain
        asyncio.run(amain(config_path, overrides=overrides))
    else:
        print(f"Usage: wbox-mcp serve [config.yaml] [-s key=value ...]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
