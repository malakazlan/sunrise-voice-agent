#!/bin/sh
# Starts coturn from exactly one configuration file: the committed settings, then the realm,
# the quotas and the shared secret from the container environment. The secret is written to
# the file, never passed as an argument, and unset before coturn starts.
set -eu

: "${TURN_HOST:?TURN_HOST is required}"
: "${TURN_SHARED_SECRET:?TURN_SHARED_SECRET is required}"
: "${MAX_CONCURRENT_CALLS:?MAX_CONCURRENT_CALLS is required}"

case "$MAX_CONCURRENT_CALLS" in
  '' | *[!0-9]*)
    echo "MAX_CONCURRENT_CALLS must be a whole number" >&2
    exit 1
    ;;
esac
if [ "$MAX_CONCURRENT_CALLS" -lt 1 ] || [ "$MAX_CONCURRENT_CALLS" -gt 20 ]; then
  echo "MAX_CONCURRENT_CALLS must be between 1 and 20" >&2
  exit 1
fi

# A call holds up to three allocations: the browser's over UDP and over TCP, and the server's.
allocations=$((3 * MAX_CONCURRENT_CALLS))
# Bytes per second per session, each direction: far above one Opus voice stream.
max_bps=64000
config=/tmp/turnserver.conf

umask 077
{
  cat /etc/coturn/sunrise-base.conf
  printf 'realm=%s\n' "$TURN_HOST"
  printf 'user-quota=%s\n' "$allocations"
  printf 'total-quota=%s\n' "$allocations"
  printf 'max-bps=%s\n' "$max_bps"
  printf 'bps-capacity=%s\n' "$((allocations * max_bps))"
  printf 'static-auth-secret=%s\n' "$TURN_SHARED_SECRET"
} > "$config"
unset TURN_SHARED_SECRET

exec turnserver -c "$config"
