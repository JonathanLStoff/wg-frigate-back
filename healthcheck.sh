#!/usr/bin/env bash
set -euo pipefail

# 1. Verify WireGuard tunnel is active
if ! wg show 2>/dev/null | grep -q 'interface:'; then
    echo "HEALTHCHECK ERROR: WireGuard tunnel is down" >&2
    exit 1
fi

# 2. Check the status of the most recent sync run in LOG_FILE
LOG_FILE="${LOG_FILE:-/logs/sync.csv}"
if [ -f "$LOG_FILE" ]; then
    python3 - <<EOF
import csv
import sys

log_file = "$LOG_FILE"
try:
    with open(log_file, "r", newline="") as f:
        rows = [row for row in csv.reader(f) if row]
    if len(rows) > 1:
        last_row = rows[-1]
        status = last_row[1] if len(last_row) > 1 else ""
        if status != "success":
            err = last_row[4] if len(last_row) > 4 else "unknown error"
            sys.stderr.write(f"HEALTHCHECK ERROR: Last sync run failed ({status}): {err}\n")
            sys.exit(1)
except Exception as e:
    sys.stderr.write(f"HEALTHCHECK ERROR: Could not parse log file {log_file}: {e}\n")
    sys.exit(1)
EOF
fi

exit 0
