#!/usr/bin/env bash
set -euo pipefail

# Backup schedule comes from the first argument, falling back to $BACKUP_SCHEDULE.
# Accepts a standard 5-field crontab expression, e.g. "0 3 * * *",
# or one of the @shortcuts (@hourly, @daily, @weekly, @monthly, @yearly).
SCHEDULE="${1:-${BACKUP_SCHEDULE:-}}"

usage() {
    echo "Usage: entrypoint.sh \"<cron expression>\"" >&2
    echo "   or: docker run -e BACKUP_SCHEDULE=\"0 3 * * *\" <image>" >&2
}

if [ -z "$SCHEDULE" ]; then
    echo "ERROR: no backup schedule given (argument or BACKUP_SCHEDULE env var)." >&2
    usage
    exit 1
fi

# Validate one cron field (minute/hour/dom/month/dow) against its numeric range.
# Supports: * , - / combinations such as "*/15", "1,15,30", "9-17", "0-59/5".
validate_field() {
    local field="$1" min="$2" max="$3"
    local part base step lo hi

    IFS=',' read -r -a parts <<< "$field"
    [ "${#parts[@]}" -ge 1 ] || return 1

    for part in "${parts[@]}"; do
        base="$part"
        if [[ "$part" == */* ]]; then
            base="${part%%/*}"
            step="${part#*/}"
            [[ "$step" =~ ^[0-9]+$ ]] && [ "$step" -ge 1 ] || return 1
        fi

        if [ "$base" = "*" ]; then
            continue
        elif [[ "$base" =~ ^[0-9]+$ ]]; then
            [ "$base" -ge "$min" ] && [ "$base" -le "$max" ] || return 1
        elif [[ "$base" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            lo="${BASH_REMATCH[1]}"
            hi="${BASH_REMATCH[2]}"
            [ "$lo" -ge "$min" ] && [ "$hi" -le "$max" ] && [ "$lo" -le "$hi" ] || return 1
        else
            return 1
        fi
    done
}

validate_schedule() {
    local expr="$1"

    case "$expr" in
        @hourly|@daily|@midnight|@weekly|@monthly|@yearly|@annually)
            return 0 ;;
        @*)
            return 1 ;;
    esac

    local -a fields
    read -r -a fields <<< "$expr"
    [ "${#fields[@]}" -eq 5 ] || return 1

    validate_field "${fields[0]}" 0 59 && \
    validate_field "${fields[1]}" 0 23 && \
    validate_field "${fields[2]}" 1 31 && \
    validate_field "${fields[3]}" 1 12 && \
    validate_field "${fields[4]}" 0 7
}

if ! validate_schedule "$SCHEDULE"; then
    echo "ERROR: '$SCHEDULE' is not a valid crontab schedule." >&2
    usage
    exit 1
fi

# --- WireGuard tunnel ---
# All container traffic is routed through the tunnel (assuming the config's
# AllowedIPs covers 0.0.0.0/0). Requires: --cap-add NET_ADMIN and, for
# full-tunnel routing inside a container,
# --sysctl net.ipv4.conf.all.src_valid_mark=1
if [ -z "${WG_CONFIG_PATH:-}" ]; then
    echo "ERROR: WG_CONFIG_PATH is not set (path to a WireGuard .conf file)." >&2
    exit 1
fi

if [ ! -f "$WG_CONFIG_PATH" ]; then
    echo "ERROR: WireGuard config not found at '$WG_CONFIG_PATH'." >&2
    echo "Mount it into the container, e.g. -v ./wg0.conf:$WG_CONFIG_PATH:ro" >&2
    exit 1
fi

# wg-quick shells out to resolvconf when the config contains a DNS= line,
# and the slim image doesn't ship it. Work from a DNS-free copy in a tmp
# dir; this also lets the original be mounted read-only.
WG_RUNTIME_DIR="$(mktemp -d)"
WG_RUNTIME_CONF="$WG_RUNTIME_DIR/$(basename "$WG_CONFIG_PATH")"
grep -Eiv '^[[:space:]]*DNS[[:space:]]*=' "$WG_CONFIG_PATH" > "$WG_RUNTIME_CONF"
chmod 600 "$WG_RUNTIME_CONF"

# Full-tunnel configs need this sysctl, which can only be set from outside
# the container. Fail early with a clear message instead of mid-wg-quick.
if grep -q '0\.0\.0\.0/0' "$WG_RUNTIME_CONF" && \
   [ "$(cat /proc/sys/net/ipv4/conf/all/src_valid_mark 2>/dev/null)" != "1" ]; then
    echo "ERROR: full-tunnel config requires the container to run with:" >&2
    echo "  --sysctl net.ipv4.conf.all.src_valid_mark=1" >&2
    exit 1
fi

echo "Bringing up WireGuard tunnel from $WG_RUNTIME_CONF (DNS lines stripped)..."
wg-quick up "$WG_RUNTIME_CONF"
wg show

# cron starts jobs with an almost-empty environment, so snapshot the
# container's env (SFTP_*, paths, ...) for the job to source at run time.
export -p > /app/container.env
chmod 600 /app/container.env

# /proc/1/fd/1|2 sends the job's output to the container's stdout/stderr
# so `docker logs` picks it up.
{
    echo "SHELL=/bin/bash"
    echo "$SCHEDULE . /app/container.env; python3 /app/sync.py >> /proc/1/fd/1 2>> /proc/1/fd/2"
} | crontab -

echo "Backup schedule installed: $SCHEDULE -> python3 /app/sync.py"

# Initial run so the first backup doesn't wait for the schedule to come around.
# A failure here is logged but doesn't kill the container; cron retries on schedule.
echo "Running initial sync..."
if python3 /app/sync.py; then
    echo "Initial sync completed."
else
    echo "WARNING: initial sync failed with exit code $? - cron will retry on schedule." >&2
fi

echo "Starting cron in the foreground..."

# Replace this script with the cron daemon (PID 1) so the container keeps running.
exec cron -f
