#!/bin/bash
# Runs INSIDE the image (tests/test-image.sh). The repository's tests/ folder is mounted at /t.
set -uo pipefail
export LC_ALL=C

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}
state() { python3 -c 'import json;print(json.load(open("/tmp/flux-state.json")).get("state"))' 2>/dev/null; }

# --- What the scripts need is in the image -----------------------------------------------------
for tool in steamcmd python3 pgrep pkill script mkfifo timeout tail sed curl unzip tar; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "flux-console is on the PATH" "0" "$(status command -v flux-console)"
check "flux-cs2 is on the PATH" "0" "$(status command -v flux-cs2)"
check "flux-launch.py runs" "0" "$(status python3 /opt/flux/flux-launch.py)"
for script in flux-entrypoint.sh flux-console.sh flux-lib.sh; do
  check "${script} parses" "0" "$(status bash -n "/opt/flux/${script}")"
done
check "steamclient.so is there for the game's Steam login" "0" "$(status test -f /root/.local/share/Steam/steamcmd/linux64/steamclient.so)"
check "libicu is there for CounterStrikeSharp's .NET runtime" "0" "$(status bash -c 'ldconfig -p | grep -q libicuuc')"
check "the plugin catalogue is valid JSON with ids, repos and assets" "0" "$(status python3 -c 'import json;c=json.load(open("/opt/flux/plugins.json"));assert c and all(k in p for p in c for k in ("id","repo","asset","description"))')"

# --- The supervisor, around a stand-in Steam and a stand-in server -----------------------------
# The stand-in behaves like cs2 where it matters: it only reads its console from a TERMINAL, prints
# to one, answers `say` and `status`, ends with 0 on `quit` (but, like the real one, only after a
# short while) and with 139 on `crash`. The path carries "linuxsteamrt64/cs2 " as the real one.
cat >/usr/local/bin/steamcmd <<'STUB'
#!/bin/bash
dir=""
while [ $# -gt 0 ]; do
  [ "$1" = "+force_install_dir" ] && dir="$2"
  shift
done
[ -n "${dir}" ] || exit 0
mkdir -p "${dir}/game/csgo/cfg" "${dir}/game/bin/linuxsteamrt64"
printf 'PatchVersion=1.41.9.1\nServerVersion=2000933\n' >"${dir}/game/csgo/steam.inf"
printf '"GameInfo"\n{\n\tSearchPaths\n\t{\n\t\tGame_LowViolence\tcsgo_lv\n\t\tGame\tcsgo\n\t}\n}\n' >"${dir}/game/csgo/gameinfo.gi"
cat >"${dir}/game/cs2.sh" <<'GAME'
#!/bin/bash
exec -a "$(dirname "$0")/bin/linuxsteamrt64/cs2" bash -c '
[ -t 0 ] && [ -t 1 ] || { sleep 1000; exit 1; }
echo "args: $*"
echo "No Steam account token was specified."
while IFS= read -r line; do
  case "${line}" in
    say\ *) echo "Console: ${line#say }" ;;
    quit) echo "Source2Shutdown"; sleep 1; exit 0 ;;
    crash) exit 139 ;;
    status) q=$(printf "\047"); printf "steamid  : [A:1:1:1] (1)\n---------players--------\n  id     time ping loss      state   rate adr name\n   2      BOT    0    0     active      0 %sBot%s\n#end\n" "$q" "$q" ;;
    banid\ *) echo "${line}" >>/tmp/stub-bans; echo "banid ok" ;;
    writeid) cp /tmp/stub-bans "${FLUX_SERVER_DIR}/game/csgo/cfg/banned_user.cfg"; echo "Writing cfg/banned_user.cfg." ;;
    *) echo "Unknown command \"${line}\"" ;;
  esac
done' x "$@"
GAME
chmod +x "${dir}/game/cs2.sh"
echo "Success! App '730' fully installed."
STUB
chmod +x /usr/local/bin/steamcmd

export FLUX_SERVER_DIR=/tmp/srv FLUX_DATA_DIR=/tmp/data FLUX_RESTART_BACKOFF=1 CS2_ADDONS=false
logs=/tmp/srv/flux-logs
CS2_HOSTNAME='Test Server' CS2_RCON_PASSWORD=abc12345 GAME_PORT=31000 CS2_MAXPLAYERS=64 FLUX_GAME_LOG_MAX_BYTES=3000 FLUX_GAME_LOG_KEEP=3 \
  /opt/flux/flux-entrypoint.sh >/tmp/sup.log 2>&1 &
sup=$!
check "the server comes up under a terminal and its console reaches the log" "0" "$(status wait_for "grep -qh 'No Steam account token' ${logs}/*-console.log 2>/dev/null" 20)"
check "the launch line arrives as words" "0" "$(status grep -qh 'args: -dedicated -console -usercon -port 31000 -maxplayers 64 +exec banned_user.cfg +exec banned_ip.cfg' ${logs}/*-console.log)"
check "the quoted name" "0" "$(status grep -qh '+hostname "Test Server"' ${logs}/*-console.log)"
check "the console reaches the container output too" "0" "$(status grep -q 'No Steam account token' /tmp/sup.log)"
check "the build is logged" "0" "$(status grep -q 'Counter-Strike 2 build 1.41.9.1' /tmp/sup.log)"
check "the missing token is warned about" "0" "$(status grep -q 'WARN no Steam Game Server Login Token' /tmp/sup.log)"
check "the state is starting (no A2S answer from the stand-in)" "starting" "$(state)"
check "csgo/cfg/server.cfg is the data volume's" "/tmp/data/cfg/server.cfg" "$(readlink /tmp/srv/game/csgo/cfg/server.cfg)"
check "the panel's settings folder is the data volume's" "/tmp/data/cfg/flux" "$(readlink /tmp/srv/game/csgo/cfg/flux)"
check "the ban list is the data volume" "/tmp/data/banned_user.cfg" "$(readlink /tmp/srv/game/csgo/cfg/banned_user.cfg)"
check "every mode execs the panel's settings last" "exec flux/settings" "$(tail -1 /tmp/srv/game/csgo/cfg/gamemode_casual_server.cfg)"
check "a server.cfg to edit" "0" "$(status test -s /tmp/data/cfg/server.cfg)"
check "flux-console reaches the console and prints the answer" "0" "$(status grep -q 'Console: hi there' <<<"$(flux-console say hi there)")"
check "the roster reads the console's status" "Bot" "$(flux-cs2 players | python3 -c 'import json,sys;print(json.load(sys.stdin)["players"][0]["name"])')"
check "status says what the Steam login is" "missing" "$(flux-cs2 status | python3 -c 'import json,sys;print(json.load(sys.stdin)["gslt"]["state"])')"

check "a backup is made on the first start" "1" "$(find /tmp/data/flux/backups -name '*-cs2.tar.gz' | wc -l)"
check "the generation is published for the hub" "1" "$(cut -d' ' -f1 /tmp/flux-generation)"
check "the console logs are local, not on the data volume" "0" "$(find /tmp/data -name '*-console.log' | wc -l)"

# A ban the panel makes is on the data volume at once (the stand-in has no ban list, so the file
# is written by the helper): a kill -9 right after loses nothing.
check "a ban is written at once" "banid 0 [U:1:12345]" "$(flux-cs2 ban 76561197960278073 >/dev/null; cat /tmp/data/banned_user.cfg)"
check "a setting is written at once" "mp_roundtime 3.0" "$(flux-cs2 set-settings '{"mp_roundtime":3}' >/dev/null; grep mp_roundtime /tmp/data/cfg/flux/settings.cfg)"

# A restore through the hub's command: the server quits, the next start puts the backup back.
first="$(basename "$(find /tmp/data/flux/backups -name '*-cs2.tar.gz' | head -1)")"
printf 'banid 0 [U:1:4242]\n' >/tmp/data/banned_user.cfg
check "a restore is accepted" "0" "$(status flux-cs2 restore "${first}")"
check "the server restarts for it and the backup is back" "0" "$(status wait_for "grep -q 'restore: ' /tmp/sup.log && ! grep -q 4242 /tmp/data/banned_user.cfg && test \"\$(cut -d' ' -f1 /tmp/flux-generation)\" = 2" 25)"
check "the restore is not counted as a crash" "0" "$(grep -c 'restarts within' /tmp/sup.log)"
check "a safety backup was taken first" "2" "$(find /tmp/data/flux/backups -name '*-cs2.tar.gz' | wc -l)"

for n in $(seq 1 120); do flux-console say "filler line number ${n} to roll the log over" >/dev/null; done
check "the console log is rotated and pruned" "1" "$(status test "$(find "${logs}" -name '*-console.log' | wc -l)" -gt 3)"

# A crash: restarted in place.
flux-console crash >/dev/null
check "a crash is restarted in place" "0" "$(status wait_for "grep -q 'restarting the server in place' /tmp/sup.log" 10)"
check "the state says restarting meanwhile" "0" "$(status wait_for "[ \"\$(state)\" = restarting ] || [ \"\$(state)\" = starting ]" 12)"
check "and the new server comes up" "0" "$(status wait_for "grep -q 'generation 3' /tmp/sup.log" 15)"
check "the crash is reported with its code" "0" "$(status grep -q 'exit 139' /tmp/sup.log)"
sleep 2

# A stop must fit Docker's 10 s: the stand-in takes 1 s after "Source2Shutdown".
t0="$(date +%s)"
kill -TERM "${sup}"
wait "${sup}"
check "a stop exits 0" "0" "$?"
check "a stop takes well under Docker's 10 s" "0" "$(status test $(($(date +%s) - t0)) -lt 8)"
check "a stop is a console quit" "0" "$(status grep -q 'stopped on request (server exited 0)' /tmp/sup.log)"
check "the state is stopped" "stopped" "$(state)"
check "nothing is left running" "1" "$(status pgrep -f 'linuxsteamrt64/cs2 |script -qfec')"

# A port that is not one: needs-fix, not a crash loop.
GAME_PORT=80 /opt/flux/flux-entrypoint.sh >/tmp/fix.log 2>&1 &
fix=$!
check "an unusable port is a needs-fix state" "0" "$(status wait_for "[ \"\$(state)\" = needs-fix ]" 10)"
kill -TERM "${fix}"
wait "${fix}"
check "and stops cleanly" "0" "$?"

# --- flux.log is local; the banner ----------------------------------------------------------------
fl_srv=/tmp/srv
fl_data=/tmp/data
check "flux.log is on the install volume" "0" "$(status test -s "${fl_srv}/flux-logs/flux.log")"
check "and not on the synced data volume" "1" "$(status test -e "${fl_data}/flux/flux.log")"
check "the banner is in the container output" "0" "$(status grep -q 'Counter-Strike 2 server image by FluxCloud - https://runonflux.com' /tmp/sup.log)"
check "and in flux.log" "0" "$(status grep -q 'Host this and 30+ other games in one click: https://runonflux.com/games' "${fl_srv}/flux-logs/flux.log")"
check "at the container start and again for each generation" "1" "$([ "$(grep -c 'server image by FluxCloud' /tmp/sup.log)" -ge 2 ] && echo 1 || echo 0)"

printf '%s passed, %s failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
