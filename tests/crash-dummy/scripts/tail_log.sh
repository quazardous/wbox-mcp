#!/bin/bash
tail -n "${WBOX_ARG_LINES:-50}" ./log/crash_dummy.log 2>/dev/null || echo "(no log yet)"
