#!/bin/sh
# Starts coturn from exactly one configuration file: the committed settings, then the realm,
# the quotas and the shared secret from the container environment. The secret is written to
# the file, never passed as an argument, and unset before coturn starts.
#
# Every value is checked before anything is written, by the same rules the app applies
# (app/config.py; both sides are tested against deploy/coturn/validation-cases.json). A bad
# value stops the container with the variable's name, never its value.
set -eu

refuse() {
  echo "$1 is not valid" >&2
  exit 1
}

host=${TURN_HOST:-}
case "$host" in
  '' | *[!A-Za-z0-9.-]*) refuse TURN_HOST ;;
esac

secret=${TURN_SHARED_SECRET:-}
case "$secret" in
  '' | *[!A-Za-z0-9._~+/=-]*) refuse TURN_SHARED_SECRET ;;
esac
[ "${#secret}" -ge 32 ] || refuse TURN_SHARED_SECRET

# Empty means the app's default, 3. Digits only; leading zeros are dropped, as the app does.
calls=${MAX_CONCURRENT_CALLS:-3}
case "$calls" in
  *[!0-9]*) refuse MAX_CONCURRENT_CALLS ;;
esac
calls=$(printf '%s' "$calls" | sed 's/^0*//')
case "$calls" in
  [1-9] | 1[0-9] | 20) ;;
  *) refuse MAX_CONCURRENT_CALLS ;;
esac

# One call holds at most 5 allocations: the browser's, one per TURN URL (udp and tcp) on up to
# two networks, so 4 on its credential, plus 1 on the server's own credential. A credential is
# never shared, so user-quota=4 is one call's worth. total-quota leaves room beside the live
# calls for one dead client per call slot, whose allocations expire within
# max-allocate-lifetime: (5 + 4) * MAX_CONCURRENT_CALLS.
user_quota=4
total_quota=$((9 * calls))
# Bytes per second per session, each direction: far above one Opus voice stream.
max_bps=64000
config=/tmp/turnserver.conf

umask 077
{
  cat /etc/coturn/sunrise-base.conf
  printf 'realm=%s\n' "$host"
  printf 'user-quota=%s\n' "$user_quota"
  printf 'total-quota=%s\n' "$total_quota"
  printf 'max-bps=%s\n' "$max_bps"
  printf 'bps-capacity=%s\n' "$((total_quota * max_bps))"
  printf 'static-auth-secret=%s\n' "$secret"
} > "$config"
unset TURN_SHARED_SECRET secret

exec turnserver -c "$config"
