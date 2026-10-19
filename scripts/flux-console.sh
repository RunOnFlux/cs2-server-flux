#!/bin/bash
# flux-console <command...>: types one command into the running server's console and prints what
# the server wrote in the next moments.
#
#   flux-console status
#   flux-console say Restarting in 5 minutes
#   flux-console kickid 2 "Being rude"
#
# The console is the server's stdin (a FIFO the supervisor holds open, through a pseudo-terminal),
# and its answer goes to the console log the supervisor writes (the install volume, flux-logs/), so it is
# read back from there: the lines written after the command. This waits for the log to grow, up to
# FLUX_CONSOLE_WAIT seconds (default 3), and then half a second more for the rest of the answer.
#
# Exit 1 when no server is running, 64 without a command.
set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source /opt/flux/flux-lib.sh

[ $# -gt 0 ] || { echo "usage: flux-console <command...>" >&2; exit 64; }

log="$(flux_game_log)"
before=0
[ -f "${log}" ] && before="$(wc -c <"${log}")"

if ! flux_console "$*"; then
  echo "no server is running" >&2
  exit 1
fi

waited=0
limit=$(( ${FLUX_CONSOLE_WAIT:-3} * 5 ))
while [ "${waited}" -lt "${limit}" ]; do
  sleep 0.2
  waited=$((waited + 1))
  if [ -f "${log}" ] && [ "$(wc -c <"${log}")" -gt "${before}" ]; then
    # FLUX_CONSOLE_UNTIL: a line that ends the answer (`status` ends with "#end"): wait for it.
    if [ -n "${FLUX_CONSOLE_UNTIL:-}" ]; then
      until tail -c +$((before + 1)) "${log}" | grep -qF "${FLUX_CONSOLE_UNTIL}" || [ "${waited}" -ge "${limit}" ]; do
        sleep 0.2
        waited=$((waited + 1))
      done
    fi
    sleep 0.5
    break
  fi
done
[ -f "${log}" ] && tail -c +$((before + 1)) "${log}"
exit 0
