"""
sandbox_agent.py — wbox's side inside Windows Sandbox.

The win32 backend drives an app through the Win32 API, which only works on
the desktop the app is on. With `sandbox:` in config.yaml that desktop is
Windows Sandbox's, so the backend runs there, as this agent, and the host's
backend (compositor/wsb.py) sends it each call.

The channel is a folder shared read-write with the host (the only one):

    inbox                 the requests, a JSON line each, appended by the host:
                          {"seq": 1, "method": "click", "args": {"x": 1, "y": 2}}
    responses/<seq>.json  what the method returned (a dict), or {"error": ...}
    shots/                the screenshots, which the host moves to its own dir
    agent.ready           written once the agent listens: {"pid": ...}

An answer is written under a temporary name, then renamed, and a request
line is read once its newline is there: neither side reads half of one. No network: the sandbox can run with networking off.

    agent.json            what the agent needs: {"name", "title_hint", "timeouts"}

    python -m wbox.sandbox_agent IO_DIR
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

log = logging.getLogger("wbox.sandbox_agent")

# The calls the host may make: the win32 backend's public surface, nothing
# else (no attribute a request could reach through getattr by accident).
METHODS = {
    "launch", "stop", "kill", "is_running", "screenshot", "click", "type_text",
    "key", "keys", "mouse_move", "list_windows", "focus_window", "get_size",
    "resize", "clipboard_read", "clipboard_write",
    "scroll", "double_click", "drag", "hold",
}

POLL_S = 0.02


def write_json(path: Path, data: dict) -> None:
    """Writes `data` at `path` whole: under a temporary name, then renamed."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def response_name(seq: int) -> str:
    """The file of the answer to request number `seq`."""
    return f"{seq:08d}.json"


def read_new_lines(inbox: Path, offset: int) -> tuple[list[str], int]:
    """The complete lines added to `inbox` since `offset`, and the new offset.
    A line still being written waits for its newline."""
    try:
        with open(inbox, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset
    complete = chunk[:chunk.rfind(b"\n") + 1]
    return complete.decode("utf-8", "replace").splitlines(), offset + len(complete)


def serve(io_dir: Path, comp) -> None:
    """Answers the requests in `io_dir` until asked to `shutdown`.

    The requests are lines added to one file, `inbox`, which exists from the
    start: in Windows Sandbox the folder is a mapped share, and the guest
    learns of a new file seconds late, while it reads a known one afresh."""
    inbox, responses = io_dir / "inbox", io_dir / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    if not inbox.exists():
        inbox.write_bytes(b"")
    write_json(io_dir / "agent.ready", {"pid": os.getpid()})
    log.info("agent ready in %s", io_dir)
    offset = 0
    while True:
        lines, offset = read_new_lines(inbox, offset)
        if not lines:
            time.sleep(POLL_S)
            continue
        for line in lines:
            try:
                request = json.loads(line)
                seq = int(request["seq"])
            except (ValueError, KeyError, TypeError) as e:
                log.warning("unreadable request %r: %s", line[:200], e)
                continue
            method = request.get("method", "")
            log.debug("request %d: %s (sent %.3f, read %.3f)", seq, method,
                      request.get("sent", 0), time.time())
            answer = responses / response_name(seq)
            if method == "shutdown":
                write_json(answer, {"status": "shutting_down"})
                log.info("shutdown asked")
                return
            write_json(answer, call(comp, method, request.get("args") or {}))


def call(comp, method: str, args: dict) -> dict:
    """One call on the backend, as a dict whatever happens."""
    if method == "ping":
        return {"status": "ok", "pid": os.getpid()}
    if method not in METHODS:
        return {"error": f"unknown method {method!r}"}
    try:
        result = getattr(comp, method)(**args)
    except Exception as e:  # the host gets the error, the agent carries on
        log.error("%s failed: %s", method, traceback.format_exc())
        return {"error": f"{method}: {e}"}
    if isinstance(result, dict):
        return result
    return {"result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wbox.sandbox_agent")
    parser.add_argument("io_dir")
    args = parser.parse_args(argv)

    io_dir = Path(args.io_dir)
    logging.basicConfig(
        filename=io_dir / "agent.log", level=logging.DEBUG if os.environ.get("WBOX_AGENT_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform != "win32":
        log.error("the sandbox agent runs on Windows only")
        return 2

    from .compositor.win32 import Win32Compositor

    try:
        settings = json.loads((io_dir / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    comp = Win32Compositor(
        instance_name=f"{settings.get('name', 'sandbox')}-guest",
        timeouts=settings.get("timeouts") or {},
        title_hint=settings.get("title_hint", ""),
    )
    shots = io_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    comp.state.screenshot_dir = shots
    try:
        serve(io_dir, comp)
    finally:
        try:
            comp.kill()
        except Exception:
            pass
        (io_dir / "agent.ready").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
