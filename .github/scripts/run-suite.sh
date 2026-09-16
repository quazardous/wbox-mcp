#!/usr/bin/env bash
# Run the integration suite for the labwc+hybrid combos and fail on an
# unexpected skip.
#
# The fixtures skip when a compositor or an input tool is missing. That is the
# right behaviour on a developer's machine and the wrong one in CI, where a
# silently skipped suite is indistinguishable from a passing one. So here a
# skip fails the job — unless it is one of the skips we know to be deliberate.
set -euo pipefail

python -m pytest tests/test_integration.py \
    -k "labwc-hybrid or (not labwc and not weston and not cage)" \
    -v -rs \
    --junitxml=report.xml

python - <<'PY'
import sys
import xml.etree.ElementTree as ET

# Skips that describe real, documented behaviour rather than a broken runner.
# Substring match against the skip message.
#
# Nothing here fires while the job runs labwc only — cage is deselected. It is
# staged for whoever widens this workflow to the full matrix, so that cage's
# intentional resize skip does not look like a regression on day one.
EXPECTED = (
    "kiosk compositor: resize unsupported",
)

root = ET.parse("report.xml").getroot()
unexpected = []
for tc in root.iter("testcase"):
    for child in tc:
        if child.tag != "skipped":
            continue
        message = child.get("message", "")
        if not any(ok in message for ok in EXPECTED):
            unexpected.append(f"{tc.get('name')}: {message}")

if unexpected:
    print("::error::the suite skipped tests CI expected to run")
    for line in unexpected:
        print(f"  skipped: {line}")
    sys.exit(1)

print("no unexpected skips — every selected test either ran or skipped by design")
PY
