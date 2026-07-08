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

# /proc/1/fd/1|2 sends the job's output to the container's stdout/stderr
# so `docker logs` picks it up.
echo "$SCHEDULE python3 /app/sync.py >> /proc/1/fd/1 2>> /proc/1/fd/2" | crontab -

echo "Backup schedule installed: $SCHEDULE -> python3 /app/sync.py"
echo "Starting cron in the foreground..."

# Replace this script with the cron daemon (PID 1) so the container keeps running.
exec cron -f
