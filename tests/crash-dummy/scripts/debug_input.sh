#!/bin/bash
# Send a command to crash dummy via its FIFO.
# Usage: debug_input.sh <command>
# Commands: dump, ping, open_popup, close_popup, set_text <text>

FIFO="${CRASH_DUMMY_FIFO:-./log/crash_dummy.fifo}"
CMD="${WBOX_ARG_COMMAND:-$1}"

if [ -z "$CMD" ]; then
    echo "error: no command specified"
    exit 1
fi

if [ ! -p "$FIFO" ]; then
    echo "error: FIFO not found at $FIFO — is crash dummy running?"
    exit 1
fi

# Write command to FIFO (timeout to avoid hanging if no reader)
echo "$CMD" > "$FIFO" &
WRITE_PID=$!
sleep 0.5
if kill -0 "$WRITE_PID" 2>/dev/null; then
    kill "$WRITE_PID" 2>/dev/null
    echo "error: FIFO write timed out — crash dummy may not be reading"
    exit 1
fi

echo "sent: $CMD"

# If it was a dump, wait a bit and show the dump line from the log
if [ "$CMD" = "dump" ]; then
    sleep 0.3
    LOG="${CRASH_DUMMY_LOG:-./log/crash_dummy.log}"
    if [ -f "$LOG" ]; then
        grep "DUMP " "$LOG" | tail -1
    fi
fi
